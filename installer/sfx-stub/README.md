# installer/sfx-stub —— 自编 SFX 存根

QTrade 安装包的**外壳**(两段式的外层,docs/03 §2.1)。
这里放的是**补丁与构建脚本**,不放第三方源码 —— 源码由 `fetch-sdk.ps1` 按 sha256 取回。

---

## 1. 为什么要自己编

第四批用哑 EXE 把官方 `7zSD.sfx`(LZMA SDK 的 SFXSetup)的行为实测坐实了三条:

| 事实 | 后果 |
|---|---|
| 只认 7 个配置键:`Title / BeginPrompt / Progress / Directory / RunProgram / ExecuteFile / ExecuteParameters` | `InstallPath` 是第三方 7zsfxmm 的键,官方存根**静默忽略** |
| 解压到 `%TEMP%\7zS<随机>\`,`RunProgram` 跑完**把整个临时目录删掉** | 而 §2.1 要求解到 `%ProgramData%\QTrade` 并**留存**(续跑/升级/修复/卸载都要它) |
| 退出码**不透传**:源码末尾硬编码 `return 0` | 🔴 §8b 的验收判据几乎全是退出码 —— 不透传等于「全 0 = 全部看起来成功」,整套 M0/M1 验收失真 |

第四批的应对是方案 (B):保留官方存根,让 `precheck-disk.cmd` 在临时目录里把载荷**搬**到
`%ProgramData%\QTrade`。那条路能出包,但第三条(退出码)在外层根本解决不了。

安琳 2026-09-20 拍板:**重编存根**。于是有了这个目录。

---

## 2. 许可与出处

| 项 | 值 |
|---|---|
| 源码 | 官方 **LZMA SDK 23.01**(`lzma2301.7z`,https://www.7-zip.org/a/lzma2301.7z) |
| sha256 | `317DD834D6BBFD95433488B832E823CD3D4D420101436422C03AF88507DD1370` |
| 许可 | **公有领域(public domain)** |

> LZMA SDK is written and placed in the public domain by Igor Pavlov.
> Anyone is free to copy, modify, publish, use, compile, sell, or distribute the
> original LZMA SDK code, either in source code form or as a compiled binary, for
> any purpose, commercial or non-commercial, and by any means.
>
> —— `DOC/lzma-sdk.txt`,LICENSE 段

公有领域意味着**修改并分发二进制不需要开源、不需要署名**。我们仍然在这里写明出处与改动,
理由不是许可要求,而是:两年后有人拿到 `QTradeSD.sfx`,得能知道它是什么、怎么复现出来。

仓库里只存 `qtrade-sfx.patch` 与构建脚本;源码不入库(几千个文件、与本仓库无关)。

---

## 3. 补丁改了什么(只有两处)

`qtrade-sfx.patch` 只动 `CPP/7zip/Bundles/SFXSetup/` 下的三个文件,
**绝不碰 SDK 的公共代码** —— 否则下次升 SDK 版本就是一场灾难。

### (a) 新增配置键 `InstallPath`

* 值里支持 `%ProgramData%` 这类环境变量(`ExpandEnvironmentStringsW` 展开);
  展开结果必须是 `<盘符>:\...` 形式的**绝对路径**(用 SDK 自带的 `NName::IsDrivePath` 判),
  否则判配置错 —— 相对路径会落在「谁双击、从哪个壳启动」决定的位置,不可预期。
* **有**此键:解压到该目录并**留存**(压根不创建临时目录,`CTempDir` 保持未 `Create` 状态,
  它的析构 `Remove()` 便是安全的无操作)。
* **无**此键:与官方存根**逐字同行为**(临时目录 + 跑完即删)。
* 解压**前**判目标卷可用空间,门槛 **max(6 GiB, 解包总大小 × 1.1)**:
  * 6 GiB 来自 §2.1 的硬下限;× 1.1 是解压期余量(目录项、簇对齐、引擎随后要落的少量文件)。
  * 不足则**一个字节都不解**、目标目录也不建,退 **26**(`E_INSTALL_DISK_LOW`)。
  * 判据插在归档 `Open2()` 之后、`CreateComplexDir()` 之前 —— 那一刻已经能读到每项的
    解包大小,而还没有任何东西被写出来。
  * 🔴 **这个判据只在走 `InstallPath` 时启用**(`ExtractArchive` 的 `checkSpace` 形参)。
    无此键的普通 SFX 包不该凭空多出一个 6 GiB 门槛。
  * 取不到可用空间时**放行**(fail-open):判据是保护,不该变成新的失败源。
    已知代价:`InstallPath` 指向未挂载盘符时会放行,随后建目录失败,退的是 1 而不是 26。

> 这同时收回了 §2.1「解压前判 6 GB」的偏差 —— 第四批因为官方存根没有 pre-extract 钩子,
> 只能把判断退到「引擎跑之前」;现在存根自己做,真的是**解压前**。

### (b) 透传子进程退出码

`WaitForSingleObject` 之后加 `GetExitCodeProcess`,以子进程退出码作为自身退出码。
`exitCode` 初值取 **1 而不是 0** —— 拿不到进程句柄时也不能退 0,
否则等于在专门消灭「假 0」的这个补丁里又留了一条假 0。
启动失败的几条路径沿用原本的 `return 1`,不动。

### 附带的两处小改(都是评审提出、成本极低)

* 子进程起来后,把**存根自身**的当前目录挪到系统目录。进程的当前目录会钉住该目录
  (句柄不含 `DELETE` 共享权限),存根若一直待在 `%ProgramData%\QTrade`,
  引擎给安装根改名或回滚删除时会拿到 `ERROR_SHARING_VIOLATION`。
  原版目标是临时目录、跑完即删,从来碰不上这件事。
* 缓冲区长度只写一处(`Z7_ARRAY_SIZE`),避免「改一处忘另一处」变成栈溢出。

### 🔴 源码必须带 UTF-8 BOM

补丁往源码里加了**中文注释**。没有 BOM 时 `cl.exe` 只能按系统 ANSI 代码页解析:

* ACP = 936(中文 Windows):能编,注释显示为乱码;
* ACP ≠ 936(英文 Windows、多数 CI 容器):UTF-8 续字节里的
  `0x81/0x8D/0x8F/0x90/0x9D` 落在 CP1252 的**未定义码位**,每一处触发一条 **C4819**,
  而 7-Zip 的 `CFLAGS` 带 **`-WX`**(告警即错误)=> **必然编译失败**。

`fetch-sdk.ps1` 的补丁应用器写出来的就是带 BOM 的,`build.ps1` 再验一道。
**不要改用 GNU `patch` / `git apply`** —— 它们不加 BOM,而这个坑只在某些机器上炸。

(字符串**字面量**里的中文另用 `\uXXXX` 通用字符名写,与代码页无关。)

---

## 4. 怎么用

```powershell
cd installer\sfx-stub

# 1) 取源码 + 打补丁(WSL2 环境下走 Windows 侧代理)
.\fetch-sdk.ps1 -Proxy http://172.19.176.1:7890
#    已经有归档就不联网: .\fetch-sdk.ps1 -ArchivePath D:\dl\lzma2301.7z

# 2) 只看工具链够不够
.\build.ps1 -ProbeOnly

# 3) 真编 -> ..\build\QTradeSD.sfx
.\build.ps1
```

### 工具链要求

`build.ps1` 用 `vswhere` 找**带 C++ 工具集**的 VS / Build Tools 实例
(组件 `Microsoft.VisualStudio.Component.VC.Tools.x86.x64`),再调 `vcvars32.bat` + `nmake`。

产物是 **x86、静态 CRT(`-MT`)** —— `-MT` 本来就是 7-Zip `Build.mak` 的默认
(只有定义了 `MY_DYNAMIC_LINK` 才会变成 `-MD`),所以不需要额外改构建参数。
x86 是为了任何 Windows 都能跑这个外壳(§2.4.1 的架构判断由内层引擎做)。

🔴 **缺工具链时 `build.ps1` 只报缺、不代装**,以退出码 2 结束。

### 产物自检

落地前 `Test-QtSfxStubBinary` 做三项自证,任何一项不过就不落地:

1. PE 机器类型 = x86(`0x014C`);
2. 二进制里出现字符串 `InstallPath` —— 证明补丁 (a) 真编进去了;
3. 没有 `msvcr*` / `vcruntime*` / `api-ms-win-crt*` 的导入 —— 证明是静态 CRT。

第 2 条防的是最容易犯的错:**把官方存根改名成 `QTradeSD.sfx`**。
那样出包脚本的 G5 会按 8 键白名单放行 `InstallPath`,而存根其实不认,
载荷照样解到 `%TEMP%` 跑完即删。`build/build.ps1` 在缝存根之前也会跑这一检查。

---

## 5. 两条出包路径

`installer/build/build.ps1` 同时保留两条,`-Stub auto`(缺省)优先用自编存根:

| | `-Stub qtrade` | `-Stub official` |
|---|---|---|
| 存根 | `build/QTradeSD.sfx`(本目录出) | `build/7zSD.sfx`(官方) |
| 配置 | `build/sfx-config-qtrade.txt` | `build/sfx-config.txt` |
| 认的键 | 官方 7 个 **+ `InstallPath`** | 官方 7 个 |
| 链首 | `engine/run-engine.cmd`(只拉引擎 + 落盘退出码) | `engine/precheck-disk.cmd`(判空间 + 搬运 + 拉引擎) |
| 解压位置 | 直接 `%ProgramData%\QTrade`,留存 | `%TEMP%`,由链首搬到 `%ProgramData%\QTrade` |
| 解压前判空间 | ✅ 存根做,不足退 26 | ❌ 没有钩子,最早只能在引擎前判 |
| 退出码 | ✅ 透传 | ❌ 恒 0,只能读 `<目标>\logs\last-exit-code.txt` |

🔴 **存根 / 配置 / 链首三者必须配套**。配错的后果不是报错,是**静默跑偏**。
`build.ps1` 的 G5 门按存根切换白名单,并校验配置里的 `RunProgram`
正是它待会儿要塞进载荷的那个链首脚本 —— 三条阴性分支都验证过会红。

### 配 `InstallPath` 时 `RunProgram` 必须写相对路径

存根会先 `SetCurrentDir(<InstallPath>)` 再启动,而启动命令是
`dirPrefix + appLaunched` 拼出来的(`dirPrefix` 缺省是 `".\"`)。
若 `RunProgram` 写成绝对路径(或用 `%%T` 展开成绝对路径),会被拼成
`.\C:\...\xxx.cmd` 而启动失败 —— **这是原版就有的行为**,不是补丁引入的。

---

## 6. 目录内容

| 文件 | 说明 |
|---|---|
| `fetch-sdk.ps1` | 下载 → **先校验 sha256 再解压** → 解出 `C/` `CPP/` → 打补丁 |
| `qtrade-sfx.patch` | 两处改动的 unified diff(+221 / −15,多数是中文注释) |
| `build.ps1` | 探工具链 → 验补丁标记与 BOM → `vcvars32` + `nmake` → 验货 → 落 `../build/QTradeSD.sfx` |
| `QTrade.SfxStub.psm1` | 上面两个脚本的实现(取源/打补丁/探工具链/验货),单测在 `tests/QTrade.SfxStub.Tests.ps1` |
| `src/` | 第三方源码,**gitignore** |
| `work/` | 归档与中间产物,**gitignore** |

### 关于那个自己写的补丁应用器

本机 Windows 侧**没有** `git.exe`,也没有 `patch.exe`(实测),而 fetch/build 全在 Windows 上跑。
所以 `Invoke-QtUnifiedDiff` 自己解析 unified diff。它是**严格**的:

* 上下文差一个字符就抛,**绝不**像 GNU `patch` 那样「偏移 N 行后找到了」就悄悄应用;
* 重复应用会抛(不会悄悄打第二遍);
* 输出一律 CRLF + UTF-8 with BOM。

正确性不是靠自说自话:测试里让它和 GNU `patch` 对同一棵源码树各打一遍,
**逐字节比对**(仅差那个有意加的 BOM),三个文件全部一致。
