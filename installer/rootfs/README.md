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

## 8. 本轮:依赖上锁(2026-09-21 14:10,基于提交 `d134964`)

起因:独立端到端第二轮复测的 **R-1**——发行版依赖只在 `pip install <wheel>` 时按「构建当天 PyPI
最新且满足下限」解析,`contents.json` 只登记 wheel 的 sha256、不登记依赖集合 ⇒ 同一份 wheel 在
不同日期能重建出行为不同的发行版(已见 uvicorn `ws="websockets"`、websockets.legacy、starlette
testclient 三处弃用信号),追溯不到。本轮把运行期依赖**锁死**。

### 三个文件

| 文件 | 作用 |
|---|---|
| `requirements.in` | 顶层运行期依赖,与 `pyproject.toml` 的 `[project] dependencies` 同下限(fastapi/uvicorn/websockets/aiosqlite) |
| `runtime-constraints.txt` | 版本事实来源 = 开发机 `~/.venvs/qtrade`(全量测试全绿)的 `uv pip freeze`;作 `uv pip compile --constraint`,把闭包版本钉到这套已验证的版本 |
| `requirements.lock` | `uv pip compile` 生成的**带 sha256 hash** 全集(15 个运行期包);Dockerfile 用 `--require-hashes` 安装 |

### 重新生成锁(需出网走代理)

```bash
export https_proxy=http://172.19.176.1:7890 http_proxy=http://172.19.176.1:7890 no_proxy=localhost,127.0.0.1
# 事实来源:先在开发机 venv 上刷新 runtime-constraints.txt(可选)
uv pip freeze --python ~/.venvs/qtrade/bin/python > installer/rootfs/runtime-constraints.txt   # 顶部注释需保留/补回
# 生成 lock
uv pip compile installer/rootfs/requirements.in \
    --constraint installer/rootfs/runtime-constraints.txt \
    --generate-hashes --python-version 3.12 \
    --output-file installer/rootfs/requirements.lock
```

### 构建侧改动

- `Dockerfile`:新增 `COPY payload/requirements.lock`;依赖装法由 `pip install <wheel>` 改为
  **先** `pip install --require-hashes --only-binary=:all: -r requirements.lock`(装齐运行期依赖)、
  **再** `pip install --no-deps --only-binary=:all: <wheel>`(只装 Agent 本体)。任一包 hash 对不上直接构建失败,
  不静默换版本、也不静默改走源码构建(`--only-binary` 的原因见下一小节)。
- `build-rootfs.sh`:备料时把 `requirements.lock` 拷进构建上下文;`contents.json` 新增
  `requirements_lock { path, packages, sha256 }` 一项 —— 依赖集合从此也进了 G-10 完整性清单(正是 R-1 要的)。

### 为什么两处 install 都要 `--only-binary=:all:`(E3-1,2026-09-21 补)

独立端到端第三轮发现:`uv pip compile --generate-hashes` 为每个包**同时**登记 wheel 与同版本 sdist
的 hash,而 pip 的 `--require-hashes` 是「命中该包任一登记 hash 即放行」。只写 `--require-hashes` 时,
一旦 pip 选中的 wheel 的 hash 对不上(锁被改坏,或 PyPI / 代理上的 wheel 被替换),pip **不报错**,
而是退回同版本 sdist(其 hash 仍在锁里、校验通过),再联网装**不在锁内、不校验 hash** 的构建后端
(fastapi ⇒ `pdm-backend`,pydantic-core ⇒ maturin + 现场下载 Rust)现场编译。纯 Python 包能编译成功,
`pip freeze` 仍与锁逐行一致 ⇒ 自检看不出,「改坏任一 hash 就构建失败」的承诺并不成立。

修法:两处 `pip install` 都加 `--only-binary=:all:` ⇒ 只许装 wheel,sdist 退路被堵死,wheel hash 不符即报
`THESE PACKAGES DO NOT MATCH THE HASHES`、构建失败。锁文件**不改**:锁里的 sdist hash 从此永远用不到,
留着无害;不改锁可保持上面的再生成命令逐字复现同一份 lock(sha256 不变,`contents.json` 的
`requirements_lock` 登记不变)。第二处装的是本地 Agent `.whl`,本来就不走 sdist,同样加上是防日后
有人把路径改成源码目录 / sdist 时静默走构建。
重建后可在构建日志里核对:不应出现 `Building wheel for` / `Installing build dependencies`。

### `wheels.expected`:实装 wheel 逐包对清单(E4-1,2026-09-21 补)

独立端到端第四轮发现:`--only-binary=:all:` 只堵 sdist,堵不住「另一个也在锁里的 wheel」。锁为每个包登记了
该版本 PyPI 上**全部**文件的 hash;同一包在目标平台(cp312 / manylinux x86_64,glibc 2.35)若有 ≥2 个可用
wheel,被选中那个的 hash 对不上时 pip 把它静默滤掉、改装另一个,rc=0、`pip freeze` 与锁一致。逐包核过:
15 个包里**只有 websockets 17.1** 如此(cp312 manylinux 带 C 加速 / py3-none-any 纯 Python),其余 14 包在目标
平台各只有 1 个可用 wheel(pydantic-core 只有 cp312 manylinux_2_17,无纯 Python wheel)。
收窄锁(只留目标平台 hash)解决不了:websockets 的两个 wheel **都**是目标平台可用的。

修法按「类」设防,不点名某个包:

| 文件 | 作用 |
|---|---|
| `wheels.expected` | 15 行 `规范名 版本 wheel 文件名 sha256` —— 锁完好时 pip 在目标平台选中的那一个文件。随包进 rootfs 的 `/opt/qtrade/agent/wheels.expected`,`contents.json` 登记为 `wheel_manifest { path, packages, sha256 }` |
| `verify-wheels.py` | `check`:Dockerfile 装完依赖紧接着跑,断言 ①清单包集合 = 锁包集合、每行版本 = 锁、sha256 ∈ 锁;②`pip install --report` 里实装包集合 = 清单、下载文件名与 sha256 = 清单;③site-packages 里 dist-info 集合 = 清单 ∪ {pip}、每包 `WHEEL` 的 Tag 集合 = 清单文件名展开的 tag、版本 = 清单、RECORD 在;④ pip 版本 = `pip-bootstrap.lock`。任一不符 `exit 1` ⇒ 构建失败。`emit`:再生成用 |
| `gen-wheel-manifest.sh` | 再生成清单:在构建镜像(jammy + python3.12)里按 Dockerfile 同样的 pip 版本与开关对锁跑 `pip install --dry-run --report`,交给 `verify-wheels.py emit`。只 `docker build --output type=local`,不产生镜像 / 容器 |

再生成(需出网走代理;基础镜像默认 `qtrade-build/rootfs:rootfs-1.0.0`,本机没有就先跑一次 build-rootfs.sh,或 `--base` 指定任一 jammy + deadsnakes python3.12 的镜像):

```bash
export https_proxy=http://172.19.176.1:7890 http_proxy=http://172.19.176.1:7890 no_proxy=localhost,127.0.0.1
bash installer/rootfs/gen-wheel-manifest.sh > installer/rootfs/wheels.expected
git diff installer/rootfs/wheels.expected   # 人眼看文件名变化(尤其平台 wheel ↔ py3-none-any)再提交
```

**何时必须再生成**:`requirements.lock` 变了(重新 compile、升降任一版本)、`pip-bootstrap.lock` 的 pip 版本变了、
或发行版 Python 的 minor / glibc 基线变了(Dockerfile 的 `ARG PY`、`FROM`)。忘了再生成会怎样:包数对不上时
`build-rootfs.sh` 备料阶段就 `die`;包数相同但版本 / 文件变了时 Dockerfile 的 `verify-wheels.py check` 报不符、构建失败
—— 都不会静默出包。

### pip 钉版本(E3-O1,2026-09-21 补)

原先 `pip install --upgrade pip` 不带版本与 hash ⇒ 执行整个 `--require-hashes` 校验的那个 pip 随构建日期漂移。
现改为按 `pip-bootstrap.lock` 升级:`pip==26.2.1`(= 上一版 rootfs `b9771c36…` 里实际装着的版本),只登记 wheel
`pip-26.2.1-py3-none-any.whl` 的 sha256,安装时带 `--require-hashes --only-binary=:all:` ⇒ hash 不符即构建失败;
`verify-wheels.py check` 再断言 venv 里 pip 版本 = 钉值。`pip-bootstrap.lock` 与 `verify-wheels.py` 只在构建期用,
Dockerfile 在同一 RUN 末尾删掉,不进 rootfs。升级 pip:改 `pip-bootstrap.lock` 的版本与 hash(PyPI
`https://pypi.org/pypi/pip/<ver>/json` 里 `py3-none-any.whl` 那条 `digests.sha256`),再跑 `gen-wheel-manifest.sh`。

### 连带:`pyproject.toml` 删了 `docker>=7`

全仓无 `import docker`(dockerd 走子进程),该依赖声明未用。删除后发行版 venv 少
`docker/requests/urllib3/charset-normalizer` 四包(锁解析确认它们非运行期传递依赖,故不进锁),
供应链面收窄。产物 sha256 与八项自检(尤其 ⑧ `pip freeze` 与锁逐行一致)登记在产物根 `SOURCES.md` §13,
交接见 `.omc/handoffs/rootfs-rebuild-2.md`。
