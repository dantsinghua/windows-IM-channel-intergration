"""工作流定义与校验(02 §2.2.6「YAML 形态(M5 直线流程)」;#39 解析校验失败 ``400`` 带行号、#47 ``/workflows/validate``)。

**M5 只做直线流程(R-13,基线 §11.22 [SCOPE] 降级)**:固定步骤顺序执行 + 失败即停 + 每步留痕,
**不做**条件分支、循环、变量、表达式求值。**固定工作流,无 LLM(A-1)**:步骤顺序全由 YAML 顺序决定,
不接任何大模型做规划/选步/改参。

⚠️ 原设计的 ``for_each`` / ``{{ where }}`` 过滤 / ``on_error: retry|skip`` / ``parallel:`` 块
**一律推迟到 M5 之后**(不在本版设计面内)—— 本校验器对它们**显式报错**,而不是静默忽略:
静默忽略会让一份写了 ``on_error: skip`` 的 YAML「看起来被接受了、行为却是失败即停」。

规格里的 YAML 形态(逐字抄自 §2.2.6):

.. code-block:: yaml

    name: broadcast_notice
    version: 1
    args: { }
    steps:
      - id: s1
        op: send_text
        account: qd01
        args: { session: "qd01:g_123", text: "巡检开始" }   # 参数是字面量,不做表达式求值
      - id: s3
        op: webhook            # 引擎内置步骤:把前面结果 POST 给业务系统(固定 URL,取自 settings.callback_url)
        args: { url_ref: "settings.callback_url" }
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import yaml_min

# §3.1 workflow_steps.account_id 注:「内置步骤(webhook/sleep)为 NULL」
BUILTIN_OPS = ("webhook", "sleep")
# R-13 推迟到 M5 之后的四类写法(出现即报错,不静默忽略)
DEFERRED_KEYS = ("for_each", "where", "on_error", "parallel")
EXPR_RE = re.compile(r"\{\{.*?\}\}")
STEP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,64}$")

Loader = Callable[[str], Any]


@dataclass
class WorkflowError:
    line: int
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "message": self.message}


class WorkflowParseError(ValueError):
    """#39 / #47:``400 INVALID_ARGS`` 带行号;``errors`` 逐条给控制台编辑器。"""

    def __init__(self, errors: list[WorkflowError]):
        super().__init__("; ".join(f"第 {e.line} 行:{e.message}" for e in errors) or "工作流校验失败")
        self.errors = errors

    def as_list(self) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self.errors]


@dataclass
class Step:
    """一步 = 对 ``bus.submit`` 的调用(内置 ``webhook``/``sleep`` 除外);``idx`` 是**线性执行序**(R-13,非 for_each 展开)。"""
    idx: int
    id: str
    op: str
    args: dict[str, Any] = field(default_factory=dict)
    account: Optional[str] = None
    line: int = 0

    @property
    def is_builtin(self) -> bool:
        return self.op in BUILTIN_OPS


@dataclass
class WorkflowDef:
    name: str
    version: int
    steps: list[Step]
    args: dict[str, Any] = field(default_factory=dict)
    yaml: str = ""

    @property
    def checksum(self) -> str:
        """§3.1 ``workflows.checksum`` = ``sha256(yaml)``。"""
        return hashlib.sha256(self.yaml.encode("utf-8")).hexdigest()


def _line_of(yaml_text: str, needle: str, default: int = 1) -> int:
    """在原文里找 ``needle`` 所在行号(报错带行号用;找不到回 ``default``)。"""
    for i, raw in enumerate(yaml_text.splitlines(), start=1):
        if needle in raw:
            return i
    return default


def _scan_deferred(yaml_text: str, errors: list[WorkflowError]) -> None:
    for i, raw in enumerate(yaml_text.splitlines(), start=1):
        body = raw.split("#", 1)[0]
        for key in DEFERRED_KEYS:
            if re.search(rf"(^|\s|-\s){key}\s*:", body):
                errors.append(WorkflowError(i, f"`{key}` 属于 R-13 推迟到 M5 之后的写法,本版只做直线流程(02 §2.2.6)"))
        if EXPR_RE.search(body):
            errors.append(WorkflowError(i, "参数是字面量,不做表达式求值(`{{ }}` 不在本版设计面内,02 §2.2.6)"))


def parse_workflow(yaml_text: str, *, loader: Optional[Loader] = None) -> WorkflowDef:
    """解析 + 校验;失败抛 :class:`WorkflowParseError`(带行号)。``loader`` 缺省 = 内置最小子集解析器。"""
    errors: list[WorkflowError] = []
    _scan_deferred(yaml_text, errors)
    load = loader or yaml_min.safe_load
    try:
        doc = load(yaml_text)
    except yaml_min.YamlError as e:
        errors.append(WorkflowError(e.line, e.message))
        raise WorkflowParseError(errors) from e
    except Exception as e:                       # 注入的 PyYAML 抛自己的异常类型
        line = getattr(getattr(e, "problem_mark", None), "line", None)
        errors.append(WorkflowError((line + 1) if isinstance(line, int) else 1, f"YAML 解析失败:{e}"))
        raise WorkflowParseError(errors) from e

    if not isinstance(doc, dict):
        errors.append(WorkflowError(1, "顶层必须是映射(name/version/args/steps)"))
        raise WorkflowParseError(errors)

    name = doc.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append(WorkflowError(_line_of(yaml_text, "name:"), "`name` 必填,且只能是字母/数字/`_-.`(≤64)"))
        name = str(name or "")
    version = doc.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append(WorkflowError(_line_of(yaml_text, "version:"), "`version` 必须是 ≥1 的整数"))
        version = 1
    wf_args = doc.get("args") or {}
    if not isinstance(wf_args, dict):
        errors.append(WorkflowError(_line_of(yaml_text, "args:"), "`args` 必须是映射"))
        wf_args = {}

    raw_steps = doc.get("steps")
    steps: list[Step] = []
    if not isinstance(raw_steps, list) or not raw_steps:
        errors.append(WorkflowError(_line_of(yaml_text, "steps:"), "`steps` 必填,且至少一步"))
    else:
        seen: set[str] = set()
        for idx, raw in enumerate(raw_steps):
            line = _line_of(yaml_text, f"id: {raw.get('id')}" if isinstance(raw, dict) else "steps:")
            if not isinstance(raw, dict):
                errors.append(WorkflowError(line, f"第 {idx + 1} 步必须是映射"))
                continue
            sid = raw.get("id")
            if not isinstance(sid, str) or not STEP_ID_RE.match(sid):
                errors.append(WorkflowError(line, f"第 {idx + 1} 步的 `id` 必填,且只能是字母/数字/`_-`(≤64)"))
                sid = str(sid or f"s{idx + 1}")
            if sid in seen:
                errors.append(WorkflowError(line, f"步骤 id 重复:{sid}"))
            seen.add(sid)
            op = raw.get("op")
            if not isinstance(op, str) or not op:
                errors.append(WorkflowError(line, f"步骤 {sid} 的 `op` 必填"))
                op = str(op or "")
            step_args = raw.get("args") or {}
            if not isinstance(step_args, dict):
                errors.append(WorkflowError(line, f"步骤 {sid} 的 `args` 必须是映射"))
                step_args = {}
            account = raw.get("account")
            if op in BUILTIN_OPS:
                if account is not None:
                    errors.append(WorkflowError(line, f"内置步骤 `{op}` 不挂账号(§3.1:内置步骤的 account_id 为 NULL)"))
                if op == "webhook" and not isinstance(step_args.get("url_ref"), str):
                    errors.append(WorkflowError(line, "内置步骤 `webhook` 须带 `args.url_ref`(如 `settings.callback_url`)"))
                if op == "sleep" and not isinstance(step_args.get("seconds"), (int, float)):
                    errors.append(WorkflowError(line, "内置步骤 `sleep` 须带数值 `args.seconds`"))
            elif not isinstance(account, str) or not account:
                errors.append(WorkflowError(line, f"步骤 {sid} 须显式写 `account`(不做跨账号隐式展开,02 §2.2.6)"))
            extra = set(raw) - {"id", "op", "account", "args"}
            if extra:
                errors.append(WorkflowError(line, f"步骤 {sid} 出现未定义的键:{sorted(extra)}"))
            steps.append(Step(idx=idx, id=sid, op=op, args=step_args,
                              account=account if isinstance(account, str) else None, line=line))
    extra_top = set(doc) - {"name", "version", "args", "steps"}
    if extra_top:
        errors.append(WorkflowError(1, f"顶层出现未定义的键:{sorted(extra_top)}"))

    if errors:
        raise WorkflowParseError(errors)
    return WorkflowDef(name=name, version=version, steps=steps, args=wf_args, yaml=yaml_text)


def validate(yaml_text: str, *, loader: Optional[Loader] = None) -> dict[str, Any]:
    """#47 ``POST /workflows/validate`` 的出参:``{ok, errors:[{line, message}]}``。"""
    try:
        parse_workflow(yaml_text, loader=loader)
    except WorkflowParseError as e:
        return {"ok": False, "errors": e.as_list()}
    return {"ok": True, "errors": []}
