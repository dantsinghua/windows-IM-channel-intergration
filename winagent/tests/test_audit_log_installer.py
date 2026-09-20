"""日志行格式与脱敏(02 §2.9)、``wa_audit_log``(02 §2.4)、``installer-ops``(03 §3.3 + 02 §2.4.1 R3-15)、装配自检。"""
from __future__ import annotations

import os

import pytest

import re

from qtrade_winagent import __version__
from qtrade_winagent.audit import ACTOR_SVC_TO_USER, ACTORS, Audit
from qtrade_winagent.db import SCHEMA_PATH, Db
from qtrade_winagent.errors import HTTP_BY_CODE, WaError, user_agent_offline
from qtrade_winagent.fakes import FakeCrypto
from qtrade_winagent.ids import login_session_id, trace_id, ulid
from qtrade_winagent.installer_ops import KERNEL_TO_STATES, InstallerOps
from qtrade_winagent.logfmt import format_line, get_logger, iso8601, redact, redact_path


# ---------------------------------------------------------------- 02 §2.9 日志行
def test_line_field_order_is_fixed():
    line = format_line(ts_ms=1_758_240_000_123, level="INFO", logger="wa.bus", msg="delivered", trace="01J8Q",
                       acct="wx01", op="send_text", code="DELIVERED", cost_ms=1650)
    assert " INFO  " in line and "wa.bus    " in line
    assert line.index("trace=") < line.index("acct=") < line.index("op=") < line.index("code=") < line.index("cost_ms=")
    assert line.index("cost_ms=") < line.index('msg="')


def test_missing_fields_are_omitted():
    line = format_line(ts_ms=0, level="WARN", logger="wa.pipe", msg="x")
    assert "trace=" not in line and "acct=" not in line and "op=" not in line


def test_logger_prefix_is_wa():
    assert get_logger("wslctl").name == "wa.wslctl"
    assert get_logger("wa.vault").name == "wa.vault"                    # 已带前缀不重复加


def test_iso8601_has_millis_and_offset():
    s = iso8601(1_758_240_000_123)
    assert s[10] == "T" and "." in s and (s[-6] in "+-") and s[-3] == ":"


@pytest.mark.parametrize("key,value,expect_contains", [
    ("password", "hunter2", "***"), ("token", "abc", "***"), ("authorization", "Bearer x", "***"),
    ("api_key", "k", "***"), ("signature", "s", "***"),
])
def test_credential_keys_are_masked(key, value, expect_contains):
    assert redact({key: value})[key] == expect_contains


def test_secret_ref_shows_reference_only():
    assert redact({"secret_ref": "vault://account/qd01"})["secret_ref"] == "vault://account/qd01"


def test_text_keys_become_sha8_len():
    out = redact({"text": "你好世界"})["text"]
    assert out.startswith("sha8:") and out.endswith(":len4")


def test_b64_and_addr_and_path_rules():
    assert redact({"qrcode_png_b64": "A" * 120})["qrcode_png_b64"] == "<b64:120B>"
    assert redact({"to_addrs": ["ops@corp.com"]})["to_addrs"] == ["o***@corp.com"]
    assert redact_path(r"C:\Users\anlin\AppData\x") == r"%USERPROFILE%\AppData\x"


def test_redaction_is_recursive():
    out = redact({"a": {"password": "p", "b": [{"body_text": "hi"}]}})
    assert out["a"]["password"] == "***" and out["a"]["b"][0]["body_text"].startswith("sha8:")


# ---------------------------------------------------------------- 错误信封(00 §10)
def test_http_status_map_matches_00_10():
    assert HTTP_BY_CODE["INVALID_ARGS"] == 400 and HTTP_BY_CODE["UNAUTHORIZED"] == 401
    assert HTTP_BY_CODE["FORBIDDEN"] == 403 and HTTP_BY_CODE["TARGET_NOT_FOUND"] == 404
    assert HTTP_BY_CODE["RESOURCE_EXHAUSTED"] == 409 and HTTP_BY_CODE["RATE_LIMITED"] == 429
    assert HTTP_BY_CODE["DISK_FULL"] == 507 and HTTP_BY_CODE["NOT_READY"] == 503


def test_envelope_shape_and_mixed_stage_partial():
    b = WaError("INVALID_ARGS", "参数不对", reason="bad").body("T1")
    assert b["ok"] is False and b["code"] == "INVALID_ARGS" and b["trace_id"] == "T1"
    assert "stage" not in b["error"]                                     # 非混合端点不带
    m = WaError("INTERNAL", "炸了", stage="user", partial=["svc:a"]).body(None)
    assert m["error"]["stage"] == "user" and m["error"]["partial"] == ["svc:a"]


def test_user_agent_offline_message_is_verbatim():
    e = user_agent_offline()
    assert e.http_status == 503 and e.message == "用户会话代理未运行(用户未登录或代理被结束)"


# ---------------------------------------------------------------- ids
def test_ulid_is_26_chars_crockford_and_prefixes():
    u = ulid()
    assert len(u) == 26 and set(u) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert login_session_id().startswith("ls_") and len(trace_id()) == 26


# ---------------------------------------------------------------- wa_audit_log
def test_actor_enum_includes_svc_to_user_arrow():
    assert ACTOR_SVC_TO_USER == "svc→user" and ACTOR_SVC_TO_USER in ACTORS


def test_audit_strips_values_for_vault_actions():
    db = Db(":memory:").open()
    a = Audit(db)
    a.record(actor="agent", action="vault.read", target="account/qd01",
             detail={"scope": "account", "value": "SECRET", "len": 6, "sha": "abc"})
    row = db.one("SELECT detail_json FROM wa_audit_log")
    assert "SECRET" not in row["detail_json"] and "len" not in row["detail_json"] and "sha" not in row["detail_json"]
    assert "account" in row["detail_json"]
    db.close()


def test_audit_redacts_paths_and_pages_backwards():
    db = Db(":memory:").open()
    a = Audit(db)
    for i in range(5):
        a.record(actor="console", action=f"a{i}", detail={"path": r"C:\Users\anlin\x"})
    page = a.page(limit=2)
    assert len(page["items"]) == 2 and page["items"][0]["action"] == "a4"
    assert page["items"][0]["detail"]["path"] == r"%USERPROFILE%\x"
    page2 = a.page(limit=2, cursor=page["next_cursor"])
    assert page2["items"][0]["action"] == "a2"
    db.close()


# ---------------------------------------------------------------- installer-ops
def mk_installer(tmp_path, hub=None):
    db = Db(":memory:").open()
    return db, InstallerOps(db, kernel_dir=str(tmp_path / "kernel"), wsl_backup_dir=str(tmp_path / "wslbak"),
                            package_version=__version__, crypto=FakeCrypto(), audit=Audit(db), hub=hub)


def test_install_state_single_row_and_json_sections(tmp_path):
    db, io_ = mk_installer(tmp_path)
    io_.put_state(state="PRECHECK", env={"wsl_state": "OK"})
    io_.put_state(state="WINAGENT_INSTALLED", substate="firewall", clients={"wechat": {"match": "SUPPORTED"}})
    rows = db.query("SELECT * FROM install_state")
    assert len(rows) == 1                                                # key='current' 固定单行
    st = io_.state()
    assert st["state"] == "WINAGENT_INSTALLED" and st["env"] == {"wsl_state": "OK"}   # 未传的段保留
    assert st["clients"]["wechat"]["match"] == "SUPPORTED"
    db.close()


def test_install_history_actor_enum(tmp_path):
    db, io_ = mk_installer(tmp_path)
    io_.history(to_state="DONE", from_state="SELFTEST_OK", actor="installer", note="ok")
    with pytest.raises(WaError):
        io_.history(to_state="X", actor="不存在的执行者")
    assert io_.history_page()[0]["to_state"] == "DONE"
    db.close()


# ---------------------------------------------------------------- R6-58 (au):install_history.to_state 七个内核迁移名
def test_kernel_to_states_match_ddl_registration():
    """``install_history.to_state`` **无 CHECK 是有意的**(schema_winagent.sql 注释逐字登记七个名字)——
    installer_ops.KERNEL_TO_STATES 必须与该注释逐字一致,实现方不得另起同义名。"""
    sql = open(SCHEMA_PATH, encoding="utf-8").read()
    m = re.search(r"实现方不得另起同义名\):(.+?)\(#26/#27/#46 写\)", sql, re.S)
    assert m is not None, "schema_winagent.sql 里的 R6-58 (au) 登记注释找不到了,DDL 与代码脱节"
    names = tuple(n.strip() for n in re.sub(r"--", " ", m.group(1)).split("/") if n.strip())
    assert names == KERNEL_TO_STATES
    assert KERNEL_TO_STATES == ("KERNEL_APPLYING", "KERNEL_APPLIED", "KERNEL_APPLY_FAILED", "KERNEL_VERIFIED",
                                "KERNEL_VERIFY_FAILED", "KERNEL_ROLLING_BACK", "KERNEL_ROLLED_BACK")


def test_history_rejects_kernel_to_state_synonyms(tmp_path):
    db, io_ = mk_installer(tmp_path)
    with pytest.raises(WaError) as e:
        io_.history(to_state="KERNEL_APLLIED", actor="winagent")        # 打错的同义名
    assert e.value.reason == "bad_kernel_to_state"
    io_.history(to_state="KERNEL_APPLYING", actor="winagent")           # 登记过的名字放行
    assert io_.history_page()[0]["to_state"] == "KERNEL_APPLYING"
    io_.history(to_state="DONE", actor="installer")                    # 非 KERNEL_ 前缀(00 §8.2 安装状态机键)不受本校验约束
    db.close()


def test_stage_kernel_copies_and_tightens_acl(tmp_path):
    db, io_ = mk_installer(tmp_path)
    src = tmp_path / "bzImage.src"
    src.write_bytes(b"KERNEL")
    dst = io_.stage_kernel(str(src))
    assert os.path.exists(dst) and open(dst, "rb").read() == b"KERNEL"
    assert io_.current_kernel() == dst
    db.close()


async def test_mixed_endpoint_without_pipe_fails_at_svc_stage(tmp_path):
    db, io_ = mk_installer(tmp_path)                                     # hub=None
    src = tmp_path / "bzImage"
    src.write_bytes(b"K")
    with pytest.raises(WaError) as e:
        await io_.kernel_apply(confirm_shutdown=True, kernel_src=str(src))
    assert e.value.stage == "svc" and e.value.reason == "no_pipe"
    assert e.value.partial == ["svc:stage_kernel", "svc:history_begin"]   # 已完成的半要如实报出来
    db.close()


async def test_kernel_apply_without_kernel_file_fails(tmp_path):
    db, io_ = mk_installer(tmp_path)
    with pytest.raises(WaError) as e:
        await io_.kernel_apply(confirm_shutdown=True)
    assert e.value.reason == "kernel_missing" and e.value.stage == "svc"
    db.close()


# ---------------------------------------------------------------- 装配自检(--dev 路径与真机路径同一段代码)
def test_dev_assembly_uses_fakes_and_touches_nothing_real(tmp_path):
    from qtrade_winagent.config import WinAgentConfig
    from qtrade_winagent.main_svc import build_real_deps
    d = build_real_deps(WinAgentConfig(), root=str(tmp_path), install_user_sid="S-1-5-21-x-1001", fake=True)
    assert d.cfg.api.port == 17610 and d.tokens.role_of(None) is None
    assert os.path.exists(os.path.join(str(tmp_path), "winagent.db"))
    d.db.close()


def test_installer_token_can_be_revoked():
    from qtrade_winagent.svc import ROLE_AGENT, ROLE_INSTALLER, Tokens
    t = Tokens(agent="a", console="c", installer="i")
    assert t.role_of("i") == ROLE_INSTALLER and t.role_of("a") == ROLE_AGENT
    t.revoke_installer()
    assert t.role_of("i") is None                                         # 03:安装完即吊销


def test_expand_falls_back_under_root_when_env_var_unexpandable(tmp_path):
    """``expand()`` 单函数的落点契约 —— **本用例按平台分叉是正当的**(2026-09-21 真机实测后分叉)。

    ``expand`` 是**生产**路径解析工具:02 §7.2 的 ``[vault] dir`` 默认值就写作
    ``%ProgramData%\\QTrade\\winagent\\vault``,指的正是真机上那个真实目录。所以两边结论本就不同:

    * **Windows**:``%ProgramData%`` 展得开 ⇒ 返回 ``C:\\ProgramData\\QTrade\\winagent\\vault``。
      这是**期望**的生产语义,**不该**被塞进 ``--root``。
    * **非 Windows**:变量根本不存在,``expandvars`` 原样返回带 ``%`` 的串;若直接拿去用会被当成
      **相对路径**在 cwd 里造出名字带 ``%`` 的目录(``--dev`` 冒烟时实测在仓库里拉了一坨),
      故兜到 ``--root`` 下。

    🔴 **这个分叉不可外推**:它只说明 ``expand`` 这一个函数两平台结论不同,**不**等于
    「``--dev`` 自包含」也能按平台分叉 —— 自包含是另一条契约,见下一个用例(那条**不分叉**)。
    """
    import os as _os
    from qtrade_winagent.main_svc import expand
    root = str(tmp_path)
    out = expand(r"%ProgramData%\QTrade\winagent\vault", root)

    # 两个平台共同的不变量
    assert "%" not in out                                                 # 不许留未展开的变量
    assert _os.path.isabs(out)                                            # 不许是相对路径
    assert out.endswith(_os.path.join("winagent", "vault"))
    assert expand("/var/lib/qtrade", root) == "/var/lib/qtrade"           # 已是绝对路径就别动

    if _os.name == "nt":
        assert out == _os.path.join(_os.environ["ProgramData"], "QTrade", "winagent", "vault")
        assert expand(r"%LOCALAPPDATA%", root) == _os.environ["LOCALAPPDATA"]
    else:
        assert out.startswith(root)
        assert expand(r"%LOCALAPPDATA%", root).startswith(root)

    assert not _os.path.exists("%ProgramData%")                           # cwd 里不许留垃圾


def test_dev_assembly_keeps_everything_under_root(tmp_path):
    """``--dev`` / 假后端装配**必须完全自包含在 ``--root`` 下** —— 🔴 **两平台同一条契约,不分叉**。

    依据(不是用例自己发明的口径):

    * ``winagent/README.md`` §3 对这条命令的原话是「**只起 HTTP + 假后端,不动任何真系统状态**」,
      给的示例正是 ``--dev --root /tmp/wa-dev`` —— 既然要显式给 ``--root``,落点就该全在它下面。
    * ``main_svc.expand()`` 自己的 docstring 也写着「``--dev`` 因此是自包含的」。

    ⚠️ **本用例在 Windows 上会红,红的是实现、不是用例**:``expand`` 在 Windows 上把 ``%ProgramData%``
    展开成真实目录,于是 vault / 内核落盘 / ``.wslconfig`` 备份三个**写入**落点一起逃出 ``--root``,
    落进真机**生产**目录 ``C:\\ProgramData\\QTrade\\``。
    实测后果(2026-09-21 首次在真 Windows 上跑 winagent 测试):跑一次 ``pytest`` 就在
    ``C:\\ProgramData\\QTrade\\winagent\\vault\\`` 里造出了 ``blobs\\`` 和一把 32 字节 ``entropy.bin`` ——
    那是 ``FakeCrypto`` 的产物,而 ``FakeCrypto.tighten_acl`` 是空操作所以 ACL 没收紧(实测 ``-rwxrwxrwx``),
    它却占在**生产熵文件**的位置上:日后真装机时 ``startup_selfcheck()`` 见文件在就不报
    ``VAULT_ENTROPY_MISSING``,真凭据会绑到这把假熵上(README §4.1 V1/V3 的后果)。
    ``--dev`` 的自包含从来没真正实现过,只是「Linux 上恰好展不开」带来的假象。

    ⚠️ 本用例**只做路径断言、绝不落盘**(刻意不调 ``ensure_entropy()``),
    好让它在 Windows 上红得干净、不会再往 ``C:\\ProgramData`` 写第二次。

    「落点」= WinAgent 会**写**的地方:``winagent.db`` / vault 目录 / 内核落盘目录 / ``.wslconfig`` 备份目录。
    ``monitor`` 的 ``disks`` 是**只读**的受检目标(还含硬编码的 ``D:\\``),按定义不在此列。
    """
    from qtrade_winagent.config import WinAgentConfig
    from qtrade_winagent.main_svc import build_real_deps
    root = str(tmp_path / "root")
    d = build_real_deps(WinAgentConfig(), root=root, install_user_sid="S-1-5-21-x-1001", fake=True)
    try:
        abs_root = os.path.abspath(root)
        # 私有属性:InstallerOps 没有公开这两个目录,而它们恰是最容易逃逸的两个写入落点
        written = {
            "winagent.db": d.db.path,
            "vault(entropy.bin/blobs)": d.vault.entropy_path,
            "installer.kernel_dir": d.installer._kernel_dir,
            "installer.wsl_backup_dir": d.installer._wsl_backup_dir,
        }
        for label, p in written.items():
            abs_p = os.path.abspath(p)
            assert "%" not in p, f"{label} 残留未展开的变量:{p}"
            assert abs_p == abs_root or abs_p.startswith(abs_root + os.sep), (
                f"--dev 的写入落点 {label} 逃出了 --root:{p}(root={root})"
            )
        assert not os.path.exists("%ProgramData%")                        # cwd 里不许留垃圾
    finally:
        d.db.close()


def test_dev_user_agent_keeps_backup_dir_under_root(tmp_path):
    """**会话代理侧**(``main_user``)也得自包含:``.wslconfig`` 备份目录是它这一侧唯一的写入落点。

    同一个病有两个入口 —— 服务侧(``build_real_deps``)修好了而这边漏掉的话,``--dev`` 起的会话代理
    照样会往生产的 ``%ProgramData%\\QTrade\\wsl`` 写 ``.wslconfig`` 备份。故两侧同一条口径:
    ``fake=True`` ⇒ 重基到 ``--root`` 下,不看平台、不看变量展不展得开。

    与上一个用例一样**只做路径断言、不落盘**(``WslCtl.__init__`` 只存路径,真正的
    ``os.makedirs`` 在备份时才发生)。
    """
    from qtrade_winagent.config import WinAgentConfig
    from qtrade_winagent.main_user import build_user_agent
    root = str(tmp_path / "root")
    ua = build_user_agent(WinAgentConfig(), session_id="Console", user_sid="S-1-5-21-x-1001",
                          root=root, fake=True)
    abs_root, p = os.path.abspath(root), os.path.abspath(ua.wslctl._backup_dir)
    assert "%" not in p, f".wslconfig 备份目录残留未展开的变量:{p}"
    assert p == abs_root or p.startswith(abs_root + os.sep), (
        f"--dev 会话代理的 .wslconfig 备份目录逃出了 --root:{p}(root={root})"
    )


@pytest.mark.parametrize("entry", ["main_svc", "main_user"])
def test_dev_refuses_to_start_without_explicit_root(entry):
    """``--dev`` **必须显式给 ``--root``**,两个入口都得堵。

    光有「落点重基到 ``root``」还不够:``DEFAULT_ROOT`` 在真 Windows 上展开就是**生产位置**
    ``C:\\ProgramData\\QTrade\\winagent``,而 ``winagent.db`` 是无条件 ``os.path.join(root, ...)``、
    **不经重基**的。所以只要 ``--dev`` 还能省略 ``--root``,冒烟一跑照样写进生产目录 ——
    自包含等于白做。README §3 给的写法本来就是 ``--dev --root /tmp/wa-dev``。

    ``argparse`` 的 ``error()`` ⇒ ``SystemExit(2)``,且在**参数校验处**就退,
    不会真去起服务 / 连管道 / 碰文件系统。
    """
    import importlib
    mod = importlib.import_module(f"qtrade_winagent.{entry}")
    with pytest.raises(SystemExit) as ei:
        mod.main(["--dev"])
    assert ei.value.code == 2, f"{entry}:--dev 缺 --root 时应被 argparse 拒绝(得到 code={ei.value.code})"
