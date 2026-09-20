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
    return set(re.findall(r"(/QT_[A-Z_]+)", code))


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
        if "UTF8Encoding($true)" in text and "Tests" not in p.name:
            pytest.fail(f"{p.name} 里出现了带 BOM 的 UTF8Encoding($true)")


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
