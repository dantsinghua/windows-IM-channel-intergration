"""#89 ``PUT /settings/{group}`` 的密钥「只进 Vault、``settings``/``agent.toml`` 里只留引用」(backend-sec-2)。

此前 ``_stash_secrets`` 只收**顶层**密钥键,已知键的值是对象/数组时,里面的密码原样进 ``settings['config.<group>']``
(及 ``config.__all__``、数组形态还会进 ``agent.toml``)。本模块把判定改成递归,并给出存量迁移。

- 密钥键判定复用 ``mail.route_secrets.is_secret_key``,另并上 #89 顶层一直用的后缀判定(``endswith secret|password|token``),
  两者取并集 ⇒ 顶层原来收的键一个不少,嵌套里也同样收。
- Vault 路径:顶层 ``settings/<group>/<key>``(**与改前逐字一致**,存量 ``config.<group>.<key>_ref`` 不失效);
  嵌套 ``settings/<group>/<路径…>/<key>``(路径段 = 键名或数组下标,``~``/``/`` 按 RFC 6901 转义)。
- 顶层:键从 body 摘掉,引用另存 ``settings['config.<group>.<key>_ref']``(改前行为);
  嵌套:原位置换成 ``<key>_ref: "vault://…"``。
- 值为非空串 ⇒ 写 Vault;空串/``null``/非字符串 ⇒ 不写 Vault、不落值;嵌套时沿用上一版同位置的 ``<key>_ref``(与顶层「留空不动原引用」同义)。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .mail.route_secrets import is_secret_key

log = logging.getLogger(__name__)

#: #89 顶层一直用的后缀(= ``api/app.py`` 的 ``SECRET_KEYS``)
TOP_SUFFIXES = ("secret", "password", "token")
#: WinAgent ``vault_index.scope`` 只收七类(02 §3.2 CHECK);原来传的 ``settings`` 不在其内,真机会 400
VAULT_SCOPE = "other"


def is_secret(k: Any) -> bool:
    return is_secret_key(k) or any(str(k).endswith(x) for x in TOP_SUFFIXES)


def _seg(k: Any) -> str:
    return str(k).replace("~", "~0").replace("/", "~1")


@dataclass
class Sealed:
    body: dict[str, Any]
    puts: list[tuple[str, str]] = field(default_factory=list)      # (裸名, 值) —— 值只在内存里过一下
    top_refs: dict[str, str] = field(default_factory=dict)          # 顶层键 → vault://…


def seal(group: str, body: dict[str, Any], prev: Any = None, *, prefix: str = "") -> Sealed:
    """纯函数、不改入参:返回去掉密钥值的 body + 待写 Vault 的条目。``prev`` = 上一版同组值(沿用嵌套引用用)。"""
    base = prefix or f"settings/{group}"
    out = Sealed(body={})

    def walk(obj: Any, before: Any, path: list[str]) -> Any:
        if isinstance(obj, list):
            before = before if isinstance(before, list) else []
            return [walk(v, before[i] if i < len(before) else None, path + [str(i)]) for i, v in enumerate(obj)]
        if not isinstance(obj, dict):
            return obj
        before = before if isinstance(before, dict) else {}
        res = {k: walk(v, before.get(k), path + [_seg(k)]) for k, v in obj.items() if not is_secret(k)}
        for k, v in obj.items():                     # 密钥键最后处理:同时带 `password` 与 `password_ref` 时新值胜
            if not is_secret(k):
                continue
            name = "/".join([base, *path, _seg(k)])
            if isinstance(v, str) and v:
                out.puts.append((name, v))
                res[f"{k}_ref"] = f"vault://{name}"
            elif f"{k}_ref" not in res and isinstance(before.get(f"{k}_ref"), str):
                res[f"{k}_ref"] = before[f"{k}_ref"]
        return res

    prev = prev if isinstance(prev, dict) else {}
    for k, v in body.items():
        if is_secret(k):
            if isinstance(v, str) and v:
                name = f"{base}/{k}"                 # 🔴 顶层路径与改前逐字一致(不做转义),存量引用不失效
                out.puts.append((name, v))
                out.top_refs[k] = f"vault://{name}"
            continue
        out.body[k] = walk(v, prev.get(k), [_seg(k)])
    return out


async def migrate_plaintext(store: Any, vault: Any) -> int:
    """存量迁移:``settings`` 里 ``config.<group>`` / ``config.__all__`` 行中残留的密钥键 → 先写 Vault,再比较并交换改库。

    任一步失败该行原样保留(下轮再试),不会出现「明文已删、Vault 没写成」。没有密钥键的库只读一遍、一条语句不写。
    返回改动的行数。
    """
    rows = store.con.execute("SELECT key, value_json FROM settings WHERE key LIKE 'config.%'").fetchall()
    changed = 0
    groups_raw = {r["key"][7:]: r["value_json"] for r in rows if "." not in r["key"][7:]}
    for key_group, raw in groups_raw.items():
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if key_group == "__all__":
            new, puts, tops = {}, [], {}
            for g, blob in value.items():
                if not isinstance(blob, dict):
                    new[g] = blob
                    continue
                # 与 `config.<g>` 同值(正常情形:#89 两处写同一份)⇒ 同一套路径;不同则另起前缀,免得互相覆盖 Vault 值
                same = _loads(groups_raw.get(g)) == blob
                s = seal(g, blob, blob, prefix="" if same else f"settings/__all__/{g}")
                new[g], puts = s.body, puts + s.puts
                tops.update({(g, k): r for k, r in s.top_refs.items()} if same else {})
        else:
            s = seal(key_group, value, value)
            new, puts, tops = s.body, s.puts, {(key_group, k): r for k, r in s.top_refs.items()}
        if new == value and not puts:
            continue
        try:
            for name, v in puts:
                await vault.put(name, v, scope=VAULT_SCOPE)
        except Exception as e:                       # noqa: BLE001 —— Vault 不在/拒写:本行原样保留,下轮再试
            log.warning("settings 存量密钥迁移:%s 写 Vault 失败(%s),本轮跳过", f"config.{key_group}", e)
            continue
        if not store.settings_swap(f"config.{key_group}", raw, new, actor="system:secret_migrate"):
            continue                                 # 期间被并发 PUT 改过:下轮按新值再判
        for (g, k), ref in tops.items():
            store.settings_set(f"config.{g}.{k}_ref", ref, actor="system:secret_migrate")
        changed += 1
    return changed


def _loads(raw: Any) -> Any:
    try:
        return json.loads(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def redact(obj: Any) -> Any:
    """递归把密钥类键的值擦成 ``***``;返回新对象,不改入参(给落库/日志/事件的副本用)。"""
    if isinstance(obj, dict):
        return {k: ("***" if is_secret(k) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


__all__ = ["TOP_SUFFIXES", "VAULT_SCOPE", "Sealed", "is_secret", "seal", "migrate_plaintext", "redact"]
