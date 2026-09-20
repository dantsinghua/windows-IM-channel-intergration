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

**这一条命令按顺序做了**:四道门(编码 / 语法 / 防火墙红线 / 微信 `/S` 红线)→ ISCC 编译引擎 →
收集载荷并生成 `manifest.json`(含两条硬校验)→ 7z 两块归档 → 归档 ↔ manifest 路径复核 → 拼 SFX。
任何一步不过都会**中止并打印中文原因**,不会出半成品。

出包之后还差**签名**(§2.2.3,本脚本不做,见第 5 节):

```powershell
signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /a ..\out\QTrade-Setup-1.0.0.exe
```

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

> 三样都没有也能验管线:`.\build.ps1 -SelfCheck -AllowMissing`(只跑四道门 + 生成 manifest),
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
| **signtool** | Windows SDK | — | 包没签名;目标机多有 AppLocker/EDR,未签名大概率被拦 |

> `build.ps1` **不会替你装任何工具**:缺件只报缺、给获取方式、列出它找过哪些位置。

---

## 3. build.ps1 的几种跑法

```powershell
.\build.ps1 -Version 1.0.0 -SourceRoot <根>     # 完整包(要 ISCC + 7z + 7zSD.sfx + 全部载荷)
.\build.ps1 -Version 1.0.0 -AllowMissing        # 轻量验证包(缺件占位,只验管线)
.\build.ps1 -SelfCheck -SourceRoot <根>         # 四道门 + 收载荷 + 生成 manifest(不需要任何打包工具)
.\build.ps1 -CheckOnly                          # 只跑四道门
.\build.ps1 -Version 1.0.0 -IsccPath 'D:\Inno Setup 6\ISCC.exe' -SevenZipPath 'D:\7-Zip\7z.exe' -SfxStubPath 'D:\7zSD.sfx'
```

四道门(任何模式都先跑):

1. **G1 脚本编码** —— 全部 `.ps1`/`.psm1` 必须 UTF-8 **with BOM**。
   🔴 Windows PowerShell 5.1 把无 BOM 的 UTF-8 脚本按系统 ANSI(中文机 = GBK)解析,中文注释里的字节会被当成引号 → `ParserError`,而且**报错本身也是乱码**(§2.6.7 W1;验收 M0-10)。
2. **G2 语法解析** —— `[Parser]::ParseFile` 逐个过。
3. **G3 防火墙红线** —— 随包脚本里零处 `netsh advfirewall` / `New-NetFirewallRule`(§6;验收 M1-14)。
4. **G4 微信卸载红线** —— 零处以 `/S` 运行微信 `Uninstall.exe`。🔴 `/S` = **卸载并清空聊天记录与登录态**(§2.9.3 事实 1)。

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
| **控制台** | 在 `console/` 跑 `npm run build:dir`(= `electron-builder --dir`)。`electron-builder.yml` 的 `directories.output: release` + `target: dir` ⇒ 产物在 `console/release/win-unpacked/`。**不出 NSIS/MSI**(§2.1 末:安装引导归 7z-SFX + Inno 引擎)。 |
| **Agent wheel** | 仓库根跑 `python -m build`(或 `pip wheel . -w dist`),出 `dist/qtrade_agent-*.whl`。真值登记在 `manifest.rootfs_contents.agent_wheel`,由 rootfs 构建写进发行版的 `/etc/qtrade/contents.json`,**两处必须逐字一致**(G-10)。 |
| **内核 / rootfs / kcheck** | 由 `qtrade-redroid-installer` 那套产出(原机在 WSL 里的 `~/work/qtrade-redroid-installer/{kernel,rootfs}/`)。🔴 那些路径**在 WSL 里,Windows 侧打包机看不到** —— 用 `-SourceRoot` 或 `QT_SRC_KERNEL` / `QT_SRC_ROOTFS` / `QT_SRC_KCHECK` 指到打包机能访问的位置。 |
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

## 5. 签名(§2.2.3,本脚本不做)

外壳 EXE、引擎 EXE、`qtrade-winagent-svc.exe`、Electron 主程序、**以及全部 `.ps1`** 用同一张代码签名证书(**OV 起步**,A-4)签名,SHA-256 + RFC 3161 时间戳:

```powershell
signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /a <file>
```

- 载荷内的 ps1 一律 `-ExecutionPolicy Bypass -File` 调用,同时**做 Authenticode 签名**,AllSigned 策略的企业机也能跑。
- 单文件签名覆盖整个 EXE(含归档)——**改载荷必须重签**。
- SmartScreen:OV 新签名初期必然弹「Windows 已保护你的电脑」,靠下载量累积信誉;目标机多有 AppLocker/EDR,**未签名大概率直接被拦**。

---

## 6. 单测与对账

```powershell
# PowerShell 模块单测(Pester 5)
..\tests\run-pester.ps1 -Bootstrap

# 规格逐字对账(退出码表 / /QT_* 开关 / 状态机键名 / 受管键表 / BOM / 红线 / 派发器覆盖)
~/.venvs/qtrade/bin/python -m pytest -q installer/tests
```

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

### 8.2 第三方 7zsfxmm(`chrislake/7zsfxmm` 1.7.1.3901,LGPL-3.0,2017)

支持 `InstallPath` 等 19 个键(strings 可见),实测确实能解压到指定目录并**留存**。但:

- **两次哑实验都没跑通**:一次挂住(等一个没人点的对话框)、一次退 8、一次挂住且只建了空目录树。要调通得反复试错。
- **2017 年的件,8 年未更新,无签名**。§2.1 已点名「SFX 存根在某些 EDR 里有历史误报」——第三方改版的误报面只会更大,而目标机是行内/公司电脑,多有 AppLocker/EDR。
- 退出码是否透传**没测到**(连基本流程都没跑通)。

### 8.3 我们的选择:**官方存根 + `precheck-disk.cmd` 搬运**

理由:
1. **存根用原厂件**,EDR/AppLocker 面最小 —— 这是 §2.1 反复强调的现场约束。
2. **解压位置**在 `precheck-disk.cmd` 里解决:同卷 `move` 是元数据改名,**O(1)、不额外占空间**(`%TEMP%` 与 `%ProgramData%` 默认同在系统盘);跨卷才退化成真拷贝,那时门槛抬到 ≈2 倍(12 GB)并提示。
3. **退出码透传做不到** —— 这条外层解决不了,已提交裁决(见 handoff)。当前兜底:引擎退出码**落盘**到 `%ProgramData%\QTrade\logs\last-exit-code.txt`。

**🔴 自动化/验收不要把 SFX EXE 的退出码当判据 —— 它恒为 0。** 读 `last-exit-code.txt` 或 `install_state.json` 的 `state`。

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
| 3 | 官方存根回退路径端到端(搬运 + `last-exit-code.txt`) | 回退路径只在自编存根被 EDR/AppLocker 拦时才走,本机没有那个环境 | 在装了 EDR 的测试机上,或手工 `-Stub official` 出包后跑 |
| 4 | 代码签名后 UAC 行为(§2.2.3) | 本机没有签名证书 | CI 签完名后,在干净 VM 上确认不再弹「未知发布者」 |
| 5 | 磁盘峰值(§2.1「解压 + 载荷」两份共存) | 轻量验证包只有 1.7 MB,量级差太远 | 真载荷出包后在小盘 VM 上量峰值 |

> 1 的**规则**部分已在本机验过:`sfx-stub/verify-space-rule.ps1` 把
> `ExtractEngine.cpp` 里那段判定**原样抽出来**(不是抄一份)、用同一个 cl.exe
> 编译成小程序跑边界断言,全过。缺的只是「真机上真的触发一次」。
