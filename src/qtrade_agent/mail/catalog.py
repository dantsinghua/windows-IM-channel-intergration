"""能力目录的只读视图 —— 规格:docs/06 §2.8「单一来源(E-2)」:邮件指令里 ``操作`` 的取值集合、每个 op 的参数名/类型/必填、

``confirmable``/``danger``/``kind`` 属性,**全部来自 02 §3.10 的能力目录 JSON**(``capabilities/<op>.json``,``GET /capabilities`` 下发同一份)。
本册**不复写 danger 清单**——新增/变更 danger 项只改 02 §3.10 一处,邮件侧零改动。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

CAPABILITIES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "capabilities")


@dataclass
class Capability:
    op: str
    kind: str = "read"
    danger: bool = False
    confirmable: bool = False
    channels: dict[str, str] = field(default_factory=dict)
    args_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def properties(self) -> dict[str, Any]:
        return dict(self.args_schema.get("properties") or {})

    @property
    def required(self) -> list[str]:
        return [str(x) for x in (self.args_schema.get("required") or [])]

    @property
    def additional_properties(self) -> bool:
        return bool(self.args_schema.get("additionalProperties", True))


class Catalog:
    """``GET /capabilities`` 的那一份;邮件侧只读不改。"""

    def __init__(self, caps: dict[str, Capability]):
        self.caps = dict(caps)

    @classmethod
    def load(cls, directory: str = CAPABILITIES_DIR) -> "Catalog":
        caps: dict[str, Capability] = {}
        if os.path.isdir(directory):
            for name in sorted(os.listdir(directory)):
                if not name.endswith(".json"):
                    continue
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    d = json.load(f)
                op = str(d.get("op") or os.path.splitext(name)[0])
                caps[op] = Capability(op=op, kind=str(d.get("kind", "read")), danger=bool(d.get("danger", False)),
                                      confirmable=bool(d.get("confirmable", False)),
                                      channels=dict(d.get("channels") or {}),
                                      args_schema=dict(d.get("args_schema") or {}))
        return cls(caps)

    @classmethod
    def from_dicts(cls, items: list[dict[str, Any]]) -> "Catalog":
        """测试/接线用:直接给目录项(含尚未落成 JSON 的 danger op,如 ``account_stop``)。"""
        return cls({str(d["op"]): Capability(
            op=str(d["op"]), kind=str(d.get("kind", "read")), danger=bool(d.get("danger", False)),
            confirmable=bool(d.get("confirmable", False)), channels=dict(d.get("channels") or {}),
            args_schema=dict(d.get("args_schema") or {})) for d in items})

    def get(self, op: str) -> Optional[Capability]:
        return self.caps.get(op)

    def __contains__(self, op: str) -> bool:
        return op in self.caps

    def expand_allow_ops(self, allow_ops: list[str]) -> set[str]:
        """🔴 R2-1 / 基线 §11.17 [MAILOPS] ②:``["*"]`` **只展开 ``danger=false`` 的能力**;

        ``danger=true`` 的 op **只有被逐字写进 ``allow_ops`` 才生效**——不是「全能力目录」。
        """
        out: set[str] = set()
        for item in allow_ops or []:
            if item == "*":
                out |= {op for op, c in self.caps.items() if not c.danger}
            elif item in self.caps:
                out.add(item)
        return out
