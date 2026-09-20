"""代码 ↔ 文档对账:配置默认值与 02 §7.1 同值;schema 文件与 02 §3.1 的 sql 块逐字一致;能力目录会话参数一律叫 session。"""
from __future__ import annotations

import glob
import json
import os
import re

import pytest

from qtrade_agent.config import AgentConfig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC02 = glob.glob(os.path.join(ROOT, "docs", "02-*.md"))[0]
DOC06 = glob.glob(os.path.join(ROOT, "docs", "06-*.md"))[0]
SCHEMA = os.path.join(ROOT, "src", "qtrade_agent", "store", "schema_agent.sql")
CAPS = os.path.join(ROOT, "src", "qtrade_agent", "capabilities")


def _doc(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def test_schema_file_matches_doc_sql_block():
    blocks = re.findall(r"```sql\n(.*?)```", _doc(DOC02), re.S)
    assert _doc(SCHEMA).endswith(blocks[1]), "schema_agent.sql 与 02 §3.1 的 ```sql 块不一致:改表先改 02,再重新抽取"


@pytest.mark.parametrize("key, value", [
    ("confirm_timeout_qidian_ms", AgentConfig().bus.confirm_timeout_qidian_ms),
    ("out_merge_window_s", AgentConfig().bus.out_merge_window_s),
    ("late_after_s", AgentConfig().messages.late_after_s),
    ("confirm_poll_interval_ms", AgentConfig().qidian.confirm_poll_interval_ms),
    ("poll_interval_s", AgentConfig().qidian.poll_interval_s),
])
def test_config_defaults_match_doc_02(key, value):
    t = _doc(DOC02)
    if key == "confirm_timeout_qidian_ms":
        m = re.search(r"`confirm_timeout_qidian_ms`[^|\n]*\|\s*`\d+`\s*/\s*`(\d+)`", t)
    elif key == "confirm_poll_interval_ms":
        m = re.search(r"\| `confirm_poll_interval_ms` \| `(\d+)` \| \*\*R6-38", t)
    elif key == "poll_interval_s":
        m = re.search(r"\| `poll_interval_s` \| `(\d+)` \| 旁路读库", t)
    else:
        m = re.search(r"`%s` \| `(\d+)`" % key, t)
    assert m, f"02 §7.1 找不到 {key}"
    assert int(m.group(1)) == value


def test_gap_keys_match_doc_02():
    m = re.search(r"`gap_check_interval_s` / `gap_window_days` / `gap_min_missing` \| `(\d+)` / `(\d+)` / `(\d+)`", _doc(DOC02))
    c = AgentConfig().qidian
    assert tuple(map(int, m.groups())) == (c.gap_check_interval_s, c.gap_window_days, c.gap_min_missing)


def test_norm_definition_in_code_equals_doc_06():
    """06 §2.9.2 的 def norm 函数体逐行等于 text.py 里的实现(去掉注释与空行)。"""
    m = re.search(r"def norm\(s: str \| None\) -> str:(.*?)```", _doc(DOC06), re.S)
    assert m
    doc_lines = [re.sub(r"\s*#.*$", "", l).strip() for l in m.group(1).splitlines()]
    doc_lines = [l for l in doc_lines if l and not l.startswith('"""')]
    src = _doc(os.path.join(ROOT, "src", "qtrade_agent", "text.py"))
    body = re.search(r"def norm\(s: str \| None\) -> str:(.*?)\n\n\ndef ", src, re.S).group(1)
    body = re.sub(r'""".*?"""', "", body, flags=re.S)                # 去掉 docstring
    code_lines = [re.sub(r"\s*#.*$", "", l).strip() for l in body.splitlines()]
    code_lines = [l for l in code_lines if l]
    assert doc_lines == code_lines, (doc_lines, code_lines)


def test_capabilities_session_param_name():
    for p in glob.glob(os.path.join(CAPS, "*.json")):
        cap = json.load(open(p, encoding="utf-8"))
        assert cap["op"] == os.path.basename(p)[:-5]
        assert cap["kind"] in ("read", "write", "admin") and isinstance(cap["danger"], bool)
        for name in cap["args_schema"].get("properties", {}):
            assert name not in ("peer", "chat", "target", "talker", "group_id", "user_id"), f"{p}: 会话参数一律命名 session"
