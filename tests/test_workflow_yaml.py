"""工作流 YAML 解析与校验(02 §2.2.6 形态;#39 带行号、#47 ``{ok, errors}``)。

重点:R-13 砍掉的四类写法(``for_each``/``where``/``on_error``/``parallel``)与 ``{{ }}`` 表达式**必须报错**,
不能静默忽略 —— 否则一份写了 ``on_error: skip`` 的 YAML 会「看起来被接受、行为却是失败即停」。
"""
from __future__ import annotations

import pytest

from qtrade_agent.workflow import parse_workflow, validate
from qtrade_agent.workflow.model import WorkflowParseError
from qtrade_agent.workflow.yaml_min import YamlError, safe_load

SPEC_YAML = """name: broadcast_notice
version: 1
args: { }
steps:
  - id: s1
    op: send_text
    account: qd01
    args: { session: "qd01:g_123", text: "巡检开始" }   # 参数是字面量,不做表达式求值
  - id: s2
    op: send_text
    account: qq03
    args: { session: "qq03:g_456", text: "巡检开始" }
  - id: s3
    op: webhook            # 引擎内置步骤
    args: { url_ref: "settings.callback_url" }
"""


# ---------------------------------------------------------------- 最小子集解析器
def test_spec_yaml_parses_to_expected_shape():
    wf = parse_workflow(SPEC_YAML)
    assert wf.name == "broadcast_notice" and wf.version == 1 and wf.args == {}
    assert [s.id for s in wf.steps] == ["s1", "s2", "s3"]
    assert [s.idx for s in wf.steps] == [0, 1, 2]                  # idx = 线性执行序(R-13)
    assert wf.steps[0].args == {"session": "qd01:g_123", "text": "巡检开始"}
    assert wf.steps[2].account is None and wf.steps[2].is_builtin
    assert len(wf.checksum) == 64


def test_loader_handles_scalars_comments_and_flow_collections():
    doc = safe_load('a: 1\nb: "x#y"   # 注释\nc: true\nd: null\ne: [1, "z"]\nf: { k: v }\ng: 1.5\n')
    assert doc == {"a": 1, "b": "x#y", "c": True, "d": None, "e": [1, "z"], "f": {"k": "v"}, "g": 1.5}


def test_loader_handles_nested_block_mapping_under_step():
    doc = safe_load("steps:\n  - id: s1\n    op: send_text\n    args:\n      session: s\n      text: t\n")
    assert doc == {"steps": [{"id": "s1", "op": "send_text", "args": {"session": "s", "text": "t"}}]}


def test_loader_rejects_tabs_unclosed_quotes_and_non_mapping_lines():
    with pytest.raises(YamlError) as e:
        safe_load("a: 1\n\tb: 2\n")
    assert e.value.line == 2
    with pytest.raises(YamlError):
        safe_load('a: "no close\n')
    with pytest.raises(YamlError) as e3:
        safe_load("just a line\n")
    assert e3.value.line == 1


def test_loader_rejects_duplicate_keys():
    with pytest.raises(YamlError) as e:
        safe_load("a: 1\na: 2\n")
    assert "重复的键" in e.value.message


# ---------------------------------------------------------------- R-13 砍掉的写法必须报错
@pytest.mark.parametrize("snippet,key", [
    ("    for_each: accounts\n", "for_each"),
    ("    where: enabled\n", "where"),
    ("    on_error: skip\n", "on_error"),
    ("    parallel: true\n", "parallel"),
])
def test_deferred_keys_are_rejected_with_line_number(snippet, key):
    y = "name: x\nversion: 1\nsteps:\n  - id: s1\n    op: send_text\n    account: qd01\n" + snippet
    result = validate(y)
    assert result["ok"] is False
    hit = [e for e in result["errors"] if key in e["message"]]
    assert hit and hit[0]["line"] == 7


def test_expression_placeholders_are_rejected():
    y = 'name: x\nversion: 1\nsteps:\n  - id: s1\n    op: send_text\n    account: qd01\n    args: { text: "{{ acct }}" }\n'
    errors = validate(y)["errors"]
    assert any("字面量" in e["message"] and e["line"] == 7 for e in errors)


# ---------------------------------------------------------------- 结构校验
def test_missing_name_or_steps_is_an_error():
    assert validate("version: 1\nsteps:\n  - id: a\n    op: send_text\n    account: qd01\n")["ok"] is False
    assert validate("name: x\nversion: 1\n")["ok"] is False


def test_duplicate_step_id_and_missing_account():
    y = ("name: x\nversion: 1\nsteps:\n  - id: s1\n    op: send_text\n    account: qd01\n"
         "  - id: s1\n    op: send_text\n")
    msgs = [e["message"] for e in validate(y)["errors"]]
    assert any("步骤 id 重复" in m for m in msgs)
    assert any("须显式写 `account`" in m for m in msgs)


def test_builtin_steps_have_their_own_rules():
    assert validate("name: x\nversion: 1\nsteps:\n  - id: s\n    op: webhook\n")["ok"] is False   # 缺 url_ref
    assert validate('name: x\nversion: 1\nsteps:\n  - id: s\n    op: webhook\n    account: qd01\n'
                    '    args: { url_ref: "settings.callback_url" }\n')["ok"] is False           # 内置不挂账号
    assert validate("name: x\nversion: 1\nsteps:\n  - id: s\n    op: sleep\n    args: { seconds: 3 }\n")["ok"] is True


def test_unknown_keys_are_rejected_top_level_and_per_step():
    assert any("顶层出现未定义的键" in e["message"]
               for e in validate("name: x\nversion: 1\nschedule: '* * * * *'\nsteps:\n  - id: s\n    op: sleep\n"
                                 "    args: { seconds: 1 }\n")["errors"])
    assert any("未定义的键" in e["message"]
               for e in validate("name: x\nversion: 1\nsteps:\n  - id: s\n    op: sleep\n    retries: 3\n"
                                 "    args: { seconds: 1 }\n")["errors"])


def test_version_must_be_positive_int():
    assert validate("name: x\nversion: 0\nsteps:\n  - id: s\n    op: sleep\n    args: { seconds: 1 }\n")["ok"] is False


def test_parse_error_carries_all_errors_and_lines():
    with pytest.raises(WorkflowParseError) as e:
        parse_workflow("name: x\nversion: 1\nsteps:\n  - id: s1\n    op: send_text\n")
    assert all({"line", "message"} == set(item) for item in e.value.as_list())


def test_validate_ok_shape():
    assert validate(SPEC_YAML) == {"ok": True, "errors": []}


def test_external_loader_can_be_injected():
    """装了 PyYAML 的部署可注入 ``yaml.safe_load``;校验规则不变。"""
    called: list[str] = []

    def loader(text: str):
        called.append(text)
        return {"name": "x", "version": 1, "steps": [{"id": "s", "op": "sleep", "args": {"seconds": 1}}]}

    wf = parse_workflow("name: x\n", loader=loader)
    assert called and wf.steps[0].op == "sleep"
