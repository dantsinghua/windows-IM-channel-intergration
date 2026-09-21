# `installer/rootfs/` —— 两个 WSL 发行版的构建工程

规格:`docs/03` §2.6.1(kcheck)、§2.7(发行版 qtrade)、§2.2.1(manifest / `rootfs_contents` / G-10 / G-11);
`docs/02` §2.1(Agent 进程部署);`docs/05` §2.5.1 + §7(设备身份档案库与其落盘路径);
裁决:`docs/00` §15g **R6-60 (a)(b)**、**R6-61/W4**、**R6-62 Ⅰ(i) / Ⅶ②**。

安装包要往 WSL 里导两个发行版,这里各有一套构建:

| 产物 | 脚本 | 体积 | 干什么用 |
|---|---|---|---|
| `kcheck-rootfs.tar` | `build-kcheck.sh` | ~1.3 MB | **只**用来回答「换上自编内核后 WSL2 还能不能起发行版」。切内核前在**官方内核**上预先导入(§2.6.1 W14) |
| `rootfs.tar` | `build-rootfs.sh` + `Dockerfile` + `files/` | ~3 GB | 发行版 `qtrade` 本体:Ubuntu 22.04 + docker + 预载镜像 + Agent |

---

## 1. kcheck

```bash
./build-kcheck.sh --out <产物根>/rootfs/out/kcheck-rootfs.tar
# 离线(busybox 已下好时):
./build-kcheck.sh --out … --busybox /path/to/busybox-x86_64
```

只有一个静态链接的 busybox 加几个配置文件。三条硬要求写在脚本文件头,改之前先读:
**极小**(它在切内核前后各被拉起一次)、**无 systemd**、**无 interop / 无 automount**
——后两条是为了不给「内核能不能起来」这个判据加无关的失败源。

busybox 的 sha256 钉死在脚本里(`BUSYBOX_SHA256`),换版本时连那一行一起改。

---

## 2. 发行版 qtrade

```bash
export https_proxy=http://172.19.176.1:7890 http_proxy=http://172.19.176.1:7890
./build-rootfs.sh \
    --source-root /mnt/c/Users/anlin/qtrade-payload \
    --images-dir  ~/work/qtrade-payload-build/images \
    --out         /mnt/c/Users/anlin/qtrade-payload/rootfs/out/rootfs.tar
```

### 输入(`--source-root` 下)

| 路径 | 内容 | 备注 |
|---|---|---|
| `dist/qtrade_agent-*.whl` | Agent wheel | 恰好一个;多于一个直接报错(装哪个不确定) |
| `third_party/platform-tools-linux/` | Linux 版 platform-tools | 🔴 **必须与 `third_party/platform-tools/`(Windows 版)同 `Pkg.Revision`**,脚本里是硬闸 |
| `third_party/scrcpy/scrcpy-server` | scrcpy-server | 与 Windows 侧 scrcpy 客户端同版本 |
| `third_party/frida/frida-server-*-android-x86_64` | frida-server | 恰好一个 |
| `third_party/apk/ADBKeyboard.apk` | ADBKeyboard | |
| `profiles/device_profiles.json` | 设备身份档案库 | 在**本目录**里,不在 source-root;落进 rootfs 的路径 = `docs/05` §7 的 `/opt/qtrade/agent/data/device_profiles.json`(§5) |

`--images-dir` 放两个 `docker save` 出来的 tar:`redroid-11.tar`、`napcat.tar`。

### 流程

1. 备料到临时目录(顺带跑 **G-11 版本一致性硬闸**);
2. `docker build` 出 `qtrade-build/rootfs:<ver>`;
3. `docker create` + `docker export` → `rootfs-base.tar`;
4. 生成 `/etc/qtrade/contents.json`(G-10 内容清单);
5. `tar --append` 把预载镜像与 contents.json 追加进去 → `rootfs.tar`。

> 🔴 预载镜像**不进 Dockerfile** —— 3.5 GB 进 docker 层会让构建缓存与导出各多扛一份。
> `docker export` 出来的是无压缩流式 tar,可以直接 `--append`,比「解包 → 重打」快一个量级,
> 也不会在解包/重打过程中丢 uid/mode。

> 工作目录固定在 `/var/tmp`(WSL 原生盘),**不能**放 `/mnt/c` —— 9p 上 export 与 append 慢一个量级。

### `--skip-images`

不追加预载镜像,出一个只验流程的瘦 rootfs(`contents.json` 里两项标 `present:false`)。
用来快速验 Dockerfile / systemd 单元的改动,不用每次等 1.4 GB 的 tar 复制。

---

## 3. `files/` 里都是什么

| 文件 | 规格 | 一句话 |
|---|---|---|
| `etc/wsl.conf` | §2.7.3 | `systemd=true` / `default=root` / `automount=true` / `appendWindowsPath=false` |
| `systemd/qtrade-docker-config.service` | §2.7.3 R5-9 | 🔴 `Before=docker.service`。**这条 Before= 是整个地址池选段机制的成败所在**,别改成 After= |
| `systemd/qtrade-firstboot.service` | §2.7.3 ② | `After=docker.service`,`docker load` 两镜像 → 建库 → 起 Agent → 写 `.imported` |
| `systemd/qtrade-agent.service` | 02 §2.1 | `After=`(不是 `Requires=`)docker —— dockerd 未就绪 Agent 也要起来,`runtime` 置 `not_ready`;🔴 `StartLimitIntervalSec`/`StartLimitBurst` 在 **`[Unit]`** 段(systemd ≥230),写进 `[Service]` 会被静默忽略 |
| `systemd/qtrade-container-janitor.service` | §2.7.3 E-19 ② | 监听 `docker events`,分发钩子 |
| `opt/qtrade/bin/qtrade-docker-config.sh` | §2.7.3 | 从 `install.env` 读选定段写 `daemon.json`。**不 `source` 那个文件**(等于让安装器往 root shell 注入任意命令),只 grep 两个认识的键 |
| `opt/qtrade/bin/qtrade-firstboot.sh` | §2.7.3 ②(a)~(e) | (a) load 镜像 →(d)`--init-db` →(e)`enable --now` →(b)写 `.imported`;🔴 (d) **无条件调用**,不设「库在就跳过」的闸门(§4) |
| `opt/qtrade/bin/qtrade-container-janitor.sh` | §2.7.3 E-19 ② | **只做事件监听与钩子分发,不含任何清理动作** —— 清理逻辑归 02 §2.8.8 `_purge_ephemeral`,往 `/etc/qtrade/hooks/on-container-stop.d/` 放钩子即可 |

### 时区(G-02)两处都设

`/etc/localtime` + `/etc/timezone` 在 Dockerfile 里固化成 `Asia/Shanghai`,
`qtrade-agent.service` 里**再来一遍** `Environment=TZ=Asia/Shanghai`。
只改 `/etc/localtime` 对 Python 的 `datetime.now()` 够,对 systemd 服务不够(服务不继承交互 shell 的环境),
而 02 的 `cleanup_at="03:00"`、备份 03:30、06 的邮件每日序号全是本地时间语义 —— 少一处就差 8 小时。

### Python:deadsnakes 的 **3.12 正式版**,且不动 `python3` 指向

Agent 的 `requires-python >= 3.11`,jammy 的默认 `python3` 是 3.10,所以要另装一个。

🔴 **不能用 jammy 自带源的 `python3.11`** —— 它是 `3.11.0~rc1-1~22.04.1`,
一个 **release candidate**(独立端到端测试 D-03 实测:venv 里 `python --version` = `Python 3.11.0rc1`)。
生产发行版不该跑候选版。

改走 **deadsnakes PPA**(`ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu jammy`,
Dockerfile 里按**密钥指纹** `F23C5A6CF475977595C89F51BA6932366A755776` 逐字校验后才加源),
它在 jammy 上提供 `python3.11=3.11.15` 与 `python3.12=3.12.13`,**两个都是正式版**。

**选 3.12(`ARG PY=python3.12`)的理由**:两者都满足 `requires-python >= 3.11`,
而本仓库的全量测试(≈1900+ 条)跑在开发机的 **3.12.3** 上 —— 取同一个 minor,
「测过的解释器」与「发出去的解释器」才是同一条线;选 3.11 等于发一个从没跑过测试的 minor。
要回退到 3.11 只改 `ARG PY` 一处(下文一律用 `${PY}`)。

⚠️ deadsnakes 只装 `/usr/bin/python3.12`,**不动** `/usr/bin/python3` 的指向:jammy 自己的
apt / ubuntu-advantage 等一堆系统脚本写死跟 3.10 走,改指向会在升级时炸。
Agent 走 `/opt/qtrade/agent/venv`,三边互不干涉。

**构建期自检**(Dockerfile 里,任一条不过直接中止构建):

| 自检 | 拦什么 |
|---|---|
| `pip check` | Agent 依赖在该 Python 上有冲突 |
| `sys.version` 不含 `rc`/`a`/`b` 且 `releaselevel == 'final'` | 又拿到一个候选版(D-03 那类) |
| 按**默认配置**加载机型档案库,条数 = 库文件条数 | 档案库落错目录(见 §5) |

Dockerfile 里那条 `pip install` 之后有一句自检:`schema_agent.sql` 与 **≥16 个**能力目录 JSON
必须真的进了包。缺能力目录会让 `expand_allow_ops(["*"])` 与 HMAC 入站的 `op_allowed`
把高危 op 全放行,而那是**装完之后才会发作**的安全问题,必须在构建期就挡住。

---

## 4. ✅ 首启 (d)(e) 的顺序与「无条件调 `--init-db`」——**已裁决**

> 本节原为「已知的一处规格张力(待裁决)」。两条都已收口,裁决号见下;
> `qtrade-firstboot.sh` 的 `init_db_and_start_agent()` 函数头与本节是同一份说明,改一处要改两处。

### ① 顺序对调 —— **`docs/00` §15g R6-60 (a)**

`docs/03` §2.7.3 ② 原写「(d) `systemctl enable --now qtrade-agent`;(e) 第一次 `agent --init-db`」。
照字面先 `enable --now`,Agent 会在 `agent.db` 还不存在时启动、自己开库跑迁移(02 §2.1 第 4 步),
紧接着的 `--init-db` 就撞上一个已被 Agent 持有(WAL + 连接)的库;
而 02 §2.1 第 4 步明写「迁移失败**拒绝启动**」—— 一次竞争就是一次首装失败,且只在首装出现、复现困难。

**裁决:采纳实现的顺序** —— **(d) `agent --init-db`;(e) `systemctl enable --now qtrade-agent`**。
两条路径终态一致、没有竞争窗口。`docs/03` §2.7.3 ② 已按此改写,编号也已对调
(所以现在 (d) 是建库、(e) 是拉起服务,别再按旧编号读)。

### ② 🔴 **不得自加「库文件在就不调 `--init-db`」的前置闸门** —— **R6-61/W4 + R6-62 Ⅰ(i)**

`--init-db` **自身幂等**:已是最新则一条语句都不写、回 `0`;落后则按 `schema_version` 把迁移跑完。
所以调用方**不需要**、也**不允许**在外面加一道「库在就跳过」的闸门 —— 那等于把 R6-61/W4 刚作废的
「库已存在即跳过」语义换个地方(shell 层)写活,后果有两条:

1. **升级 / 修复路径上迁移不跑**,库静默停在旧 schema;
2. 失败退化成「Agent 起不来」,拿不到可诊断的 `3`/`4`/`5`
   —— `docs/03` §8b.3 **M1-12b ②③** 正是因此**交付即恒红**。

本轮已删掉脚本里那道 `[ -f "$AGENT_DB" ]` 闸门(D-01 修复后的连带项,R6-62 Ⅴ①)。

### ③ 退出码分诊(owner = `docs/02` §2.1)

`--init-db` 非 `0` 时,首启脚本按码分别记日志再 `die`:

| 码 | 含义 | 脚本处置 |
|---|---|---|
| `0` | 成功(含「已最新、什么都没改」的幂等路径) | 继续 (e) |
| `2` | argparse 用法错误 | `die` —— 首启脚本与 wheel 版本对不上 |
| `3` | 库损坏 / 不是 SQLite(`quick_check` 未过),**不动该文件** | `die`,提示带诊断包报障、别自行删库 |
| `4` | 库 `schema_version` 高于本版代码上限,**不动该文件** | `die`,不支持降级 |
| `5` | 其它(父目录建不出 / DDL 或迁移失败 / 磁盘满 / 权限不足) | `die`,提示清磁盘修权限后重跑;下次**无条件再调一次**续跑 |

⚠️ 这三个码**只在本机首启日志里可见**(`journalctl -t qtrade-firstboot`)。
`die` 之后 Agent 没起来,安装引擎只看得到 `systemctl is-active` 不是 active
⇒ **引擎侧统一表现为 `AGENT_NOT_READY`**(退出码 `75`,`docs/03` §5.1 / §3.4)。
要让引擎区分这三个码须由首启另落标记文件供引擎读 = 新设计,须另起裁决。

---

## 5. 档案库 `profiles/device_profiles.json`

按 `docs/05` §2.5.1 建,32 条真实市售机型(规格要 30~50 条),13 个品牌,
全部 Android 11 / sdk 30(与 redroid 11 一致)。

库里**只有机型模板**。`serialno` / `mac` / `generated_ms` 是每账号生成一次、永不改变的**实例**字段,
由 Agent 分配时生成后落 `device_profiles` 表与 `accounts/<id>/profile.json`,不在库里 —— 这一点
`docs/05` §2.5.1 的示例 JSON 把模板字段和实例字段画在一起,容易误读。

### ✅ 字段名 —— **已裁决:`profile_key`**(`docs/00` §15g **R6-60 (b)**)

`docs/02` §3.1 的 `device_profiles` 表列名是 **`profile_key`**,而 `docs/05` §2.5.1 的示例 JSON
曾写 **`template_key`**。裁决**统一用 `profile_key`**(以 02 §3.1 DDL 为准),`docs/05` 三处旧名已作废。
本库用的就是 `profile_key`;Agent 侧 `device_profiles.parse_library()` 对旧名 `template_key`
**直接拒收**(静默接受等于把废名养活)。

### ✅ 落盘路径 —— **已裁决:`/opt/qtrade/agent/data/device_profiles.json`**(`docs/00` §15g **R6-62 Ⅶ②**)

裁决:**以 `docs/05` §7 `[device_profiles] library` 写的那一个为准,文档不改;rootfs 侧改落点。**

这一个路径要在**三处**逐字同值,任一处漂了都不会报错、只会静默降级:

| 处 | 位置 |
|---|---|
| 配置默认值 | `src/qtrade_agent/config.py` `DeviceProfilesConfig.library` |
| rootfs 落点 | `Dockerfile` 的 `COPY payload/device_profiles.json …` |
| G-10 登记 | `build-rootfs.sh` 写 `contents.json` 的 `device_profiles.path` |

🔴 **落错的后果是静默的**:Agent 按配置默认值读不到文件时**不拒绝启动**,它回落到
`device_profiles.py` 里那 **10 条内置保底清单**、只记一条 ERROR —— 机型池从 32 条缩到 10 条,
要等真机上新建企点账号才发作。本轮起 Dockerfile 里有一条**构建期自检**拦它:
用镜像里的 Agent venv 按 `AgentConfig()` 的**默认配置**加载档案库,断言
`source == 'library'`(不是回落清单)且条数 = 库文件里的条数。
(改前的落点 `/opt/qtrade/profiles/` 正是那个对不上的旧路径。)

⚠️ 机型参数(`build_id` / `fingerprint` / `incremental`)按公开的市售机型资料编写,
**不是从真机 dump 出来的**。企点对机型指纹的校验强度未在真机验证过,
真机验证清单里建议加一格:用库里的档案起一个 redroid 容器,`getprop` 比对后跑一次企点登录。

---

## 6. 本次重建改了什么(2026-09-21,修 D-01 连带项 + D-02 + D-03 + R6-62 Ⅶ②)

起因:独立端到端测试报告 `.omc/handoffs/e2e-rootfs.md` 的 D-01~D-04,与第五/六轮终审转出
rootfs 方的两条(`docs/00` §15g **R6-62 Ⅴ①⑦**)。

| # | 改了什么 | 文件 | 为什么 |
|---|---|---|---|
| 1 | 删掉 `[ -f "$AGENT_DB" ]` 那道「库在就跳过 `--init-db`」的闸门,改**无条件调用**;非零退出按 `3`/`4`/`5` 分别记日志再 `die` | `files/opt/qtrade/bin/qtrade-firstboot.sh` | R6-62 Ⅰ(i)/Ⅴ①:闸门 = 把 R6-61/W4 作废的语义换个地方写活,升级路径上迁移不跑;且 `docs/03` §8b.3 M1-12b ②③ 交付即恒红。顺序仍是 (d)`--init-db` → (e)`enable --now`(R6-60 (a)) |
| 2 | `StartLimitIntervalSec` / `StartLimitBurst` 从 `[Service]` 移到 `[Unit]` | `files/systemd/qtrade-agent.service` | D-02:systemd ≥230 起这两键属 `[Unit]`,写在 `[Service]` 被静默忽略(`Unknown key name … ignoring.`)⇒ 限流永不触发、启动失败时无限 auto-restart 刷 journal、`is-active` 永远不是 `failed` |
| 3 | Python 从 jammy 源的 `python3.11`(= **3.11.0rc1** 候选版)换成 deadsnakes PPA 的 **`python3.12` = 3.12.13 正式版**;加三条构建期自检(`pip check`、正式版判定、档案库加载) | `Dockerfile` | D-03;版本选择的理由见 §3「Python」一节 |
| 4 | 机型档案库落点 `/opt/qtrade/profiles/` → **`/opt/qtrade/agent/data/`**(`Dockerfile` 的 COPY、目录骨架、`contents.json` 的 `device_profiles.path` 三处) | `Dockerfile`、`build-rootfs.sh` | R6-62 Ⅶ②:以 `docs/05` §7 `[device_profiles] library` 为准。落错不报错、只静默回落到 10 条内置清单(详见 §5) |
| 5 | §4 §5 两节由「待裁决」改为「已裁决」并指向裁决号;§3 的 Python 一节重写;本节新增 | `README.md` | R6-62 Ⅴ③ 点名的 installer 侧遗留 |

**未改**(仍是 payload 交接 §6「几条别改坏的地方」里的原样):
`qtrade-docker-config.service` 的 `Before=docker.service`、`qtrade-agent.service` 的 `After=`(非 `Requires=`)docker、
janitor 只监听不清理、时区两处都设、不动 `/usr/bin/python3` 指向、`qtrade-docker-config.sh` 不 `source` `install.env`、
预载镜像不进 Dockerfile 而用 `tar --append`、工作目录固定 `/var/tmp`。

## 7. 再次重建(2026-09-21 13:00,基于提交 `0180100`,构建流程未改)

只为换上新 Agent wheel(含 D-05 `--init-db` 字节级幂等、后端第四~六批、两批安全修复、企点解码 R6-66),
`Dockerfile`、`build-rootfs.sh`、`files/` 一字未改。产物 sha256 与自检结果登记在产物根 `SOURCES.md` §12,
交接见 `.omc/handoffs/rootfs-rebuild.md`。本次首启 `--init-db` 连跑三次主库 sha256 完全相同 ⇒ §6 之后
遗留的 D-05(字节级不幂等)在发行版上已闭合。
