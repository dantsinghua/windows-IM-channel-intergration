"""媒体子系统(02 §2.8.2 媒体策略;#96 懒加载触发;#50/#55 取用;E-10 保留期由 ``maintenance`` 清)。

- **落盘路径**:``<media_dir>/<yyyymm>/<sha256>``(**无扩展名**,类型记在 ``media.mime``);
  下载中间文件先落 ``<media_dir>/tmp/``(C-09),``fsync`` 后 ``rename`` 进月份目录 —— 半截文件永远不会被当成 ready。
- **mime 按字节头识别,不信 ``Content-Type``**(ibquote 教训,§2.8.2 原句)。
- **去重按 ``sha256``**:同一张图被多账号收到只存一份,``ref_count`` 计数;保留期清理只删 ``ref_count=0`` 的(P-17)。
- **上限按 kind(C-23)**:``image/voice`` 20 MB、``file/video`` 50 MB;超限 ``status='skipped_oversize'`` **不重试**。
- **下载器可注入**(``Downloader`` 协议):开发容器里一律注 ``FakeDownloader``,**绝不出网**;
  真实现 ``UrllibDownloader`` 走标准库 + ``asyncio.to_thread``(§2.3.1 ⑤ 阻塞 I/O 离开事件循环)。
- 磁盘 ``high`` 水位起停下载(``maintenance.media_downloads_allowed``),``critical`` 起连元数据都不写。
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
import urllib.error
import urllib.request
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Protocol

from .events import TZ_SHANGHAI

log = logging.getLogger("qtrade.media")

KINDS = ("image", "voice", "file", "video", "other")
#: C-23 上限:``image``/``voice`` 20 MB,``file``/``video`` 50 MB(§2.8.2;与 ChatlogClient.MAX_IMAGE_BYTES 同源)
MAX_BYTES = {"image": 20 * 1024 * 1024, "voice": 20 * 1024 * 1024,
             "file": 50 * 1024 * 1024, "video": 50 * 1024 * 1024, "other": 20 * 1024 * 1024}
#: 字节头 → mime(§2.8.2「按字节头识别不信 Content-Type」);认不出的一律 ``application/octet-stream``
MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),                    # RIFF....WEBP;进一步区分见 sniff_mime
    (b"BM", "image/bmp"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"ID3", "audio/mpeg"), (b"\xff\xfb", "audio/mpeg"),
    (b"OggS", "audio/ogg"),
    (b"#!AMR", "audio/amr"),
    (b"\x00\x00\x00\x18ftyp", "video/mp4"), (b"\x00\x00\x00\x20ftyp", "video/mp4"),
)


def sniff_mime(head: bytes) -> str:
    """只看字节头。``RIFF….WEBP`` / ``RIFF….WAVE`` 两个容器要再看第 8~12 字节才分得开。"""
    if head[:4] == b"RIFF" and len(head) >= 12:
        tag = head[8:12]
        if tag == b"WEBP":
            return "image/webp"
        if tag == b"WAVE":
            return "audio/wav"
        if tag == b"AVI ":
            return "video/x-msvideo"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video/mp4"
    for magic, mime in MAGIC:
        if head.startswith(magic):
            return mime
    return "application/octet-stream"


@dataclass
class Fetched:
    """一次下载的结果;``ok=False`` 时 ``reason`` 是落 ``media.fail_reason`` 的那一句。"""
    ok: bool
    data: bytes = b""
    reason: str = ""
    declared_size: Optional[int] = None


class Downloader(Protocol):
    async def fetch(self, ref: dict[str, Any], *, max_bytes: int) -> Fetched: ...


@dataclass
class FakeDownloader:
    """测试用:``blobs[ref_key] = bytes`` 命中即成功;``fail`` 里的键一律失败。**不出网。**"""
    blobs: dict[str, bytes] = field(default_factory=dict)
    fail: set[str] = field(default_factory=set)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def fetch(self, ref: dict[str, Any], *, max_bytes: int) -> Fetched:
        self.calls.append(dict(ref))
        key = ref_key(ref)
        if key in self.fail:
            return Fetched(False, reason="fake_download_failed")
        blob = self.blobs.get(key)
        if blob is None:
            return Fetched(False, reason="not_found")
        if len(blob) > max_bytes:
            return Fetched(False, reason="oversize", declared_size=len(blob))
        return Fetched(True, data=blob, declared_size=len(blob))


class UrllibDownloader:
    """真下载器(标准库;跑在 ``asyncio.to_thread`` 里)。``honor_env_proxy=False`` 时显式装空 ``ProxyHandler``。

    只认 ``ref.url``(QQ 的 URL 段)与 ``ref.wa_path``(微信 chatlog ``/wa/v1/wechat/media/<md5>``,经 WinAgent 客户端取);
    企点的 ``download_image`` 走 adb 拉文件,由适配器自己给 ``bytes``,不经这里。
    """

    def __init__(self, *, honor_env_proxy: bool = False, wa_raw: Optional[Callable[[str], Any]] = None,
                 timeout_s: float = 30.0, user_agent: str = "qtrade-agent"):
        handlers = [] if honor_env_proxy else [urllib.request.ProxyHandler({})]
        self._opener = urllib.request.build_opener(*handlers)
        self._wa_raw = wa_raw
        self._timeout_s = timeout_s
        self._ua = user_agent

    async def fetch(self, ref: dict[str, Any], *, max_bytes: int) -> Fetched:
        import asyncio
        if ref.get("wa_path"):
            if self._wa_raw is None:
                return Fetched(False, reason="winagent_not_wired")
            try:
                data = await self._wa_raw(str(ref["wa_path"]))
            except Exception as e:                       # 会话代理不在线 / chatlog 挂
                return Fetched(False, reason=f"winagent:{e!r}"[:180])
            if len(data) > max_bytes:
                return Fetched(False, reason="oversize", declared_size=len(data))
            return Fetched(True, data=data, declared_size=len(data))
        url = ref.get("url")
        if not url:
            return Fetched(False, reason="no_ref")
        return await asyncio.to_thread(self._get, str(url), max_bytes)

    def _get(self, url: str, max_bytes: int) -> Fetched:
        req = urllib.request.Request(url, headers={"User-Agent": self._ua})
        try:
            with self._opener.open(req, timeout=self._timeout_s) as resp:      # noqa: S310
                declared = int(resp.headers.get("Content-Length") or 0) or None
                if declared and declared > max_bytes:
                    return Fetched(False, reason="oversize", declared_size=declared)
                data = resp.read(max_bytes + 1)          # 多读一个字节:没有 Content-Length 时也能判超限
        except (urllib.error.URLError, OSError, ValueError) as e:
            return Fetched(False, reason=repr(e)[:180])
        if len(data) > max_bytes:
            return Fetched(False, reason="oversize", declared_size=len(data))
        return Fetched(True, data=data, declared_size=len(data))


def ref_key(ref: dict[str, Any]) -> str:
    """来源引用的稳定键(用于假下载器与「同一引用不重复下载」)。"""
    return str(ref.get("url") or ref.get("wa_path") or ref.get("file") or ref.get("ref") or "")


def kind_of(raw: Optional[str]) -> str:
    return raw if raw in KINDS else "other"


class MediaStore:
    """``media`` 表 + 落盘的唯一入口(02 §2.8.2)。

    ``disk_allows()`` 由装配方接 ``maintenance.media_downloads_allowed``(high 水位起转 lazy);
    ``ingest_allows()`` 接 ``maintenance.ingest_allowed``(critical 起连元数据都不写)。
    """

    def __init__(self, store, *, media_dir: str, downloader: Optional[Downloader] = None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 disk_allows: Optional[Callable[[], bool]] = None,
                 ingest_allows: Optional[Callable[[], bool]] = None,
                 write_guard: Optional[Callable[[str], Any]] = None):
        self._store = store
        self.media_dir = media_dir
        self._dl = downloader
        self._clock = clock
        self._disk_allows = disk_allows or (lambda: True)
        self._ingest_allows = ingest_allows or (lambda: True)
        #: §2.8.8「`store`/`mail`/`media` 任何写失败第一诊断项是磁盘满」——**落盘这一路此前没有包**:
        #: `open/write/fsync` 的 `ENOSPC` 会原样冒上去,被各处 `except Exception` 兜成 `INTERNAL` + 可重试。
        #: 装配方(`app.py`)挂 `maintenance.guard_write`;缺省空壳 ⇒ 未装配 maintenance 时行为逐字不变。
        self.write_guard: Callable[[str], Any] = write_guard or (lambda what: nullcontext())

    # ------------------------------------------------------------------ 路径
    def rel_path_for(self, sha256: str, *, now_ms: Optional[int] = None) -> str:
        ym = datetime.fromtimestamp((now_ms or self._clock()) / 1000, tz=TZ_SHANGHAI).strftime("%Y%m")
        return f"media/{ym}/{sha256}"

    def abs_path(self, rel_path: str) -> str:
        return os.path.join(self.media_dir, rel_path.split("media/", 1)[-1] if rel_path.startswith("media/") else rel_path)

    # ------------------------------------------------------------------ 查/建行
    def by_sha256(self, sha256: str) -> Optional[dict[str, Any]]:
        r = self._store.con.execute("SELECT * FROM media WHERE sha256=?", (sha256,)).fetchone()
        return dict(r) if r else None

    def get(self, media_id: int) -> Optional[dict[str, Any]]:
        r = self._store.con.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
        return dict(r) if r else None

    def note(self, *, kind: str, origin: dict[str, Any], message_id: Optional[str] = None,
             status: str = "pending", now_ms: Optional[int] = None) -> Optional[int]:
        """`lazy` 路径:**只记引用**(此时还没有 sha256),回 ``media.id``;磁盘 critical 时不写、回 ``None``。"""
        if not self._ingest_allows():
            log.warning("磁盘 critical 水位:media 行不写(§2.8.8)")
            return None
        import json
        now = now_ms or self._clock()
        with self._store._tx() as c:
            cur = c.execute("INSERT INTO media(kind, status, origin_json, first_message_id, ref_count, first_seen_ms) "
                            "VALUES (?,?,?,?,0,?)",
                            (kind_of(kind), status, json.dumps(origin, ensure_ascii=False), message_id, now))
            return int(cur.lastrowid)

    # ------------------------------------------------------------------ 落盘
    def put_bytes(self, data: bytes, *, kind: str, origin: dict[str, Any], message_id: Optional[str] = None,
                  now_ms: Optional[int] = None) -> dict[str, Any]:
        """把已经拿到手的字节落盘 + 写 ``media`` 行;返回 ``{media_id, sha256, status, rel_path, mime, size}``。

        已存在同 ``sha256`` 的 ready 行 ⇒ 直接复用(去重,P-17),只 ``ref_count+1``。
        """
        import json
        now = now_ms or self._clock()
        k = kind_of(kind)
        limit = MAX_BYTES[k]
        if len(data) > limit:
            mid = self.note(kind=k, origin=origin, message_id=message_id, status="skipped_oversize", now_ms=now)
            return {"media_id": mid, "status": "skipped_oversize", "size": len(data), "sha256": None}
        sha = hashlib.sha256(data).hexdigest()
        hit = self.by_sha256(sha)
        if hit and hit["status"] == "ready":
            self.add_ref(int(hit["id"]), now_ms=now)
            return {"media_id": int(hit["id"]), "sha256": sha, "status": "ready", "rel_path": hit["rel_path"],
                    "mime": hit["mime"], "size": hit["size"], "deduped": True}
        rel = self.rel_path_for(sha, now_ms=now)
        self._write_file(rel, data)
        mime = sniff_mime(data[:16])
        with self._store._tx() as c:
            if hit:
                c.execute("UPDATE media SET sha256=?, mime=?, size=?, rel_path=?, status='ready', ready_ms=?, "
                          "ref_count=ref_count+1, last_ref_ms=? WHERE id=?",
                          (sha, mime, len(data), rel, now, now, hit["id"]))
                mid = int(hit["id"])
            else:
                cur = c.execute("INSERT INTO media(sha256, kind, mime, size, rel_path, status, origin_json, "
                                "first_message_id, ref_count, first_seen_ms, ready_ms, last_ref_ms) "
                                "VALUES (?,?,?,?,?,'ready',?,?,1,?,?,?)",
                                (sha, k, mime, len(data), rel, json.dumps(origin, ensure_ascii=False),
                                 message_id, now, now, now))
                mid = int(cur.lastrowid)
        return {"media_id": mid, "sha256": sha, "status": "ready", "rel_path": rel, "mime": mime, "size": len(data)}

    def _write_file(self, rel_path: str, data: bytes) -> None:
        """先写 ``media/tmp/``、``fsync`` 后 ``rename`` 进月份目录(C-09)—— 半截文件不会被当成 ready。"""
        final = self.abs_path(rel_path)
        tmp_dir = os.path.join(self.media_dir, "tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        os.makedirs(os.path.dirname(final), exist_ok=True)
        tmp = os.path.join(tmp_dir, os.path.basename(final) + ".part")
        with self.write_guard("media.write_file"):
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
            except BaseException:
                with suppress(OSError):
                    os.unlink(tmp)                      # 盘满时半截 .part 不留着继续占地方
                raise
            os.replace(tmp, final)

    # ------------------------------------------------------------------ 下载(#96 懒加载 / eager 策略)
    async def fetch_into(self, media_id: int, *, now_ms: Optional[int] = None) -> dict[str, Any]:
        """把一条 ``pending`` 的 media 行下载落盘。磁盘 high 水位起**不下载**(只回当前态,不算失败)。"""
        import json
        row = self.get(media_id)
        if row is None:
            return {"media_id": media_id, "status": "failed", "fail_reason": "not_found"}
        if row["status"] in ("ready", "skipped_oversize"):
            return {"media_id": media_id, "status": row["status"], "sha256": row["sha256"]}
        if not self._disk_allows():
            return {"media_id": media_id, "status": "pending", "fail_reason": "disk_high_watermark"}
        if self._dl is None:
            return {"media_id": media_id, "status": "pending", "fail_reason": "downloader_not_wired"}
        now = now_ms or self._clock()
        origin = json.loads(row["origin_json"] or "{}")
        k = kind_of(row["kind"])
        got = await self._dl.fetch(origin, max_bytes=MAX_BYTES[k])
        if not got.ok:
            status = "skipped_oversize" if got.reason == "oversize" else "failed"
            with self._store._tx() as c:
                c.execute("UPDATE media SET status=?, fail_reason=?, attempts=attempts+1, size=COALESCE(?, size) WHERE id=?",
                          (status, got.reason[:200], got.declared_size, media_id))
            return {"media_id": media_id, "status": status, "fail_reason": got.reason}
        # 🔴 **就地把这一行标 ready**,不能再走 `put_bytes` 建新行 —— `media.sha256` 上有唯一索引,
        # 同一内容插第二行会 `UNIQUE constraint failed`(而 `pending` 行的 sha256 是 NULL,查不到自己)。
        sha = hashlib.sha256(got.data).hexdigest()
        dup = self.by_sha256(sha)
        if dup is not None and int(dup["id"]) != media_id:
            # 别的账号早下过同一张图:本行不再占一份文件,直接指向它并把自己删掉(去重按 sha256,P-17)
            self.add_ref(int(dup["id"]), now_ms=now)
            with self._store._tx() as c:
                c.execute("DELETE FROM media WHERE id=?", (media_id,))
            return {"media_id": int(dup["id"]), "sha256": sha, "status": "ready", "rel_path": dup["rel_path"],
                    "mime": dup["mime"], "size": dup["size"], "deduped": True}
        rel = self.rel_path_for(sha, now_ms=now)
        self._write_file(rel, got.data)
        mime = sniff_mime(got.data[:16])
        with self._store._tx() as c:
            c.execute("UPDATE media SET sha256=?, mime=?, size=?, rel_path=?, status='ready', ready_ms=?, "
                      "ref_count=ref_count+1, last_ref_ms=? WHERE id=?",
                      (sha, mime, len(got.data), rel, now, now, media_id))
        return {"media_id": media_id, "sha256": sha, "status": "ready", "rel_path": rel, "mime": mime, "size": len(got.data)}

    # ------------------------------------------------------------------ 从 messages.media_json 建行(eager/lazy 策略的落点)
    #: 02 §7.1 ``[media] policy``:per-kind 的 ``eager|lazy``(账号/会话级 ``media_policy_json`` 可覆盖;本期只用全局)
    DEFAULT_POLICY = {"image": "eager", "voice": "eager", "file": "lazy", "video": "lazy", "other": "lazy"}

    def scan_messages(self, *, limit: int = 200, now_ms: Optional[int] = None) -> list[int]:
        """把还没有 ``media_id`` 的 ``messages.media_json`` 条目建成 ``media`` 行,并把 ``media_id``/``state`` 写回该数组。

        适配器入库时只记引用(``{kind, file/url, state:'pending'}``),**下载与落盘归本模块**(§2.8.2);
        这里是两者之间的唯一桥。返回本轮新建的 ``media.id`` 列表。
        """
        import json
        now = now_ms or self._clock()
        rows = self._store.con.execute(
            "SELECT id, account_id, media_json FROM messages WHERE media_json IS NOT NULL AND media_json <> '[]' "
            "AND media_json NOT LIKE '%\"media_id\"%' ORDER BY ts_ms DESC LIMIT ?", (limit,)).fetchall()
        created: list[int] = []
        for r in rows:
            try:
                items = json.loads(r["media_json"] or "[]")
            except ValueError:
                continue
            if not isinstance(items, list):
                continue
            changed = False
            for item in items:
                if not isinstance(item, dict) or item.get("media_id") is not None:
                    continue
                origin = {k: item[k] for k in ("url", "wa_path", "file", "ref", "sha256") if item.get(k)}
                origin["account_id"] = r["account_id"]
                mid = self.note(kind=item.get("kind") or "other", origin=origin, message_id=r["id"], now_ms=now)
                if mid is None:
                    return created                      # 磁盘 critical:本轮到此为止
                item["media_id"], item["state"] = mid, "pending"
                created.append(mid)
                changed = True
            if changed:
                with self._store._tx() as c:
                    c.execute("UPDATE messages SET media_json=? WHERE id=?",
                              (json.dumps(items, ensure_ascii=False), r["id"]))
        return created

    def policy_for(self, kind: str) -> str:
        return self.DEFAULT_POLICY.get(kind_of(kind), "lazy")

    async def fetch_eager(self, *, limit: int = 20) -> dict[str, int]:
        """把 ``policy=eager`` 的 ``pending`` 行下一轮(§2.8.2「默认立即下载」);``lazy`` 的等 #96 触发。"""
        rows = self._store.con.execute(
            "SELECT id, kind FROM media WHERE status='pending' ORDER BY id LIMIT ?", (limit * 4,)).fetchall()
        done = {"fetched": 0, "failed": 0, "skipped": 0}
        for r in rows:
            if self.policy_for(r["kind"]) != "eager":
                done["skipped"] += 1
                continue
            out = await self.fetch_into(int(r["id"]))
            done["fetched" if out.get("status") == "ready" else "failed"] += 1
            if done["fetched"] >= limit:
                break
        return done

    # ------------------------------------------------------------------ 引用计数
    def add_ref(self, media_id: int, *, now_ms: Optional[int] = None) -> None:
        now = now_ms or self._clock()
        with self._store._tx() as c:
            c.execute("UPDATE media SET ref_count=ref_count+1, last_ref_ms=? WHERE id=?", (now, media_id))

    def read_file(self, media_id: int) -> Optional[bytes]:
        """#50/#55 取用:``ready`` 且文件在才给字节;到期删文件后是 ``expired``(回 ``None``,端点回 410)。"""
        row = self.get(media_id)
        if row is None or row["status"] != "ready" or not row["rel_path"]:
            return None
        try:
            with open(self.abs_path(row["rel_path"]), "rb") as f:
                return f.read()
        except OSError:
            return None


__all__ = ["Downloader", "FakeDownloader", "Fetched", "KINDS", "MAX_BYTES", "MediaStore", "UrllibDownloader",
           "kind_of", "ref_key", "sniff_mime"]
