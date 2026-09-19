# QTrade 企点多实例控制台 —— 规划设计(v0.1,2026-09-17)

> 目标平台:Windows 10(x64)。有没有装过 WSL/WSL2 都能装。
> 本文是**规划设计**,不含实现。所有「实测」均来自 2026-09-16~17 在一台 Win + WSL2 机器上的真实打通过程,
> 细节与证据见 `kernel/out/APPLY-WSL.md`、`rootfs/out-wsl-local/`、`xunjia-agent/relay/side_a/README-wsl-x86.md`。

## 0. 目标与非目标

**要做到(对应四条需求)**
1. 一个 EXE,在任意一台能上网(可能带代理)的 Win10 上一键装好「带 binder 的 WSL2 内核 + redroid 运行环境」,能起 redroid。
2. 同机开多个 redroid,每个是一台**身份独立且稳定**的安卓设备,各登一个企点号。
3. 装好后有一个桌面控制台:新增、查看、启停、看画面、删除本机的 redroid 企点实例。
4. 所有实例接受**同一套 RPA 元指令**(读、回复、下载图片、上滑、下滑、语音转文字、切换聊天、查找人员……),复杂操作由元指令编排而成。

**不做**
- 不做自动登号、不存账号密码、不绕过短信/滑块验证——登录永远是人在画面里操作。
- 不做 go-cqhttp 一类协议端;只走「真客户端 + 界面自动化」。
- 不做跨机集群调度(v1 只管本机)。
- 不做「设备指纹轮换」。身份一经生成就**固定不变**(见 §3.3)。

## 1. 总体架构

```
┌──────────────────────── Windows 10 ────────────────────────────────────────────┐
│  QTrade Console (Electron)                                                       │
│   ├─ 渲染进程:实例列表 / 新增向导 / 画面 / 指令调试台 / 日志                      │
│   └─ 主进程:安装与自检、wsl.exe 编排、scrcpy 进程管理、本地 API 客户端           │
│                 │ HTTP+WebSocket  127.0.0.1:17600 (令牌鉴权)                     │
│  ───────────────┼───────────── WSL2(自编内核:binder/binderfs 内建)────────────  │
│  发行版 qtrade-redroid(安装器导入,与用户已有发行版互不干扰)                      │
│   ├─ qtrade-agent(Python 常驻服务,systemd 拉起)                                 │
│   │    ├─ 实例管理器:docker 编排、端口/数据卷/身份档案分配、健康检查               │
│   │    ├─ RPA 执行器:每实例一条串行队列 → 元指令 → adb/uiautomator/截图          │
│   │    └─ 编排引擎:工作流(元指令的有序组合)+ 全程留痕                           │
│   └─ dockerd ── redroid#1  redroid#2  …  redroid#N(各自 /data 卷、各自 adb 端口)  │
└──────────────────────────────────────────────────────────────────────────────────┘
外部调用方(业务系统、脚本、询价引擎)与控制台用的是**同一个本地 API**。
```

设计取舍:
- **大脑放在 WSL 里的 agent,不放在 Electron 里。** adb、docker、截图、OCR 都在 Linux 侧最顺;Electron 只是外壳和画面。控制台关掉,实例和 RPA 照常跑。
- **API 是唯一入口。** 控制台不享有特权通道,这样「界面能做的,程序都能做」,也方便以后换前端。
- **专用发行版。** 不往用户已有的 Ubuntu 里装东西。但内核是全局的(见 §2.3 的风险与对策)。

## 2. 安装器

### 2.1 交付物
- `QTrade-Console-Setup-x.y.z.exe`(Electron + electron-builder/NSIS;或沿用现有 Inno Setup 外壳再内嵌 Electron 应用)。
- 内含:`bzImage`(自编内核)、`rootfs.tar`(Ubuntu + docker + **预载 redroid 镜像** + agent + ADBKeyboard + 脚本)、`scrcpy`/`adb` Windows 版、WSL 离线安装包(MSI,见 2.2)。
- 体积预估 1.5–2 GB。企点 APK **不内置**(版权与版本问题),由用户在控制台里选择本地 APK 文件导入,或配置一个内部下载地址。

### 2.2 安装流程(每步可重入,失败可回滚)

| 步 | 动作 | 说明 |
|---|---|---|
| 1 预检 | Win10 版本 ≥ 19041(2004)且建议 22H2;x64;BIOS 虚拟化已开;磁盘 ≥ 20 GB;内存 ≥ 8 GB | 虚拟化没开只能提示用户进 BIOS,软件开不了 |
| 2 装 WSL | 没有 WSL:启用「虚拟机平台」「适用于 Linux 的 Windows 子系统」两个功能 → **要求重启一次** → 重启后安装器自动续跑(写 RunOnce);装内置的 WSL MSI(不依赖微软商店,解决内网/无商店机器) | 已有 WSL1:`wsl --set-default-version 2`;已有 WSL2:只补 MSI 到 2.x |
| 3 落内核 | `bzImage` → `%ProgramData%\QTrade\kernel\`;校验 sha256 | 放 ProgramData 而不是某个用户目录,多用户机器也能用 |
| 4 写 `.wslconfig` | 先备份;已有 `kernel=` 且不是我们的 → **停下来问用户**;写入后 `wsl --shutdown` | 这一步会杀掉用户正在跑的所有 WSL 进程,必须明确提示并让用户确认时机 |
| 5 验内核 | 起发行版 → `uname -r` 带 `-binder`、`/proc/filesystems` 有 binder、试挂 binderfs 见三设备 | **任一项失败自动恢复 `.wslconfig` 备份并再次 shutdown**(已有成品脚本 `apply-binder-kernel.ps1`) |
| 6 导入发行版 | `wsl --import qtrade-redroid …rootfs.tar`;首启 oneshot:`docker load` 预载镜像、启用 agent | 镜像离线自带,**安装期不需要 docker pull**,代理环境再差也能装完 |
| 7 自检 | agent 健康检查 → 起一个临时 redroid → `boot_completed` → 删掉 | 通过才算装好,给出绿色结论 |

卸载:停实例 → `wsl --unregister qtrade-redroid` → 从 `.wslconfig` 去掉我们的 `kernel=` 行(保留用户其他配置)→ 删文件。数据卷是否保留让用户选(里面是登录态)。

### 2.3 内核:已验证的做法与必须写进构建的坑
- 基线 `linux-msft-wsl-6.6.y` + `binder.config`;binder/binderfs **编进内核**。
- 🔴 **不要把官方的 `=m` 无差别改成 `=y`。** 实测 `CONFIG_VIRTIO_VSOCKETS=y` 会抢占 vsock 的 G2H 槽位,WSL 的 `hv_sock` 注册失败,**虚拟机直接关机**,而 QEMU 测不出来。原则改为:只内建「切换前 `lsmod` 里实际在用的」+ docker 必需的那一族,其余保持官方原样。
- `CONFIG_CRYPTO_TEST` 必须关(内建后开机刷 75 条自测失败)。
- 编译带 `LOCALVERSION=`,版本串不要出现 `+`。
- 6.x 开了 binderfs 后 **`/dev/binder` 不会预先存在**,只在挂 binderfs 时出现(redroid 自己挂)。判据用「config + `/proc/filesystems` + 试挂」,别 `ls /dev/binder`。
- 内核是**全局**的:用户已有的发行版也会跑在它上面。对策=内核只做官方配置的「最小超集」+ 第 5 步自动验证与回滚 + 安装前明示。CI 里固定产物 sha256。
- WSL 版本跨度大时(用户机器 WSL 很新),内核分支要能跟着换;CI 保留 5.15 与 6.6 两条线的产物,安装器按 `wsl --version` 选择。

### 2.4 网络(目标机 = 普通电脑,不预设任何隧道/代理架构)
> ⚠️ 目标机就是一台**普通公司电脑**:网络简单,**最多可能开着公司 VPN**。设计**不涉及** frp / tailscale / wireguard / 反向隧道 / NAT 打洞 / TUN 代理 / gVisor 这类东西——那些是开发机的私有环境,不进产品。
- **公司 VPN 的影响(唯一要考虑的网络变量)**:公司 VPN 可能改写 DNS 与路由,使某些地址走公司内网、某些走本地出口。这会影响三处可达性:①**内部 APK 下载地址**、②**邮件服务器(POP3/SMTP)**、③**企点/QQ 服务器**。预检做**基本连通性探测**:对这三类目标各测一次能否连上,连不上就明确报"目标不可达,请确认公司 VPN 是否需要开启/关闭",不猜原因、不改用户的 VPN。
- 企点的消息通道是私有 TCP 协议,**不走 HTTP 代理**。若目标机的出网被强制走 HTTP 代理而无直连,企点登不上——这是硬边界,预检据实告知。QQ(NapCat)同理走自有协议。
- **默认不配任何代理**:普通电脑直连即可。仅当机器确有系统代理且必要时,agent 才把它透传给 docker 拉镜像;`androidboot.redroid_net_proxy_*` 参数保留作可选项,默认不用。
- 安装期不联网(镜像与依赖离线打包);只有内部 APK 下载、docker 升级拉镜像才需要出网,走机器本身的网络(含公司 VPN)即可。
- WSL 与宿主之间用本地回环通信;所有对外服务端口默认绑 `127.0.0.1`(API 是否放开到局域网见附录 A.6,由使用方决定)。

## 3. 多实例

### 3.1 实例模型
```jsonc
{
  "id": "q01",                      // 本机唯一,短 ID
  "label": "张三-固收",              // 给人看的名字
  "state": "running",               // created | starting | running | login_required | stopped | error
  "adb_port": 16001,                // Windows/WSL 侧 127.0.0.1 上的端口
  "data_dir": "/var/lib/qtrade/instances/q01/data",
  "device_profile": { … 见 3.3 … },
  "app": { "package": "com.tencent.qidian", "version": "6.9.7", "logged_in": true },
  "resources": { "mem_limit_mb": 3072, "cpus": 2 },
  "created_at": "…", "last_seen": "…"
}
```
元数据存 agent 侧的 SQLite;数据卷是登录态的命根子,**删除实例默认保留数据卷**,要二次确认才真删。

### 3.2 容器与端口
- 每实例一个 redroid 容器,`--privileged`,参数沿用已验证的一组:`redroid_gpu_mode=guest`、`use_memfd=true`、`ro.enable.native.bridge.exec=1`、`ro.dalvik.vm.native.bridge=libnb.so`(官方 redroid 11 镜像**自带** libndk_translation,`libnb.so` 是其软链;但 `prop.default` 里是 `native.bridge=0`,**这一行不能省**)。
- 🔴 **adb 端口不要用 5555–5585。** adb 守护进程会把这段端口自动当模拟器扫描并注册成 `emulator-55xx`,再手动 `connect` 会出现双重注册互相卡死;且它**只在启动时扫一次**,守护进程早于容器启动就永远看不到设备。多实例统一用 **16001 起**的端口 + 显式 `adb connect 127.0.0.1:<port>`,序列号就是 `127.0.0.1:<port>`,稳定、可预测。
- 重启策略:`restart: "no"`,由 agent 决定何时拉起(避免坏实例无限重启刷日志);WSL 重启后 agent 按「上次是 running 的」自动恢复。
- 每个 redroid 的 Android init 会往宿主 dmesg 写日志,属正常噪声。

### 3.3 设备身份:「不同 IMEI」的正确落点
实测结论:redroid 的 Android **没有电话模块,不存在 IMEI**(`telephony` 特性 0 个)。企点实际依赖的身份是:

| 标识 | 来源 | 多实例下 |
|---|---|---|
| `wtlogin_guid`、安全组件 `guid`、`qimei36` | 企点首次启动时自己生成,**存在 `/data` 里** | 每实例独立数据卷 ⇒ 天然不同 |
| `android_id` | 系统首次启动生成,存 `/data` | 同上 |
| 网卡 MAC | docker 分配 | 天然不同;agent 固定写死,避免重建容器后变化 |
| 机型/品牌/指纹/序列号 | 系统属性 | 默认都是 `redroid11_x86_64`,**这是多实例最该处理的一项** |

设计:
- 新建实例时从内置的**机型档案库**里抽一份(品牌、型号、`ro.build.fingerprint`、随机序列号、固定 MAC),写入实例元数据,通过 redroid 启动参数里的 `ro.product.*`/`ro.serialno` 等覆盖注入。
- **一经生成永不改变**。身份漂移比身份雷同更容易触发风控;「换设备」只能通过新建实例实现,并会重新走一次新设备短信验证。
- 同机多实例共享一个出口 IP,这与「一个办公室多台手机」的形态一致,不做 IP 层面的伪装。
- ⚠️ 合规提示:模拟设备身份、批量自动化可能违反腾讯企点的用户协议,存在被风控或封号的风险。本产品的前提是「每个号都是使用方自己的、有权自动化的企业账号」,这一点要写进首次启动的告知页,由使用方自己确认。

### 3.4 容量规划(实测)
单实例(企点已登录前台)≈ **2.5 GB 内存、0.15 核**常态;启动峰值更高。

| 物理内存 | 建议给 WSL | 建议实例数 |
|---|---|---|
| 8 GB | 5 GB | 1 |
| 16 GB | 11 GB | 3 |
| 32 GB | 24 GB | 7–8 |

安装器按物理内存写 `.wslconfig` 的 `memory=`(用户已有设置则不动,只提示)。每实例设 `--memory=3g` 上限防止单个泄漏拖垮整机。控制台在新增实例时做余量检查,不够就拒绝并说明。

### 3.5 生命周期
`新增` → 分配 ID/端口/数据卷/档案 → 起容器 → 等 `boot_completed` → 装 ADBKeyboard + 企点 APK → 拉起企点 → 状态 `login_required` → 用户在画面里同意协议、登录、过短信验证 → agent 检测到主界面 → `running`。

实测要处理的两个启动期问题:
- 企点**首次启动**(同意协议之前)那一轮,消息服务进程可能初始化不完整(登录鉴权对象为空),表现为登录 40 秒后提示「当前网络不可用」。对策:agent 在用户点完「同意」后**自动强制重启一次企点**再让用户登录。
- 日志里的 `Unexcepted activeNetInfo type:9`(企点不认识以太网类型)不影响登录与长连接,忽略;若个别功能因此受限,再评估给 redroid 加虚拟 Wi-Fi。

## 4. 控制台(Electron)

### 4.1 页面
- **实例列表**:卡片或表格——名称、状态灯、账号昵称(登录后读取)、内存/CPU、最近一条指令、启停/重启/删除。
- **新增向导**:起名 → 选机型档案(默认随机)→ 选企点 APK → 资源上限 → 创建 → 自动跳到画面让用户登录。
- **画面**:v1 直接拉起随包的 `scrcpy.exe`(每实例一个窗口,标题带实例名);v2 内嵌(scrcpy-server 的 H.264 流 → WebCodecs 解码,鼠标键盘事件经 agent 注入)。v1 先求稳。
- **指令调试台**:选实例 → 选元指令 → 填参数 → 执行 → 看返回 JSON、耗时、前后截图。这是开发 RPA 定位表的主要工具。
- **工作流**:列出、运行、查看每一步留痕。
- **环境**:内核/WSL/docker/agent 版本,一键自检(含 §2.4 的交替目的地探测),日志导出。

### 4.2 主进程职责
安装与升级、`wsl.exe` 调用(起停发行版、取日志)、scrcpy 子进程管理、开机自启与托盘、把 API 令牌安全地交给渲染进程(不落明文到渲染侧存储)。

### 4.3 安全
- API 只监听 `127.0.0.1`,随机令牌存 Windows 凭据管理器;可选开启局域网访问(默认关)。
- 不记录消息正文到控制台日志(只记指令名、对象、耗时、结果码);正文留痕在 agent 侧按需开关、可设保留期。
- 画面与截图可能含敏感业务信息,导出日志时默认不带截图。

## 5. 统一 RPA 指令层

### 5.1 分层(沿用并推广 `relay/side_a` 已验证的七件套)
```
工作流(编排)        例:巡检未读 → 逐个会话读新消息 → 回调业务系统 → 按返回回填
  └─ 中层任务         chat_session(peer) 会话括号:进入必校验标题,退出必归一化
       └─ 元指令      read / send / scroll / find / download …(下表)
            └─ 驱动   uiautomator dump · input tap/swipe · ADBKeyboard · screencap · 文件拉取
                 └─ 应用档案(profile):企点各页面的定位表,与引擎解耦
```
- **每实例一条串行队列**:同一实例同一时刻只执行一条指令;不同实例之间并行。
- **一律「动作 → 等待 → 读态验证 → 重试」**,不假设界面即时生效。
- **归一化重力** `to_ready()`:每条会改变界面的指令前后,把界面拉回「会话列表顶部、无弹窗」的稳态。
- **安全闸保留**:发送类指令发前发后各校验一次当前会话标题(双闸门);出口词表拦截;业务侧可注入自定义闸(如券码一致性)。**宁可不发,绝不发错。**

### 5.2 元指令(v1 集合)

| 指令 | 参数 | 返回 | 备注 |
|---|---|---|---|
| `get_state` | — | `READY/CHAT_OPEN/LOGGED_OUT/NOT_FOREGROUND/UNKNOWN` | 只读 |
| `list_threads` | `limit` | 会话名、未读数、预览、时间 | 只读 |
| `find_contact` | `keyword` | 候选人员/会话列表 | 走企点搜索框 |
| `switch_chat` | `peer` | 是否进入 + 实际标题 | 标题强校验,不符即失败 |
| `read_messages` | `n`,`since_cursor` | `{dir,type,text,ts}` 列表 + 新游标 | 游标=末尾 K 条序列的锚块;空读重试 |
| `send_text` | `peer`,`text`,`idempotency_key` | 是否已出现在历史里 | 双闸门 + 回读输入框 + 发送后验证 |
| `scroll_up` / `scroll_down` | `times` | 是否到顶/到底 | 列表与聊天页通用 |
| `download_image` | `message_ref` | 本地文件路径、sha256 | 点开大图 → 保存 → 从 `/sdcard` 拉取;备选:容器有 root,可直接从应用缓存取 |
| `voice_to_text` | `message_ref` | 文本 | 首选企点自带的「转文字」;备选:取语音文件 → 转码 → 本地/内网语音识别。**需实机验证哪条路可行** |
| `screenshot` | `region?` | PNG | 取证与视觉兜底 |
| `back` | — | — | 清浮层 |
| `restart_app` | — | — | 自愈用 |

每条指令统一返回:`{ok, code, data, cost_ms, trace_id, state_before, state_after}`;失败码分「可重试(界面没就绪)/ 不可重试(闸门拦截、对象不存在)/ 需人工(掉线、验证码)」。

### 5.3 读内容的三级策略
1. **控件树**:实测企点的协议弹窗、登录页、短信验证页都是**原生控件**,带完整 resource-id,可直接定位。
2. **截图 + 识别**:聊天页若是 Flutter 自绘(只有一个 FlutterView),走截图 → OCR/多模态转录,数字零改动、带拒答重试。
3. **frida 读**(可选增强):钩消息落地函数拿完整正文,最准但要维护钩子;作为第二阶段。
> 聊天页到底是哪一级,要等首个实例登录后实机 dump 才能定。**这是 RPA 层当前最大的未知数**,排在里程碑 M2 的第一件事。

### 5.4 编排
- 工作流用一份 YAML/JSON 描述:步骤、参数引用上一步结果、条件分支、循环、失败策略(重试/跳过/转人工)。
- 引擎在 agent 里执行,天然拿到串行队列和安全闸;每步留痕(指令、参数摘要、结果码、前后状态、可选截图)。
- 对外两种触发:API 调用、内置调度(如每 5 秒巡检未读)。事件(新消息、掉线、需人工)经 WebSocket 推给控制台和业务系统。

### 5.5 API 草案
```
GET    /instances                      列表
POST   /instances                      新增
POST   /instances/{id}/start|stop|restart
DELETE /instances/{id}?keep_data=true
POST   /instances/{id}/commands        { "op": "send_text", "args": {…}, "idempotency_key": "…" }
POST   /instances/{id}/workflows/{name}/run
GET    /instances/{id}/screenshot
WS     /events                         状态变化 / 新消息 / 指令完成 / 告警
POST   /broadcast/commands             同一条指令发给多个实例(显式列出 id,不提供"全部")
```

## 6. 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| **M0 内核加固** | 按 §2.3 重做 `binder.config`(最小超集);CI 产出固定 sha256 的 bzImage(5.15 与 6.6 两条线);QEMU 冒烟 + 真机切换脚本 | 干净 Win10 22H2 上切换成功、现有发行版不受影响、可一键回滚 |
| **M1 单实例安装器** | EXE:预检 → 装/补 WSL → 内核 → 发行版(预载镜像+agent)→ 自检;含重启续跑与卸载 | 三类机器各一台通过:无 WSL / 有 WSL1 / 有 WSL2 |
| **M2 agent + 单实例 RPA** | 实例管理、API;**实机 dump 企点聊天页定读取策略**;元指令 v1 在真企点上跑通;side_a 的 15 项自测迁移为回归集 | 真企点上「读新消息 → 回一条 → 校验已发出」闭环,双闸门负例全过 |
| **M3 多实例** | 端口/数据卷/机型档案分配、容量检查、WSL 重启自恢复 | 同机 3 实例各登一个号,并行收发互不串 |
| **M4 控制台** | 列表/新增向导/scrcpy 画面/指令调试台/环境自检 | 不碰命令行完成「新增 → 登录 → 发一条消息」 |
| **M5 编排与对外** | 工作流引擎、事件推送、广播指令、留痕与导出 | 一条「巡检未读并回调」工作流稳定跑 24 小时 |

## 7. 风险与待你定的事

**风险**
- **全局内核**会影响用户已有的 WSL 环境。靠最小超集 + 自动验证回滚 + 明示来控制,无法消除。
- **宿主机代理软件**可能破坏 WSL 出网(§2.4)。只能检测与告知。
- **企点聊天页可读性**未知(§5.3),可能被迫走截图识别,速度与准确率都会打折。
- **风控**:新设备必过短信验证;多实例同 IP、自动化节奏过快都有风险。对策:节流(发送间隔 ≥1.5 秒加随机)、不频繁重登、身份稳定。
- **Win10 自身**:家庭版可用,但企业里常被策略禁用虚拟化功能或禁装驱动,预检要能识别并给出明确原因。
- **企点版本升级**会打破定位表。profile 与引擎解耦、按版本存多份、升级前先在调试台回归。

**需要你拍板**
1. 企点 APK 的分发方式:用户手选本地文件,还是配一个内部下载地址?
2. 画面 v1 用独立 scrcpy 窗口是否可接受(内嵌放到后面)?
3. 是否要 frida 读这条增强路线,还是只做控件树 + 截图识别?
4. 语音转文字若企点自带功能不可用,允许接哪种识别服务(本地模型 / 内网服务 / 云)?
5. 同机实例数的目标值是多少?这决定推荐硬件和是否需要更激进的内存优化。
6. API 是否需要开放给局域网内的业务系统,还是只限本机?

## 8. 与现有资产的关系
- `qtrade-redroid-installer/`:内核构建、rootfs、Inno 安装脚本、`apply/rollback` 脚本 → 演进为 M0/M1。
- `xunjia-agent/relay/side_a/`:七件套与自测 → 演进为 agent 的 RPA 执行器与回归集;`mock_app` 继续当无账号环境下的回归目标。
- `rootfs/out-wsl-local/`:单实例 compose 与脚本 → 被 agent 的实例管理器取代,保留作手工排障用。

---

# 附录 A:六项决定的落地(2026-09-18,安琳拍板)

## A.1 企点 APK = 内部下载地址 + 自动新增并登录
- 安装器与 agent 配置一个**内部 APK 下载地址**(HTTP,含版本号与 sha256 校验);控制台不再让用户手选文件。agent 首次需要时下载并缓存,后续实例复用本地缓存;版本升级时按新 URL 重新拉取。
- **新增实例做到"尽量自动、只在必须时找人"**:创建 → 起容器 → 装 ADBKeyboard + 企点 → 拉起 → 自动强制重启一次企点(规避首启初始化不全)→ 自动勾选同意协议、填入账号密码、点登录。
  - 账号密码由调用方通过 API 传入(`POST /instances` 带 `login: {account, secret}`),agent **用后即弃、不落盘、不写日志**;控制台里输入框内容只在内存中传递。
  - 卡在**短信/滑块验证**时,实例进 `login_required` 状态并经事件推送通知调用方与控制台,由人在画面里完成。**这一步无法自动**,是硬边界(见 §0 非目标)。
  - 登录成功(agent 检测到会话列表主界面)→ `running`,登录态落各自数据卷。
- ⚠️ 内部下载地址在"只有 HTTP 代理、无直连"的内网里也要可达:地址应指向内网可直连的服务,或让 agent 走 Windows 系统代理拉取。企点**登录**本身仍需直连腾讯(私有协议),这条硬边界不因 APK 能下载而改变。

## A.2 画面内嵌进控制台(不再用独立 scrcpy 窗口)
- 取消 v1 的"独立 scrcpy.exe 窗口"方案,直接做内嵌。技术路线:
  - agent 侧对每个实例跑 **scrcpy-server**(推 H.264 流 + 控制通道),经本机端口暴露。
  - 控制台渲染进程用 **WebCodecs(`VideoDecoder`)** 硬解 H.264 到 `<canvas>`;鼠标/键盘/滚轮事件编码成 scrcpy 控制协议经 WebSocket 回注。
  - 多实例:列表页每个实例一个低帧率缩略流(如 540p@5fps),点开某实例进"专注视图"给到 720p@30fps。**只有前台可见的流用高规格,后台流降到最低或暂停**,这是多实例下省 CPU 的关键。
- 备选(若 WebCodecs 在目标机 Electron 版本上不稳):`ws-scrcpy` 式的 Broadway/tinyh264 软解,CPU 更高,仅作降级。
- 交互事件同样走**统一指令层的注入通道**,即"人在画面上点"和"RPA 指令点"最终是同一条 adb input 路径,便于录制人操作转成工作流。

## A.3 frida 读消息(增强路线,第二阶段并入)
- 定位:**frida 只用于"读全读准",写仍走 adb input**(方案 B,已在原项目验证过)。聊天页若是 Flutter 自绘,截图 OCR 有识别误差,frida 钩消息落地函数拿完整正文最稳。
- 落地:
  - agent 侧内置 `frida-server`(架构随 redroid = x86_64,但企点是 arm64 经翻译层运行 → **钩子要处理翻译层下的 native/JVM 混合栈**,这是本条最大不确定点,M2 首验)。
  - 优先钩 **Java 层**消息回调(企点/MobileQQ 的消息落地 `MessageRecord` 一类),避开翻译层对 native hook 的干扰;native 钩子作为兜底。
  - 读路径三级降级:**frida 命中 → 控件树 → 截图多模态**,由指令层自动回落,任一级拿到即返回,并标注来源。
  - frida 属"每实例一个 agent 会话",随实例生命周期启停;钩子脚本按企点版本存多份。
- 合规与稳定:hook 只读不改协议、不改发送;企点大版本升级可能使钩子失效,失效即自动降级到控件树/截图,不阻断主流程。

## A.4 语音转文字 = 多模态大模型 ASR
- 首选仍是**企点自带的"转文字"**(若该按钮可用,零成本、最准);不可用时走多模态大模型。
- 大模型路线:
  - `voice_to_text` 指令:定位语音消息 → 从应用缓存/`/sdcard` 取原始语音文件(容器有 root,可直接读)→ 转成模型接受的格式 → 调**多模态大模型 ASR 接口**(端点、密钥、并发在 agent 配置)→ 返回文本 + 置信度。
  - 接口抽象成 `asr_provider`,允许配置**内网自建多模态服务**或**云端多模态 API**,由使用方按合规要求选。**语音内容可能含敏感业务信息,是否出网由使用方在配置里决定**,产品默认指向内网端点。
  - 失败/低置信 → 保留语音文件路径 + 标注"需人工听",不静默丢。

## A.5 性能评估:16 GB 内存 + 2025 款 i5 裸机
**基准折算**:本机实测在 Ultra 9 275HX(24 核,单核性能约为主流桌面 i5 的 1.1~1.3 倍)。下表把实测的"容器 CPU 核·秒"折算到 **2025 款桌面 i5(约 6P+8E,14 核 20 线程,如 i5-14500/类 Arrow Lake i5)**,按"单核性能打八折、可用并行按 P 核为主"保守估。⚠️ 折算是估计,**最终以目标机实测为准**。

**实测(本机,单实例企点已登录前台)**
| 负载 | 墙钟 | 容器 CPU | 平均占用 |
|---|---|---|---|
| 空闲 20 秒 | 20s | 2.90 核·秒 | **0.14 核** |
| uiautomator dump ×5 | 7.6s | 1.95 | 0.26 核 |
| screencap ×10 | 1.0s | 1.23 | 1.19 核(短峰) |
| 上下滑动 ×10 | 7.4s | 2.24 | 0.30 核 |
| 内嵌推流 720p@30fps + 持续滑动 | — | — | 0.34 核 |
| 内嵌推流 540p@15fps | — | — | 0.31 核 |

**内存(单实例,实测)**:`memory.current` ≈ **3.3 GB**(峰值 3.65 GB),其中匿名页 1.67 GB(真占用),文件页 1.47 GB(可回收缓存)。给每实例 `--memory=3g` 上限偏紧,**建议 3.5 GB 上限**;真实常驻(匿名)约 1.7~2 GB。

**16 GB / i5 裸机的容量结论**
- Windows 10 + 桌面软件本身占 ~3–4 GB;留给 WSL 的 `.wslconfig memory=` 建议 **10–11 GB**。
- 内存是硬约束:11 GB ÷(单实例常驻 ~2 GB + 缓冲)⇒ **稳定 3 个实例,极限 4 个**(4 个时文件缓存被挤、滑动会卡)。
- CPU 不是瓶颈:3 个实例空闲态合计 ~0.5 核;i5 的 P 核足够。**峰值来自截图与视频编码**——3 个实例若同时高帧率推流,可能吃到 2–3 核,所以 A.2 的"只有前台流走高规格"必须落地。
- **启动是最重的时刻**:单实例冷启动 ~12 秒、峰值内存与 CPU 都翻倍。多实例**串行启动、不并发拉起**;开机自恢复也逐个来。
- 优化项(agent 默认开):
  - 内核已带 **KSM(相同页合并)+ ZRAM**;agent 开机启用 KSM,多实例间大量相同的系统页可合并,实测环境有明显收益(N 个实例的系统层内存不是 N 倍)。
  - WSL 侧 `.wslconfig` 开 `autoMemoryReclaim=gradual`(本机已在用),空闲内存还给 Windows。
  - redroid 用 `redroid_gpu_mode=guest`(软渲染,i5 核显在 WSL 下无法直通)、分辨率 720×1280、DPI 320,不追求高刷。
  - 后台实例的画面流暂停;RPA 巡检间隔按需拉长。
- **一句话给采购**:16 GB 是 **3 个企点号**的机器;要稳定跑 5 个以上,上 **32 GB**。i5 处理器足够,不是瓶颈,内存才是。

## A.6 API 对外开放
- API 默认**绑 `0.0.0.0`(可配),开放给局域网内的业务系统**;不再默认只限本机。
- 安全加固(开放即必须做):
  - **强制令牌**:`Authorization: Bearer <token>`,token 在控制台生成、存 Windows 凭据管理器,可轮换、可多把(按调用方发不同 token)。
  - **传输**:内网明文 HTTP 可选,建议开自签或内网 CA 的 HTTPS;WebSocket 同源鉴权。
  - **访问控制**:可配 IP 白名单(CIDR);写类指令(send/download/workflow 运行)与读类指令分权限等级。
  - **限流与幂等**:每实例串行本就限了并发;`send_text` 等写指令要求 `idempotency_key`,防重发。
  - **审计**:所有 API 调用记来源 IP、token 标识、指令、对象、结果(不记消息正文明文);可导出。
  - Windows 防火墙:安装器**默认不开**入站规则,用户在控制台"开放局域网访问"时才按所选端口精确放行(不放 Any),对应现场踩过的"安装程序自建 Any 规则"教训。
- `POST /broadcast/commands` 面向多实例统一指令,仍要求显式列出目标实例 id,不提供"全部实例"隐式广播,避免误操作放大。

## A.7 里程碑调整(并入上述决定)
- **M1** 增:内部 APK 下载地址配置 + 下载缓存校验。
- **M2** 增:frida 读增强(翻译层下的 hook 验证列为该里程碑**风险验证第一项**,与"聊天页可读性"一并在首个真实例上定);`voice_to_text` 走多模态 ASR 的抽象与一条真链路。
- **M4** 变:画面从"独立 scrcpy 窗口"改为"WebCodecs 内嵌 + 前台高规格/后台低规格";自动登录流程(除验证码外)纳入新增向导。
- **M5** 变:API 对外开放的鉴权、白名单、审计、防火墙联动作为交付项。
- 新增 **M6 性能达标**:16 GB / i5 目标机上 3 实例稳定运行 24 小时,前台画面流畅、后台 RPA 巡检不掉线,内存不 OOM;给出该机型的推荐实例数与 `.wslconfig` 模板。

---

# 附录 B:多 IM 通道接入(2026-09-18,安琳追加)

## B.0 需求
在企点(redroid)之外,接入更多 IM:
- **QQ / NTQQ**:走 NapCat(OneBot 11),现成实现在 `~/work/qtrade/bondtrade/napcat/`。
- **微信**:走 chatlog(解密库读)+ pyweixin(UI 自动化写),现成资源包在 `桌面/…/weChatlog/`。
- 三类(以及以后更多)接受**同一套指令与 API**,复杂操作同一套编排。

## B.1 关键认知:三种通道的形态根本不同
不能用一套"界面自动化"套所有。实测三者的读写路径与宿主位置各异:

| 维度 | 企点 | QQ / NTQQ | 微信 |
|---|---|---|---|
| 底层 | redroid 里的 arm64 APK | NapCat(改版 NTQQ,协议端) | 微信 PC 4.1.x 本体 |
| **宿主** | **WSL / docker** | **WSL / docker**(`mlikiowa/napcat-docker`) | **Windows 宿主机**(不在 WSL) |
| 读消息 | frida / 控件树 / 截图 | **OneBot 事件推送**(结构化,最干净) | **chatlog HTTP**(解密库轮询) |
| 发消息 | adb input(双闸门) | **OneBot `send_*_msg`** | **pyweixin UI 自动化** |
| 下载图片 | adb pull / frida | OneBot 消息段直接给 URL | chatlog `/image/<md5>` |
| 收发范式 | 轮询 / hook | **推**(正向 WS) | 读=**拉**(轮询解密库),写=UI |
| 登录 | 画面里过短信 | 扫码(存 `qq_data` 设备指纹免扫) | 微信 PC 扫码,勿休眠 |
| 身份稳定靠 | 数据卷 | `qq_data` 卷 | 微信本地登录态 |
| 实例数 | 多开(N 容器) | 多开(N 容器) | **一机一个微信**(PC 客户端限制) |

三条硬约束:
1. 🔴 **微信通道跑在 Windows 宿主机,不在 WSL 里**。chatlog 用 `wx_key.dll`(Windows DLL)读微信 PC 的加密库;pyweixin 是 Windows UI 自动化(pywinauto,靠"讲述人模式"暴露 UI 树)。这俩都出不了 Windows。
2. **QQ 是三者里最干净的**:纯 OneBot 结构化 API,读写都不碰界面,无识别误差、无风控点击。能用 NapCat 就别用界面自动化。
3. **微信是三者里最脆的**:读依赖 `wx_key.dll` 与微信小版本强绑定(换版本要换 DLL),写依赖 UI 自动化(微信改 UI 就失效),且**企业微信对 UI 自动化限制更严,此路对企业微信无效**。

## B.2 抽象:把"元指令"上提为"能力契约" + "通道适配器"
原设计的元指令(tap/swipe/dump)是**企点专用的操作层**。要容纳三种通道,在它之上加一层**语义能力契约**,上层工作流只认能力,不认底层是点击、是 API、还是解密读库。

```
工作流 / API(通道无关)
   │  只说"给账号 X 的会话 Y 发文本 Z""读 Y 的新消息"
   ▼
能力契约(Capability,语义层)  read_messages / send_text / send_image /
   │                          download_media / voice_to_text / list_sessions /
   │                          switch_session / find_contact / get_members / scroll …
   ▼
通道适配器(Adapter,每通道一个实现)
   ├─ QidianAdapter  → redroid 七件套(frida/控件树/截图 + adb input 双闸门)
   ├─ QQAdapter      → OneBot 客户端(bondtrade 的 OneBotClient,已实现收发+心跳重连+任意 action)
   └─ WeChatAdapter  → 读:chatlog HTTP 客户端(已实现);写:pyweixin(经 Windows 协处理器)
   ▼
账号实例(Account):一个通道下的一个登录实体
```

**能力矩阵**(不支持的能力返回 `UNSUPPORTED`,上层据此决定降级或跳过):

| 能力 | 企点 | QQ | 微信 |
|---|---|---|---|
| read_messages | ✅ 三级降级 | ✅ 事件+get_msg | ✅ chatlog 轮询 |
| send_text | ✅ 双闸门 | ✅ | ✅ pyweixin |
| send_image / send_file | ✅ | ✅ | ✅ pyweixin |
| download_media(图) | ✅ | ✅ URL | ✅ /image/md5 |
| voice_to_text | ✅ 多模态 ASR | ✅ record 段→ASR | △ 取语音→ASR |
| list_sessions | ✅ | ✅ 群列表 | ✅ chatroom API |
| switch_session | ✅ UI | ➖ 不需要(消息带 ID) | ✅ pyweixin |
| find_contact / get_members | ✅ UI | ✅ get_group_member_list | ✅ pyweixin get_friends |
| scroll_up/down | ✅ | ➖ 无界面概念 | △ UI |
| **红线闸/双闸门** | ✅ | ✅(发送校验 group/user_id) | ✅(校验当前窗口标题) |

- `➖` = 该通道无此概念(如 QQ 无"滚动"),契约上标 `NOT_APPLICABLE`,不算失败。
- **统一返回结构不变**:`{ok, code, data, cost_ms, trace_id, source}`,`source` 标明这条数据来自 frida / onebot / chatlog / ui,便于溯源与质检。
- **发送安全闸对所有通道生效**:出口词表(要约/成交词)拦截、限速 ≥1.5s+随机、幂等键防重发;对象校验按通道各自的锚(企点=会话标题,QQ=group/user_id,微信=窗口标题)。

## B.3 拓扑:agent 跨 WSL / Windows 两侧编排
```
┌───────────── Windows 宿主机 ─────────────────────────────────────┐
│  QTrade Console (Electron)                                        │
│  QTrade WinAgent(常驻,Windows 侧协处理器)★新增                  │
│    ├─ 微信通道:拉起/托管 chatlog_alpha(:5030)+ chatlog 读客户端 │
│    │              + pyweixin 写(pywinauto UI 自动化)             │
│    └─ 只处理"必须在 Windows 上跑"的通道;对 WSL agent 暴露本地 API │
│         │  loopback API                                           │
│  ───────┼──────────── WSL2 ─────────────────────────────────────  │
│  qtrade-agent(总控:账号注册表、能力路由、编排引擎、统一事件流)   │
│    ├─ QidianAdapter → redroid 容器群                              │
│    ├─ QQAdapter     → napcat 容器群(OneBot 正向 WS)             │
│    └─ WeChatAdapter → 调 WinAgent 的本地 API(读写都转发过去)     │
│  dockerd ── redroid#1..N ── napcat#1..M                           │
└──────────────────────────────────────────────────────────────────┘
```
- **WSL agent 是总控**,统一账号注册表、能力路由、编排、事件流、对外 API。
- **WinAgent 是新增的 Windows 侧协处理器**,只承载微信这类"离不开 Windows"的通道。WSL agent 通过 loopback 调它。若某机器不需要微信,不装 WinAgent 即可。
- QQ 和企点仍在 WSL docker 里,agent 直接管。

## B.4 统一事件流:推与拉抹平
三通道的"来了新消息"机制不同:QQ 是**推**(WS 事件),微信是**拉**(轮询解密库),企点是**轮询/hook**。适配器内部各自处理,对上一律转成 agent 的**统一事件**,经 `WS /events` 推给控制台与业务系统:
```jsonc
{ "event":"message", "account_id":"wx01", "channel":"wechat",
  "session":{"id":"群abc","name":"某某群"},
  "message":{"dir":"in","type":"text|image|voice","text":"…","media_ref":"…","ts":…,
             "sender":"张三","source":"chatlog"},
  "cursor":"…" }
```
- QQ:OneBotClient 的 `on_message` 直接转。
- 微信:WinAgent 轮询 chatlog(沿用 collector 的游标去重 `(talker, seq)`),把增量转成事件。
- 企点:frida hook 命中即推,否则轮询会话列表 diff。
- **游标/去重/幂等** 是所有通道的统一基建,不在适配器里各写一份。

## B.5 账号模型扩展
```jsonc
{
  "id": "wx01",
  "channel": "qidian | qq | wechat",     // ★新增
  "label": "…",
  "host": "wsl | windows",               // ★微信=windows,其余=wsl
  "state": "running | login_required | stopped | error",
  "endpoint": { … 通道各异:redroid adb_port / napcat ws_url / wechat 由 WinAgent 管 … },
  "capabilities": ["read_messages","send_text", …],  // 该账号实际支持的能力
  "identity": { … redroid 机型档案 / qq_data 指纹 / 微信登录态 … }
}
```
统一 API 完全不变,只是多了 `channel` 维度:
```
POST /accounts            { "channel":"qq", "label":"…", "login":{…} }
POST /accounts/{id}/commands   { "op":"send_text", "args":{…}, "idempotency_key":"…" }   // 通道无关
GET  /accounts?channel=wechat
```

## B.6 复用现成资产(不重造)
| 通道 | 直接复用 | 位置 |
|---|---|---|
| QQ | `OneBotClient`(收发+心跳重连+任意 action+echo 匹配)、`NapCatGateway`(限速队列+安全闸)、`docker-compose`(WebUI/HTTP/WS 端口、`qq_data` 卷) | `bondtrade/napcat/qqbot/onebot_client.py`、`agent/gateway.py`、`napcat/docker-compose.yml` |
| 微信读 | chatlog `chatlog.exe`(取钥+解密+HTTP API)、`ChatlogClient`(list_chatrooms/fetch_messages/download_image)、`normalize`(类型过滤)、`state_store`(游标去重) | `weChatlog/chatlog_alpha/`、`chatlog-collector/*.py` |
| 微信写 | `pyweixin`(4.1+,Messages/Files/Contacts/Monitor/AutoReply 等全套) | `weChatlog/pywechat/pyweixin/` |
| 企点 | side_a 七件套 | `xunjia-agent/relay/side_a/` |
> QQ 通道几乎是"把 bondtrade 的网关包成一个 Adapter"即可,工作量最小、最稳,建议**第一个接**。

## B.7 各通道的坑(来自资源包与 bondtrade 记录,写进实现约束)
**QQ / NapCat**
- `qq_data` 卷存设备指纹,**勿删**,删了要重新扫码且触发风控。
- 发送限速 ≥1.5s/条 + 随机延迟。
- docker daemon 代理曾把 `.1ms.run` 挡在外,已在 §2.4 统一处理。
- 正向 WS 每实例独立端口(如 3101/3201),多开按 16xxx 段分配,别撞。

**微信 / chatlog**
- `wx_key.dll` 与微信**小版本强绑定**:取钥报"模式匹配失败/0 个结果"时,拿 `wx_key1.dll`/`wx_key2.dll` 复制改名覆盖 `lib\windows_x64\wx_key.dll` 重试(实测 4.1.11.52 用 v1 失败、4.1.12.26 用 v2 成功)。WinAgent 要内置**多版本 DLL + 自动试钥**逻辑,并把"当前微信版本 → 生效 DLL"记档。
- 微信 PC **必须已登录、勿休眠、勿锁屏**;pyweixin 的 UI 自动化需要窗口可交互。
- pyweixin 依赖"**讲述人模式先于微信登录开启并持续 5 分钟以上**"才能稳定暴露 UI 树,这是它的前置仪式,WinAgent 首次配置微信通道时要引导用户完成。
- **企业微信 UI 自动化受限更严,此路不通**;若目标是企业微信,只能走它的官方 API(另设 Adapter,不在本期)。
- 微信只读 chatlog + 只写 pyweixin ⇒ **读写分离**,发送后要靠再次读库确认送达(没有 API 级回执)。
- chatlog 只保留文字/图片,视频/文件/语音在 collector 里被过滤;要支持语音转文字需在 WinAgent 侧扩展取语音文件的路径。

**企点** 见附录 A,不重复。

## B.8 里程碑增补
- **M2.5 QQ 通道**(紧接单实例 RPA,优先做,因为最稳):把 bondtrade 网关包成 `QQAdapter`,接入能力契约与统一事件流;napcat 多开纳入实例管理;控制台能新增 QQ 账号(扫码)、收发、能力矩阵正确标注 `NOT_APPLICABLE`。
- **M3.5 微信通道**:新增 **WinAgent**(Windows 侧协处理器)——托管 chatlog_alpha + 多版本 DLL 自动试钥 + chatlog 读客户端 + pyweixin 写;`WeChatAdapter` 经 loopback 调它;控制台能配置微信通道(含讲述人仪式引导)、读群消息、发消息。
- **M4** 控制台:账号列表按 `channel` 分组显示;新增向导按通道走不同流程(企点=机型档案+APK,QQ=扫码,微信=WinAgent 配置);画面仅对有界面的通道(企点/微信)提供,QQ 无画面(展示消息流即可)。
- **能力契约与安全闸** 从一开始就是通道无关的公共层,不在每个 Adapter 里重写。

## B.9 需要你定的(追加)
1. 微信通道优先级:是这一期就做(要额外做 WinAgent),还是先把 QQ + 企点做扎实、微信排后?
2. QQ 多开数量目标?(napcat 单容器比 redroid 轻得多,内存占用远小于企点实例,同机可开更多)
3. 企业微信是否在射程内?(若是,需要评估其官方 API,UI 自动化此路不通)
4. 微信"读写分离、发送无 API 回执"是否可接受?对发送可靠性要求高的话,要不要给微信也加"发送后读库确认"的强校验(更慢)?

---

# 附录 C:同机三通道并存、资源池、发送确认(2026-09-18,安琳追加)

## C.1 目标收敛
1. **企点 + QQ + 微信同机并存**,多账号登录**互不影响**。
2. **硬件资源池**统一管配额:按当前机器算出能开几个企点 redroid、几个 QQ,微信**恒为 1 且全局唯一**。
3. 企业微信**不做**(附录 B 已排除,相关分支删去)。
4. **发送一律"读回确认成功后才返回"**;对外发信 API 阻塞到确认;**审计所有对外写接口都要有这一环**。

## C.2 账号隔离(互不影响)
"互不影响"要在四个层面都成立:

| 层面 | 企点 | QQ | 微信 |
|---|---|---|---|
| 登录态/身份 | 各自 `/data` 卷 | 各自 `qq_data` 卷 | 微信本地登录态(单账号) |
| 进程 | 各自 redroid 容器 | 各自 napcat 容器 | 微信 PC 单进程 |
| 端口 | 各自 adb 端口(16xxx) | 各自 OneBot WS/HTTP(16xxx 另一段) | chatlog :5030(单实例) |
| 指令队列 | 每账号一条串行队列 | 每账号一条串行队列 | 一条串行队列 |

- **企点、QQ 天然可多开且互隔**(容器 + 独立卷 + 独立端口)。
- **微信是硬单例**:一台 Windows 只能登一个微信 PC;chatlog 也只对这一个微信取钥解密。所以"微信互不影响"退化为"微信这一个账号不被别的通道干扰"——它在 Windows 宿主、独立进程,天然不受 WSL 里企点/QQ 影响。
- 三通道的指令队列彼此独立,一个账号卡住不拖累别的账号(总控里按 `account_id` 分队列)。

## C.3 硬件资源池(本设计的核心新增)

### C.3.1 两个物理池(因为微信在 Windows 侧)
```
Windows 物理内存 ─┬─ Windows 系统 + 控制台 + WinAgent(固定预留)
                  ├─ 微信池:微信 PC + chatlog(≤1 个微信,固定占用)
                  └─ WSL 池(.wslconfig memory=)──┬─ 企点 redroid × Ne
                                                  └─ QQ napcat × Nq
```
- **WSL 池** 管企点和 QQ;其大小 = `.wslconfig` 的 `memory=`。
- **微信池** 在 Windows 侧,不吃 WSL 内存;微信 ≤1,是否启用是个开关。
- 资源管理器**同时看两个池**:开企点/QQ 找 WSL 池要额度,开微信找 Windows 侧要额度。

### C.3.2 配额单位与实测/估计基线
以**内存为主约束**(CPU 实测不是瓶颈,见附录 A.5),单账号"预算"取**常驻 + 启动峰值缓冲**:

| 通道 | 单账号内存预算 | 依据 |
|---|---|---|
| 企点 redroid | **2.5 GB**(常驻~2G + 缓冲;硬上限 `--memory=3.5g`) | 附录 A.5 实测 |
| QQ napcat | **0.6 GB**(估;headless NTQQ 常驻约 300–600M) | ⚠️ **估计值,首个 QQ 账号登录后按实测校准** |
| 微信(Windows 侧) | **~1.5 GB**(微信 PC ~0.8G + chatlog + 缓冲) | ⚠️ 估计值,实测校准 |

> QQ 比企点轻 4 倍左右,所以"没有多开目标"但资源池会算出一个上限——同样的 WSL 内存,QQ 能开的数量远多于企点。

### C.3.3 分配算法(资源管理器,agent 内)
```
WSL_budget   = wslconfig.memory - WSL基础占用(dockerd/agent/系统 ≈ 2GB)
用量           = Σ 企点账号×2.5G + Σ QQ账号×0.6G
可否新增企点   = (WSL_budget - 用量) ≥ 2.5G
可否新增 QQ    = (WSL_budget - 用量) ≥ 0.6G
可否新增微信   = 微信池启用 且 当前微信账号数 == 0        # 恒 ≤1
```
- 新增账号前**先问资源管理器**,不够就**拒绝并给出原因**("剩余 WSL 内存 1.8G,开企点需 2.5G;可改开 QQ,或去掉一个企点账号")。
- 控制台首页显示**资源仪表**:两个池的已用/总量、各通道账号数、"还能再开 N 个企点 / M 个 QQ / 微信(1/1 或 0/1)"。
- **启动串行**(附录 A.5):峰值内存翻倍,多账号逐个拉起,不并发。
- **超额保护**:每账号硬内存上限(容器 `--memory`);WSL 侧 KSM + `autoMemoryReclaim=gradual` 回收空闲。
- 参数(`2.5/0.6/1.5G`、基础占用)全部**可配**,首次在目标机跑一遍**自校准**:各起一个账号量实际 RSS,写回配置,后续按真值算。
- **16GB / i5 参考结论**(WSL 给 10–11G):示例组合——3 企点(7.5G)、或 2 企点+3 QQ(6.8G)、或 1 企点+微信+若干 QQ;微信另占 Windows 侧 ~1.5G。具体由资源管理器按实配算,不写死。

### C.3.4 资源 API
```
GET  /resources                 两个池的容量/已用/各通道账号数/可新增余量
POST /accounts (预检)            创建前自动过资源管理器;不足返回 409 + 可行替代
```

## C.4 发送确认:统一"读回确认成功才返回"

### C.4.1 现状审计(印证问题)
| 通道 | 现有发送 | 是否已确认 |
|---|---|---|
| 企点(side_a) | `send_text` 发后**在历史里验证到我方同文本** | ✅ 已是读回确认 |
| QQ(bondtrade) | `send_private/group_msg` 只 `return` OneBot API 响应(含 `message_id`) | ⚠️ **只信 API 返回,未读回** |
| 微信 | collector 只采集发邮件,**无发微信消息动作** | ❌ 发送确认能力**尚不存在** |
> 确认程度参差不齐,正是本条要统一的。

### C.4.2 统一契约:`send_*` 阻塞到确认
每个通道定义"**已送达**"的判据,发送指令**阻塞**到判据满足(或超时/失败)才返回:

| 通道 | 已送达判据 | 手段 |
|---|---|---|
| 企点 | 发送后在**本会话消息历史**里读到"我方"той条文本(内容+近时间戳) | 复用 side_a 现有逻辑 |
| QQ | 拿到 OneBot 返回的 `message_id` 后,再 `get_msg(message_id)` **读回该消息存在**(强判据);拿不到 `message_id` 视为失败 | OneBotClient 加 `get_msg`;`call_action` 已支持任意 action |
| 微信 | pyweixin 发送后,经 **chatlog 读库**在该会话读到刚发出的这条(发送方=自己、内容匹配、时间在发送之后) | WinAgent 串起 pyweixin 写 + chatlog 读;**发送后临时把 chatlog 轮询调快**(如 1s 一轮),**10s 内读到=DELIVERED,10s 内读不到=SEND_FAILED**(微信按「读不到即失败」处理,不留未确认中间态),确认结束后轮询恢复原间隔;读写分离下这是唯一可靠回执 |

返回统一:
```jsonc
{ "ok": true, "code": "DELIVERED",
  "data": { "message_id": "…", "confirmed_by": "history|get_msg|chatlog", "confirm_ms": 820 },
  "cost_ms": 1650, "trace_id": "…" }
```
失败/超时码分明:
- `SEND_CALLED_BUT_UNCONFIRMED`:发了但没在期限内读回(可能已达也可能没达)——**不静默当成功**,标"待核",可选自动重查一次。
- `SEND_FAILED`:发送动作本身失败(API 报错 / UI 点击失败 / 对象校验不符)。
- `DELIVERED`:读回确认成功。

参数:确认超时(默认 QQ 5s / 企点 8s / **微信 10s**)。⚠️**微信的语义与企点/QQ 不同**:发送后临时把解密库轮询调快(如 1s/轮),**10s 内读到判 DELIVERED、读不到直接判 SEND_FAILED**(微信读写分离、没有 API 回执,拿不到读回就当发失败,由上层按幂等键重试;幂等保证重试不会真重发已达消息);确认窗口结束后轮询恢复原间隔。企点/QQ 仍保留 SEND_CALLED_BUT_UNCONFIRMED 中间态。

### C.4.3 幂等(和确认配套,防重发)
"读回确认"引入一个风险:**发送成功但确认超时** → 上层重试 → 重复发送。对策:
- 发送指令必带 `idempotency_key`;总控记录"该 key 已进入发送中/已确认",重复请求直接返回上次结果,不再真发。
- 确认阶段读回时,**先按 key/内容查"是不是上次已经发出去了"**,是则直接判 `DELIVERED`,避免"其实发成功了、只是没及时读到"导致的重发。
- 与安全闸的"宁可不发绝不发错"一致,这里是"**宁可慢一点确认,绝不重复发**"。

### C.4.4 对外发信 API 全部纳入
```
POST /accounts/{id}/commands { "op":"send_text", "args":{…}, "idempotency_key":"…",
                               "confirm": true }     # confirm 默认 true,阻塞到 DELIVERED 才返回
POST /accounts/{id}/send      语法糖,等价上面且 confirm 恒 true
```
- **默认 `confirm=true`**:发信 API 不确认不返回。
- **允许**显式 `confirm=false`(即发即返回、拿 `message_id`),用于「高吞吐、可容忍偶发丢失」的场景;返回码明确为 `SEND_CALLED_BUT_UNCONFIRMED`,调用方自己认;⚠️**微信通道不提供 `confirm=false`**(没有 API 回执,即发即返回等于没有任何送达信息,无意义)。
- **审计要求(落地检查项)**:代码评审设一条硬规则——**任何对外暴露的"发送/回填/回复"类接口,默认路径必须走读回确认**;新增写接口若绕过确认,CI/评审拦下。把现状里 QQ 网关"只信 API 返回"、微信"尚无确认"这两处列为 M2.5 / M3.5 的**必改项**,不是可选优化。

## C.5 里程碑与决定回填
- **附录 B 的企业微信分支删除**;能力矩阵、Adapter 列表不再含企业微信。
- **M2 起**:发送确认作为**能力契约的强制部分**——企点保留现有读回;QQ Adapter 必须补 `get_msg` 读回;不确认的发送不算完成。
- **M2.5 QQ**:含 `get_msg` 读回确认 + 资源池纳管 napcat。
- **M3.5 微信**:WinAgent 串 pyweixin 写 + chatlog 读回确认;微信在资源池里恒 ≤1。
- **M4 控制台**:首页**资源仪表**(两个池 + 可新增余量);新增账号走资源预检。
- **新增 M2.9 资源池**:资源管理器 + 自校准 + 三通道配额 + 拒绝策略,先于多通道大规模并存落地。

## C.6 追加待定 → 已拍板(2026-09-18)
1. ✅ 三通道配额基线(企点 2.5 / QQ 0.6 / 微信 1.5 GB)用估计值 + 首次自校准,**认可**。
2. ✅ 发送确认:QQ 5s / 企点 8s / **微信 10s**;**微信发送后临时调快 chatlog 轮询,10s 内读库读到=成功,读不到=发送失败**(不留未确认中间态,当失败处理、由幂等重试兜底)。
3. ✅ `confirm=false` 即发即返回模式**保留**(微信通道除外,无回执不提供)。

---

# 附录 D:指令对接的三种传输入口(2026-09-18,安琳追加)

## D.0 需求
同一套指令与信息,支持三种进出方式:
1. **本地 API 直调**:本机应用直接调本地 API。
2. **第三方互联网服务调用**:外部(公网)服务调用本系统的 API。
3. **邮件摆渡**:靠收发邮件传指令与信息,含**邮件模板** + **POP3 收 / SMTP 发**。参考 `nanyin-deposit-agent`(即本仓库 ibquote)现成实现。

## D.1 核心认知:三者是"同一套指令的不同传输入口",不是三套逻辑
所有对接最终都汇到**同一条指令总线**(附录 B 的能力契约 + 附录 C 的发送确认 + 安全闸 + 幂等 + 审计),差别只在"指令怎么进来、结果怎么回去"。所以在能力契约之上再加一层**传输适配器(Transport)**,与业务逻辑解耦:

```
本地应用 ─(loopback)─┐
第三方服务 ─(公网入站)┼─► 传输适配器 ─► 【统一指令总线】─► 能力契约 ─► 通道适配器 ─► 企点/QQ/微信
邮件网关 ─(POP3/SMTP)┘        (鉴权/限流/               (发送确认/幂等/
                              解码/去重/回执)             安全闸/审计)
```
- **一次编写,三种入口复用**:同一条 `send_text`,无论从哪进来,都走同一确认与安全逻辑。
- 三种入口**同一份指令契约(schema)**;差别只是承载(HTTP body / 邮件正文)与信任级别。

## D.2 入口一:本地 API 直调(信任本机)
- 传输:`127.0.0.1:17600` 上的 HTTP + WebSocket(附录 A/C 已定义的那套 API)。
- 鉴权:本机令牌(Windows 凭据管理器),或本机进程免鉴权可配(仅 loopback)。
- 用途:控制台自己、同机业务程序、脚本。延迟最低、最信任。
- **这是"基准入口"**:另外两种入口最终都翻译成对它的调用。

## D.3 入口二:第三方互联网服务调用(公网入站,重鉴权)
外部互联网服务来调 → 必须假设调用方不可信、链路在公网。
- **暴露方式(本系统不自带任何隧道/穿透)**:目标机是普通电脑,通常没有公网 IP。让「互联网服务能调到本机」这件事**属于使用方的网络/运维**(例如把本机 API 反向代理到一台有公网入口的服务器、或部署在公司已有的 API 网关/DMZ 后),本系统**只负责把 API 做成可被反代的标准 HTTP 服务 + 做好鉴权**,不规定也不内置隧道方案。**不把整个 API 裸奔公网**,只开放白名单端点(如 `send`、`query`、事件订阅回调);默认仍绑内网,是否对外由使用方部署决定。
- **鉴权(公网必须全做)**:
  - **HMAC 签名**:每个调用方一把密钥(`app_id`+`secret`),对 `method+path+body+timestamp+nonce` 签名放 header;服务端验签。比裸 Bearer token 抗泄露(不在链路上传密钥本身)。
  - **时间戳 + nonce 防重放**:`timestamp` 超 ±5 分钟拒绝;`nonce` 一次性(短期缓存查重)。
  - **IP 白名单**(可选,CIDR)、**每 app_id 限流**、**端点级权限**(某 app 只能发某些通道/某些会话)。
- **回调(出站到第三方)**:第三方通常也要收"新消息 / 指令结果"。提供 **webhook 回调**:事件发生 → POST 到调用方登记的 URL,同样 HMAC 签名(让对方验真是我们发的)、**失败重试 + 指数退避 + 死信**。
- **异步指令**:公网往返 + 发送确认可能较慢,支持"提交即返回受理号(`202 Accepted` + `task_id`),完成后 webhook 回调结果",避免长连接阻塞;也保留同步 `confirm=true` 模式(调用方自担超时)。
- **审计**:公网入站全部记 `app_id`、来源 IP、签名校验结果、指令、对象、返回码(不记消息正文明文)。

## D.4 入口三:邮件摆渡(异步、可穿透内外网隔离)
邮件摆渡的价值是**穿透**:内外网物理隔离、只有邮件能进出时,靠收发邮件传指令与回信息。这正是 ibquote 的场景(内网工作台 ↔ 外网采集靠邮件穿透)。

### D.4.1 双向
- **入站(邮件下指令)**:轮询/推送收邮件(**POP3 收**,用户指定;也支持 IMAP)→ 按**指令邮件模板**解析成指令 → 进指令总线执行。
- **出站(邮件回信息/回执)**:采集到的 IM 消息、指令执行结果/回执 → 按**信息邮件模板**组装 → **SMTP 发**给登记收件人。

### D.4.1b 🔴 这条链路是现成的、端到端已跑通(不是从零设计)
安琳点明:**ibquote(nanyin)的收件解析,对端就是 weChatlog 的 collector 发信服务**。核对确认——collector 的 `build_message_email_html`(单条消息一封邮件)发出的字段:`群聊 / 昵称 / 消息时间 / 消息ID / 文本消息内容 / 图片-文件附件`,与 ibquote `fetcher/wechat_mail.py` 收信解析的「微信群聊消息通知」模板**逐字段对齐**、`demo.sender.build_wechat_mail` 与之同轨。即:
```
微信(chatlog 采集)─► collector 组装「微信群聊消息通知」邮件 ─SMTP─► 邮箱 ─POP3/IMAP─► ibquote fetcher 解析入库
        [发信侧,已实现]                                                      [收信侧,已实现]
```
**结论**:邮件摆渡的"信息回传"方向(IM 消息 → 邮件 → 解析)已有一整套端到端实现,本系统的邮件出站/入站直接**沿用这套模板与解析器**,不重写。本系统要新增的只有:①**指令方向**(邮件下发指令,模板 ①,collector 那套是纯信息上报、没有指令下行);②把 collector 的"采集单一群"推广成"多通道多账号"的信息源;③回执邮件(模板 ②的回执段)。

### D.4.2 复用 ibquote 邮件管线的成熟经验(不重造)
ibquote 的 `fetcher` 已经把邮件收取踩过的坑固化,直接沿用其做法:
- **收信触发**:推送为主(IMAP IDLE 秒级)+ 周期兜底全量扫描。⚠️ 用户点名 **POP3**:POP3 无 IDLE、无 UID 概念,只能**周期轮询**;若要秒级,建议 IMAP IDLE。**两种都支持,配置选**(POP3 满足"能收即可",IMAP 满足"要快")。
- **去重水位**:IMAP 用 **UID 单调水位**(不受已读/星标影响);POP3 用 **UIDL**(POP3 的消息唯一标识)做水位 + 正文 **sha256 兜底**。⚠️ 绝不拿序号当唯一键。
- **正文拉取**:IMAP 用 `BODY.PEEK`(不改已读态);附件 **Content-Type 不可信**,按**字节头/扩展名**识别类型(ibquote 教训:真实网关附件 Content-Type 不是 `image/*`)。
- **消息级去重**:落一份到磁盘/库,`(mailbox, uidl/uid, text_hash/image_hash)` 做幂等键。

### D.4.3 邮件模板(参考 ibquote 的键值对文本模板)
ibquote 的「微信群聊消息通知」模板 = **主题带前缀 + 正文键值对(每行 `键：值`,行间空行)+ 图片作 multipart 附件**。本系统推广成两类模板:

**① 指令邮件模板(入站,外部 → 本系统)**
```
主题:  QTRADE指令 [{account_id}] {op} {req_id}
正文:
QTrade 指令

指令ID：{req_id}                # 幂等键,防重复执行
账号：{account_id}              # 目标账号(企点/QQ/微信 某个号)
通道：{channel}                 # qidian | qq | wechat(可省,由 account 反查)
操作：{op}                      # send_text | send_image | read_messages | find_contact …
会话：{session}                 # 目标会话/联系人
参数：{json}                    # 结构化参数(JSON,或按 op 展开成键值)
确认：{true|false}              # 是否要回执(默认 true)
签名：{hmac}                    # 对上述字段签名,防伪造
附件:  (send_image 时,图片作附件,按字节头识别)
```

**② 信息/回执邮件模板(出站,本系统 → 外部)**——信息段**直接就是 collector/ibquote 现有「微信群聊消息通知」模板**(已端到端对齐,见 D.4.1b),本系统只在其后**追加回执段**
```
主题:  QTrade{消息|回执} [{会话名}] {摘要} {seq}
正文:
QTrade 消息通知

来源账号：{account_id}
通道：{channel}
会话：{session_name}
消息类型：{text|image|voice|receipt}
消息时间：{ISO8601 带时区}       # 沿用 ibquote 现场形态
消息ID：{msg_id}                 # 全链路幂等键
发送方：{sender}
文本内容：{text}
图片/文件附件：{名称列表}
是否撤回：{是|否}
——回执专用——
指令ID：{req_id}                 # 对应入站指令
送达状态：{DELIVERED|SEND_FAILED|SEND_CALLED_BUT_UNCONFIRMED}
确认方式：{history|get_msg|chatlog}
附件:  图片/文件作 multipart/mixed 附件
```
- 模板**版本化**(主题带模板版本号),解析侧按版本走不同 parser,避免上游漂移打破解析(ibquote 踩过 2026-08 字段漂移)。
- 模板字段是**单一来源**:构造(发)与解析(收)共用一份定义,像 ibquote 的 `demo.sender.build_wechat_mail` 与 `fetcher/wechat_mail.py` 同轨。

### D.4.4 邮件摆渡与发送确认的配合
- 入站指令若含 `send_text`,执行仍走**读回确认**(附录 C);确认结果**用出站回执邮件回给发起方**(模板 ②的回执段),形成"邮件下指令 → 执行 → 邮件回执"闭环。
- 邮件是异步的,`req_id` 贯穿始终做幂等:同一 `req_id` 的指令邮件重复收到(邮件重投)→ 只执行一次,回执可重发。

### D.4.5 配置
```
[email_transport]
enabled = true
inbound_protocol = pop3 | imap        # 用户指定 pop3;imap 更实时
inbound = { host, port, ssl, user, pass, poll_interval_s, folder }
outbound_smtp = { host, port, ssl/starttls, user, pass, from }
recipients = [ … 回信收件人 … ]
template_version = "v1"
allow_ops = [ send_text, send_image, read_messages, … ]   # 邮件入口允许的指令子集(安全)
require_signature = true               # 指令邮件必须验签,防伪造发信
```
⚠️ 邮件入口的信任级别按"公网"对待:**指令邮件必须验签**(HMAC,同 D.3),否则任何人伪造发件人就能下指令;`allow_ops` 收窄邮件能触发的操作范围。

### D.4.6 邮件定期删除(防邮箱容量不够)
收件邮箱会持续进邮件,不清理会**撑爆容量导致收不到新邮件**。所以收件箱要定期删除已处理的邮件:
- **只删"已安全处理完"的邮件**:一封邮件必须**已解析入库 / 已确认落地**后才允许删,避免删掉还没处理的。判据 = 该邮件的唯一键(IMAP UID / POP3 UIDL)已在本地处理记录里标记完成。
- **删前先本地留档**:删除前把原始邮件(.eml)与附件落一份到本地归档目录(可配保留期),**先备份再删**,防误删丢数据。归档本身也按保留期滚动清理。
- **两种触发**:①**按保留期**——收件箱里超过 `retention_days`(默认如 7 天)且已处理的邮件删除;②**按容量水位**——邮箱用量超阈值时,从最旧的已处理邮件开始删到水位以下(POP3 拿不到容量,只能按保留期/条数)。
- **协议差异**:
  - IMAP:`STORE \Deleted` 标删 + `EXPUNGE` 真删;可先移到"已处理"文件夹再延迟删。
  - POP3:`DELE` 标删,**必须 `QUIT` 正常结束**才真生效(中途断开不删);POP3 无文件夹概念,只能直接删。
- **发件箱**一般不涉及(发出去就不占本机邮箱);若用"已发送"归档也纳入同一保留期。
- **绝不删非本系统的邮件**:清理只作用于本系统用的收件邮箱/文件夹,按主题前缀(如 `QTRADE指令`/`转发：微信消息`)或专用文件夹圈定范围,不碰用户其它邮件。
- 配置补充到 `[email_transport]`:
```
retention_days = 7                 # 已处理邮件保留天数,过期删
archive_before_delete = true       # 删前先落 .eml 到本地归档
archive_dir = "…/mail_archive"     # 归档目录(自身也按 retention 滚动)
archive_retention_days = 30
cleanup_interval_min = 60          # 清理任务周期
mailbox_quota_watermark = 0.8      # (IMAP 可取容量时)超此比例从最旧已处理邮件删起
scope_subject_prefix = ["QTRADE指令", "转发：微信消息"]   # 清理范围,防误删他人邮件
```

## D.5 统一:三入口 × 一总线的落地要点
- **同一指令 schema**:HTTP body、邮件正文键值对,都反序列化成同一个 `Command` 对象再进总线。
- **同一幂等键**:本地/公网用 `idempotency_key`,邮件用 `指令ID(req_id)`,进总线后是同一个字段。
- **同一确认与回执**:三入口的发送都走读回确认;回执按来路返回——本地=HTTP 响应,公网=同步响应或 webhook,邮件=回执邮件。
- **同一审计**:所有入口统一记来路(local/http/email)、身份(token/app_id/发件人)、指令、对象、结果码。
- **信任分级**:本地(高)> 公网 HMAC / 邮件验签(低,需全套鉴权 + `allow_ops` 白名单)。

## D.6 里程碑增补
- **M4 起**:本地 API 直调(基准入口)随控制台一起可用。
- **M5**:公网入站(HMAC 签名 + 防重放 + webhook 回调 + 异步受理)、邮件摆渡(POP3/IMAP 收 + SMTP 发 + 两类模板 + 复用 ibquote 去重/PEEK/字节头识别);两者都接同一指令总线与发送确认。
- **传输适配器层**从设计上就是可插拔的:以后加"消息队列入口""内网网闸 API 入口"只是再加一个 Transport,不动指令总线与通道适配器。

## D.7 待定
1. 公网入站的暴露路径由使用方定(反代到公网服务器 / 公司 API 网关 / DMZ);本系统只提供标准 HTTP + 鉴权。需要我给一份「如何在使用方侧安全反代」的参考配置吗?
2. 邮件收取:POP3(你点名,够用但只能轮询)为主,是否也开 IMAP IDLE 选项以获得秒级?
3. 邮件指令允许的操作子集 `allow_ops`:是否限制为"只读 + 发送",不允许邮件触发"删除/作废"这类高危操作?
