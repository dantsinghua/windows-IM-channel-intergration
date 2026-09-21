"""邮件路由凭据「只进 Vault、库里只存引用」(S-8 修复,backend-sec-1)。

规格:02 #105「`inbound.secret/outbound.secret` 只写不读(写 `vault://mail/route/<id>/imap|pop3|smtp` 并回 `*_ref`)」;
02 附录提议 10「全局路由沿用 `mail/imap|pop3|smtp`」、``vault_index.scope='mail'``;00 §11.1 [CRED] 密钥不落库。

入参语义(body 的 ``inbound``/``outbound`` 各自独立判):
- ``secret``(或同义 ``password``)为**非空串** ⇒ 写 Vault(覆盖旧值),``secret_ref`` 改成本路由的规范路径;
- 为 ``null`` ⇒ 显式清空:``secret_ref`` 置空串;若旧引用正是本路由自己的规范路径,库写成功后删掉该 Vault 条目;
- **不带**(或空串:表单密码框留空的常见形态)⇒ 保留库里原引用不动 —— 控制台读不回明文、整组回写时不能把密钥清掉;
- body 自带的 ``secret_ref`` 只认「与原引用相同」或「邮件凭据规范路径」两种,否则 400:
  防止把 ``vault://account/<id>`` 之类别域条目指给路由、再由 SMTP/IMAP 登录把它发往任意主机(借路由外带密钥)。
- 其它一切密钥类键(``token``/``*_secret``/``*_password``/``*_token``,含嵌套在 ``fallback``/``hmac`` 里的)一律摘掉不落库。

顺序:先写 Vault、后写库(Vault 失败 ⇒ 整个请求失败,库一行不动;新建路由的占位行当场删掉);
删 Vault 条目放在库写成功**之后**(删失败只留孤儿条目,不留明文)。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .mail_store import MailStore

log = logging.getLogger(__name__)

#: 规范路径形态(02 #105 + 附录提议 10)
_CANON_REF = re.compile(r"^vault://mail/(route/\d+/)?(imap|pop3|smtp)$")
_VALUE_KEYS = ("secret", "password")


class BadSecretRef(ValueError):
    def __init__(self, side: str, ref: Any):
        super().__init__(f"{side}.secret_ref 只能是原引用或 vault://mail/[route/<id>/]imap|pop3|smtp")
        self.side = side


def is_secret_key(k: str) -> bool:
    kl = str(k).lower()
    return kl in ("secret", "password", "token") or kl.endswith(("_secret", "_password", "_token"))


def _strip(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if not is_secret_key(k)}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def has_plaintext(obj: Any) -> bool:
    """递归看有没有任何密钥类键(值为何都算:键本身就不该在库里)。"""
    if isinstance(obj, dict):
        return any(is_secret_key(k) or has_plaintext(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(has_plaintext(v) for v in obj)
    return False


def vault_path(*, is_global: bool, route_id: Optional[int], proto: str) -> str:
    """裸名(不带 ``vault://``)。"""
    return f"mail/{proto}" if is_global else f"mail/route/{route_id}/{proto}"


def _proto(side: str, blob: dict[str, Any], prev: dict[str, Any]) -> str:
    if side == "outbound":
        return "smtp"
    p = str(blob.get("protocol") or prev.get("protocol") or "imap")
    return p if p in ("imap", "pop3") else "imap"


@dataclass
class _Sealed:
    inbound: dict[str, Any]
    outbound: dict[str, Any]
    puts: list[tuple[str, str]] = field(default_factory=list)      # (name, value) —— value 只在内存里过一下
    deletes: list[str] = field(default_factory=list)


def _seal(*, is_global: bool, route_id: Optional[int], inbound: dict[str, Any], outbound: dict[str, Any],
          prev_inbound: dict[str, Any], prev_outbound: dict[str, Any], migrating: bool) -> _Sealed:
    out = _Sealed(inbound={}, outbound={})
    for side, blob, prev in (("inbound", inbound, prev_inbound), ("outbound", outbound, prev_outbound)):
        blob = blob if isinstance(blob, dict) else {}
        prev = prev if isinstance(prev, dict) else {}
        name = vault_path(is_global=is_global, route_id=route_id, proto=_proto(side, blob, prev))
        present = [k for k in _VALUE_KEYS if k in blob]
        value = blob[present[0]] if present else None
        clean = _strip(blob)
        prev_ref = prev.get("secret_ref")
        if isinstance(value, str) and value:
            out.puts.append((name, value))
            clean["secret_ref"] = f"vault://{name}"
        elif present and value is None and not migrating:
            clean["secret_ref"] = ""
            if prev_ref == f"vault://{name}":
                out.deletes.append(name)
        else:
            ref = clean.get("secret_ref")
            if ref is not None and ref != prev_ref and not (isinstance(ref, str) and _CANON_REF.match(ref)):
                if not migrating:
                    raise BadSecretRef(side, ref)
                ref = None
            if ref is None:
                clean.pop("secret_ref", None)
                if prev_ref is not None:
                    clean["secret_ref"] = prev_ref
        setattr(out, side, clean)
    return out


def check_route(ms: MailStore, *, channel: Optional[str], account_id: Optional[str], inbound: Any, outbound: Any) -> None:
    """无副作用的入参校验(非法 ``secret_ref`` ⇒ ``BadSecretRef``);多块一起写(#89 scopes)时先全部过一遍再动手。"""
    row = ms.route_find(channel=channel, account_id=account_id)
    _seal(is_global=channel is None and account_id is None, route_id=(row or {}).get("id") or 0,
          inbound=inbound if isinstance(inbound, dict) else {}, outbound=outbound if isinstance(outbound, dict) else {},
          prev_inbound=_loads(row.get("inbound_json")) if row else {},
          prev_outbound=_loads(row.get("outbound_json")) if row else {}, migrating=False)


async def upsert_route(ms: MailStore, vault: Any, *, channel: Optional[str], account_id: Optional[str],
                       inbound: Any, outbound: Any, enabled: bool) -> int:
    """#105 PUT / #89 mail 组共用的写入口:密钥进 Vault、库里只落引用。Vault 失败原样抛 ``VaultUnavailable``。"""
    is_global = channel is None and account_id is None
    inbound = inbound if isinstance(inbound, dict) else {}
    outbound = outbound if isinstance(outbound, dict) else {}
    row = ms.route_find(channel=channel, account_id=account_id)
    prev_in = _loads(row.get("inbound_json")) if row else {}
    prev_out = _loads(row.get("outbound_json")) if row else {}
    placeholder: Optional[int] = None
    route_id = row["id"] if row else None
    needs_id = not is_global and route_id is None and any(
        isinstance(b.get(k), str) and b.get(k) for b in (inbound, outbound) for k in _VALUE_KEYS)
    check_route(ms, channel=channel, account_id=account_id, inbound=inbound, outbound=outbound)   # 先校验再占位
    if needs_id:        # Vault 路径要 id ⇒ 先插一行不含任何密钥/引用的占位(enabled=0,reload 前也不会被用)
        placeholder = route_id = ms.route_upsert(channel=channel, account_id=account_id,
                                                 inbound_json={}, outbound_json={}, enabled=False)
    sealed = _seal(is_global=is_global, route_id=route_id, inbound=inbound, outbound=outbound,
                   prev_inbound=prev_in, prev_outbound=prev_out, migrating=False)
    try:
        for name, value in sealed.puts:
            await vault.put(name, value, scope="mail")
    except Exception:
        if placeholder is not None:
            ms.route_delete(placeholder)
        raise
    rid = ms.route_upsert(channel=channel, account_id=account_id, inbound_json=sealed.inbound,
                          outbound_json=sealed.outbound, enabled=enabled)
    for name in sealed.deletes:
        try:
            await vault.delete(name)
        except Exception as e:              # 库已不再引用它:删不掉只是孤儿条目,不影响正确性
            log.warning("邮件路由 %s 的 Vault 条目 %s 删除失败(%s),留作孤儿条目", rid, name, type(e).__name__)
    return rid


async def migrate_plaintext(ms: MailStore, vault: Any) -> int:
    """存量明文迁移:每行「先写 Vault → 再比较并交换库行」,任一步失败该行原样保留(下轮再试)。回迁走的行数。

    幂等:库里没有任何密钥类键的行一条语句都不写(只读一遍 ``mail_routes``)。
    不走 ``schema_version``:02 §3.3 迁移脚本只许加表/加列/加索引/加触发器与补 ``settings``,且要连 Vault ⇒ 放运行期。
    """
    moved = 0
    for row in ms.routes_list():
        raw_in, raw_out = row.get("inbound_json"), row.get("outbound_json")
        cur_in, cur_out = _loads(raw_in), _loads(raw_out)
        if not (has_plaintext(cur_in) or has_plaintext(cur_out)):
            continue
        is_global = row.get("channel") is None and row.get("account_id") is None
        sealed = _seal(is_global=is_global, route_id=row["id"], inbound=cur_in, outbound=cur_out,
                       prev_inbound=cur_in, prev_outbound=cur_out, migrating=True)
        try:
            for name, value in sealed.puts:
                await vault.put(name, value, scope="mail")
        except Exception as e:
            log.warning("邮件路由 %s 存量明文迁移暂缓:Vault 写失败(%s),库行保持原样,下轮重试", row["id"], type(e).__name__)
            continue
        if ms.route_swap_json(row["id"], old_inbound=raw_in, old_outbound=raw_out,
                              inbound_json=sealed.inbound, outbound_json=sealed.outbound):
            moved += 1
            log.info("邮件路由 %s 的明文凭据已迁入 Vault(%d 条),库里只留引用", row["id"], len(sealed.puts))
    return moved


def _loads(v: Any) -> dict[str, Any]:
    try:
        d = json.loads(v or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}
