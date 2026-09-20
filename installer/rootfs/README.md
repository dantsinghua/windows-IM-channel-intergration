# `installer/rootfs/` —— 两个 WSL 发行版的构建工程

规格:`docs/03` §2.6.1(kcheck)、§2.7(发行版 qtrade)、§2.2.1(manifest / `rootfs_contents` / G-10 / G-11);
`docs/02` §2.1(Agent 进程部署);`docs/05` §2.5.1(设备身份档案库)。

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
| `profiles/device_profiles.json` | 设备身份档案库 | 在**本目录**里,不在 source-root |

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
| `systemd/qtrade-agent.service` | 02 §2.1 | `After=`(不是 `Requires=`)docker —— dockerd 未就绪 Agent 也要起来,`runtime` 置 `not_ready` |
| `systemd/qtrade-container-janitor.service` | §2.7.3 E-19 ② | 监听 `docker events`,分发钩子 |
| `opt/qtrade/bin/qtrade-docker-config.sh` | §2.7.3 | 从 `install.env` 读选定段写 `daemon.json`。**不 `source` 那个文件**(等于让安装器往 root shell 注入任意命令),只 grep 两个认识的键 |
| `opt/qtrade/bin/qtrade-firstboot.sh` | §2.7.3 ②(a)~(e) | 见下「已知的一处规格张力」 |
| `opt/qtrade/bin/qtrade-container-janitor.sh` | §2.7.3 E-19 ② | **只做事件监听与钩子分发,不含任何清理动作** —— 清理逻辑归 02 §2.8.8 `_purge_ephemeral`,往 `/etc/qtrade/hooks/on-container-stop.d/` 放钩子即可 |

### 时区(G-02)两处都设

`/etc/localtime` + `/etc/timezone` 在 Dockerfile 里固化成 `Asia/Shanghai`,
`qtrade-agent.service` 里**再来一遍** `Environment=TZ=Asia/Shanghai`。
只改 `/etc/localtime` 对 Python 的 `datetime.now()` 够,对 systemd 服务不够(服务不继承交互 shell 的环境),
而 02 的 `cleanup_at="03:00"`、备份 03:30、06 的邮件每日序号全是本地时间语义 —— 少一处就差 8 小时。

### Python 3.11 而不是动 `python3` 指向

Agent 的 `requires-python >= 3.11`,jammy 的默认 `python3` 是 3.10。
装了 `python3.11` 但**不动** `/usr/bin/python3` 的指向:jammy 自己的 apt / ubuntu-advantage 等一堆
系统脚本写死跟 3.10 走,改指向会在升级时炸。Agent 走 `/opt/qtrade/agent/venv`,两边互不干涉。

Dockerfile 里那条 `pip install` 之后有一句自检:`schema_agent.sql` 与 **≥16 个**能力目录 JSON
必须真的进了包。缺能力目录会让 `expand_allow_ops(["*"])` 与 HMAC 入站的 `op_allowed`
把高危 op 全放行,而那是**装完之后才会发作**的安全问题,必须在构建期就挡住。

---

## 4. 🔴 已知的一处规格张力(未擅自改文档,待裁决)

`docs/03` §2.7.3 ② 把首启的最后两步写成:

> (d) `systemctl enable --now qtrade-agent`;(e) 第一次 `agent --init-db` 建 `agent.db`

`qtrade-firstboot.sh` **把这两步的顺序对调了**(先 `--init-db`,再 `enable --now`)。理由:

照字面先 `enable --now`,Agent 会在 `agent.db` 还不存在时启动、自己开库跑迁移(02 §2.1 第 4 步),
紧接着的 `--init-db` 就会撞上一个已被 Agent 持有(WAL + 连接)的库;两边同时建表的结果不确定,
而 02 §2.1 第 4 步明写「迁移失败**拒绝启动**」—— 一次竞争就是一次起不来,
且这种失败只在**首装**出现、复现困难。

先 `--init-db`(幂等)再 `enable --now`,两条路径的终态完全一致、没有竞争窗口。

**要么**按本节收口成 `docs/00` §15g 的新裁决,**要么**把 §2.7.3 ②(d)(e) 的次序调过来。
脚本里那段函数头注释与本节是同一份说明,改文档时两处一起改。

---

## 5. 档案库 `profiles/device_profiles.json`

按 `docs/05` §2.5.1 建,32 条真实市售机型(规格要 30~50 条),13 个品牌,
全部 Android 11 / sdk 30(与 redroid 11 一致)。

库里**只有机型模板**。`serialno` / `mac` / `generated_ms` 是每账号生成一次、永不改变的**实例**字段,
由 Agent 分配时生成后落 `device_profiles` 表与 `accounts/<id>/profile.json`,不在库里 —— 这一点
`docs/05` §2.5.1 的示例 JSON 把模板字段和实例字段画在一起,容易误读。

**一处字段名分歧(待裁决)**:`docs/02` §3.1 的 `device_profiles` 表列名是 **`profile_key`**,
而 `docs/05` §2.5.1 的示例 JSON 写的是 **`template_key`**。
按基线的冲突裁决顺序(`00 > 02(表/端点)/01 > 03~06`),本库用 **`profile_key`**。
两处措辞建议随下一条裁决统一。

⚠️ 机型参数(`build_id` / `fingerprint` / `incremental`)按公开的市售机型资料编写,
**不是从真机 dump 出来的**。企点对机型指纹的校验强度未在真机验证过,
真机验证清单里建议加一格:用库里的档案起一个 redroid 容器,`getprop` 比对后跑一次企点登录。
