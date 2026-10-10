# QTrade 安装器 —— 出包说明

> 规格唯一出处 = `docs/03-安装引导与自动化配置.md`(§2.1 两段式、§2.2 载荷与压缩、§2.2.3 签名、§2.2.4 CI 验证门 G0、§3.4 命令行与退出码)。
> 本文只讲**怎么出包**;任何口径与文档冲突时以文档为准,改实现不改文档,或先出裁决(基线 §15g,编号续 R6-N)。

---

## 0. 想马上出一个正式 EXE?看这一节就够

在一台**已装 Inno Setup 6、已把 `7zSD.sfx` 放进 `installer\build\`、并且载荷全部就位**的 Windows 上:

```powershell
cd <仓库>\installer\build
.\build.ps1 -Version 1.0.0 -SourceRoot <载荷产物根>
```

出来的就是 `installer\out\QTrade-Setup-1.0.0.exe`(单文件,≈3 GB)。

**这一条命令按顺序做了**:六道门(脚本编码 / `.cmd` 换行 / 语法 / 防火墙红线 / 微信 `/S` 红线 / SFX 配置键)→ ISCC 编译引擎 →
收集载荷并生成 `manifest.json`(含两条硬校验)→ 7z 两块归档 → 归档 ↔ manifest 路径复核 → 拼 SFX。
任何一步不过都会**中止并打印中文原因**,不会出半成品。

**签名已接进出包流程**(§2.2.3,见第 5 节)。自签名阶段先生成一张证书,再带 `-Sign` 出包:

```powershell
..\signing\New-QtSelfSignedCert.ps1 -Organization 'QTrade'     # 打印指纹
.\build.ps1 -Version 1.0.0 -SourceRoot <载荷产物根> -Sign -CertThumbprint <指纹>
```

不带 `-Sign` 时出包行为与以前**逐字节一致**(有 AST 回归守卫钉着)。

### 三样前置,缺一样就出不成正式 EXE

| 前置 | 怎么满足 | 验证 |
|---|---|---|
| **Inno Setup 6** | <https://jrsoftware.org/isdl.php>(或 `choco install innosetup`),默认装到 `C:\Program Files (x86)\Inno Setup 6\` | `.\build.ps1 -CheckOnly` 不再报缺 |
| **`7zSD.sfx`** | 🔴 **不在 7-Zip 主安装包里**。从官方 **LZMA SDK**(`lzma<ver>.7z`)或 **7-Zip Extra**(`7z<ver>-extra.7z`)里解出 `7zSD.sfx`,放进 `installer\build\` | 同上 |
| **载荷全部就位** | 见第 4 节;先用 `.\build.ps1 -SelfCheck -SourceRoot <根>` 看缺什么 | `-SelfCheck` 打印「已收集 13 项、缺件 (空)」 |

> **中文语言文件不用你操心**:Inno 6 的发行版**不一定自带** `ChineseSimplified.isl`
> (本机这版的 `Languages\` 里就没有),所以它**随仓库走** —— `installer\engine\lang\ChineseSimplified.isl`,
> 取自 Inno 官方 `issrc` 仓库的 `Files/Languages/`(官方语言,非 Unofficial),`.iss` 用相对路径引用。
> 换一台没装中文包的机器照样能编。

> 三样都没有也能验管线:`.\build.ps1 -SelfCheck -AllowMissing`(只跑六道门 + 生成 manifest),
> 或 `.\build.ps1 -AllowMissing`(再加 7z 归档;有 `7z.exe` 就行)。

---

## 1. 产物长什么样

```
QTrade-Setup-<ver>.exe(≈3 GB,单文件,签名)
  = 7-Zip SFX 存根(≈0.3 MB)+ SFX 配置(build/sfx-config.txt)+ 7z 归档(LZMA2 solid + 非 solid 块)
       │
       ├─ install\engine\qtrade-setup-engine.exe   安装引擎(Inno Setup 6 [Code] + PowerShell 模块,≈15 MB,无载荷)
       ├─ install\engine\run-step.ps1 + modules\*.psm1
       ├─ install\engine\precheck-disk.cmd          SFX 拉起的链首(6 GB 判 → 拉引擎 → 透传退出码)
       ├─ install\manifest.json                     载荷清单(每个文件带 sha256)
       └─ kernel\ wsl\ pkg\ winagent\ console\      载荷本体
```

**外壳只解压、内层才是安装程序**(MATLAB / Visual Studio 离线包同款)。
外壳把归档解到写死的 `%ProgramData%\QTrade\`,然后拉起引擎;引擎退出码原样透传为 EXE 退出码。

---

## 2. 工具链要求

| 工具 | 版本 | 从哪来 | 缺了会怎样 |
|---|---|---|---|
| **Inno Setup 6** | 6.x(含 `ISCC.exe`) | <https://jrsoftware.org/isdl.php> | 跳过引擎编译并告警,产出的包没有引擎 |
| **7-Zip** | 19+(`7z.exe`) | <https://www.7-zip.org/> | 无法归档,到载荷 stage 为止 |
| **7z SFX 存根** | `7zSD.sfx`(扩展版) | 🔴 **7-Zip Extra**(`7z<ver>-extra.7z`),不在主包里 | 无法拼单 EXE,打印手工 `copy /b` 命令后退出 |
| **PowerShell** | Windows PowerShell **5.1**(目标机自带) | — | — |
| **Pester** | **5.x**(单测用;系统自带的 3.4.0 语法不兼容) | `tests/run-pester.ps1 -Bootstrap` 下到 `%LOCALAPPDATA%\QTradeInstallerTests\pester`,**不进机器模块路径** | 单测跑不起来 |
| **Python** | 3.11+(规格对账用) | `~/.venvs/qtrade` | `test_spec_consistency.py` 跑不起来 |
| **signtool** | Windows SDK(Signing Tools)| 本机在 `C:\Program Files (x86)\Windows Kits\10\bin\10.0.19041.0\x64\signtool.exe` | 带 `-Sign` 时直接报错中止;不带 `-Sign` 则只是包没签名,而目标机多有 AppLocker/EDR,未签名大概率被拦 |

> `build.ps1` **不会替你装任何工具**:缺件只报缺、给获取方式、列出它找过哪些位置。

---

## 3. build.ps1 的几种跑法

```powershell
.\build.ps1 -Version 1.0.0 -SourceRoot <根>     # 完整包(要 ISCC + 7z + 7zSD.sfx + 全部载荷)
.\build.ps1 -Version 1.0.0 -AllowMissing        # 轻量验证包(缺件占位,只验管线)
.\build.ps1 -SelfCheck -SourceRoot <根>         # 六道门 + 收载荷 + 生成 manifest(不需要任何打包工具;**不签名**)
.\build.ps1 -CheckOnly                          # 只跑六道门
.\build.ps1 -Version 1.0.0 -IsccPath 'D:\Inno Setup 6\ISCC.exe' -SevenZipPath 'D:\7-Zip\7z.exe' -SfxStubPath 'D:\7zSD.sfx'
.\build.ps1 -Version 1.0.0 -SourceRoot <根> -Sign -CertThumbprint <指纹>   # 带代码签名出包(第 5 节)
.\build.ps1 -Version 1.0.0 -SourceRoot <根> -Sign -CertThumbprint <指纹> -NoTimestamp   # 离线环境,见第 5 节的警告
```

六道门(任何模式都先跑;带 `-Sign` 时出包末尾还有 **G6 签名复核**,见第 5.3 节):

1. **G1 脚本编码** —— 全部 `.ps1`/`.psm1` 必须 UTF-8 **with BOM**。
   🔴 Windows PowerShell 5.1 把无 BOM 的 UTF-8 脚本按系统 ANSI(中文机 = GBK)解析,中文注释里的字节会被当成引号 → `ParserError`,而且**报错本身也是乱码**(§2.6.7 W1;验收 M0-10)。
   扫描范围:`engine\`、`build\`、**`signing\`**(它要随包发给目标机同事)、`tests\`。
2. **G1b `.cmd` 换行与编码** —— `engine\` 与 `signing\` 下的 `.cmd` 必须 **CRLF、无 BOM、可执行行纯 ASCII**。
   🔴 LF-only 会让 `cmd.exe` 的 `for`/`if`/`call`/标签解析错乱,而且**只有真跑起来才暴露**。
3. **G2 语法解析** —— `[Parser]::ParseFile` 逐个过。
4. **G3 防火墙红线** —— 随包脚本里零处 `netsh advfirewall` / `New-NetFirewallRule`(§6;验收 M1-14)。
5. **G4 微信卸载红线** —— 零处以 `/S` 运行微信 `Uninstall.exe`。🔴 `/S` = **卸载并清空聊天记录与登录态**(§2.9.3 事实 1)。
6. **G5 SFX 配置键** —— 配置里的键必须是**当前这个存根认识的**那些;存根对不认识的键**静默忽略**。

压缩参数按 §2.2.2:

```powershell
# 第一块:可压缩件,solid + LZMA2 ultra
7z a -t7z -mx=9 -m0=lzma2 -ms=on -mf=BCJ2 payload.7z <files>
# 第二块:已压缩件(MSI / 微信安装包),单独**非 solid**、-mx=0
#   🔴 必须在 stage 目录里用**相对路径**调 —— 给绝对路径会把文件放进归档**根目录**,
#      解压出来路径全错、引擎按 manifest 一个也找不到,而且这种错位是**静默**的。
7z a -t7z -mx=0 -ms=off payload.7z <relative paths>
```

归档完还有一道 **归档 ↔ manifest 路径复核**:manifest 里每个非通配条目都必须能在归档里按原路径找到。

---

## 4. 载荷从哪来

`collect-payload.ps1` 的来源映射。每项都能用环境变量覆盖;`Sources` 有多个时(WinAgent)会**依次覆盖合并**到同一目标。

| 载荷内路径 | 默认来源(相对 `-SourceRoot`) | 环境变量 | critical | 已压缩 |
|---|---|---|---|---|
| `kernel/bzImage-6.6` | `kernel/out/bzImage` | `QT_SRC_KERNEL` | ✓ | |
| `wsl/kcheck-rootfs.tar` | `rootfs/out/kcheck-rootfs.tar` | `QT_SRC_KCHECK` | ✓ | |
| `wsl/wsl.msi` | `third_party/wsl/wsl.msi` | `QT_SRC_WSL_MSI` | ✓ | ✓ |
| `wsl/rootfs.tar` | `rootfs/out/rootfs.tar` | `QT_SRC_ROOTFS` | ✓ | |
| `wsl/agent` | `dist/qtrade_agent-*.whl` | `QT_SRC_AGENT_WHEEL` | | |
| `pkg/adb` | `third_party/platform-tools` | `QT_SRC_ADB` | | |
| `pkg/scrcpy` | `third_party/scrcpy` | `QT_SRC_SCRCPY` | | |
| `pkg/chatlog` | `third_party/chatlog` | `QT_SRC_CHATLOG` | | |
| `pkg/wechat/weixin_4.1.12.26.exe` | `third_party/wechat/…` | `QT_SRC_WECHAT` | ✓ | ✓ |
| `pkg/pyweixin/pywechat127-1.9.8-py3-none-any.whl` | `winagent/vendor/pyweixin/…`(仓库内;备选 `third_party/pyweixin/…`) | `QT_SRC_PYWEIXIN` | ✓ | ✓ |
| `pkg/pyweixin/LICENSE`、`SOURCE.txt` | `winagent/vendor/pyweixin/…` | `QT_SRC_PYWEIXIN_LICENSE` / `_SOURCE` | | |
| `pkg/vcredist/VC_redist.x64.exe` | `third_party/vcredist/…` | `QT_SRC_VCREDIST` | | ✓ |
| `winagent/python` | `winagent/dist/python` | `QT_SRC_WA_PYTHON` | | |
| `winagent/app` | `winagent/dist/qtrade-winagent-svc` **+** `winagent/dist/qtrade-winagent-user` | `QT_SRC_WA_APP` | | |
| `console` | `console/release/win-unpacked` | `QT_SRC_CONSOLE` | | ✓? 否 |

> `critical` 严格照 §2.2.1 标了 `critical:true` 的那 5 项 —— 它决定**安装期**校验失败算不算
> `E_INSTALL_PAYLOAD_CORRUPT`(不可续跑)。**和「打包时缺不缺」是两回事**:不带 `-AllowMissing` 时缺任何一项都不出正式包。

### 各件怎么产出

| 件 | 怎么产 |
|---|---|
| **WinAgent** | 在 `winagent/` 跑 `build/build.ps1`(PyInstaller 单目录),产出 `dist/qtrade-winagent-svc/` 与 `dist/qtrade-winagent-user/`。两个目录由 `collect-payload.ps1` 合并进 `winagent/app/`,合并后必须同时有 `qtrade-winagent-svc.exe`(服务,LocalSystem)与 `qtrade-winagent-user.exe`(会话代理,不提权)—— R-14 实名,docs/02 §2.4、docs/03 §2.8.1。 |
| **控制台** | 在 `console/` 跑 `npm run build:dir`(= `electron-builder --dir`)。`electron-builder.yml` 的 `directories.output: release` + `target: dir` ⇒ 产物在 `console/release/win-unpacked/`。**不出 NSIS/MSI**(§2.1 末:安装引导归 7z-SFX + Inno 引擎)。<br>🔴 **在 Linux/WSL 上构建时必须显式 `npx electron-builder --dir --win --x64`** —— `build:dir` 不带 `--win`,electron-builder 默认按**当前平台**打,在 WSL 里出的是 `release/linux-unpacked/`,`collect-payload.ps1` 找 `console/release/win-unpacked` 就会报缺件(2026-09-20 实测踩过)。在 Windows 上跑 `npm run build:dir` 不受影响。 |
| **Agent wheel** | 仓库根跑 `python -m build`(或 `pip wheel . -w dist`),出 `dist/qtrade_agent-*.whl`。真值登记在 `manifest.rootfs_contents.agent_wheel`,由 rootfs 构建写进发行版的 `/etc/qtrade/contents.json`,**两处必须逐字一致**(G-10)。 |
| **rootfs / kcheck** | 由**本仓库的 `installer/rootfs/`** 产出(`build-rootfs.sh` / `build-kcheck.sh`,详见那边的 `README.md`)。两个脚本都用 `--out` 直接写进 `-SourceRoot` 指的产物根;`build-rootfs.sh` 还会在 `rootfs.tar` 旁边落一份 `contents.json`,那就是 CI 要填进 `manifest.rootfs_contents` 的东西(G-10,两处逐字一致)。 |
| **内核 bzImage** | 由 `qtrade-redroid-installer` 那套产出(原机在 WSL 里的 `~/work/qtrade-redroid-installer/kernel/out/bzImage`,现役 v4 = `C:\Users\anlin\.qtrade-redroid\bzImage.v4`,两者 sha256 相同)。🔴 那个路径**在 WSL 里,Windows 侧打包机看不到** —— 用 `-SourceRoot` 或 `QT_SRC_KERNEL` 指到打包机能访问的位置。 |
| **微信安装包 / chatlog+wx_key / adb / scrcpy / VC 运行库** | 第三方件。原机的取钥工具在 `/mnt/c/Users/anlin/Desktop/盈米/蜂鸟项目/南银理财/weChatlog/`(SKILL §3)。 |

### 缺件怎么办

不带 `-AllowMissing` 时缺件直接拒绝出包,并逐项打印**用途 / 找过哪些路径 / 怎么拿**。
带 `-AllowMissing` 出的是**轻量验证包**(`manifest.lightweight=true`、`manifest.missing[]` 列出缺了什么),只能验管线,**不能拿去装机**。

### 三道硬校验(collect 里直接 throw,不等到装机现场)

- **随包微信 sha256** 必须等于 `58997cfe…4053`。
  🔴 另一候选包 `WeChatWin_4.1.12.exe` 装出来是 **4.1.12.55**,两包**外层 VersionInfo 完全相同**,文件名与版本号都分不出,**只能靠 sha256 或解包看 `install.7z` 顶层版本目录名区分**(R2-6)。
- **内核版本串**必须以 `-binder` 结尾且**不含 `+`**。
  源码目录无 git tag 时 `setlocalversion` 会追加 `+`,`uname -r` 就变成 `…-binder+`,而验证判据是与 manifest `version` **逐字相等**(K4)——带 `+` 的内核会被判失败并回滚。`build-kernel.sh` 必须固定 `LOCALVERSION=`。
- **`manifest.files[kernel].coredump_l2` 恒为 `"D"`**(R6-38)。其它值不是合法交付形态。

---

## 5. 代码签名(自签名阶段)

> 规格 = docs/03 §2.2.3。**证书类型定案是 OV 起步(A-4)**;下面这套自签名是**内部试用期的过渡形态**,
> 不是交付形态。换成公司内部 CA 或买来的 OV 证书时,**流程一个字都不改,只把 `-CertThumbprint` 换掉**。

### 5.1 四步走(安琳的操作顺序)

| 步 | 在哪台机器 | 做什么 |
|---|---|---|
| ① | 打包机(本机) | 生成自签名证书,拿到**指纹**与公钥 `.cer` |
| ② | 打包机(本机) | 带 `-Sign -CertThumbprint <指纹>` 出包 |
| ③ | — | 把 `.cer` + `installer\signing\` 下的导入脚本随包发给目标机同事 |
| ④ | 目标机(同事) | 双击「导入QTrade签名证书.cmd」(会弹 UAC) |

### 5.2 ① 生成证书

```powershell
cd <仓库>\installer\signing
.\New-QtSelfSignedCert.ps1 -WhatIf                      # 🔴 先干跑,看它打算做什么
.\New-QtSelfSignedCert.ps1 -Organization 'QTrade'       # 真生成(默认有效期 3 年)
```

**会改动这台机器上的什么**

| 改了什么 | 位置 |
|---|---|
| 多一张代码签名证书 | `Cert:\CurrentUser\My`(**当前用户**的个人存储,不是全机) |
| 多一个**公钥** `.cer` | `C:\Users\anlin\qtrade-payload\signing\QTrade-CodeSigning-<指纹>.cer`(产物根,**仓库外**) |

不需要管理员;不碰 `LocalMachine` 任何存储;不动任何已有证书。

**怎么撤销**

```powershell
Remove-Item "Cert:\CurrentUser\My\<指纹>"                             # 删证书
Remove-Item "C:\Users\anlin\qtrade-payload\signing\QTrade-CodeSigning-<指纹>.cer"
```

**幂等**:同主题且**未过期**的证书已存在时直接复用并打印指纹,不会重复生成。
过期了再跑才会生成新的。

> 🔴 **私钥不可导出**(`-KeyExportPolicy NonExportable`)。整套脚本里**没有任何一处**
> `Export-PfxCertificate` —— 密钥材料只存在于当前用户的证书存储里,永远不落盘、不进仓库。
> 代价:**换一台打包机就要重新生成证书**(指纹也会变)。这是刻意的取舍:
> 能导出的私钥迟早会被谁 `commit` 进仓库。

### 5.3 ② 带签名出包

```powershell
cd <仓库>\installer\build
.\build.ps1 -Version 1.0.0 -SourceRoot C:\Users\anlin\qtrade-payload `
            -Sign -CertThumbprint <上一步打印的指纹>
```

离线环境(连不上时间戳服务)另加 `-NoTimestamp`,并看清楚它打的警告:

```powershell
.\build.ps1 ... -Sign -CertThumbprint <指纹> -NoTimestamp
```

> 🔴 **不带时间戳的签名,在证书过期那天全部失效 —— 包括已经发出去的包**。
> 带 RFC 3161 时间戳的签名则在证书有效期内永久有效。能联网就别关。

**签名发生在流程的哪几处(顺序是硬约束,不是风格)**

| 步 | 签什么 | 为什么必须在这个位置 |
|---|---|---|
| 0b | `winagent/app` 与 `console` 的**我方 exe** | 🔴 **必须先于 manifest 算 sha256**。做法是把源目录复制到 `out\presign\` 下、在**副本**上签、再把 `QT_SRC_WA_APP` / `QT_SRC_CONSOLE` 指到副本 —— `collect-payload.ps1` 是「复制 → 算 sha256 → 写 manifest」一气呵成的,而 `build.ps1` 会**调它两次**,就地签 stage 会被第二次覆盖回未签名版本 |
| 1a | 引擎的**全部 ps1/psm1** | 签在 `out\engine-signed\` 的副本上,ISCC 从副本编译 ⇒ 引擎里内嵌的就是已签名脚本 |
| 1b | 引擎 exe | 在它被复制进 stage、被 collect 登记 sha256 **之前** |
| 5 | 外壳 EXE | **最后**一步。签名覆盖整个 EXE(含里面的 7z 归档)⇒ **此后改载荷必须重签** |
| G6 | (复核) | 逐个验证应签文件确实带签名、且签名者指纹 = 传入的指纹;并反向检查第三方件**没有**被我方证书重签;还复核 manifest 里引擎的 sha256 = stage 里已签名文件的实际值 |

**🔴 仓库里的源文件一个字节都不会被改**:签名块是追加到文件尾的,签仓库源文件会污染 git、
让规格对账的逐字比对失效。所有签名动作都发生在 `installer\out\` 下的副本上
(而 G1/G1b 门的文件枚举本来就排除了 `\out\`)。

**不签什么**

* `.cmd`(`run-engine.cmd` / `precheck-disk.cmd`)—— **Authenticode 不支持批处理**;
* 第三方件:随包微信安装包、`wsl.msi`、`VC_redist`、chatlog / `wx_key*.dll`、
  platform-tools(`adb`)、scrcpy、嵌入式 Python、Electron 自带的 `ffmpeg.dll` 等、
  PyInstaller `_internal\` 下的 CPython 与依赖 DLL。
  §2.2.3 原文:**adb/scrcpy 由其上游签名不动**。重签会毁掉原厂签名链;
  而**随包微信的 sha256 是钉死的(R2-6)**,改一个字节就是 `E_INSTALL_PAYLOAD_CORRUPT`。
  清单在 `installer\signing\QTrade.Signing.psm1` 的 `Get-QtNeverSignRule`。

**不带 `-Sign` 时行为与以前逐字节一致**(`QTrade.Signing.Tests.ps1` 有 AST 回归守卫钉着:
每一处签名调用都必须在 `if ($Sign)` 里)。

**`-SelfCheck` 不签名** —— 那条路只收载荷、生成 manifest,不出可交付的包。

### 5.4 ③ 分发给目标机

把这四样发给同事(一个文件夹即可):

```
QTrade-Setup-1.0.0.exe
QTrade-CodeSigning-<指纹>.cer
Import-QtCodeSigningCert.ps1
Remove-QtCodeSigningCert.ps1
导入QTrade签名证书.cmd
QTrade.Signing.psm1          ← 上面两个 ps1 要 Import 它
```

**同时把指纹用另一条渠道告诉对方**(微信/邮件正文里写明),让他核对 —— 这一步不是形式:
导入 `Root` 等于告诉那台机器「这张证书签什么都可信」,`.cer` 被掉包就是一条后门。

### 5.5 ④ 目标机导入(同事操作)

双击 **「导入QTrade签名证书.cmd」**。它会:

1. 自动请求管理员权限(弹 UAC);
2. 打印证书的**主题、颁发者、指纹、有效期**,要求核对;
3. 核对无误后导入两个存储。

或者用管理员 PowerShell:

```powershell
.\Import-QtCodeSigningCert.ps1 -CerPath .\QTrade-CodeSigning-<指纹>.cer -ExpectedThumbprint <指纹>
.\Import-QtCodeSigningCert.ps1 -CerPath .\xxx.cer -WhatIf     # 先看它打算做什么
```

**会改动那台机器上的什么**

| 改了什么 | 位置 | 为什么要 |
|---|---|---|
| +1 张证书 | `Cert:\LocalMachine\Root` | 自签名证书自己就是根;不导这里,Authenticode 验出来恒 `UnknownError` |
| +1 张证书 | `Cert:\LocalMachine\TrustedPublisher` | AllSigned 执行策略与 AppLocker 的发布者规则认它 |

两处都是**全机生效**,所以要管理员。别的什么都不改。**幂等**:已经有了就跳过。
`-ExpectedThumbprint` 对不上**直接拒绝,一个存储都不碰**。

**怎么撤销**

```powershell
# 管理员 PowerShell
.\Remove-QtCodeSigningCert.ps1 -Thumbprint <指纹>
```

从两个存储里各删掉那一张(幂等,不在就说不在)。已经装好的 QTrade 不受影响,
只是以后再运行已签名的包会重新显示「未知发布者」。

### 5.6 一条反直觉的事实:打包机上验签会显示「未受信任」

自签名证书在**没导入信任库的机器上**,`Get-AuthenticodeSignature` 回的**不是 `Valid`**,
而是 `UnknownError` / `NotTrusted`(链终止于一个不受信任的根)。

所以验证逻辑分**三态**(`Test-QtSignatureVerdict`):

| 状态 | 判定 | 含义 |
|---|---|---|
| `Valid` | ✅ 通过,受信任 | 证书已导入,或换成 CA 证书之后 |
| `UnknownError` / `NotTrusted` **且有签名者证书** | ✅ 通过,未受信任 | **自签名阶段的正常态**,不是失败;提示去导入证书 |
| `NotSigned` / `HashMismatch` / `NotSupportedFileFormat` | ❌ 失败 | 没签上 / 签完又被改过 / 格式不支持 |
| 指纹 ≠ 传入的指纹 | ❌ 失败 | 签是签了,但签成了另一张证书 |

> 🔴 如果照直写 `Status -eq 'Valid'` 当判据,打包机上**每个文件都会"验证失败"**,
> 接着就会有人把验证整个关掉 —— 那才是真事故。G6 门按上表判。

### 5.7 SmartScreen 与 AppLocker(§2.2.3)

* 自签名**不解决 SmartScreen**:OV 证书新签名初期都会弹「Windows 已保护你的电脑」,
  信誉靠下载量累积;**EV 证书即时通过**。
* 但自签名**解决 AppLocker/EDR 的"未签名"拦截** —— 前提是 IT 把这个发布者加白
  (把**发布者信息 + 证书指纹**写进部署说明,就是上面 5.4 要发的那份)。
* 真机上还没验过的:**代码签名后的 UAC 行为**(见本文末「须真机验证清单」第 4 条)。

### 5.8 将来换正式证书

买到 OV 证书(或拿到公司内部 CA 签发的证书)之后:

1. 把证书装进打包机的 `Cert:\CurrentUser\My`(或按 CA 的说明用硬件令牌);
2. `Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert` 拿指纹;
3. 出包命令**只换指纹**:`.\build.ps1 ... -Sign -CertThumbprint <新指纹>`。

`New-QtSelfSignedCert.ps1` 与两个导入/清理脚本届时作废(目标机不再需要导入任何东西,
证书链由公开 CA 或企业 CA 提供)。`Invoke-QtSign.ps1`、G6 门、测试**全部照旧**。

### 5.9 手工补签(排障用)

```powershell
cd <仓库>\installer\signing
.\Invoke-QtSign.ps1 -CertThumbprint <指纹> -Path <文件1>,<文件2>
.\Invoke-QtSign.ps1 -CertThumbprint <指纹> -Path <副本目录> -Scope engine    # 目录内全部 ps1/psm1
```

🔴 别拿它去签**仓库里的源文件**(理由见 5.3)。要签就签副本。

---

## 6. 单测与对账

```powershell
# PowerShell 模块单测(Pester 5)
..\tests\run-pester.ps1 -Bootstrap

# 规格逐字对账(退出码表 / /QT_* 开关 / 状态机键名 / 受管键表 / BOM / 红线 / 派发器覆盖)
~/.venvs/qtrade/bin/python -m pytest -q installer/tests
```

签名那一套单独两个文件,同样被上面两条命令覆盖:
`tests\QTrade.Signing.Tests.ps1`(Pester,**全 Mock**:不生成证书、不碰证书存储、不真签名)与
`tests\test_signing_consistency.py`(从 `docs/03` §2.2.3 现场解析应签对象清单,与 `build.ps1` 的
`$QtSignSpec`、`QTrade.Signing.psm1` 的 `Get-QtSignSpecTarget` / `Get-QtNeverSignRule` 三方对账)。

`test_spec_consistency.py` 从 `docs/03` **现场解析**退出码表与命令行块,再与 `.iss` / `QTrade.Exit.psm1` 逐条比对,
并检查派发器与 `.iss` 把 §2.3 的每个状态都接上了 —— **改文档不改实现(或反过来)会立刻红**。

---

## 7. 已知待办 / 偏差

见 `.omc/handoffs/installer.md`。要点:

1. **裁决①已落地**:§2.1 的「SFX 解压**前**判 6 GB」认偏差 —— 7zSD.sfx 没有 pre-extract 钩子,
   改为 `precheck-disk.cmd` 在**引擎之前**判(退 26);解压目录空间不足导致 SFX 解压失败,
   表现为「解压不完整 + 空间不足」,同样**映射到 26** 而不是 41。
   ⚠️ 这与 §3.4 第 123 行把「SFX 解压」列进 `DISK_FULL`(123)的写法不一致,**需要总控把那半句从 123 行移出**。
2. **裁决②已落地**:「十一键」以逐字列出的 **10 个键**为准,代码统称「`.wslconfig` 受管键表(10 键)」。
3. **裁决③已落地**:卸载按 **1 → 2 → 3 → 5(防火墙)→ 6(hosts)→ 4(服务)→ 7** 执行,注释点名了与 §2.14 编号的差异。
4. 微信安装器的 `/S`、`/D=`、降级三项未实测,故 `[wechat] silent_setup` 恒 `false`、方案①恒交互。
5. **已真编译**:ISCC **0 error / 0 warning**,引擎 2,173,002 字节,内嵌 17 个模块 + `run-step.ps1` + 中文语言文件。
   两条编译警告已处理(不是抑制):
   - `ArchitecturesAllowed=x64` 弃用告警 → 改 **`x64os`**。🔴 **没有照 ISCC 建议改成 `x64compatible`** ——
     §2.4.1 明写 ARM64 要拒(redroid 的 x86_64 镜像 + ndk_translation 只在 x86_64 上成立),
     而 `x64compatible` 会把「能跑 x64 模拟的 ARM64」放进来,正是必须拒绝的那一类;`x64os` 才是「真 x64 操作系统」。
   - `[UninstallRun]` 缺 `RunOnceId` → 补 `RunOnceId: "QTradeUninstallEngine"`。
6. **尚未真跑过产出的 EXE**(禁区);它只做过 `7z l` / `7z t` 的结构与完整性校验。
7. **🔴 已修:2 GB 以上的归档拼不进 EXE(2026-09-20 首次真载荷出包时踩到)**。
   原来的拼接写 `$bytes = [IO.File]::ReadAllBytes($part)`,而 PowerShell 5.1 跑在 .NET Framework 上、
   `byte[]` 长度上限是 `Int32.MaxValue`(2 GiB)。真载荷的 `payload.7z` 是 **2,203,995,915 字节**,
   于是最后一步抛「该文件太长。此操作当前仅限于支持大小小于 2 GB 的文件。」——
   **存根和 SFX 配置已经写进去了**,`out\QTrade-Setup-1.0.0.exe` 留下一个 ~209 KB 的残次品,
   它能双击、外观与正常包无异,只在解压时失败。已改成 `OpenRead` + `CopyTo($fs, 1MB)` 流式拼接。
   ⚠️ 这条线**以后每次出正式包都会踩**:§2.2.2 估「rootfs 3.3 GB → LZMA2 后 ~1.5 GB」的前提是
   「镜像层多为压缩前的原始文件系统」,但 `docker save` 出来的 tar 里镜像层**本身已经 gzip 压过**,
   LZMA2 再压几乎没收益(实测 rootfs.tar 2.16 GB 进归档仍 2.0 GB 出头)。
   **§2.2.2 的估算表建议按实测更正。**
   教训:这一类缺陷 90 条 Pester + 95 条 pytest 都覆盖不到 —— 它们手里没有 2 GB 的文件。
8. **🔴 已修:`manifest.rootfs_contents` 之前恒为空**(同日一并发现)。
   `collect-payload.ps1` 原来写 `rootfs_contents = [ordered]@{}` 加一句「CI 在此登记同一份」的注释,
   但**没有任何代码真去登记**,于是出的每个包这一项都是 `{}`。后果是安装期 `IMAGES_LOADED` 那步
   「按 `rootfs_contents` 在发行版内复核(`sha256sum` 各路径 + `docker images --digests` 比对)」
   **什么都没校验** —— 换过的 redroid 镜像、被替换的 adb 都能一路装完不报错,
   而这正是 G-10 要防的(§2.2.1 末段)。
   现在 `collect-payload.ps1` 从 `<SourceRoot>\rootfs\out\contents.json`
   (`installer/rootfs/build-rootfs.sh` 落的,也可用 `QT_SRC_ROOTFS_CONTENTS` 指定)
   **原样读入、一个字段都不改写**,以满足「两处逐字一致」;收了 `rootfs.tar` 却找不到清单时,
   不带 `-AllowMissing` 直接 throw。已实测 `manifest.rootfs_contents` 与发行版内
   `/etc/qtrade/contents.json` **逐字相等**。


---

## 8. 🔴 SFX 存根到底认什么 —— 实测结论(第四批)

出包的**头号风险**是选错存根。下面每条都有证据,不是文档推断。

### 8.1 官方 7-Zip SfxSetup(LZMA SDK 的 `7zSD.sfx`)

我们用的这一份:`sha256 436be3c4bada675a682802929384b548100f710b3ceaa87c9b2c7150963346b8`(126,976 字节,版权串 `Copyright (c) 1999-2023 Igor Pavlov`)。

| 事实 | 证据 |
|---|---|
| **只认 7 个键**:`Title / BeginPrompt / Progress / Directory / RunProgram / ExecuteFile / ExecuteParameters` | ①`SfxSetup.cpp` 里只对这 7 个名字调 `GetTextConfigValue`/`FindTextConfigItem`;②对二进制做 `strings` 也只有这 7 个 |
| **`InstallPath` 无效**(那是 7zsfxmm 的键,被**静默忽略**) | 哑 EXE 实测:配置写 `InstallPath="%ProgramData%\QTrade"`,运行后 `%ProgramData%\QTrade` **根本没被创建** |
| **解压到 `%TEMP%\7zS<随机>\`** | 哑 EXE 里的 `hello.cmd` 打印 `CWD=C:\Users\…\Temp\7zSC3753E36` |
| **跑完即删整个临时目录** | `SfxSetup.cpp` 的 `CTempDir` 析构 `~CTempDir(){ Remove(); }`;实测运行后 `7zS*` 已不存在 |
| ✅ **`RunProgram` 会被拉起,相对路径基准 = 临时目录根** | `SELF=…\7zSC3753E36\install\engine\hello.cmd` |
| ✅ **命令行参数原样透传给 RunProgram** | `ARGS=/QT_MODE=install /PROBE=1` |
| 🔴 **退出码不透传,恒 0** | `SfxSetup.cpp` 末尾 `WaitForSingleObject(...); return 0;`(**根本不读子进程退出码**);实测 `hello.cmd` 退 26,SFX EXE 退 **0** |
| **没有嵌 `RT_MANIFEST`** | `FindResource(h, 1, RT_MANIFEST)` 直接查 = 空。于是提权靠 UAC 的「安装程序检测」启发式(未签名 + 版本信息含 `Setup`);非提权上下文里 `CreateProcess` 直接 `ERROR_ELEVATION_REQUIRED` |
| **动态链接 `MSVCRT.dll`**(不是 `-MT` 静态) | `strings` 只见 `MSVCRT.dll`。⚠️ **这不是部署风险**:`MSVCRT.dll` 是 **Windows 自带**的旧版 CRT(System32,NT4 以来每台机器都有),**不是** VC++ 可再发行运行库。真正要怕的是 `vcruntime*`/`msvcp*`/`api-ms-win-crt*`,官方存根**没有**导入它们。自编存根是 `-MT`,两类都没有 |

### 8.2 第三方 7zsfxmm(`chrislake/7zsfxmm` 1.7.1.3901,LGPL-3.0,2017)

支持 `InstallPath` 等 19 个键(strings 可见),实测确实能解压到指定目录并**留存**。但:

- **两次哑实验都没跑通**:一次挂住(等一个没人点的对话框)、一次退 8、一次挂住且只建了空目录树。要调通得反复试错。
- **2017 年的件,8 年未更新,无签名**。§2.1 已点名「SFX 存根在某些 EDR 里有历史误报」——第三方改版的误报面只会更大,而目标机是行内/公司电脑,多有 AppLocker/EDR。
- 退出码是否透传**没测到**(连基本流程都没跑通)。

### 8.3 我们的选择:**自编存根 `QTradeSD.sfx`**(官方存根仅应急回退)

> 本节在第五/六批被推翻过一次。原先的结论是「官方存根 + `precheck-disk.cmd` 搬运」——
> 那是在**还没有编译器**的前提下能做到的最好;安琳随后拍板重编存根(R6-58 cm)。
> 两条路都保留,但**正路是自编存根**。

**正路:自编存根**(`installer/sfx-stub/`,基于官方 LZMA SDK 打三处补丁)

1. 解压到 `%ProgramData%\QTrade` 并**留存**(`InstallPath`);
2. 解压**前**判空间,不足退 **26**,一个字节都不解;
3. **退出码原样透传** —— §8b 的验收判据几乎全靠它;
4. 嵌显式清单声明 `requireAdministrator`,提权是**声明的**不是猜的。

**应急回退:官方存根 + `precheck-disk.cmd` 搬运**(`-Stub official`)

只在**自编存根被 EDR/AppLocker 拦、又来不及重签**时用。它的三个代价:

| 代价 | 后果 |
|---|---|
| 不认 `InstallPath` | 解压位置要由 `precheck-disk.cmd` 搬运来补(同卷 `move` 是改名,O(1);跨卷退化成真拷贝,门槛抬到 ≈2 倍 12 GB 并提示) |
| **退出码恒 0** | 🔴 **自动化/验收不能把 SFX EXE 的退出码当判据**。读 `%ProgramData%\QTrade\logs\last-exit-code.txt` 或 `install_state.json` 的 `state` |
| 没有清单 + 动态链接 `MSVCRT.dll` | 提权只能靠 UAC 启发式(见 §8.1)。⚠️ `MSVCRT.dll` 是 **Windows 自带**的旧版 CRT,**不构成部署风险** —— 不要把它当成「目标机要装 VC++ 运行库」 |

`build.ps1` 走回退路径时会**显式打警告**(R6-58 cm 要求「不许静默降级」)。

### 8.4 搬运逻辑的两个坑(都踩过,都做成了门)

- **自举陷阱**:脚本住在 `<src>\install\engine\`。把 `install` 整个 `MOVE` 走之后,cmd.exe 读不到脚本文件了,后面每个 `CALL` 都死在「The system cannot find the batch label specified」,而且停在**半搬运**状态。
  → 现在 `install` 用**复制**(很小,而且临时目录反正会被存根删),其余顶层项才 `MOVE`。
- **`.cmd` 必须 CRLF**:LF-only 会让 `for`/`if`/`call`/标签解析错乱,症状是「`) was unexpected at this time`」,**只有真跑才暴露**。
  → `build.ps1` 的 **G1b** 门逐个校验 `.cmd` 的 CRLF / 无 BOM / 可执行行纯 ASCII。

### 8.5 怎么复现这些实验(不碰生产目录)

生产脚本里**没有**任何「改安装目标」的开关(那既是攻击面,也迟早被误用)。自验的办法是**生成副本**:

```bash
# 把目标行替换成临时目录,再拿副本去跑
python3 - <<'EOF'
import io
src = io.open('engine/precheck-disk.cmd','rb').read().decode('utf-8')
out = src.replace(r'set "QT_DST=%ProgramData%\QTrade"', r'set "QT_DST=C:\Temp\qt-stage-probe"')
io.open('/tmp/precheck-probe.cmd','wb').write(out.encode('utf-8'))
EOF
```

⚠️ **别用环境变量做这件事**:它会在「WSL → powershell → Start-Process → SFX → cmd」这条链上悄悄丢掉。
丢了之后脚本会回落到默认目标,**把哑载荷真的搬进 `%ProgramData%\QTrade`**,而那里的 ACL 不给 `Users` 删除权限 —— 非管理员清不掉。这个坑我踩过,清理要管理员权限。

---

## 🔴 须真机验证清单(M0 验收)

下面这些**在开发机上验不了或验不全**,必须在真机/VM 上补。
每一条都写明了「为什么本机验不了」,免得下一个人以为已经验过。

| # | 要验什么 | 为什么本机验不了 | 怎么验 |
|---|---|---|---|
| 1 | **SFX 解压前「空间不足」分支**:目标卷可用 < `max(6 GiB, 解包总大小 × 1.1)` 时**一个字节都不解**、目标目录也不建、退 **26** | 判据要求可用空间低于阈值,而开发机 C:/D: 都有 280 GB+;要触发得造一个**声明解包 ≈ 260 GiB** 的哑载荷,不现实 | **小盘 VM**(系统盘留 < 6 GB 可用),跑一次完整包,断言 `%ERRORLEVEL% == 26` 且 `%ProgramData%\QTrade` **不存在** |
| 2 | 自编存根的 `requireAdministrator` 在**非管理员账户**下弹 UAC、拒绝后退出码合理 | 开发机当前账户在 Administrators 组里,走的是「同意提升」那条路 | 建一个标准用户账户跑一次 |
| 3 | 官方存根回退路径端到端(搬运 + `last-exit-code.txt` + UAC 启发式真的触发) | 回退路径只在自编存根被 EDR/AppLocker 拦时才走,本机没有那个环境;且它**没有清单**,提权是否触发取决于目标机的 `EnableInstallerDetection` 策略 | 在装了 EDR 的测试机上,或手工 `-Stub official` 出包后跑;顺带确认**干净系统**(未装任何 VC++ 运行库)上也能起来 —— 它只依赖系统自带的 `MSVCRT.dll`,预期能起 |
| 4 | 代码签名后 UAC 行为(§2.2.3) | 本机没有签名证书 | CI 签完名后,在干净 VM 上确认不再弹「未知发布者」 |
| 5 | 磁盘峰值(§2.1「解压 + 载荷」两份共存) | ~~轻量验证包只有 1.7 MB,量级差太远~~ **真载荷包已出(2.2 GB)**,量级到位了 | 在小盘 VM 上装一次,量「解压 + 载荷」两份共存时的峰值,对照 §2.1 与预检门槛(硬 16 GB / 建议 20 GB) |
| 6 | **设备身份档案库的机型指纹能不能骗过企点** | `installer/rootfs/profiles/device_profiles.json` 的 32 条模板,机型参数(`build_id` / `fingerprint` / `incremental`)是**按公开市售机型资料编写的,不是从真机 dump 出来的**;企点对机型指纹的校验强度**从没验过**,而开发机上既没有 redroid 容器也没有企点账号 | 用库里任取一条档案起一个 redroid 容器,`adb shell getprop` 比对 `ro.product.*` / `ro.build.fingerprint` / `ro.serialno` 与表值一致,再**跑一次企点真实登录**;若登录被风控拦或要求额外验证,说明指纹强度不够,档案库要改用真机 dump 的参数 |
| 7 | **WinAgent 的两条测试在真 Windows 上必然失败**(2026-09-21 实测) | `winagent/tests/test_audit_log_installer.py` 的 `test_expand_falls_back_under_root_when_env_var_unexpandable` 与 `test_dev_assembly_keeps_everything_under_root` 断言 `expand('%ProgramData%\\…', root)` 必须 `startswith(root)`,而那是**非 Windows 的回退行为**(测试 docstring 自己写着「`%ProgramData%` 在非 Windows 上展不开」)。真机上它能展开成 `C:\ProgramData\QTrade\winagent\vault` —— 这才是期望行为。Linux 上 307 条全绿看不到这一类 | 那两条测试按平台分叉:非 Windows 维持现断言;Windows 断言 `out == os.path.join(os.environ['ProgramData'],'QTrade','winagent','vault')`,两边都保留真正的不变量 `not os.path.exists('%ProgramData%')`(cwd 不留垃圾,实测 Windows 上也满足)。顺带 `winagent/build/build.ps1` 第 2 步要装 `[windows,dev]` 而不只是 `[windows]`,否则第 3 步必炸 `No module named pytest` |

> 1 的**规则**部分已在本机验过:`sfx-stub/verify-space-rule.ps1` 把
> `ExtractEngine.cpp` 里那段判定**原样抽出来**(不是抄一份)、用同一个 cl.exe
> 编译成小程序跑边界断言,全过。缺的只是「真机上真的触发一次」。
