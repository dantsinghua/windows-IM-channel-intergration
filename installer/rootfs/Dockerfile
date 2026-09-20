# QTrade 发行版 qtrade 的 rootfs —— 构建镜像
#
# 规格:docs/03 §2.7(发行版导入与首启)、§2.2.1(rootfs_contents / G-10 / G-11)、docs/02 §2.1(Agent 进程部署)
#
# 🔴 这个镜像**不是拿来跑容器的**,它只是「一棵文件树」的载体 —— build-rootfs.sh 用
#    `docker create` + `docker export` 把它导出成 rootfs.tar 交给 `wsl --import`。
#    所以这里没有 CMD/ENTRYPOINT 也不需要:WSL 的 init 是 /init(WSL 自带),
#    随后由 systemd(`/etc/wsl.conf` 的 `[boot] systemd=true`)接管。
#
# 🔴 预载的 redroid / napcat **镜像 tar 不在这个 Dockerfile 里** —— 它们有 ~3.5 GB,
#    进 docker 层会让构建缓存与导出各多扛一份。build-rootfs.sh 在 export 之后用
#    `tar --append` 把它们追加进 rootfs.tar 的 /var/lib/qtrade/images/。

FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG ROOTFS_VERSION=rootfs-1.0.0

# ── 基础包 ──────────────────────────────────────────────────────────────────
# systemd/systemd-sysv:§2.7.3 要 `[boot] systemd=true`,没有 systemd-sysv 就没有
#   /sbin/init 的 systemd 实现,WSL 起来后 `systemctl` 全是 "Failed to connect to bus"。
# docker.io  :发行版内的 dockerd(§2.7.3;数据目录 /var/lib/docker 留在 VHDX 内)。
# python3.11 :Agent 的 pyproject 要 requires-python >= 3.11,而 jammy 的默认 python3 是 3.10。
#              🔴 装了 3.11 也**不动** `/usr/bin/python3` 的指向 —— jammy 自己的
#              apt/ubuntu-advantage 等一堆系统脚本写死跟 3.10 走,改指向会在升级时炸。
#              Agent 走自己的 venv(下面),两边互不干涉。
# iproute2/iptables:dockerd 建网桥要用;jammy 的 docker.io 已依赖,显式列出防上游瘦身。
# sqlite3    :agent.db 的现场排障(`sq` 那类手查),02 §8b 的判据里直接在用。
RUN apt-get update && apt-get install -y --no-install-recommends \
        systemd systemd-sysv dbus \
        docker.io \
        python3.11 python3.11-venv python3.11-dev \
        ca-certificates curl iproute2 iptables \
        sqlite3 tzdata procps less nano jq \
    && rm -rf /var/lib/apt/lists/*

# ── 时区(§2.7.3 G-02)────────────────────────────────────────────────────────
# WSL 发行版默认 UTC;02 的 cleanup_at="03:00"、备份 03:30、06 的邮件每日序号「按北京日期」
# 全是本地时间语义,不设会在凌晨 11 点清理。/etc/localtime 对 Python 的 datetime.now() 够,
# 对 cron / systemd timer / 显式读 TZ 的库不够 —— 所以 qtrade-agent.service 里还有一遍
# `Environment=TZ=Asia/Shanghai`(见 files/systemd/qtrade-agent.service)。
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo 'Asia/Shanghai' > /etc/timezone

# ── 目录骨架 ────────────────────────────────────────────────────────────────
# /etc/qtrade/hooks/on-container-stop.d/ :§2.7.3 E-19 ②,03 只保证目录与钩子就位,
#                                          清理逻辑归 02(§2.8.8 _purge_ephemeral)。
# /var/lib/qtrade/images/                :预载镜像 tar 的落点(build-rootfs.sh 追加)。
RUN mkdir -p \
        /etc/qtrade/hooks/on-container-stop.d \
        /opt/qtrade/agent/dist \
        /opt/qtrade/platform-tools \
        /opt/qtrade/scrcpy \
        /opt/qtrade/frida \
        /opt/qtrade/apk \
        /opt/qtrade/profiles \
        /opt/qtrade/bin \
        /var/lib/qtrade/accounts \
        /var/lib/qtrade/images \
        /var/lib/qtrade/media \
        /var/log/qtrade

# ── 随包二进制(G-10 rootfs_contents 逐项)──────────────────────────────────
# 🔴 G-11:platform-tools 必须与 Windows 侧 pkg/adb 同版本 —— 版本不同的 adb 客户端
#    连上这里的 adb server 会把它杀掉(`adb server version doesn't match this client; killing`),
#    企点账号集体 offline(04 §2.7.4)。两边的版本号由 build-rootfs.sh 从同一次下载取,
#    并写进 contents.json 供 CI 比对。
COPY payload/platform-tools/ /opt/qtrade/platform-tools/
COPY payload/scrcpy-server   /opt/qtrade/scrcpy/scrcpy-server
COPY payload/frida-server    /opt/qtrade/frida/
COPY payload/ADBKeyboard.apk /opt/qtrade/apk/ADBKeyboard.apk
COPY payload/device_profiles.json /opt/qtrade/profiles/device_profiles.json
COPY payload/agent-wheel/     /opt/qtrade/agent/dist/

RUN chmod 0755 /opt/qtrade/platform-tools/adb /opt/qtrade/platform-tools/fastboot 2>/dev/null || true; \
    chmod 0755 /opt/qtrade/frida/frida-server-* 2>/dev/null || true; \
    chmod 0644 /opt/qtrade/scrcpy/scrcpy-server /opt/qtrade/apk/ADBKeyboard.apk

# ── Agent(02 §2.1 第 3~4 步:systemd 起 dockerd → qtrade-agent)─────────────
# venv 而不是 `pip install --break-system-packages`:Agent 的依赖(fastapi/uvicorn/
# websockets/aiosqlite/docker)与发行版自己的 python3-* 包没有必要互相牵制,
# 升级 Agent 时也只动这一个目录。
RUN python3.11 -m venv /opt/qtrade/agent/venv \
    && /opt/qtrade/agent/venv/bin/python -m pip install --no-cache-dir --upgrade pip wheel \
    && /opt/qtrade/agent/venv/bin/python -m pip install --no-cache-dir \
        /opt/qtrade/agent/dist/qtrade_agent-*.whl \
    && /opt/qtrade/agent/venv/bin/python -c "import qtrade_agent, importlib.resources as r; \
        assert (r.files('qtrade_agent.store') / 'schema_agent.sql').is_file(), 'schema_agent.sql 没进包'; \
        import json; \
        caps = [p.name for p in r.files('qtrade_agent.capabilities').iterdir() if p.name.endswith('.json')]; \
        assert len(caps) >= 16, f'能力目录只有 {len(caps)} 个,缺目录会让 expand_allow_ops([\"*\"]) 把高危 op 全放行'; \
        print('Agent 装好,能力目录', len(caps), '个')"

# 让 `adb` / `qtrade-agent` 在交互 shell 里直接可用(排障时省得敲全路径)
RUN printf '%s\n' \
        'export PATH=/opt/qtrade/platform-tools:/opt/qtrade/agent/venv/bin:$PATH' \
        > /etc/profile.d/qtrade.sh \
    && chmod 0644 /etc/profile.d/qtrade.sh

# ── 配置与 systemd 单元 ─────────────────────────────────────────────────────
COPY files/etc/wsl.conf              /etc/wsl.conf
COPY files/opt/qtrade/bin/           /opt/qtrade/bin/
COPY files/systemd/                  /etc/systemd/system/

RUN chmod 0755 /opt/qtrade/bin/*.sh && chmod 0644 /etc/systemd/system/qtrade-*.service

# 🔴 §2.7.3 R5-9 的核心:`qtrade-docker-config.service` 必须在 dockerd **首次启动之前**
#    把选定的地址池写进 /etc/docker/daemon.json,否则 dockerd 用默认 172.17 起过、
#    docker0 已建,事后 restart 也改不回来,选段白做。顺序由单元里的
#    `Before=docker.service` + 这里的 enable 共同保证。
#
#    `qtrade-agent.service` **不在这里 enable** —— §2.7.3 ②(d) 明写它由首启 oneshot
#    `systemctl enable --now qtrade-agent` 拉起,时点在 docker load 与 agent --init-db 之后。
RUN systemctl enable qtrade-docker-config.service \
    && systemctl enable qtrade-firstboot.service \
    && systemctl enable qtrade-container-janitor.service \
    && systemctl enable docker.service

# ── 版本标记 ────────────────────────────────────────────────────────────────
# /etc/qtrade/contents.json(G-10)由 build-rootfs.sh 在 export 之后写入 —— 它要含
# 镜像 tar 的 sha256,而那两个 tar 这时候还没进来。这里只留一个人眼可读的版本串。
RUN echo "$ROOTFS_VERSION" > /etc/qtrade/rootfs-version

# 明确不设 CMD:见文件头,这棵树是给 `wsl --import` 的,不是给 `docker run` 的。
