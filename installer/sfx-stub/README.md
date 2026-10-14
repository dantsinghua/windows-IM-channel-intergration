# installer/sfx-stub：独立随机暂存的自编 SFX

本目录维护补丁与构建脚本，SDK 源码按固定 SHA256 下载，不提交第三方源码。现行合同为 [03 §2.1](../../docs/03-安装引导与自动化配置.md) 与 R6-102；旧“直接覆盖 ProgramData/QTrade”和“官方存根应急搬运回退”均退役。

## 1. 当前行为

- 正式配置的 `InstallPath` 只能是 `%ProgramData%\QTrade-Staging`；实际 ProgramData 由 `SHGetFolderPathW(CSIDL_COMMON_APPDATA)` 取得，不能用进程环境伪造。
- 检查父目录各级 reparse、所有者与 DACL，保护暂存父目录；每次以新 GUID 建独占目录，不复用同版本暂存、不覆盖旧正式安装。
- 解压前仍执行原空间门 `max(6 GiB, 归档解包总大小 × 1.1)`，不足退 26；预检通过不等于后续 stage/live/VHDX 总峰值足够。
- 从 stage 切换当前目录后执行使用相对路径的 `RunProgram`，正式链首为 `install/engine/run-engine.cmd`；引擎退出码原样透传。成功、失败都保留 stage 供诊断，本版不自动递归删除。
- 保留显式 requireAdministrator 清单、系统级 DPI 声明、真实子进程等待和退出码处理。旧无 InstallPath 的 SDK 行为不是本版可交付路径。
- 源码生成时保持 UTF-8 BOM，原构建 Parser/ASCII/编码、原生编译与存根身份门不能放宽。

外壳只负责独立暂存和启动；确认清空、核归属、停机、复制复核与 fresh install 全由引擎执行。外壳不得绕过确认直接覆盖 live。

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

## 3. 构建与验货

`fetch-sdk.ps1` 取固定 SDK 并应用当前 `qtrade-sfx.patch`，构建入口和原存根验货工具保留；需 MSVC C++ 与 Windows SDK。构建脚本通过 vswhere 定位 C++ 工具集，再经 `vcvars32.bat` 与 nmake 构建 x86 存根，使用静态 CRT（`-MT`），不要求目标机安装 MSVC 运行库。SDK 下载 hash、补丁 hash、编译器版本、产物 hash 与 manifest 资源结果应随本次候选记录。

构建输出仅是候选存根；总装通过显式 SfxStubPath 使用它。`build.ps1` 已拒绝 official，auto 不再回退；不能用旧 `installer/build/QTradeSD.sfx` 因同名而冒充新版。新补丁与旧“只有两处改动”的说明不再对应，按当前 patch 实物审查。

## 4. 已知缺口与验证范围

本轮新存根已由责任人完成 native 编译、静态身份/manifest 检查及 fake Win32 动态用例；没有真实提权解压/启动引擎/目标机 ACL 验收。旧 2026-09-20 哑 EXE 的退出码实测属于旧存根，不外推随机暂存能力；细节保留在历史交接。本批文档没有执行产物。

整体发布现因新 MQ STS 认证前置要求暂停；状态和未验证范围见 [本轮交接](../../docs/handoffs/2026-10-13-portable-installer.md)。
