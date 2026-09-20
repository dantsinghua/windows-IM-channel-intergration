# -*- coding: utf-8 -*-
"""代码签名 ↔ 规格 的逐字对账。

规格唯一出处 = ``docs/03-安装引导与自动化配置.md`` §2.2.3「数字签名与 SmartScreen」:

* 第 1 条 —— **全部可执行件**(外壳 EXE、引擎 EXE、``qtrade-winagent-svc.exe``、
  Electron 主程序;``adb``/``scrcpy`` **由其上游签名不动**)用**同一张**代码签名证书
  Authenticode 签名,**SHA-256**,**带时间戳(RFC 3161)**。
* 第 3 条 —— 载荷内的 **ps1 一律做 Authenticode 签名**(AllSigned 策略的企业机也能跑)。

本文件**现场解析**那一段文档,再与实现侧的两处唯一出处逐条比对:

* ``installer/build/build.ps1`` 的 ``$QtSignSpec``(出包流程里的应签清单)
* ``installer/signing/QTrade.Signing.psm1`` 的 ``Get-QtSignSpecTarget`` / ``Get-QtNeverSignRule``

**改文档不改实现(或反过来)会立刻红。**

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
BUILD_PS1 = INSTALLER_ROOT / "build" / "build.ps1"
SIGNING_DIR = INSTALLER_ROOT / "signing"
SIGNING_PSM1 = SIGNING_DIR / "QTrade.Signing.psm1"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


# ── 从 docs/03 §2.2.3 解析规格 ──────────────────────────────────────────────
def _section_223(doc: str) -> str:
    i = doc.index("#### 2.2.3 数字签名与 SmartScreen")
    # 到下一个同级或更高级标题为止
    m = re.search(r"\n#{1,4} ", doc[i + 10 :])
    j = i + 10 + (m.start() if m else len(doc) - i - 10)
    return doc[i:j]


def parse_signed_targets(section: str) -> tuple[set[str], set[str]]:
    """解析「全部可执行件(...)」那条 → (须签名的对象, 由上游签名不动的对象)。

    括号里是「、」分隔的项;带「由其上游签名不动」字样的那一项列的是**不重签**的件。
    """
    line = next(l for l in section.splitlines() if "全部可执行件" in l)
    inner = re.search(r"[((]([^))]*)[))]", line).group(1)
    must: set[str] = set()
    never: set[str] = set()
    for raw in inner.split("、"):
        item = raw.strip().strip("`")
        if not item:
            continue
        if "由其上游签名不动" in item:
            # 形如 "adb/scrcpy 由其上游签名不动"
            names = item.split("由其上游签名不动")[0].strip()
            for n in re.split(r"[/／]", names):
                if n.strip():
                    never.add(n.strip())
            continue
        must.add(item)
    return must, never


def parse_ps1_requirement(section: str) -> bool:
    """§2.2.3 第 3 条:对 ps1 做 Authenticode 签名。"""
    return any(
        "ps1" in l and "Authenticode" in l and "签名" in l for l in section.splitlines()
    )


@pytest.fixture(scope="module")
def section() -> str:
    return _section_223(read(DOC_03))


@pytest.fixture(scope="module")
def build_text() -> str:
    return read(BUILD_PS1)


@pytest.fixture(scope="module")
def signing_text() -> str:
    return read(SIGNING_PSM1)


# ── 实现侧:从 build.ps1 的 $QtSignSpec 里抠出 Spec 值 ───────────────────────
def parse_build_sign_spec(build: str) -> dict[str, str]:
    """``$QtSignSpec = @( @{ Key='…'; Spec='…'; Step='…' } … )`` → {Key: Spec}。"""
    i = build.index("$QtSignSpec = @(")
    j = build.index("\n)", i)
    block = build[i:j]
    out: dict[str, str] = {}
    for m in re.finditer(r"Key\s*=\s*'([^']+)'\s*;\s*Spec\s*=\s*'([^']+)'", block):
        out[m.group(1)] = m.group(2)
    return out


def parse_module_sign_spec(psm1: str) -> dict[str, str]:
    """``Get-QtSignSpecTarget`` 里的 ``Key`` / ``Spec``。"""
    i = psm1.index("function Get-QtSignSpecTarget")
    j = psm1.index("\nfunction ", i + 10)
    block = psm1[i:j]
    out: dict[str, str] = {}
    for m in re.finditer(r"Key\s*=\s*'([^']+)'\s*;\s*Spec\s*=\s*'([^']+)'", block):
        out[m.group(1)] = m.group(2)
    return out


def parse_never_sign_prefixes(psm1: str) -> list[str]:
    i = psm1.index("function Get-QtNeverSignRule")
    j = psm1.index("\nfunction ", i + 10)
    return re.findall(r"Prefix\s*=\s*'([^']+)'", psm1[i:j])


# ════════════════════════════════════════════════════════════════════════════
class TestSpecCoverage:
    """§2.2.3 点名的每一类,实现侧都得有。"""

    def test_section_found(self, section: str) -> None:
        assert "全部可执行件" in section
        assert "Authenticode" in section

    def test_build_covers_every_doc_target(self, section: str, build_text: str) -> None:
        must, _never = parse_signed_targets(section)
        assert must, "§2.2.3 里没解析出任何应签对象 —— 文档结构变了,先看是不是该改这个解析器"
        specs = set(parse_build_sign_spec(build_text).values())
        missing = must - specs
        assert not missing, (
            "docs/03 §2.2.3 要求签名、但 build.ps1 的 $QtSignSpec 里没有的对象:"
            + "、".join(sorted(missing))
        )

    def test_ps1_requirement_covered(self, section: str, build_text: str) -> None:
        assert parse_ps1_requirement(section), "§2.2.3 第 3 条(ps1 做 Authenticode 签名)不见了"
        assert "ps1" in set(parse_build_sign_spec(build_text).values())

    def test_build_and_module_spec_agree(self, build_text: str, signing_text: str) -> None:
        """两处清单必须一一对应(build.ps1 是流程侧,psm1 是模块侧)。"""
        assert parse_build_sign_spec(build_text) == parse_module_sign_spec(signing_text)

    def test_winagent_user_also_signed(self, build_text: str) -> None:
        """§2.2.3 只点名了 svc,但 R-14 是**两个**可执行体,会话代理同样是我方件。"""
        specs = set(parse_build_sign_spec(build_text).values())
        assert "qtrade-winagent-svc.exe" in specs
        assert "qtrade-winagent-user.exe" in specs

    def test_upstream_signed_items_are_never_resigned(
        self, section: str, signing_text: str
    ) -> None:
        """§2.2.3:adb / scrcpy **由其上游签名不动** ⇒ 必须在禁签清单里。"""
        _must, never = parse_signed_targets(section)
        assert never, "§2.2.3 里没解析出「由其上游签名不动」的件"
        prefixes = parse_never_sign_prefixes(signing_text)
        for name in never:
            assert any(
                name.lower() in p.lower() for p in prefixes
            ), f"{name} 在文档里标了「由其上游签名不动」,但不在 Get-QtNeverSignRule 里"

    def test_pinned_wechat_is_never_resigned(self, signing_text: str) -> None:
        """随包微信的 sha256 是钉死的(R2-6),重签一个字节就是 E_INSTALL_PAYLOAD_CORRUPT。"""
        prefixes = parse_never_sign_prefixes(signing_text)
        assert any("wechat" in p for p in prefixes)


class TestSignAlgorithms:
    """§2.2.3:SHA-256 + RFC 3161 时间戳。"""

    def test_sha256_everywhere(self, signing_text: str) -> None:
        assert "/fd', 'sha256'" in signing_text or "'/fd', 'sha256'" in signing_text
        assert "HashAlgorithm = 'SHA256'" in signing_text

    def test_rfc3161_timestamp(self, signing_text: str) -> None:
        # signtool 侧:/tr(RFC 3161)+ /td sha256;ps1 侧:-TimestampServer
        assert "'/tr'" in signing_text
        assert "'/td', 'sha256'" in signing_text
        assert "TimestampServer" in signing_text

    def test_not_using_legacy_authenticode_timestamp(self, signing_text: str) -> None:
        """``/t`` 是旧式 Authenticode 时间戳(非 RFC 3161),§2.2.3 要的是 ``/tr``。"""
        assert "'/t'," not in signing_text

    def test_cert_is_rsa3072_sha256_codesigning(self, signing_text: str) -> None:
        assert "-Type CodeSigningCert" in signing_text
        assert "-HashAlgorithm SHA256" in signing_text
        assert "$KeyLength = 3072" in signing_text


class TestPrivateKeyNeverLeaves:
    """私钥不可导出、不落盘、不进仓库。"""

    def test_no_pfx_export_in_module(self, signing_text: str) -> None:
        code = re.sub(r"<#.*?#>", "", signing_text, flags=re.S)
        code = "\n".join(l for l in code.splitlines() if not l.lstrip().startswith("#"))
        assert "Export-PfxCertificate" not in code
        assert ".pfx" not in code
        assert "NonExportable" in code
        assert not re.search(r"KeyExportPolicy\s+Exportable", code)

    def test_no_key_material_in_repo(self) -> None:
        """仓库里不许出现任何密钥/证书材料。"""
        bad = [
            p
            for p in INSTALLER_ROOT.rglob("*")
            if p.is_file() and p.suffix.lower() in {".pfx", ".p12", ".key", ".snk"}
        ]
        assert not bad, "仓库里出现了密钥材料:" + ", ".join(str(p) for p in bad)

    def test_cert_dir_is_outside_repo(self, signing_text: str) -> None:
        """公钥 .cer 默认导到**产物根**,不在仓库里。"""
        m = re.search(r"QtDefaultCertDir\s*=\s*'([^']+)'", signing_text)
        assert m, "找不到默认证书目录"
        assert "qtrade-payload" in m.group(1)
        assert "Desktop" not in m.group(1)


class TestBuildIntegration:
    """build.ps1 的签名开关与顺序约束。"""

    def test_sign_switches_declared(self, build_text: str) -> None:
        for sw in ("[switch] $Sign", "[string] $CertThumbprint", "[string] $TimestampUrl",
                   "[switch] $NoTimestamp", "[string] $SignToolPath"):
            assert sw in build_text, f"build.ps1 少了参数 {sw}"

    def test_sign_requires_thumbprint(self, build_text: str) -> None:
        assert "-Sign 必须同时给 -CertThumbprint" in build_text

    def test_never_signs_repo_sources(self, build_text: str) -> None:
        """签名只发生在 out\\ 下的副本上(签仓库源文件会污染 git、破逐字对账)。"""
        assert "$SignedEngineDir = Join-Path $OutDir 'engine-signed'" in build_text
        assert "$PresignDir = Join-Path $OutDir 'presign'" in build_text
        assert "$IssSourceDir = $EngineDir" in build_text
        assert "$IssSourceDir = $SignedEngineDir" in build_text
        assert "Join-Path $IssSourceDir 'qtrade-setup-engine.iss'" in build_text

    def test_sign_before_manifest_hash(self, build_text: str) -> None:
        """预签载荷必须在「步 2 收集载荷与 manifest」之前(否则 manifest 记的是签名前的哈希)。"""
        i_presign = build_text.index("步 0b 预签载荷中的我方可执行件")
        i_collect = build_text.index("Write-Section '步 2 收集载荷与 manifest'")
        assert i_presign < i_collect

    def test_engine_scripts_signed_before_iscc(self, build_text: str) -> None:
        i_sign = build_text.index("步 1a 在引擎副本上签")
        i_iscc = build_text.index("& $Iscc ")
        assert i_sign < i_iscc

    def test_shell_signed_last(self, build_text: str) -> None:
        i_concat = build_text.index("$fs = [IO.File]::Create($FinalExe)")
        i_shell = build_text.index("步 5 签外壳 EXE")
        i_engine = build_text.index("步 1b:签引擎 exe")
        assert i_engine < i_shell, "内层(引擎)必须先于外壳签"
        assert i_concat < i_shell, "外壳要在拼接完成之后签"

    def test_g6_gate_exists_and_is_last(self, build_text: str) -> None:
        i_g6 = build_text.index("Write-Section 'G6 签名复核")
        i_shell = build_text.index("步 5 签外壳 EXE")
        assert i_g6 > i_shell
        assert "G6 签名复核不通过" in build_text

    def test_cmd_not_signed(self, build_text: str) -> None:
        """.cmd 不支持 Authenticode,链首脚本仍从仓库原件复制。"""
        assert "Copy-Item -LiteralPath (Join-Path $EngineDir $ChainHeadName)" in build_text
        assert ".cmd 不支持 Authenticode" in build_text


class TestSigningFilesEncoding:
    """§2.6.7 W1:ps1/psm1 一律 UTF-8 **with BOM**;.cmd 一律 CRLF、**无** BOM。"""

    def test_ps1_have_bom(self) -> None:
        bad = []
        for p in sorted(SIGNING_DIR.rglob("*")):
            if p.suffix.lower() in {".ps1", ".psm1", ".psd1"}:
                if p.read_bytes()[:3] != b"\xef\xbb\xbf":
                    bad.append(p.name)
        assert not bad, "这些脚本缺 BOM(PowerShell 5.1 会按 GBK 解析中文注释并 ParserError):" + ", ".join(bad)

    def test_cmd_is_crlf_without_bom_and_ascii_executable_lines(self) -> None:
        for p in sorted(SIGNING_DIR.rglob("*.cmd")):
            data = p.read_bytes()
            assert data[:3] != b"\xef\xbb\xbf", f"{p.name} 带 BOM,cmd.exe 第一行会解析失败"
            text = data.decode("utf-8")
            assert text.count("\n") == text.count("\r\n"), f"{p.name} 有 LF-only 换行"
            for line in text.split("\r\n"):
                s = line.lstrip()
                if not s or s.lower().startswith("rem"):
                    continue
                assert all(ord(c) < 128 for c in line), (
                    f"{p.name} 的可执行行含非 ASCII(目标机代码页未知,会乱码):{line.strip()}"
                )

    def test_expected_files_exist(self) -> None:
        for name in (
            "QTrade.Signing.psm1",
            "New-QtSelfSignedCert.ps1",
            "Invoke-QtSign.ps1",
            "Import-QtCodeSigningCert.ps1",
            "Remove-QtCodeSigningCert.ps1",
            "导入QTrade签名证书.cmd",
        ):
            assert (SIGNING_DIR / name).exists(), f"缺文件:installer/signing/{name}"


class TestDocAndReadmeAgree:
    """出包说明要把签名步骤写清楚(它是安琳真执行时唯一会看的东西)。"""

    def test_readme_has_signing_section(self) -> None:
        readme = read(INSTALLER_ROOT / "build" / "README.md")
        assert "代码签名(自签名阶段)" in readme
        assert "New-QtSelfSignedCert.ps1" in readme
        assert "-Sign -CertThumbprint" in readme
        assert "导入QTrade签名证书.cmd" in readme
        # 换成公司内部 CA / 购买的 OV 证书时只需换指纹
        assert "OV" in readme

    def test_readme_documents_undo(self) -> None:
        """每一步都要说明「改动了机器上的什么、怎么撤销」。"""
        readme = read(INSTALLER_ROOT / "build" / "README.md")
        assert "怎么撤销" in readme or "如何撤销" in readme
        assert "Remove-QtCodeSigningCert.ps1" in readme
