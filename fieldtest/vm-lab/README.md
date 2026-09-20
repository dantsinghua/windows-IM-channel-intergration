# QTrade 测试虚拟机实验室（fieldtest/vm-lab）

给安琳的一套脚本：在这台本机上开一台 Hyper-V 虚拟机，专门用来反复验 QTrade 安装器
——**装一遍、看状态机、卸一遍、看还原干净没有，再一键回到干净起点重来**。

> 🔴 **本目录的脚本没有一条会偷偷动你的机器。**
> 每个会改动机器的脚本都先把**将要执行的每条命令打印出来**，要你亲手输入 `YES` 才继续；
> 所有脚本都带 `-WhatIfOnly`，加上它就只打印计划、什么都不做。
> **没有任何一个脚本会自动重启你的电脑。**

---

## 一、这套东西要解决什么问题

QTrade 安装器会对一台 Windows 做很重的整机级改动：装 WSL、**换掉 WSL 的内核**、注册系统服务、
写 `%ProgramData%`、改 `.wslconfig`、写防火墙规则和 hosts 行。这些东西一旦装上，
「卸载是不是真的还原干净了」只能在**一台可以随便糟蹋、还能秒回到出厂状态**的机器上验。

虚拟机 + 基线检查点正好干这个：回滚一次十几秒，比重装系统快两个数量级。

**这台虚拟机主要验四件事**（见「已知限制」一节，它验不了全部）：

1. 安装器**状态机**（`PRECHECK → … → SELFTEST_OK`，每步的停车、续跑、退出码）
2. **内核切换**（`.wslconfig` 合并、`wsl --shutdown` 确认、验证失败自动回滚）
3. **服务注册**（`QTradeWinAgent` 服务、计划任务、防火墙规则）
4. **卸载还原**（服务删干净没有、hosts 行删干净没有、`.wslconfig` 恢复了没有）

---

## 二、按顺序做（每一步在做什么、改了什么、怎么撤销、要多久）

### 第 0 步：只读体检 —— `00-只读体检.ps1`

| | |
|---|---|
| **做什么** | 查系统版本/SKU、Hyper-V 各功能当前状态、组件包数量、固件虚拟化、内存与磁盘余量、ISO 在不在、当前 WSL 与 docker 现状、有没有别的虚拟化软件 |
| **改动主机** | **一个字节都不改**。纯查询 |
| **怎么撤销** | 不需要 |
| **耗时** | 约 1 分钟 |
| **要重启吗** | 不要 |

```powershell
# 双击 00-只读体检.cmd 也行；想拿最准的功能状态就用管理员 PowerShell：
powershell -NoProfile -ExecutionPolicy Bypass -File .\00-只读体检.ps1
```

> 关于「固件虚拟化」那一行：主机上只要跑着 Hyper-V 监控程序（**WSL2 也算**），
> `VirtualizationFirmwareEnabled` 就常年报 `False`，**这不代表你 BIOS 里没开 VT-x**。
> 这台机器的判据以「**WSL2 现在跑得起来**」为准 —— 跑得起来，虚拟化就一定是开的。

---

### 第 1 步：启用 Hyper-V —— `01-启用HyperV.ps1` 🔴 **这一步之后要重启一次**

| | |
|---|---|
| **做什么** | 把 `%SystemRoot%\servicing\Packages` 下的 Hyper-V 组件包逐个 `DISM /Add-Package`，再 `/Enable-Feature /FeatureName:Microsoft-Hyper-V-All /All /NoRestart` |
| **改动主机** | ① 组件存储（WinSxS）增大约 1~2 GB；② 启用 Hyper-V 监控程序，重启后 Windows 自身运行在它之上；③ **自动出现一个 `vEthernet (Default Switch)` 虚拟网卡与 Windows NAT 服务**；④ 新增一批 `vm*` 系统服务 |
| **怎么撤销** | `01b-撤销HyperV.ps1`（`/Disable-Feature`），同样需要重启一次 |
| **耗时** | DISM 约 5~15 分钟；**加一次重启（3~5 分钟）** |
| **要重启吗** | 🔴 **要，而且只有这一步要**。脚本**不会**替你重启，时机完全由你挑 |

```powershell
# 先看它打算做什么（不做任何改动）：
powershell -NoProfile -ExecutionPolicy Bypass -File .\01-启用HyperV.ps1 -WhatIfOnly

# 真做（管理员 PowerShell）：会把每条命令打印出来，要你输入 YES
powershell -NoProfile -ExecutionPolicy Bypass -File .\01-启用HyperV.ps1
```

**🔴 重启会中断什么**：主机 WSL 里正在跑的**全部容器与终端**。跑 01 之前请先把手头的活停妥，
或者干脆先只跑 `-WhatIfOnly` 看一眼，等你方便的时候再真做。
**重启只是重启**：它不改你的 WSL 配置、不动 `.wslconfig`、不动任何容器数据。

重启后这样验证：

```powershell
Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All | Select State  # 应为 Enabled
Get-VMSwitch                                                                          # 应看到 Default Switch
wsl -l -v                                                                             # 确认发行版都还在
```

> **⚠️ 如实说明：这是家庭版上的非官方启用方式。**
> Windows 11 家庭版官方不带 Hyper-V，「启用或关闭 Windows 功能」里也没有它的勾。
> 上面的做法是社区长期在用的办法 —— 组件包本来就躺在系统盘里，只是家庭版没把功能暴露出来。
> 但要清楚三件事：
> 1. **微软不为此提供支持**，出问题不能报官方工单；
> 2. **Windows 大版本更新后可能需要重做**（累积更新一般不受影响，功能更新有可能把它还原）；
> 3. 极少数情况下个别组件包 `/Add-Package` 会失败 —— 脚本会把失败的包列出来继续往下走，
>    只要最后的 `/Enable-Feature` 成功就没问题。

---

### 第 2 步：建测试虚拟机 —— `02-建测试虚拟机.ps1`

| | |
|---|---|
| **做什么** | 建一台第 2 代虚拟机 `QTrade-Test-Win11`，落在 `D:\HyperV\QTrade-Test\` |
| **改动主机** | 只在 `D:\HyperV\QTrade-Test\` 下多出虚拟机配置与一个虚拟磁盘；Hyper-V 里多一台虚拟机。**不新建虚拟交换机、不改主机网络、不碰 WSL** |
| **怎么撤销** | `Stop-VM -Name 'QTrade-Test-Win11' -TurnOff; Remove-VM -Name 'QTrade-Test-Win11' -Force; Remove-Item -Recurse -Force 'D:\HyperV\QTrade-Test'` |
| **耗时** | 约 10 秒 |
| **要重启吗** | 不要 |

虚拟机规格与理由：

| 项 | 值 | 为什么 |
|---|---|---|
| 代数 | 第 2 代（UEFI） | Windows 11 必需 |
| 内存 | **16 GB 静态** | 🔴 嵌套虚拟化**要求关闭动态内存** |
| 处理器 | 8 个 | 主机 24 逻辑核，留一多半给主机 |
| 磁盘 | **120 GB 动态扩展** | 初始只占几 GB，随装随涨；QTrade 的 `ext4.vhdx` 会涨到约 8 GB，加上系统与安装包，120 GB 宽裕 |
| **嵌套虚拟化** | `ExposeVirtualizationExtensions = $true` | 🔴 **这是能在虚拟机里跑 WSL2 的关键**，少了它虚拟机里装不上 WSL2，整个测试就没意义 |
| vTPM + 安全启动 | 开 | Windows 11 安装程序要检查。家庭版上万一开不出来，脚本会打印 `LabConfig` 绕过办法 |
| 网络 | **只接 Default Switch（NAT）** | 见下面的「网络为什么这么保守」 |
| 自动检查点 | 关 | 每次开机自动打快照会拖慢测试、吃磁盘 |
| 开机自启 | 关（`AutomaticStartAction Nothing`） | 主机重启时它不会自己起来抢 16 GB 内存 |

**幂等**：同名虚拟机已存在时，脚本只打印现状、不重复建、不改它。

> **🔴 网络为什么这么保守**：这台机器有过 `fse.sys`（WSL 网络特性触发）导致**全系统卡死**的历史。
> 所以本目录的脚本：**只用 Hyper-V 自带的 Default Switch**，
> **不新建外部交换机、不绑定物理网卡、不碰 WSL 的任何网络设置**。
> 脚本里连「找不到 Default Switch 就自动建一个」这种便利逻辑都**故意没写** —— 宁可报错让你看一眼。
>
> 需要如实说明的一点：**启用 Hyper-V 本身就会自动生成 `vEthernet (Default Switch)` 虚拟网卡和 WinNAT**，
> 这是绕不开的，属于第 1 步的固有改动。它不绑定物理网卡、也不改 WSL 的网络配置，
> 但它确实往主机网络栈里加了东西。若重启后主机网络出现异常，先跑 `01b-撤销HyperV.ps1` 回退。

---

### 第 2b 步：做无人值守应答 ISO —— `02b-制作应答ISO.ps1`（可选但强烈推荐）

| | |
|---|---|
| **做什么** | 把 `autounattend.xml.template` 填好参数生成 `autounattend.xml`，再打成一张几百 KB 的小 ISO |
| **改动主机** | 只在 `D:\HyperV\QTrade-Test\` 下写两个文件；用 `-ListImages` 时会**临时挂载**一次安装 ISO 读版本清单，读完立刻卸载 |
| **怎么撤销** | 删掉那两个文件即可 |
| **耗时** | 约 1 分钟 |
| **要重启吗** | 不要 |

```powershell
# ① 先看清 ISO 里有哪些版本名（会临时挂载一次 ISO）
powershell -NoProfile -ExecutionPolicy Bypass -File .\02b-制作应答ISO.ps1 -ListImages

# ② 生成应答 ISO（会问你要密码，两次确认）
powershell -NoProfile -ExecutionPolicy Bypass -File .\02b-制作应答ISO.ps1 -ImageName 'Windows 11 专业版'

# ③ 建虚拟机时把它一起挂上
powershell -NoProfile -ExecutionPolicy Bypass -File .\02-建测试虚拟机.ps1 `
    -AnswerIsoPath 'D:\HyperV\QTrade-Test\autounattend.iso'
```

应答文件干的事：全程中文、自动分区、跳过联网与微软账号、建本地管理员 `qtest`、不加入域、
关闭首次登录动画，并在首次登录后关掉休眠、放开脚本执行策略、建好 `C:\QTrade-Test\` 落点目录。

> **🔴 密码不写死进仓库**：`02b` 在运行时问你要密码，只填进生成的 `autounattend.xml`
> （落在 `D:\HyperV\QTrade-Test\`，**不在 git 仓库里**）。
> 仓库里只有 `autounattend.xml.template`，里面是 `{{...}}` 占位符。
> 密码以 base64 混淆形式写入（应答文件格式就这样）—— **base64 只是混淆、不是加密**，
> 所以**别用你真在用的密码**，给这台一次性测试虚拟机单独起一个就行。

**打 ISO 的三档退路**（脚本自动往下退，不会让你去装 Windows ADK）：
1. 有 ADK 的 `oscdimg.exe` → 用它；
2. 没 ADK → 用 **Windows 自带的 IMAPI2 刻录组件**（纯系统能力，不用装任何东西）；
3. 两条都不行 → 打印**手动装系统的完整步骤**（约 15 分钟，含 `LabConfig` 与 `BypassNRO` 的按键与命令）。

---

### 第 2c 步：装系统

```powershell
Start-VM -Name 'QTrade-Test-Win11'
vmconnect.exe localhost 'QTrade-Test-Win11'
```

> ⚠️ 开机头几秒屏幕提示 `Press any key to boot from CD or DVD...` 时**必须按一下键盘**，
> 不然它会跳过光驱、进不去安装程序。这是唯一一处必须你动手的地方。

| | |
|---|---|
| **耗时** | 无人值守约 15~25 分钟（嵌套环境比物理机慢）；手动约 15 分钟 + 若干次点击 |
| **要重启吗** | 虚拟机自己会重启几次，**主机不用重启** |

---

### 第 3 步：打基线检查点 —— `03-快照与回滚.ps1`

| | |
|---|---|
| **做什么** | 在**系统刚装好、还没装任何 QTrade 东西**的时刻，打一个名为 `clean-baseline` 的检查点 |
| **改动主机** | 只在 `D:\HyperV\QTrade-Test\` 下多一个差异盘（avhdx）；主机设置一律不动 |
| **怎么撤销** | `Remove-VMSnapshot -VMName 'QTrade-Test-Win11' -Name 'clean-baseline'` |
| **耗时** | 打基线 10~30 秒；**回滚 10~20 秒** |
| **要重启吗** | 不要 |

```powershell
.\03-快照与回滚.ps1            # 打基线（建议虚拟机处于关机状态）
.\03-快照与回滚.ps1 -List      # 看现有检查点
.\03-快照与回滚.ps1 -Restore   # 🔴 每轮安装测试【之前】跑这一句，回到干净起点
```

---

### 第 4 步：把安装包送进去 —— `04-把安装包送进虚拟机.ps1`

| | |
|---|---|
| **做什么** | 用 PowerShell Direct（走 VMBus，不需要虚拟机联网）把 `installer\out\QTrade-Setup-1.0.0.exe`（2.26 GB）与 `fieldtest\collect-evidence.ps1` 拷进 `C:\QTrade-Test\`；然后打印「静默安装 + 取回退出码与日志」的骨架命令 |
| **改动主机** | **主机只读仓库文件，不写不改**。改的是虚拟机 |
| **怎么撤销** | `.\03-快照与回滚.ps1 -Restore`，虚拟机里的一切一笔勾销 |
| **耗时** | 拷 2.26 GB 约 3~10 分钟；加 `-RunInstall` 真装一遍另算（WSL + 镜像加载，可能 20~60 分钟） |
| **要重启吗** | 主机不用。**虚拟机**在安装器返回 3010 时要重启（骨架里有对应命令） |

```powershell
.\04-把安装包送进虚拟机.ps1 -WhatIfOnly   # 只看计划
.\04-把安装包送进虚拟机.ps1               # 拷贝 + 打印骨架
.\04-把安装包送进虚拟机.ps1 -RunInstall    # 连静默安装一起跑，并把日志取回主机
```

静默安装用的开关以 `docs/03-安装引导与自动化配置.md` **§3.4** 为准，几个要点：

- **`/QT_ACCEPT_SHUTDOWN=1`** —— 内核切换那一步要 `wsl --shutdown`。
  按 §2.6.3，静默模式下**必须显式带这个开关**才执行，否则引擎停车、退出码 `10`；
  规格明确把「在命令行上显式带这个开关」算作红线 6 要求的**用户确认**。
- **`/QT_ACCEPT_REBOOT=0`（缺省）** —— 需要重启时不自动重启，而是退 `3010` 并写好 RunOnce。
  **测试时建议保持 0**，这样你能亲眼看到 3010 与重启续跑路径对不对。
- 🔴 **必须用 `Start-Process -Wait -PassThru`**：安装器是 GUI 程序，
  直接 `& $exe` 调用会**立刻返回**，拿不到真的退出码。
- 退出码有**两个来源**（§3.4）：自编存根 `QTradeSD.sfx` 会把引擎退出码原样透传成 EXE 退出码；
  走官方存根回退路径时 EXE 退出码**恒 0**，这时判据要改读
  `%ProgramData%\QTrade\logs\last-exit-code.txt`。脚本两个都读，并按这条规则判最终码。
- 脚本内置了 §3.4 的**完整退出码对照表**，拿到码会直接打印中文含义。

> `fieldtest\collect-evidence.ps1` **目前仓库里还没有**（由现场取证那条线交付）。
> 04 脚本检测到它不存在会打印提醒并跳过，不影响拷安装包；等它到位后重跑一次就会一并送进去。

---

## 三、资源占用

| 资源 | 占用 | 说明 |
|---|---|---|
| **内存** | **虚拟机开机时固定吃 16 GB**（静态，不还） | 主机 63 GB。开机前建议先关掉一部分 WSL 容器。**关机时不占内存** |
| **磁盘** | **最多 120 GB**（动态扩展，用多少涨多少） | 落在 D 盘（当前剩 390 GB）。实际用量：系统约 25 GB + 安装包 2.26 GB + QTrade 装完约 15 GB ≈ **45 GB 上下**；检查点的差异盘会再额外占一些 |
| **CPU** | 8 个虚拟处理器 | 主机 24 逻辑核，虚拟机不跑时不占 |
| **C 盘** | 启用 Hyper-V 增约 1~2 GB（WinSxS） | C 盘当前剩 282 GB |

**清理**：测试彻底做完后，`Remove-VM` + 删 `D:\HyperV\QTrade-Test` 就能全部回收；
不想让主机长期带着 Hyper-V 跑，再跑一次 `01b-撤销HyperV.ps1`。

---

## 四、为什么虚拟机里装 QTrade 不会影响主机的 WSL

这是这套方案成立的前提，讲清楚：

1. **两层 hypervisor，互不可见**。虚拟机是 Hyper-V 的一个独立分区，
   来宾系统里的 WSL2 跑在**嵌套的第二层**监控程序里，
   跟主机那个跑着你 12 个容器的 WSL2 工具虚拟机完全是两回事。

2. **QTrade 改的每一样东西都在虚拟磁盘里**：
   - `%ProgramData%\QTrade\`、`%LOCALAPPDATA%\QTrade\` → 来宾的 C 盘
   - `%USERPROFILE%\.wslconfig`（**内核切换改的就是这个文件**）→ 来宾用户的家目录
   - WSL 发行版注册（`HKCU\…\Lxss`）、`ext4.vhdx` → 来宾的注册表与磁盘
   - `QTradeWinAgent` 服务、计划任务、防火墙规则、hosts 行 → 来宾的系统

   **你主机上的 `.wslconfig` 一个字节都不会被碰。**

3. **`wsl --shutdown` 只在它自己那台机器里生效**。安装器在虚拟机里执行这条命令时，
   停的是**来宾的** WSL，主机的容器毫无感觉。这正是拿虚拟机验内核切换的最大价值 ——
   在主机上验这一步，代价是中断你正在跑的全部容器。

4. **唯一的主机级交集只有三处**，而且都是可预期的：
   - 第 1 步启用 Hyper-V **要重启主机一次**（重启会中断容器，但不改任何配置）；
   - 虚拟机开机时占 16 GB 内存、占 D 盘空间；
   - 启用 Hyper-V 会新增 `vEthernet (Default Switch)` 虚拟网卡与 WinNAT（见第 2 步的说明）。

---

## 五、已知限制（这台虚拟机验不了什么）

**如实说，不要在这上面得出过度的结论：**

1. **嵌套虚拟化下 WSL2 性能明显更差**。第二层 hypervisor 的开销摆在那儿，
   I/O 与启动都会慢。所以**不要拿虚拟机里的耗时当性能结论**，只看「能不能成」。

2. **redroid 在嵌套环境里未必跑得到企点登录**。Android 容器依赖自编内核的 binder、
   依赖 `ndk_translation` 做 x86 翻译，这一层在嵌套虚拟化下**不保证可用**。
   **这一层仍以另一台物理机为准。**

3. 因此，**这台虚拟机的定位是**：验**安装器状态机、内核切换、服务注册、卸载还原**。
   自检步（`SELFTEST_OK`）里跟 redroid 启动相关的判据（退出码 100）在这里失败，
   **不能直接认定是安装器的 bug** —— 要先排除嵌套环境本身的限制。

4. **vTPM 在家庭版 Hyper-V 上不保证可用**。开不出来时 `02` 脚本会打印 `LabConfig` 绕过办法，
   应答文件里也预置了那几个绕过键，安装 Windows 不受影响。

5. **虚拟机里的 WSL2 网络可能需要额外处理**。若来宾里 WSL2 起得来但网络不通，
   一个常见办法是给虚拟机网卡开 MAC 地址欺骗：
   `Set-VMNetworkAdapter -VMName 'QTrade-Test-Win11' -MacAddressSpoofing On`。
   **脚本默认没开** —— 它会改动网络行为，按「网络上保守」的原则留给你显式决定。

6. **家庭版启用 Hyper-V 是非官方路径**（见第 1 步的说明）：微软不支持，
   Windows 功能更新后可能需要重做。

---

## 六、文件清单

| 文件 | 干什么 | 会改机器吗 |
|---|---|---|
| `00-只读体检.ps1` | 启用前的全面只读检查 | ❌ 纯只读 |
| `00-只读体检.cmd` | 上面那个的双击入口 | ❌ 纯只读 |
| `01-启用HyperV.ps1` | DISM 逐包启用 Hyper-V | ✅ **之后要重启一次** |
| `01b-撤销HyperV.ps1` | 上面那步的撤销 | ✅ 之后要重启一次 |
| `02-建测试虚拟机.ps1` | 建 Gen2 虚拟机（嵌套虚拟化已开） | ✅ 只在 D 盘落盘 |
| `02b-制作应答ISO.ps1` | 生成 autounattend.xml + 小 ISO | ✅ 只在 D 盘写两个文件 |
| `autounattend.xml.template` | 无人值守应答文件模板（**无密码**） | ❌ 只是模板 |
| `03-快照与回滚.ps1` | 基线检查点 / `-List` / `-Restore` | ✅ 只改虚拟机 |
| `04-把安装包送进虚拟机.ps1` | 送安装包 + 静默安装骨架 + 取回日志 | ✅ 只改虚拟机 |

**编码约定**：`.ps1` 与 `.xml.template` 一律 **UTF-8 带 BOM + CRLF**
（PowerShell 5.1 读无 BOM 的 UTF-8 会把中文认成乱码）；
`.cmd` 是 **UTF-8 无 BOM + CRLF**（`cmd.exe` 会把 BOM 当成第一条命令的一部分并报错，
所以批处理反过来**不能**带 BOM，脚本靠第二行的 `chcp 65001` 处理中文）。

**日志落点**：`01` / `01b` 的操作日志写在 `%LOCALAPPDATA%\QTrade-VMLab\logs\`，
**刻意避开 `%ProgramData%\QTrade`**，不干扰安装器自己的状态与日志。

---

## 七、一轮完整测试长什么样

```powershell
# —— 只在第一次做 ——
.\00-只读体检.ps1                       # 1 分钟，只读
.\01-启用HyperV.ps1                     # 5~15 分钟 → 🔴 你自己挑时间重启一次
.\02b-制作应答ISO.ps1 -ListImages       # 看清版本名
.\02b-制作应答ISO.ps1 -ImageName '…'    # 设密码，生成应答 ISO
.\02-建测试虚拟机.ps1 -AnswerIsoPath 'D:\HyperV\QTrade-Test\autounattend.iso'
Start-VM -Name 'QTrade-Test-Win11'; vmconnect.exe localhost 'QTrade-Test-Win11'
#   （开机那几秒按一下键盘，然后等它自己装完）
Stop-VM -Name 'QTrade-Test-Win11'       # 装完关机
.\03-快照与回滚.ps1                     # 打基线 clean-baseline

# —— 以后每一轮都这样 ——
.\03-快照与回滚.ps1 -Restore            # 10 秒回到干净起点
Start-VM -Name 'QTrade-Test-Win11'
.\04-把安装包送进虚拟机.ps1 -RunInstall  # 拷 + 静默装 + 取回退出码与日志
#   看退出码、看 install_state.json、看服务/防火墙/hosts、再跑一次卸载看还原
```
