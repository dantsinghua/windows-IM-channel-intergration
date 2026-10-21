"""按账号把目录交给公网文件网关。

总索引每次可以更新。企点通讯录没有正文时不上传，避免空文件盖掉已有通讯录。
对象名以 catalog.json 结尾的一律拒绝。同一明文不重复上传。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .envelope import pack_piece_envelope
from .object_io import ObjectIO
from .split import catalog_pieces

_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def catalog_from_rows(workbench_id: str, accounts: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """从采集库的账号和群会话拼目录。同一企点号只有一个采集端时，路径里不插采集端编号。"""
    usable = []
    for account in accounts:
        channel = str(account.get("channel") or "")
        uid = str(account.get("self_uid") or "").strip()
        if channel not in ("qidian", "wechat", "qq") or _IDENT.fullmatch(uid) is None:
            continue
        usable.append(account)
    counts: dict[tuple[str, str], int] = {}
    for account in usable:
        key = (str(account["channel"]), str(account["self_uid"]))
        counts[key] = counts.get(key, 0) + 1
    out = []
    for account in usable:
        uid = str(account["self_uid"])
        channel = str(account["channel"])
        item: dict[str, Any] = {
            "channel": channel,
            "account_uid": uid,
            "account_nick": str(account.get("self_nick") or ""),
        }
        if counts[(channel, uid)] > 1:
            item["collector_id"] = str(account.get("id") or "")
        if channel == "wechat":
            groups = []
            for session in sessions:
                if session.get("account_id") != account.get("id") or session.get("kind") != "group":
                    continue
                native = str(session.get("native_id") or "").strip()
                if not native:
                    continue
                groups.append({"native_id": native, "name": str(session.get("name") or native)})
            item["groups"] = groups
        org = account.get("org")
        if channel == "qidian" and isinstance(org, dict):
            item["org"] = org
        contacts = account.get("contacts")
        if channel in ("qidian", "qq") and isinstance(contacts, list):
            item["contacts"] = contacts
        out.append(item)
    return {"workbench_id": workbench_id, "accounts": out}


def _digest(body: dict[str, Any]) -> str:
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _has_book(body: dict[str, Any]) -> bool:
    if body.get("contacts"):
        return True
    org = body.get("org")
    if isinstance(org, dict) and (org.get("departments") or org.get("members")):
        return True
    return False


def publish_changed(
    box: Any,
    pieces: list[tuple[str, dict[str, Any]]],
    deposit_key: bytes,
    xunjia_key: bytes,
    prior: dict[str, str],
) -> dict[str, str]:
    """只上传变了的对象。返回新的摘要表。空通讯录不上传。"""
    state = dict(prior)
    for name, body in pieces:
        if name.endswith("/catalog.json") or ".." in name.split("/"):
            raise ValueError("拒绝写入整包目录")
        if name.endswith("/contacts.json") and not _has_book(body):
            continue
        digest = _digest(body)
        if state.get(name) == digest:
            continue
        box.put(name, pack_piece_envelope(body, deposit_key, xunjia_key))
        state[name] = digest
    return state


def pieces_for(workbench_id: str, accounts: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    catalog = catalog_from_rows(workbench_id, accounts, sessions)
    if not catalog["accounts"]:
        return []
    return catalog_pieces(catalog)


log = logging.getLogger("qtrade.ossbox.catalog")


class CatalogPublisher:
    """一轮：从采集库拼目录，只把变了的分片双封套上传。不写 catalog.json。"""

    def __init__(self, box: Any, store: Any, workbench_id: str, deposit_key: bytes, xunjia_key: bytes, state_path: str) -> None:
        self.box = box
        self.store = store
        self.workbench_id = workbench_id
        self.deposit_key = deposit_key
        self.xunjia_key = xunjia_key
        self.state_path = state_path
        self._io = ObjectIO()

    @classmethod
    def from_env(cls, store: Any, state_path: str, *, box: Any = None,
                 workbench_id: str | None = None) -> "CatalogPublisher | None":
        if workbench_id is None:
            workbench_id = os.environ.get("OSS_WORKBENCH_ID", "").strip()
        deposit_hex = (os.environ.get("OSS_SEAL_KEY_DEPOSIT") or os.environ.get("OSSBOX_KEY_DEPOSIT") or "").strip()
        xunjia_hex = (os.environ.get("OSS_SEAL_KEY_XUNJIA") or os.environ.get("OSSBOX_KEY_XUNJIA") or "").strip()
        if not all((workbench_id, deposit_hex, xunjia_hex)):
            log.warning("目录发布未装配，对象存储或两把封套密钥不齐")
            return None
        try:
            deposit_key = bytes.fromhex(deposit_hex)
            xunjia_key = bytes.fromhex(xunjia_hex)
        except ValueError:
            log.warning("目录封套密钥不是十六进制，不上传")
            return None
        if len(deposit_key) not in (16, 24, 32) or len(xunjia_key) not in (16, 24, 32):
            log.warning("目录封套密钥长度无效，不上传")
            return None
        from .oss_http import OssV1Box

        try:
            box = box if box is not None else OssV1Box.from_env()
        except ValueError:
            log.warning("目录对象存储配置无效，未装配发布")
            return None
        if box is None:
            log.warning("目录对象存储配置不完整，未装配发布")
            return None

        return cls(
            box,
            store,
            workbench_id,
            deposit_key,
            xunjia_key,
            state_path,
        )

    def publish_once(self) -> None:
        self._publish_pieces(self._pieces())

    async def publish(self) -> None:
        """数据库快照在所属线程取得，只有对象 IO 和缓存文件移到线程。"""
        self._io.check_idle()
        pieces = self._pieces()
        if pieces:
            await self._io.run(self._publish_pieces, pieces)

    def _pieces(self) -> list[tuple[str, dict[str, Any]]]:
        accounts = self.store.list_accounts()
        sessions: list[dict[str, Any]] = []
        for account in accounts:
            if account.get("channel") != "wechat" or not account.get("self_uid"):
                continue
            sessions.extend(self.store.list_sessions(account_id=account["id"], kind="group", limit=None))
        return pieces_for(self.workbench_id, accounts, sessions)

    def _publish_pieces(self, pieces: list[tuple[str, dict[str, Any]]]) -> None:
        if not pieces:
            return
        prior = self._load()
        state = publish_changed(self.box, pieces, self.deposit_key, self.xunjia_key, prior)
        self._save(state)

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        digests = data.get("digests") if isinstance(data, dict) else None
        if not isinstance(digests, dict):
            return {}
        return {str(key): str(value) for key, value in digests.items()}

    def _save(self, digests: dict[str, str]) -> None:
        path = Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"digests": digests}, ensure_ascii=False), encoding="utf-8")
