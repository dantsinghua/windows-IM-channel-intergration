"""把 shape_audit.py 采到的两份 JSON 做「键路径 + 类型」差集(前端期望 = mock,后端实际 = 真 Agent)。"""
from __future__ import annotations

import json
import sys


def skeleton(v, prefix="", out=None, depth=0):
    out = {} if out is None else out
    if depth > 6:
        return out
    if isinstance(v, dict):
        for k, val in v.items():
            p = f"{prefix}.{k}" if prefix else k
            out[p] = type(val).__name__ if not isinstance(val, (dict, list)) else ("obj" if isinstance(val, dict) else "arr")
            skeleton(val, p, out, depth + 1)
    elif isinstance(v, list):
        if v:
            skeleton(v[0], f"{prefix}[]", out, depth + 1)
    return out


def main():
    real = json.load(open(sys.argv[1], encoding="utf-8"))
    mock = json.load(open(sys.argv[2], encoding="utf-8"))
    for cid in sorted(set(real) | set(mock)):
        r, m = real.get(cid, {}), mock.get(cid, {})
        rs, ms = r.get("status"), m.get("status")
        sr = skeleton(r.get("body")) if isinstance(r.get("body"), (dict, list)) else {}
        sm = skeleton(m.get("body")) if isinstance(m.get("body"), (dict, list)) else {}
        only_mock = {k: sm[k] for k in sm if k not in sr}
        only_real = {k: sr[k] for k in sr if k not in sm}
        type_diff = {k: (sm[k], sr[k]) for k in sm if k in sr and sm[k] != sr[k]}
        same = not (only_mock or only_real or type_diff) and rs == ms
        print(f"\n### {cid}   [{r.get('src') or m.get('src')}]  {r.get('request',m.get('request',{})).get('method')} {r.get('request',m.get('request',{})).get('path')}")
        print(f"  status: mock={ms} real={rs}" + ("   ✅ 形状一致" if same else ""))
        if only_mock:
            print(f"  ❌ 前端(mock)有、后端缺: {json.dumps(only_mock, ensure_ascii=False)[:1400]}")
        if only_real:
            print(f"  ⚠️ 后端有、mock 无: {json.dumps(only_real, ensure_ascii=False)[:1400]}")
        if type_diff:
            print(f"  ❗类型不同(mock vs real): {json.dumps(type_diff, ensure_ascii=False)[:1200]}")


if __name__ == "__main__":
    main()
