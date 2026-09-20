# -*- coding: utf-8 -*-
"""安装器 ↔ 规格 的逐字对账。

规格唯一出处 = ``docs/03-安装引导与自动化配置.md``:

* §3.4「退出码」表      → ``engine/modules/QTrade.Exit.psm1`` 与 ``engine/qtrade-setup-engine.iss``
* §3.4「命令行」代码块  → ``engine/qtrade-setup-engine.iss`` 的 ``SW_QT_*`` 常量
* §2.3 状态机键名       → 两处的状态常量
* §2.6.7 W1            → 全部 ``.ps1``/``.psm1``/``.iss`` 必须 UTF-8 with BOM
* §6 / 验收 M1-14      → 引擎里零处自建防火墙规则
* §2.9.3 事实 1        → 任何地方都不得以 ``/S`` 运行微信 ``Uninstall.exe``

跑法::

    ~/.venvs/qtrade/bin/python -m pytest -q installer/tests
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

INSTALLER_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = INSTALLER_ROOT.parent
DOC_03 = REPO_ROOT / "docs" / "03-安装引导与自动化配置.md"
DOC_04 = REPO_ROOT / "docs" / "04-系统监控与本地网络.md"
ISS = INSTALLER_ROOT / "engine" / "qtrade-setup-engine.iss"
EXIT_PSM1 = INSTALLER_ROOT / "engine" / "modules" / "QTrade.Exit.psm1"
WSL_PSM1 = INSTALLER_ROOT / "engine" / "modules" / "QTrade.Wsl.psm1"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


# ── 从 docs/03 解析规格 ────────────────────────────────────────────────────
def _slice(text: str, start_marker: str, end_marker: str) -> str:
    i = text.index(start_marker)
    j = text.index(end_marker, i + len(start_marker))
    return text[i:j]


def parse_exit_codes(doc: str) -> dict[str, int]:
    """解析 §3.4「退出码」表 → {名: 码}。

    表里一行可能带多组 ``码 | `名` `` 或 ``码 `名` ``(如 ``| 26 | `E_INSTALL_DISK_LOW` / 27 `E_INSTALL_MEM_LOW` |``),
    故按「数字 + 可选竖线 + 反引号名」逐组抓;``| 0 | OK |`` 那行名字没有反引号,单独处理。
    """
    block = _slice(doc, "**退出码**(与 §8.2 状态一一对应", "\n---\n")
    codes: dict[str, int] = {}
    pair = re.compile(r"(\d+)\s*\|?\s*`(E_INSTALL_[A-Z0-9_]+)`")
    for line in block.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        if re.match(r"^\|\s*0\s*\|\s*OK\s*\|", line):
            codes["OK"] = 0
        for code, name in pair.findall(line):
            codes[name] = int(code)
    return codes


def parse_qt_switches(doc: str) -> set[str]:
    """解析 §3.4「命令行」代码块里的 ``/QT_*`` 开关名(不含 ``=``)。"""
    block = _slice(doc, "**命令行**(Inno 标准 + 本产品 `/QT_*`)", "**退出码**")
    fence = block.index("```")
    end = block.index("```", fence + 3)
    code = block[fence + 3 : end]
    # 🔴 代码块里有「**禁止**自造某个开关」的说明(R6-58 co 给 `--restore-data` 加的那条:
    #    「⚠️ 它是成对参数,**不是** /QT_* 开关 …… 不要自造 `/QT_RESTORE_DATA=`」)。
    #    照字面抓 /QT_* 会把这个**被禁止的名字**当成规格要求的开关,然后反过来逼引擎去声明它 ——
    #    正好做了规格明令不要做的事。所以每行在「不要自造」处截断。
    lines = []
    for line in code.splitlines():
        cut = line.find("不要自造")
        lines.append(line if cut < 0 else line[:cut])
    return set(re.findall(r"(/QT_[A-Z_]+)", "\n".join(lines)))


def parse_state_names(doc: str) -> set[str]:
    """§2.3 状态机图里的键名(基线 §8.2)。"""
    block = _slice(doc, "### 2.3 安装期状态机", "`install_state.json`")
    fence = block.index("```")
    end = block.index("```", fence + 3)
    diagram = block[fence + 3 : end]
    names = set(re.findall(r"\b([A-Z][A-Z_]{3,})\b", diagram))
    # 图里还有 FAILED / parked 这类非状态词
    return names - {"FAILED", "RunOnce"}


def parse_wslconfig_keys(doc04: str) -> set[str]:
    """docs/04 §2.7.1 表里逐字列出的、归属 03 写的键。"""
    block = _slice(doc04, "#### 2.7.1 `.wslconfig` 键的归属与默认值", "#### 2.7.2")
    keys: set[str] = set()
    for m in re.finditer(r"^\|\s*`\[(wsl2|experimental)\]\s+([A-Za-z]+)`", block, re.M):
        keys.add(m.group(2))
    # 「不写、不动」那一行是多个键并排:`[wsl2] networkingMode` / `dnsTunneling` / …
    return keys


# ── 从安装器工程解析实现 ───────────────────────────────────────────────────
def parse_iss_consts(iss: str, prefix: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"^\s*(%s[A-Za-z0-9_]*)\s*=\s*([^;]+);" % re.escape(prefix), iss, re.M):
        out[m.group(1)] = m.group(2).strip()
    return out


def parse_psm1_exit_table(psm1: str) -> dict[str, int]:
    block = _slice(psm1, "$script:QtExitTable = [ordered]@{", "\n}")
    out: dict[str, int] = {}
    # ⚠️ 名字里有数字(E_INSTALL_NOT_X64),字符类必须含 0-9
    for m in re.finditer(r"^\s*'([A-Z0-9_]+)'\s*=\s*(\d+)\s*$", block, re.M):
        out[m.group(1)] = int(m.group(2))
    return out


@pytest.fixture(scope="module")
def doc03() -> str:
    assert DOC_03.exists(), f"规格缺失:{DOC_03}"
    return read(DOC_03)


@pytest.fixture(scope="module")
def doc04() -> str:
    assert DOC_04.exists(), f"规格缺失:{DOC_04}"
    return read(DOC_04)


@pytest.fixture(scope="module")
def iss() -> str:
    assert ISS.exists(), f"引擎缺失:{ISS}"
    return read(ISS)


# ── 退出码 ────────────────────────────────────────────────────────────────
def test_spec_exit_table_parsed(doc03: str) -> None:
    """先证明解析器真的从文档里抓到了东西(否则后面的对账会假绿)。"""
    spec = parse_exit_codes(doc03)
    assert len(spec) >= 40, f"只解析出 {len(spec)} 条退出码,解析器或文档格式变了"
    assert spec["OK"] == 0
    assert spec["E_INSTALL_WAIT_USER"] == 10
    assert spec["E_INSTALL_REBOOT_REQUIRED"] == 3010
    assert spec["E_INSTALL_DISK_FULL"] == 123
    assert spec["E_INSTALL_INTERNAL"] == 200


def test_psm1_exit_table_matches_spec(doc03: str) -> None:
    spec = parse_exit_codes(doc03)
    impl = parse_psm1_exit_table(read(EXIT_PSM1))
    assert impl == spec, (
        "QTrade.Exit.psm1 的退出码表与 docs/03 §3.4 不一致。\n"
        f"文档有实现没有:{sorted(set(spec) - set(impl))}\n"
        f"实现有文档没有:{sorted(set(impl) - set(spec))}\n"
        f"码不同:{sorted((k, spec[k], impl[k]) for k in set(spec) & set(impl) if spec[k] != impl[k])}"
    )


def test_iss_exit_consts_match_spec(doc03: str, iss: str) -> None:
    spec = parse_exit_codes(doc03)
    consts = parse_iss_consts(iss, "E_INSTALL_")
    consts_int = {k: int(v) for k, v in consts.items()}
    ok = re.search(r"^\s*OK\s*=\s*(\d+);", iss, re.M)
    assert ok, ".iss 里缺 OK 常量"
    consts_int["OK"] = int(ok.group(1))
    assert consts_int == spec, (
        ".iss 的退出码常量与 docs/03 §3.4 不一致。\n"
        f"文档有 .iss 没有:{sorted(set(spec) - set(consts_int))}\n"
        f".iss 有文档没有:{sorted(set(consts_int) - set(spec))}\n"
        f"码不同:{sorted((k, spec[k], consts_int[k]) for k in set(spec) & set(consts_int) if spec[k] != consts_int[k])}"
    )


def test_exit_codes_are_unique(doc03: str) -> None:
    spec = parse_exit_codes(doc03)
    seen: dict[int, str] = {}
    dupes = []
    for name, code in spec.items():
        if code in seen:
            dupes.append((code, seen[code], name))
        seen[code] = name
    assert not dupes, f"文档里同一个码被两个名字占用:{dupes}"


def test_disk_low_and_disk_full_are_distinct(doc03: str) -> None:
    """§3.4 第 123 行明写「区别于 26 DISK_LOW」—— 两个量不得互相顶替。"""
    spec = parse_exit_codes(doc03)
    assert spec["E_INSTALL_DISK_LOW"] == 26
    assert spec["E_INSTALL_DISK_FULL"] == 123


# ── /QT_* 开关 ────────────────────────────────────────────────────────────
def test_spec_switches_parsed(doc03: str) -> None:
    sw = parse_qt_switches(doc03)
    assert len(sw) >= 12, f"只解析出 {len(sw)} 个 /QT_* 开关,解析器或文档格式变了"
    assert "/QT_MODE" in sw
    assert "/QT_ACCEPT_SHUTDOWN" in sw
    assert "/QT_KEEP_DATA" in sw


def test_iss_declares_every_spec_switch(doc03: str, iss: str) -> None:
    spec = parse_qt_switches(doc03)
    declared = {v.strip().strip("'").rstrip("=") for v in parse_iss_consts(iss, "SW_QT_").values()}
    missing = sorted(spec - declared)
    assert not missing, f".iss 没有声明 docs/03 §3.4 里的开关:{missing}"


def test_iss_switches_are_all_documented(doc03: str, iss: str) -> None:
    """反向:.iss 声明的每个开关都必须在 docs/03 的某处出现,不许自造。"""
    declared = {v.strip().strip("'").rstrip("=") for v in parse_iss_consts(iss, "SW_QT_").values()}
    undocumented = sorted(s for s in declared if s not in doc03)
    assert not undocumented, f".iss 自造了规格里没有的开关:{undocumented}"


def test_wechat_reinstall_downgrades_in_silent(iss: str) -> None:
    """P-19:`/QT_WECHAT=reinstall` 在静默模式下降级为 `check`(§3.4 / §2.9.3)。"""
    assert "OptWeChat := 'check'" in iss
    assert "IsSilentRun()" in iss


# ── 状态机键名 ────────────────────────────────────────────────────────────
def test_state_names_match_spec(doc03: str, iss: str) -> None:
    spec = parse_state_names(doc03)
    psm1 = read(EXIT_PSM1)
    block = _slice(psm1, "$script:QtStates = @(", "\n)")
    impl = set(re.findall(r"'([A-Z_]+)'", block))
    assert impl == spec, (
        "状态机键名与 docs/03 §2.3 图不一致。\n"
        f"文档有实现没有:{sorted(spec - impl)}\n实现有文档没有:{sorted(impl - spec)}"
    )
    iss_states = set(re.findall(r"^\s*ST_[A-Z_]+\s*=\s*'([A-Z_]+)';", iss, re.M))
    assert iss_states == spec, (
        ".iss 的 ST_* 常量与 docs/03 §2.3 图不一致。\n"
        f"文档有 .iss 没有:{sorted(spec - iss_states)}\n.iss 有文档没有:{sorted(iss_states - spec)}"
    )


def test_qt_modes_match_spec(doc03: str) -> None:
    """`/QT_MODE=` 的六个取值(§3.4)。"""
    # ⚠️ 只在 §3.4 的命令行代码块里找 —— 全文搜会先撞上 §2.8.1 的 `/QT_MODE=repair|verify-kernel`
    block = _slice(doc03, "**命令行**(Inno 标准 + 本产品 `/QT_*`)", "**退出码**")
    m = re.search(r"/QT_MODE=([a-z|\-]+)\s", block)
    assert m, "docs/03 §3.4 里找不到 /QT_MODE 的取值列表"
    spec = set(m.group(1).split("|"))
    psm1 = read(EXIT_PSM1)
    impl = set(re.findall(r"'([a-z\-]+)'", _slice(psm1, "$script:QtModes = @(", ")")))
    assert impl == spec, f"/QT_MODE 取值不一致:文档 {sorted(spec)} vs 实现 {sorted(impl)}"


# ── .wslconfig 键集 ───────────────────────────────────────────────────────
def test_wslconfig_managed_keys_subset_of_doc04(doc04: str) -> None:
    """docs/04 §2.7.1 是 `.wslconfig` 受管键表的唯一出处;实现里管的键必须都在那张表里。

    🔴 裁决②:行文里的「十一键」与逐字列出的键对不上(逐字 10 个),**以逐字列出的为准**。
    故这里既断言「实现 ⊆ 文档」,也把总数钉成 10 —— 将来文档补出第 11 个键时这条会红,提醒同步。
    """
    spec = parse_wslconfig_keys(doc04)
    assert len(spec) >= 8, f"只从 docs/04 §2.7.1 解析出 {len(spec)} 个键,解析器或文档格式变了"
    block = _slice(read(WSL_PSM1), "$script:QtWslManagedKeys = @(", "\n)")
    impl = set(re.findall(r"Name\s*=\s*'([A-Za-z]+)'", block))
    extra = sorted(impl - spec)
    assert not extra, f"实现管了 docs/04 §2.7.1 表里没有的键:{extra}"
    assert len(impl) == 10, f"受管键应为 10 个(裁决②),实得 {len(impl)}:{sorted(impl)}"


def test_wslconfig_untouched_keys_are_not_managed() -> None:
    """「不写、不动」的键绝不能出现在被管理的键集里(§2.6.2 R2)。"""
    text = read(WSL_PSM1)
    managed = set(
        re.findall(r"Name\s*=\s*'([A-Za-z]+)'", _slice(text, "$script:QtWslManagedKeys = @(", "\n)"))
    )
    untouched = set(
        re.findall(r"'([A-Za-z]+)'", _slice(text, "$script:QtWslUntouchedKeys = @(", ")\n"))
    )
    assert untouched, "解析不到「不写、不动」键集"
    assert not (managed & untouched), f"同一个键既被管又标「不动」:{sorted(managed & untouched)}"


# ── 编码与红线 ────────────────────────────────────────────────────────────
def _script_files(scope: str = "all") -> list[Path]:
    """scope='all' 全部;scope='runtime' 只要 engine/ 下随包发行的脚本
    (build/ 与 tests/ 里故意写着被禁模式的字样——它们**是**守门人与用例)。"""
    roots = [INSTALLER_ROOT] if scope == "all" else [INSTALLER_ROOT / "engine"]
    out: list[Path] = []
    for root in roots:
        for suffix in ("*.ps1", "*.psm1", "*.iss"):
            out.extend(
                p for p in root.rglob(suffix)
                if ".omc" not in p.parts and "out" not in p.parts
            )
    return sorted(out)


_BLOCK_COMMENT = re.compile(r"<#.*?#>", re.S)


def strip_comments(text: str) -> str:
    """去掉 PowerShell/Inno 的注释,只留可执行部分。

    红线类检查必须看**代码**而不是注释 —— 模块里写着「🔴 引擎从不 New-NetFirewallRule」
    这种说明文字,不该被当成违规。
    """
    text = _BLOCK_COMMENT.sub("", text)
    kept = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith(";") or stripped.startswith("//"):
            continue
        kept.append(line)
    return "\n".join(kept)


def test_all_scripts_are_utf8_with_bom() -> None:
    """🔴 W1:Windows PowerShell 5.1 把无 BOM 的 UTF-8 脚本按系统 ANSI(中文机=GBK)解析,
    中文注释里的字节会被当成引号 → ParserError,而且报错本身也是乱码。验收 M0-10。"""
    files = _script_files()
    assert files, "一个脚本都没找到,路径不对"
    bad = [str(p.relative_to(INSTALLER_ROOT)) for p in files if p.read_bytes()[:3] != b"\xef\xbb\xbf"]
    assert not bad, f"以下文件不是 UTF-8 with BOM:{bad}"


def test_engine_never_creates_firewall_rules() -> None:
    """🔴 §6 / 验收 M1-14:建规则唯一入口是 WinAgent `POST /wa/v1/firewall/ensure`。

    只看 engine/ 下随包发行的脚本、且只看代码(注释里写「从不 New-NetFirewallRule」不算违规)。
    `Remove-NetFirewallRule` 是允许的:§2.14 第 5 步的卸载兜底,且被 Remove-QtFirewallRuleByName
    限定只能删 `QTrade-*`。"""
    hits = []
    for p in _script_files("runtime"):
        code = strip_comments(read(p))
        for pat in (r"netsh(\.exe)?\s+advfirewall", r"\bNew-NetFirewallRule\b"):
            if re.search(pat, code):
                hits.append((str(p.relative_to(INSTALLER_ROOT)), pat))
    assert not hits, f"引擎里出现了自建防火墙规则的调用:{hits}"


def test_never_silent_uninstall_wechat() -> None:
    """🔴 §2.9.3 事实 1:微信 `Uninstall.exe /S` = 卸载并清空聊天记录与登录态。"""
    hits = []
    for p in _script_files("runtime"):
        for i, line in enumerate(strip_comments(read(p)).splitlines(), 1):
            if re.search(r"(?i)(Uninstall\w*\.exe|UninstallString)[^\n]*['\"]\s*/S\s*['\"]", line):
                hits.append(f"{p.relative_to(INSTALLER_ROOT)}:{i}: {line.strip()}")
    assert not hits, f"出现了以 /S 运行微信卸载器的调用:{hits}"


def test_wslconfig_is_written_only_without_bom() -> None:
    """🔴 W3:`.wslconfig` 带 BOM 会被 WSL 整段忽略 → 静默用默认内核。
    写 `.wslconfig` 只允许经 Write-QtUtf8NoBom(UTF8Encoding($false))。"""
    native = read(INSTALLER_ROOT / "engine" / "modules" / "QTrade.Native.psm1")
    assert "New-Object Text.UTF8Encoding($false)" in native
    for p in _script_files():
        text = read(p)
        # sfx-stub/ 是打包期工具:它把补丁写回 **C++ 源码**,那里必须带 BOM
        # (否则 cl.exe 按 GBK 解析补丁里的中文注释)。它从不碰 .wslconfig ——
        # 这一点由 test_sfx_stub_never_touches_wslconfig 另行兜住。
        if "UTF8Encoding($true)" in text and "Tests" not in p.name and "sfx-stub" not in p.parts:
            pytest.fail(f"{p.name} 里出现了带 BOM 的 UTF8Encoding($true)")


def test_sfx_stub_never_touches_wslconfig() -> None:
    """上面放行了 sfx-stub 的 BOM 写法,这里把放行的边界钉死:
    它只写 C++ 源码,绝不碰 .wslconfig / 内核 / WSL 任何东西。"""
    for name in ("QTrade.SfxStub.psm1", "fetch-sdk.ps1", "build.ps1"):
        text = read(INSTALLER_ROOT / "sfx-stub" / name)
        for forbidden in (".wslconfig", "wsl.exe", "wsl --", "kernel="):
            assert forbidden not in text, f"sfx-stub/{name} 不该出现 {forbidden}"


def test_binder_check_command_has_no_double_quote() -> None:
    """🔴 W7:传给 `--exec sh -c` 的命令串里有双引号会被 PowerShell 5.1 弄坏。"""
    kernel = read(INSTALLER_ROOT / "engine" / "modules" / "QTrade.Kernel.psm1")
    m = re.search(r"\$script:QtBinderCheckCommand\s*=\s*'([^']*)'", kernel)
    assert m, "找不到 binder 判据命令串"
    cmd = m.group(1)
    assert '"' not in cmd, "binder 判据命令串里不得有双引号(W7)"
    assert "/dev/binder" not in cmd, "不得用 `ls /dev/binder` 判(K5:6.x 开 binderfs 后不预建)"
    assert "BINDERFS_OK" in cmd


def test_kernel_line_is_always_6_6() -> None:
    """A-3:内核线恒 6.6,5.15 线已砍。"""
    kernel = read(INSTALLER_ROOT / "engine" / "modules" / "QTrade.Kernel.psm1")
    assert "$script:QtKernelLine = '6.6'" in kernel


def test_coredump_l2_is_pinned_to_D() -> None:
    """🔴 R6-38:manifest 的 `coredump_l2` 恒为 "D",其它值 CI 构建失败。"""
    payload = read(INSTALLER_ROOT / "engine" / "modules" / "QTrade.Payload.psm1")
    assert "$script:QtCoredumpL2Required = 'D'" in payload
    collect = read(INSTALLER_ROOT / "build" / "collect-payload.ps1")
    assert "$CoredumpL2 = 'D'" in collect


def test_bundled_wechat_sha256_is_pinned() -> None:
    """R2-6:两个候选微信包外层 VersionInfo 完全相同,只能靠 sha256 分辨。"""
    doc = read(DOC_03)
    m = re.search(r'"sha256":\s*"([0-9a-f]{64})".*weixin_4\.1\.12\.26', doc)
    if not m:
        m = re.search(r'weixin_4\.1\.12\.26\.exe",\s*"sha256":\s*"([0-9a-f]{64})"', doc)
    assert m, "docs/03 §2.2.1 里找不到随包微信的 sha256"
    collect = read(INSTALLER_ROOT / "build" / "collect-payload.ps1")
    assert m.group(1) in collect, "collect-payload.ps1 里钉的 sha256 与 docs/03 §2.2.1 不一致"


def test_disk_thresholds_match_spec(doc03: str) -> None:
    """E-18:硬门槛 16 GB / 建议 20 GB(§2.2.2、§7 [install])。"""
    assert "disk_hard_gb = 16" in doc03
    assert "disk_recommend_gb = 20" in doc03
    pre = read(INSTALLER_ROOT / "engine" / "modules" / "QTrade.Preflight.psm1")
    assert "$script:QtDiskHardGB = 16" in pre
    assert "$script:QtDiskRecommendGB = 20" in pre


def test_firewall_rule_names_match_doc04(doc04: str) -> None:
    """规则名的唯一出处 = docs/04 §2.6.3。"""
    assert "QTrade-WinAgent-17610-from-WSL" in doc04
    assert "QTrade-Agent-17600-LAN" in doc04
    fw = read(INSTALLER_ROOT / "engine" / "modules" / "QTrade.Firewall.psm1")
    assert "QTrade-WinAgent-17610-from-WSL" in fw
    assert "QTrade-Agent-17600-LAN" in fw


def test_hosts_marker_is_line_suffix_not_fence(doc04: str) -> None:
    """🔴 R6-15:hosts 是**逐行行尾标记**,不是 BEGIN/END 围栏块。"""
    assert "# QTrade-wechat-update-block" in doc04
    for p in _script_files():
        text = read(p)
        assert ">>> QTrade" not in text, f"{p.name} 里出现了围栏式标记(R6-15 已作废)"
        assert "<<< QTrade" not in text, f"{p.name} 里出现了围栏式标记(R6-15 已作废)"


def test_strip_comments_really_strips() -> None:
    """守门人自检:证明 strip_comments 没把代码一起吃掉,也确实吃掉了注释。
    否则上面两条红线检查会假绿。"""
    sample = "# New-NetFirewallRule in a comment\n<#\n New-NetFirewallRule in a block\n#>\nNew-NetFirewallRule -Name x\n"
    code = strip_comments(sample)
    assert code.count("New-NetFirewallRule") == 1
    assert "-Name x" in code


def test_mutex_is_held_by_the_engine_process(iss: str) -> None:
    """§2.12 并发保护:命名互斥体 `Global\\QTradeSetup`,第二个实例退出 29。

    🔴 必须由**长驻的引擎进程**持有。放进 run-step.ps1 那样的一次性子进程里,
    进程一跑完互斥体就释放,等于没有保护(验收 M1-21 会红)。
    """
    assert "Global\\QTradeSetup" in iss
    assert "CreateMutexW" in iss
    assert "ERROR_ALREADY_EXISTS" in iss
    assert "ExitProcess(E_INSTALL_ALREADY_RUNNING)" in iss
    run_step = read(INSTALLER_ROOT / "engine" / "run-step.ps1")
    assert "New-QtMutex" not in strip_comments(run_step), (
        "run-step.ps1 不该自己抢互斥体 —— 它是一次性子进程,抢了也白抢"
    )


def test_engine_passes_exit_code_through(iss: str) -> None:
    """§2.1:引擎退出码**原样透传**为 EXE 退出码。

    Inno 原生退出码只有 0~8,所以必须经 kernel32 的 ExitProcess 强制设置。
    """
    assert "external 'ExitProcess@kernel32.dll stdcall'" in iss
    assert "ExitProcess(LastStepExit)" in iss or "ExitProcess(Code)" in iss or "FailWith(LastStepExit" in iss


def test_shutdown_requires_explicit_confirmation() -> None:
    """🔴 红线 6:`wsl --shutdown` 只在用户明示确认后才发。

    这条钉在代码里(Invoke-QtWslShutdown 的 -Confirmed 是 Mandatory 且未确认即抛),
    而不是只写在注释里。
    """
    kernel = read(INSTALLER_ROOT / "engine" / "modules" / "QTrade.Kernel.psm1")
    body = _slice(kernel, "function Invoke-QtWslShutdown", "function Invoke-QtKernelVerify")
    assert "[Parameter(Mandatory)][bool] $Confirmed" in body
    assert "if (-not $Confirmed) {" in body
    assert "throw" in body
    # 整个模块里除了这一个函数,没有别处直接发 --shutdown
    calls = re.findall(r"Invoke-QtWsl\s+-WslArgs\s+@\('--shutdown'", strip_comments(kernel))
    assert len(calls) == 1, f"--shutdown 出现在 {len(calls)} 处,应当只经 Invoke-QtWslShutdown 一个口子"


# ── 第二批:派发器与 .iss 要接齐 §2.3 的全部状态 ───────────────────────────
RUN_STEP = INSTALLER_ROOT / "engine" / "run-step.ps1"


def _dispatcher_branches() -> set[str]:
    """run-step.ps1 的 `switch ($Step)` 里有哪些分支。"""
    text = read(RUN_STEP)
    body = text[text.index("switch ($Step) {"):]
    return set(re.findall(r"^\s*'([A-Za-z_\-]+)' \{", body, re.M))


def test_dispatcher_covers_every_state(doc03: str) -> None:
    """§2.3 链上的每个状态都要有一个派发器分支(否则装到那一步就没人干活)。

    例外:`WSLCONFIG_WRITTEN` 与 `KERNEL_VERIFIED` 在 §2.6.3 是**一个用户确认的原子段**,
    派发器里合成一个 `KERNEL_SWITCH` 动作 —— 拆开执行会留下「写了配置却没 shutdown」
    这个 §2.6.3 点名说最危险的中间态。
    """
    branches = _dispatcher_branches()
    atomic = {"WSLCONFIG_WRITTEN", "KERNEL_VERIFIED"}
    need = set(parse_state_names(doc03)) - {"REBOOT_PENDING", "KERNEL_ROLLED_BACK"} - atomic
    missing = sorted(need - branches)
    assert not missing, f"派发器缺这些状态的分支:{missing}"
    assert "KERNEL_SWITCH" in branches, "内核原子段必须有 KERNEL_SWITCH 分支(§2.6.3)"


def test_dispatcher_covers_every_mode(doc03: str) -> None:
    """`/QT_MODE=` 的每个取值都要有落点。

    `install` / `resume` 不是独立分支:它们由 bootstrap 算出续跑点,再逐个跑状态分支。
    """
    branches = _dispatcher_branches()
    for mode in ("uninstall", "upgrade", "repair", "verify-kernel"):
        assert mode in branches, f"派发器没有 /QT_MODE={mode} 的落点"
    assert "bootstrap" in branches
    assert "diag" in branches, "§5.3 的诊断包导出没有落点"


def test_iss_drives_every_state() -> None:
    """.iss 的 CurStepChanged 要真的把每一步跑起来 —— 只定义常量不调用等于没接。"""
    iss_text = read(ISS)
    for step in ("ST_PRECHECK", "ST_PAYLOAD_STAGED", "ST_WSL_FEATURE", "ST_WSL_MSI",
                 "ST_KERNEL_STAGED", "ST_DISTRO_IMPORTED", "ST_IMAGES_LOADED",
                 "ST_WINAGENT_INSTALLED", "ST_CLIENTS_CHECKED", "ST_SELFTEST_OK", "ST_DONE"):
        assert f"RunStep({step})" in iss_text, f".iss 没有调 RunStep({step})"
    assert "RunStep('KERNEL_SWITCH')" in iss_text
    assert "RunStep('upgrade')" in iss_text
    assert "RunStep('repair')" in iss_text
    assert "RunStep('uninstall')" in iss_text
    assert "RunStep('diag')" in iss_text


def test_iss_has_interactive_branches() -> None:
    """§4 / 红线 6 / 红线 8:三处必须是**用户点过**才动的交互分支。"""
    iss_text = read(ISS)
    # 内核替换确认([NOSHUTDOWN] / 红线 6)
    assert "我已知晓内核替换的影响" in iss_text
    assert "OptAcceptShutdown := True" in iss_text
    # 同名发行版冲突(§2.7.1;**不提供改名绕过**)
    assert "WAIT_DISTRO_CONFLICT" in iss_text
    assert "改名绕过" in iss_text
    # 微信重装引导(红线 8)
    assert "WeChatUserApproved := True" in iss_text
    # 升级期停账号容器也要确认(§2.13 第 1 步)
    assert "WAIT_SHUTDOWN_CONFIRM" in iss_text


# ── 裁决落地 ──────────────────────────────────────────────────────────────
def test_ruling_1_sfx_disk_maps_to_26() -> None:
    """裁决①:解压前判 6 GB 认偏差;precheck 在引擎前判、解压目录空间不足映射到 26。"""
    cmd = read(INSTALLER_ROOT / "engine" / "precheck-disk.cmd")
    assert "exit /b 26" in cmd
    assert "6442450944" in cmd, "6 GB 门槛要写成明确的字节数"
    assert "裁决" in cmd, "偏差要在脚本里写明,而不是只在交接里"
    # 🔴 **可执行行(非 rem 行)必须纯 ASCII** —— 它由 SFX 在未知代码页下拉起,
    #    给用户看的字符串带中文就是乱码;rem 注释行不参与执行,允许中文。
    bad = [
        f"{i}: {line}"
        for i, line in enumerate(cmd.splitlines(), 1)
        if line.strip() and not line.lstrip().lower().startswith("rem") and not line.isascii()
    ]
    assert not bad, f"precheck-disk.cmd 的可执行行里有非 ASCII 字符:{bad}"
    assert (INSTALLER_ROOT / "engine" / "precheck-disk.cmd").read_bytes()[:3] != b"\xef\xbb\xbf", \
        ".cmd 不能带 BOM —— 会让第一行命令解析失败"


def test_ruling_2_managed_keys_called_ten() -> None:
    """裁决②:代码里的常量与措辞统一称「受管键表(10 键)」,不再说十一键。"""
    wsl = read(WSL_PSM1)
    assert "受管键表" in wsl
    assert "十一键" not in wsl or "行文里的「十一键」" in wsl, "只允许在解释裁决时提到「十一键」"


def test_ruling_3_uninstall_order_documented() -> None:
    """裁决③:卸载按正确执行顺序,并在注释里点名与 §2.14 编号的差异。"""
    un = read(INSTALLER_ROOT / "engine" / "modules" / "QTrade.Uninstall.psm1")
    assert "执行顺序 ≠ 文档编号顺序" in un
    assert "1 → 2 → 3 → 5(防火墙)→ 6(hosts)→ 4(服务/计划任务)→ 7" in un


# ── 诊断包的排除清单是红线 2 的执行点 ──────────────────────────────────────
def test_diag_exclusions_cover_redline_2() -> None:
    diag = read(INSTALLER_ROOT / "engine" / "modules" / "QTrade.Diag.psm1")
    block = _slice(diag, "$script:QtDiagExcludePatterns = @(", ")\n")
    # 用子串比对而不是正则 —— 清单里存的**本来就是正则**,拿正则去搜正则会自己把自己转义掉
    for must in ("vault", "entropy", "xwechat", "winagent", "QTrade-WeChat-Backup"):
        assert must in block, f"诊断包排除清单里缺:{must}"


# ── 两条把我反复咬到的 PowerShell 坑,做成守门 ─────────────────────────────
def test_no_nested_script_scoped_functions() -> None:
    """不许在函数体里 `function Script:Foo` —— 每次调用都重新定义、作用域污染,
    而且 Pester 无法 Mock。私有助手一律放模块顶层。"""
    hits = []
    for p in _script_files("runtime"):
        for i, line in enumerate(read(p).splitlines(), 1):
            if re.search(r"^\s+function\s+Script:", line):
                hits.append(f"{p.name}:{i}")
    assert not hits, f"出现了函数内的 Script: 作用域函数:{hits}"


def test_no_strictmode_property_landmine() -> None:
    """不许用「取 .PSObject.Properties.Name 再 -contains」判属性 ——
    对象没有任何属性时 `.Properties` 是空集合,取 `.Name` 在 StrictMode 下直接抛。
    轻量包的空 manifest 正好会走到那条路。一律用 Test-QtHasProperty。"""
    hits = []
    for p in _script_files("runtime"):
        for i, line in enumerate(strip_comments(read(p)).splitlines(), 1):
            if ".PSObject.Properties.Name" in line:
                hits.append(f"{p.name}:{i}: {line.strip()}")
    assert not hits, f"出现了 StrictMode 地雷写法:{hits}"


def test_build_has_selfcheck_mode() -> None:
    """`-SelfCheck` = 四道门 + 收载荷生成 manifest,不需要 ISCC / 7z / SFX 存根。"""
    b = read(INSTALLER_ROOT / "build" / "build.ps1")
    assert "[switch] $SelfCheck" in b
    assert "if ($SelfCheck) {" in b


def test_build_verifies_archive_against_manifest() -> None:
    """归档后必须复核「manifest 里每个非通配条目都能在归档里按原路径找到」——
    7z 收到绝对路径时会把文件放进归档根目录,这种错位是**静默**的。"""
    b = read(INSTALLER_ROOT / "build" / "build.ps1")
    assert "归档 ↔ manifest 路径复核通过" in b
    assert "Push-Location $StageDir" in b


# ── 真编译踩出来的四条,做成守门 ───────────────────────────────────────────
def test_iss_rejects_arm64_via_x64os(iss: str) -> None:
    """🔴 §2.4.1:ARM64 → `E_INSTALL_NOT_X64`(redroid 的 x86_64 镜像 + ndk_translation
    **只在 x86_64 上成立**)。

    所以架构标识必须是 `x64os`(真正的 x64 操作系统),**不能**用 ISCC 建议的 `x64compatible`
    —— 后者把「能跑 x64 模拟的 ARM64」也放了进来,正是我们必须拒绝的那一类。
    """
    assert "ArchitecturesAllowed=x64os" in iss
    assert "ArchitecturesInstallIn64BitMode=x64os" in iss
    assert "x64compatible" not in re.sub(r"^\s*;.*$", "", iss, flags=re.M), \
        "x64compatible 只允许出现在解释「为什么不用它」的注释里"


def test_iss_uninstallrun_has_runonceid(iss: str) -> None:
    """[UninstallRun] 不带 RunOnceId 会被 ISCC 警告;卸载引擎只该在卸载过程中跑一次。"""
    block = _slice(iss, "[UninstallRun]", "[Code]")
    assert "RunOnceId:" in block


def test_iss_uses_existing_inno_api(iss: str) -> None:
    """真编译抓到过 `SaveStringToUTF8File`(单数)不存在。

    Unicode Inno 下 `SaveStringToFile` 按 ANSI 写,选项 JSON 里只要有中文路径就会写坏,
    所以必须用 `SaveStringsToUTF8File`(复数,收 TArrayOfString)。
    """
    assert "SaveStringsToUTF8File" in iss
    assert "SaveStringToUTF8File(" not in iss, "单数形式在 Inno 6 里不存在,会编译失败"


def test_chinese_language_file_ships_with_repo(iss: str) -> None:
    r"""🔴 界面文案仅中文,而 Inno 6 的发行版**不一定自带** ChineseSimplified.isl
    (本机 6.x 的 Languages\ 里就没有)。所以它随仓库走,.iss 用相对路径引用。"""
    isl = INSTALLER_ROOT / "engine" / "lang" / "ChineseSimplified.isl"
    assert isl.exists(), "缺 installer/engine/lang/ChineseSimplified.isl"
    text = isl.read_text(encoding="utf-8")
    assert "LanguageName=简体中文" in text
    assert "MessagesFile: \"lang\\ChineseSimplified.isl\"" in iss, ".iss 必须用相对路径引用随仓库的语言文件"
    assert "compiler:Default.isl" not in iss, "别再退回 Default.isl —— 那样界面大半是英文"
    # 官方 .isl 本身不带 BOM,和 Inno 自带的那些一致;别给它加 BOM
    assert isl.read_bytes()[:3] != b"\xef\xbb\xbf"


def test_packaging_intermediate_not_shipped() -> None:
    """`.packed-entries.txt` 只是告诉 build.ps1 哪些进第二块的中间件,
    既不在 manifest 里、装机现场也没人用 —— 不许进包。"""
    b = read(INSTALLER_ROOT / "build" / "build.ps1")
    assert "-x!install\\.packed-entries.txt" in b


# ── 第三批:补齐的三项 ────────────────────────────────────────────────────
def test_upgrade_wires_image_load_step() -> None:
    """§2.13 第 6 步:新镜像 tar 变了 → `docker load`。
    `Get-QtUpgradePlan` 判了 `Images` 还不够,upgrade 分支必须真的去 load。"""
    rs = read(RUN_STEP)
    body = _slice(rs, "'upgrade' {", "'repair' {")
    assert "Invoke-QtDockerLoadImages" in body, "upgrade 分支没接上镜像加载"
    assert "$plan.Images" in body


def test_old_images_are_never_removed() -> None:
    """🔴 §2.13 第 6 步:**旧镜像保留** —— 账号容器按 `account_runtime` 记的镜像 tag 起,
    逐账号升级由 Agent 做、不在安装器里。把旧 tag 删了,正在跑的账号下次起不来。"""
    hits = []
    for p in _script_files("runtime"):
        code = strip_comments(read(p))
        for pat in (r"\bdocker\s+rmi\b", r"\bimage\s+prune\b", r"\bdocker\s+image\s+rm\b"):
            if re.search(pat, code):
                hits.append((p.name, pat))
    assert not hits, f"出现了删镜像的调用:{hits}"


def test_dispatcher_has_readonly_wechat_detect() -> None:
    """MULTIPLE_INSTALLS 的选择页要在**向导阶段**就知道有哪几处 —— 靠一个**只读**的检测动作。
    它绝不能改任何东西(不写 state、不动微信)。"""
    branches = _dispatcher_branches()
    assert "wechat-detect" in branches
    rs = read(RUN_STEP)
    body = _slice(rs, "'wechat-detect' {", "\n        }")
    assert "Set-QtState" not in body, "只读动作不该迁状态"
    assert "Start-QtWeChatSetup" not in body and "Start-QtWeChatInteractiveUninstall" not in body, \
        "只读动作不该碰微信安装器/卸载器"
    # 回值用扁平键,免得 Pascal 侧要写 JSON 数组解析
    assert "install_{0}_label" in body and "install_{0}_path" in body


def test_iss_has_multiple_installs_selection_page(iss: str) -> None:
    """§2.9.3 末:MULTIPLE_INSTALLS 时列出各处路径/版本/是否正在运行,用户选**一处**;
    其余**不卸、不改**。只有真检测到 ≥2 处时才显示。"""
    assert "PageWeChatSelect" in iss
    assert "RunStep('wechat-detect')" in iss
    assert "WeChatSelectedPath" in iss
    assert "其余各处我们一律不动" in iss
    # 只在 ≥2 处时显示
    assert "WeChatMultiple := Count >= 2" in iss
    assert "Result := not WeChatMultiple" in iss


def test_iss_handles_interactive_uninstall_branch(iss: str) -> None:
    """§2.9.3 方案②:向导要引导用户确认「保留本地数据」勾选框,
    并在轮询超时时给【我已卸载,继续】/【跳过微信通道】——不替用户决定。"""
    assert "WAIT_WECHAT_UNINSTALL" in iss
    assert "保留本地数据" in iss
    assert "我已经卸载完了" in iss
    assert "WeChatUninstallConfirmed := True" in iss
    # 🔴 卸载器不带任何静默参数的提醒必须在文案里
    assert "不带任何静默参数" in iss


# ── 第四批:SFX 存根的真实能力(实测坐实,见 build/README §8)────────────────
OFFICIAL_SFX_KEYS = {"Title", "BeginPrompt", "Progress", "Directory",
                     "RunProgram", "ExecuteFile", "ExecuteParameters"}


def _sfx_config_keys() -> set[str]:
    cfg = read(INSTALLER_ROOT / "build" / "sfx-config.txt")
    out = set()
    for line in cfg.splitlines():
        s = line.strip()
        if not s or s.startswith(";"):
            continue
        m = re.match(r"^([A-Za-z_]+)\s*=", s)
        if m:
            out.add(m.group(1))
    return out


def test_sfx_config_uses_only_official_keys() -> None:
    """🔴 官方 SfxSetup(LZMA SDK)**只认 7 个键**,对别的键**静默忽略**。

    `InstallPath` / `GUIMode` / `OverwriteMode` / `ExtractTitle` … 是第三方 7zsfxmm 的键:
    写了不报错、也不生效,结果是载荷被解到 `%TEMP%` 然后跑完即删 —— 装到现场才暴露。
    证据:SfxSetup.cpp 只对这 7 个名字调 GetTextConfigValue/FindTextConfigItem;
    对 7zSD.sfx 做 strings 也只有这 7 个;哑 EXE 实测 InstallPath 完全无效。
    """
    keys = _sfx_config_keys()
    assert keys, "sfx-config.txt 一个键都没解析到"
    extra = sorted(keys - OFFICIAL_SFX_KEYS)
    assert not extra, f"sfx-config.txt 用了官方存根不认识的键(那是 7zsfxmm 的):{extra}"
    assert "RunProgram" in keys, "缺 RunProgram,外壳不知道该拉起谁"


def test_precheck_stages_payload_to_programdata() -> None:
    r"""官方存根只解到 %TEMP% 且跑完即删,所以 §2.1 要的「解压到 %ProgramData%\QTrade 并留存」
    必须由 precheck-disk.cmd 搬运完成。"""
    cmd = read(INSTALLER_ROOT / "engine" / "precheck-disk.cmd")
    assert 'set "QT_DST=%ProgramData%\\QTrade"' in cmd
    assert "robocopy" in cmd and "/MOVE" in cmd, "要能搬(同卷 move / 已存在则 robocopy /MOVE 合并)"
    assert "last-exit-code.txt" in cmd, "存根会丢掉退出码,必须落盘供自动化读"


def test_precheck_does_not_move_its_own_directory() -> None:
    r"""🔴 自举陷阱:脚本住在 <src>\install\engine\。
    把 `install` MOVE 走之后,cmd.exe 就读不到脚本文件了,后面每个 CALL 都死在
    「The system cannot find the batch label specified」—— 而且是**半搬运**状态。
    所以 `install` 必须用**复制**,其余顶层项才 MOVE。"""
    cmd = read(INSTALLER_ROOT / "engine" / "precheck-disk.cmd")
    assert 'if /I not "%%~nxD"=="install"' in cmd, "install 目录必须被排除在 MOVE 之外"
    assert re.search(r'robocopy "%QT_SRC%\\install" "%QT_DST%\\install" /E(?! /MOVE)', cmd), \
        "install 要用 /E 复制,不能带 /MOVE"


def test_precheck_has_no_install_target_backdoor() -> None:
    """生产脚本里**不留**「改安装目标」的开关:既是攻击面,也迟早被误用。
    打包期自验改用「生成副本、替换目标行」的办法(build/README §8)。"""
    # 只看**可执行行** —— rem 注释里解释「为什么不要这么做」是应该的,不算后门
    code = "\n".join(
        line for line in read(INSTALLER_ROOT / "engine" / "precheck-disk.cmd").splitlines()
        if line.strip() and not line.lstrip().lower().startswith("rem")
    )
    assert "QT_INSTALL_ROOT" not in code, "别再用环境变量覆盖安装目标(它会在进程链上丢掉)"
    assert "--qt-stage-to" not in code, "别在 batch 里解析参数(`=` 是 for 的默认分隔符,解析会坏)"
    assert ":parse_arg" not in code


def test_cmd_files_are_crlf_ascii_no_bom() -> None:
    """🔴 LF-only 的 .cmd 会让 cmd.exe 的 for/if/call/标签解析错乱,
    症状是「cannot find the batch label」「) was unexpected at this time」,只有真跑才暴露。"""
    for p in INSTALLER_ROOT.rglob("*.cmd"):
        if ".omc" in p.parts or "out" in p.parts:
            continue
        b = p.read_bytes()
        assert b[:3] != b"\xef\xbb\xbf", f"{p.name} 带 BOM"
        assert b.count(b"\n") == b.count(b"\r\n"), f"{p.name} 有 LF-only 换行"
        for i, line in enumerate(b.decode("utf-8").splitlines(), 1):
            if not line.strip() or line.lstrip().lower().startswith("rem"):
                continue
            assert line.isascii(), f"{p.name}:{i} 可执行行含非 ASCII:{line.strip()}"


# ── 自编 SFX 存根(第五批)────────────────────────────────────────────────
SFX_STUB_DIR = INSTALLER_ROOT / "sfx-stub"
SFX_PATCH = SFX_STUB_DIR / "qtrade-sfx.patch"
SFX_STUB_PSM1 = SFX_STUB_DIR / "QTrade.SfxStub.psm1"

# 自编存根 = 官方 7 键 + InstallPath。多一个键都不行:多出来的仍然会被静默忽略。
QTRADE_SFX_KEYS = OFFICIAL_SFX_KEYS | {"InstallPath"}


def _read_bytes_text(path: Path) -> str:
    """补丁文件里内容行带着源码的 CR(对 diff 来说 CR 是行内容),按原样读。"""
    return path.read_bytes().decode("utf-8")


def _config_keys(name: str) -> set[str]:
    cfg = read(INSTALLER_ROOT / "build" / name)
    out = set()
    for line in cfg.splitlines():
        s = line.strip()
        if not s or s.startswith(";"):
            continue
        m = re.match(r"^([A-Za-z_]+)\s*=", s)
        if m:
            out.add(m.group(1))
    return out


def test_qtrade_sfx_config_adds_exactly_installpath() -> None:
    """自编存根只比官方多认一个键。这条把「补丁改了什么」和「配置敢写什么」焊在一起:
    以后谁想再往配置里加 GUIMode/OverwriteMode 之类,这里就会红。"""
    keys = _config_keys("sfx-config-qtrade.txt")
    assert keys, "sfx-config-qtrade.txt 一个键都没解析到"
    extra = sorted(keys - QTRADE_SFX_KEYS)
    assert not extra, f"自编存根也不认这些键(它们是 7zsfxmm 的):{extra}"
    assert "InstallPath" in keys, "自编存根的全部价值就在 InstallPath 上,缺了等于白编"


def test_official_config_still_has_no_installpath() -> None:
    """🔴 两份配置绝不能混用:官方存根遇到 InstallPath 是**静默忽略**,
    装到现场才暴露(载荷解到 %TEMP%、跑完即删)。"""
    assert "InstallPath" not in _config_keys("sfx-config.txt")


def test_each_stub_points_at_its_own_chain_head() -> None:
    r"""存根 / 配置 / 链首脚本三者必须配套:
      * 官方存根不搬运 -> 链首必须是 precheck-disk.cmd(它来搬);
      * 自编存根已经解到 %ProgramData%\QTrade 并留存 -> 链首是 run-engine.cmd(只拉引擎)。
    """
    official = read(INSTALLER_ROOT / "build" / "sfx-config.txt")
    qtrade = read(INSTALLER_ROOT / "build" / "sfx-config-qtrade.txt")
    assert 'RunProgram="install\\\\engine\\\\precheck-disk.cmd"' in official
    assert 'RunProgram="install\\\\engine\\\\run-engine.cmd"' in qtrade


def test_run_engine_cmd_does_not_stage_anything() -> None:
    """自编存根路径下链首**不搬运** —— 搬运是官方存根路径的补偿动作。
    如果这里也搬,等于把已经解对位置的载荷再挪一遍,纯属自找麻烦。"""
    code = "\n".join(
        line for line in read(INSTALLER_ROOT / "engine" / "run-engine.cmd").splitlines()
        if line.strip() and not line.lstrip().lower().startswith("rem")
    )
    assert "robocopy" not in code, "run-engine.cmd 不该搬运"
    assert "move /Y" not in code, "run-engine.cmd 不该搬运"
    assert "last-exit-code.txt" in code, "退出码仍要落盘存证"
    assert "qtrade-setup-engine.exe" in code, "链首总得把引擎拉起来"


def test_patch_adds_installpath_key() -> None:
    """补丁改动 (a):存根必须真的去读 InstallPath 这个配置键。
    (配置里写了、存根不读 —— 这正是官方存根的坑,别在自己身上重演。)"""
    patch = _read_bytes_text(SFX_PATCH)
    added = "\n".join(l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
    assert 'GetTextConfigValue(pairs, "InstallPath")' in added, \
        "补丁没有新增读取 InstallPath 的代码"
    assert "QTrade_ExpandInstallPath" in added, "InstallPath 要支持 %ProgramData% 这类环境变量展开"
    assert "ExpandEnvironmentStringsW" in added


def test_patch_propagates_child_exit_code() -> None:
    """🔴 补丁改动 (b):官方存根末尾硬编码 `return 0`,子进程退 26 它照样退 0。
    §8b 的验收判据几乎全是退出码 —— 不透传等于「全 0 = 全部看起来成功」。"""
    patch = _read_bytes_text(SFX_PATCH)
    added = "\n".join(l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = "\n".join(l[1:] for l in patch.splitlines() if l.startswith("-") and not l.startswith("---"))
    assert "GetExitCodeProcess" in added, "补丁没有取子进程退出码"
    assert "return (int)exitCode;" in added, "取了却没拿它当自己的退出码"
    assert re.search(r"^\s*return 0;\s*$", removed, re.M), "原来那句硬编码 return 0 没被删掉"


def test_patch_disk_low_matches_spec_exit_code(doc03: str) -> None:
    """存根里那个「解压前空间不足」的退出码,必须逐字等于 §3.4 的 E_INSTALL_DISK_LOW。"""
    codes = parse_exit_codes(doc03)
    patch = _read_bytes_text(SFX_PATCH)
    m = re.search(r"kQTradeExitDiskLow\s*=\s*(\d+)", patch)
    assert m, "补丁里找不到 kQTradeExitDiskLow"
    assert int(m.group(1)) == codes["E_INSTALL_DISK_LOW"], \
        f"存根用了 {m.group(1)},规格是 {codes['E_INSTALL_DISK_LOW']}"


def test_patch_space_threshold_is_6gib_and_10_percent() -> None:
    """门槛 = max(6 GiB, 解包总大小 x 1.1)。6 GiB 来自 §2.1 的硬下限,
    x1.1 是解压期余量。两个数都必须能在代码里看见。"""
    patch = _read_bytes_text(SFX_PATCH)
    assert "((UInt64)6) << 30" in patch, "6 GiB 下限不见了"
    assert "unpackSize + unpackSize / 10" in patch, "x1.1 余量不见了"
    assert "QTrade_GetRequiredBytes" in patch


def test_patch_checks_space_before_creating_target_dir() -> None:
    """🔴 判空间必须在 CreateComplexDir 之前 —— 空间不足时连目标目录都不该建出来,
    否则留下一个空壳目录,下次跑 precheck 还以为装过。"""
    patch = _read_bytes_text(SFX_PATCH)
    # `CreateComplexDir(dirPath)` 落在 hunk 的三行上下文窗口之外,抓不到;
    # 用紧挨着它的 `FString dirPath = DestFolder;` 当锚 —— 目录就是从这行往下建的。
    i_check = patch.index("QTrade_GetFreeSpace(DestFolder")
    i_dir = patch.index("FString dirPath = DestFolder;")
    assert i_check < i_dir, "空间判据跑到建目录后面去了"
    # 且判据那一支是直接 return,不往下走
    block = patch[i_check:i_dir]
    assert "NoSpace = true;" in block and "return;" in block


def test_patch_touches_only_the_sfxsetup_bundle() -> None:
    """补丁只改 SFXSetup 这一个目录的三个文件 —— 绝不碰 SDK 的公共代码,
    否则下次升 SDK 版本就是一场灾难。"""
    patch = _read_bytes_text(SFX_PATCH)
    files = sorted(set(re.findall(r"^\+\+\+ b/(.+?)\s*$", patch, re.M)))
    assert files == [
        "CPP/7zip/Bundles/SFXSetup/ExtractEngine.cpp",
        "CPP/7zip/Bundles/SFXSetup/ExtractEngine.h",
        "CPP/7zip/Bundles/SFXSetup/SfxSetup.cpp",
    ], f"补丁碰了计划外的文件:{files}"


def test_sdk_source_is_pinned_by_sha256() -> None:
    """第三方源码只按 sha256 取。摘要写死在模块里,fetch 脚本从模块读 —— 只有一处真值。"""
    psm1 = read(SFX_STUB_PSM1)
    assert "317DD834D6BBFD95433488B832E823CD3D4D420101436422C03AF88507DD1370" in psm1
    fetch = read(SFX_STUB_DIR / "fetch-sdk.ps1")
    assert "Assert-QtSha256" in fetch, "fetch 脚本必须先校验再解压"
    assert re.search(r"Assert-QtSha256[\s\S]{0,400}?7z", fetch) or \
           fetch.index("Assert-QtSha256") < fetch.index("$args7z"), "校验必须发生在解压之前"


def test_stub_build_never_self_installs_toolchain() -> None:
    """🔴 禁区:缺工具链就报缺,不代装。"""
    build = read(SFX_STUB_DIR / "build.ps1")
    psm1 = read(SFX_STUB_PSM1)
    code = strip_comments(build) + "\n" + strip_comments(psm1)
    # 🔴 这里要区分「**打印**一条给人看的安装指引」和「**执行**安装」。
    #    前者正是我们要的(缺件时告诉人怎么补),后者才是禁区。
    #    所以只看那些不是在往输出里塞字符串的行。
    emits = re.compile(r"AppendLine|Write-Host|Write-Output|Write-Warning|throw|-HowTo")
    for line in code.splitlines():
        if emits.search(line):
            continue
        for forbidden in ("choco install", "winget install", "vs_buildtools.exe", "Start-BitsTransfer"):
            assert forbidden not in line, f"构建脚本里出现了自行安装动作:{line.strip()}"
    assert "不会自行安装" in psm1, "缺件说明里要写明不自行安装"


def test_build_supports_both_stubs() -> None:
    """两条出包路径都要在 build.ps1 里保留,且各有各的 G5 白名单。"""
    b = read(INSTALLER_ROOT / "build" / "build.ps1")
    assert "$StubKind" in b
    assert "'qtrade'" in b and "'official'" in b
    assert "sfx-config-qtrade.txt" in b and "sfx-config.txt" in b
    assert "run-engine.cmd" in b and "precheck-disk.cmd" in b
    assert "Test-QtSfxStubBinary" in b, \
        "缝自编存根前必须验二进制 —— 防「把官方存根改名成 QTradeSD.sfx」"


def test_patch_space_check_only_applies_to_installpath_path() -> None:
    """🔴 「无 InstallPath 时保持官方原行为」是安琳给的硬要求。
    空间判据如果无条件生效,一个 200 MB 的普通 SFX 包在只剩 5 GB 的机器上
    会被凭空拦下 —— 那就不是「逐字一致」了。判据必须由调用方按需开启。"""
    patch = _read_bytes_text(SFX_PATCH)
    assert "bool checkSpace" in patch, "ExtractArchive 缺 checkSpace 形参"
    assert "if (CheckSpace && archive)" in patch, "判据没有被 CheckSpace 守住"
    assert "!installPath.IsEmpty(), isCorrupt, noSpace" in patch, \
        "调用方没有把「是否走 InstallPath」传进去"


def test_patch_leaves_no_fake_zero_exit() -> None:
    """补丁 (b) 的全部意义是消灭「假 0」。拿不到进程句柄时也不能退 0,
    否则等于自己在补丁里又留了一条。"""
    patch = _read_bytes_text(SFX_PATCH)
    assert "DWORD exitCode = 1;" in patch, \
        "exitCode 初值必须是非 0 —— 初值 0 会在 hProcess 为空时退 0(假成功)"


def test_patch_unpins_install_root_from_the_stub_process() -> None:
    r"""进程的当前目录会钉住该目录(句柄不含 DELETE 共享权限)。
    存根若把当前目录留在 %ProgramData%\QTrade,引擎给安装根改名/回滚删除时
    会拿到 ERROR_SHARING_VIOLATION —— 原版因为目标是临时目录,从不会碰上。"""
    patch = _read_bytes_text(SFX_PATCH)
    assert "GetSystemDir(sysDir)" in patch and "SetCurrentDir(sysDir)" in patch, \
        "子进程起来后没有把存根自身的当前目录挪出安装根"


def test_stub_build_gates_source_bom() -> None:
    """🔴 打过补丁的源码必须带 BOM(补丁里有中文注释 + 7-Zip 用 -WX)。
    这道门挡的是「改用 GNU patch / git apply」那条路 —— 它们不加 BOM,
    而且只在非 936 代码页的机器上才炸,是最难查的一类。"""
    build = read(SFX_STUB_DIR / "build.ps1")
    assert "0xEF" in build and "0xBB" in build and "0xBF" in build
    assert "C4819" in build, "要在报错里点名 C4819,不然下一个人看不懂为什么"


def test_pester_names_have_no_placeholder_brackets() -> None:
    r"""🔴 Pester 5 把 It/Describe/Context 名字里的 `<xxx>` 当**数据占位符**,
    渲染时展开成 `$xxx`。run-pester.ps1 开着 StrictMode,未定义的变量直接抛 ——
    于是测试本体明明是对的,却以 `RuntimeException: 检索不到变量"$步名"` 失败,
    而且**只在渲染测试名的详细输出路径上**才炸,用 Verbosity=None 跑还是绿的。
    这类假红极难查,所以在这里一次性堵死:名字里想写尖括号就用 « »。
    """
    pat = re.compile(r"""^\s*(?:It|Describe|Context)\s+(['"])(.*?)\1\s*\{""")
    placeholder = re.compile(r"<[^<>\s/]+>")
    bad = []
    for p in sorted((INSTALLER_ROOT / "tests").glob("*.Tests.ps1")):
        for i, line in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
            m = pat.match(line)
            if m and placeholder.search(m.group(2)):
                bad.append(f"{p.name}:{i}: {m.group(2)}")
    assert not bad, "这些测试名会被 Pester 当占位符展开:" + "; ".join(bad)


def test_restore_data_is_a_paired_arg_not_a_qt_switch(doc03: str) -> None:
    """🔴 R6-58 (co):`--restore-data <tar>` 是**成对参数**,不是 `/QT_*` 开关。
    规格在命令行块里专门写了「不要自造 `/QT_RESTORE_DATA=`」—— 那是给实现方的禁令,
    不是给解析器的输入。这条同时钉住两件事:解析器不把它当开关、引擎也没有自造它。"""
    assert "/QT_RESTORE_DATA" not in parse_qt_switches(doc03), \
        "解析器把规格明令禁止的开关名当成了要求"
    assert "--restore-data" in doc03
    iss_text = read(ISS)
    assert "QT_RESTORE_DATA" not in iss_text, ".iss 自造了规格明令禁止的 /QT_RESTORE_DATA"
    # 但执行体必须在:§2.13 的还原动作
    upgrade = read(INSTALLER_ROOT / "engine" / "modules" / "QTrade.Upgrade.psm1")
    assert "function Restore-QtAgentData" in upgrade


def test_r6_58_cm_selfbuilt_stub_is_primary_and_fallback_warns(doc03: str) -> None:
    """🔴 R6-58 (cm):外壳的**正路**是自编存根;官方存根只是回退路径,
    而且「构建脚本必须在用回退路径时显式打出这条警告,不许静默降级」。"""
    assert "QTradeSD.sfx" in doc03, "规格已把自编存根写成正路"
    b = read(INSTALLER_ROOT / "build" / "build.ps1")
    # 缺省 -Stub auto:有自编存根就用自编的
    assert "$Stub -eq 'auto' -and (Test-Path -LiteralPath $QtStubFile)" in b, \
        "auto 模式没有优先选自编存根"
    # 回退时必须打警告,且点名「退出码不可作判据」
    assert "不透传退出码" in b and "last-exit-code.txt" in b, \
        "走官方存根回退路径时没有显式警告 —— 规格不许静默降级"


def test_r6_58_cn_sfx_extract_no_longer_maps_to_123(doc03: str) -> None:
    """🔴 R6-58 (cn):「SFX 解压」已从 123(DISK_FULL)的枚举里移出 ——
    解压前判据「一个字节都没写、直接拒」属于 26,不该显示成「可续跑」。"""
    block = _slice(doc03, "**退出码**(与 §8.2 状态一一对应", "\n---\n")
    row = [l for l in block.splitlines() if re.match(r"^\|\s*123\s*\|", l)]
    assert row, "§3.4 里找不到 123 那一行"
    head = row[0].split("🔴")[0]
    assert "SFX 解压" not in head, "123 的枚举里仍然列着 SFX 解压"
    # 存根侧:解压前空间不足退的是 26
    patch = _read_bytes_text(SFX_PATCH)
    assert "kQTradeExitDiskLow = 26" in patch
