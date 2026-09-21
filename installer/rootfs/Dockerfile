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
# Agent venv 的 Python。🔴 **必须是正式版**,构建期有自检(见下面的「Python 正式版自检」)。
ARG PY=python3.12
# deadsnakes PPA 的签名密钥指纹(Launchpad 公示值),下面按指纹逐字校验,不信任裸下载。
ARG DEADSNAKES_KEY=F23C5A6CF475977595C89F51BA6932366A755776

# ── Python 正式版的来源:deadsnakes PPA ──────────────────────────────────────
# 🔴 为什么不用 jammy 自带源:`apt-get install python3.11` 在 jammy 上拿到的是
#    **3.11.0~rc1-1~22.04.1 —— 一个 release candidate**(独立端到端测试 D-03 实测),
#    生产发行版不该跑候选版(3.11.0 final 之后修过不少 asyncio/tomllib/gc 的 bug)。
#    deadsnakes 在 jammy 上提供 python3.11=3.11.15 与 python3.12=3.12.13,都是正式版。
# 🔴 选 3.12 而不是 3.11 的理由:`pyproject.toml` 的 `requires-python = ">=3.11"` 两者都满足,
#    而本仓库的全量测试(≈1900+ 条)跑在开发机的 **3.12.3** 上 —— 发行版取同一个 minor,
#    「测过的解释器」与「发出去的解释器」才是同一条线;选 3.11 等于发一个从没跑过测试的 minor。
#    (若将来要回退到 3.11,只改 ARG PY 即可,下面一律用 $PY。)
# 🔴 与「不动 /usr/bin/python3」不冲突:deadsnakes 只装 /usr/bin/python3.12,
#    jammy 自己的 apt / ubuntu-advantage 等系统脚本继续跟 3.10 走。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${DEADSNAKES_KEY}" \
         | gpg --dearmor -o /usr/share/keyrings/deadsnakes.gpg \
    && gpg --no-default-keyring --keyring /usr/share/keyrings/deadsnakes.gpg \
           --list-keys --with-colons | grep -q "^fpr:::::::::${DEADSNAKES_KEY}:" \
    && echo "deb [signed-by=/usr/share/keyrings/deadsnakes.gpg] https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu jammy main" \
         > /etc/apt/sources.list.d/deadsnakes.list \
    && rm -rf /var/lib/apt/lists/*

# ── 基础包 ──────────────────────────────────────────────────────────────────
# systemd/systemd-sysv:§2.7.3 要 `[boot] systemd=true`,没有 systemd-sysv 就没有
#   /sbin/init 的 systemd 实现,WSL 起来后 `systemctl` 全是 "Failed to connect to bus"。
# docker.io  :发行版内的 dockerd(§2.7.3;数据目录 /var/lib/docker 留在 VHDX 内)。
# $PY(python3.12):Agent 的 pyproject 要 requires-python >= 3.11,而 jammy 的默认 python3 是 3.10。
#              来源与版本选择见上一段(deadsnakes 正式版,不是 jammy 源里的 3.11.0rc1)。
#              🔴 装了它也**不动** `/usr/bin/python3` 的指向 —— jammy 自己的
#              apt/ubuntu-advantage 等一堆系统脚本写死跟 3.10 走,改指向会在升级时炸。
#              Agent 走自己的 venv(下面),两边互不干涉。
# iproute2/iptables:dockerd 建网桥要用;jammy 的 docker.io 已依赖,显式列出防上游瘦身。
# sqlite3    :agent.db 的现场排障(`sq` 那类手查),02 §8b 的判据里直接在用。
RUN apt-get update && apt-get install -y --no-install-recommends \
        systemd systemd-sysv dbus \
        docker.io \
        ${PY} ${PY}-venv ${PY}-dev \
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
        /opt/qtrade/agent/data \
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
# 🔴 落点 = docs/05 §7 `[device_profiles] library` 的那一个(裁决 00 §15g R6-62 Ⅶ②:
#    以 05 §7 为准、文档不改)。Agent 侧的配置默认值 `src/qtrade_agent/config.py`
#    `DeviceProfilesConfig.library` 与它逐字同值;落错目录 ⇒ Agent 按默认路径读不到、
#    回落到 `device_profiles.py` 内置的 10 条小清单并记 ERROR(机型池只剩那几条)。
#    此前落在 /opt/qtrade/profiles/ —— 正是那个对不上的旧落点。
COPY payload/device_profiles.json /opt/qtrade/agent/data/device_profiles.json
COPY payload/agent-wheel/     /opt/qtrade/agent/dist/
# 🔴 依赖锁(R-1):运行期依赖的带 hash 锁文件,由 build-rootfs.sh 从
#    installer/rootfs/requirements.lock 备进构建上下文(uv pip compile 生成,版本钉死
#    到开发机 ~/.venvs/qtrade 那套已验证版本)。下面用 --require-hashes 安装 ⇒ 同一份
#    wheel 在不同日期重建出的依赖集合一致、可复现;并登记进 contents.json。
COPY payload/requirements.lock /opt/qtrade/agent/requirements.lock

RUN chmod 0755 /opt/qtrade/platform-tools/adb /opt/qtrade/platform-tools/fastboot 2>/dev/null || true; \
    chmod 0755 /opt/qtrade/frida/frida-server-* 2>/dev/null || true; \
    chmod 0644 /opt/qtrade/scrcpy/scrcpy-server /opt/qtrade/apk/ADBKeyboard.apk

# ── Agent(02 §2.1 第 3~4 步:systemd 起 dockerd → qtrade-agent)─────────────
# venv 而不是 `pip install --break-system-packages`:Agent 的依赖(fastapi/uvicorn/
# websockets/aiosqlite)与发行版自己的 python3-* 包没有必要互相牵制,
# 升级 Agent 时也只动这一个目录。
# 🔴 依赖用**锁文件 + --require-hashes** 装(R-1):运行期闭包的版本钉死到
#    requirements.lock(uv pip compile 生成、带 sha256 hash),而不是「构建当天 PyPI
#    最新且满足下限」。之后 wheel 用 --no-deps 装(依赖已由锁装齐),两次不同日期的
#    重建得到逐位一致的依赖集合。任一包 hash 对不上直接构建失败,不静默换版本。
# 🔴 两处 install 都带 `--only-binary=:all:`(E3-1,独立端到端第三轮):uv 生成的锁为每个包
#    **同时**登记 wheel 与同版本 sdist 的 hash,pip 的 --require-hashes 语义是「命中该包
#    任一登记 hash 即放行」。不加这个开关时,被选中 wheel 的 hash 一旦对不上(锁被改坏,或
#    PyPI/代理上的 wheel 被替换),pip **不报错**,而是退回同版本 sdist(其 hash 仍在锁里、
#    校验通过),再联网装**不在锁内、不校验 hash** 的构建后端(fastapi ⇒ pdm-backend、
#    pydantic-core ⇒ maturin + 现场下 Rust)现场编译 —— 纯 Python 包能编译成功、freeze 仍与锁
#    一致,自检看不出。只许装 wheel 后,sdist 这条退路被堵死 ⇒ wheel hash 不符即
#    `THESE PACKAGES DO NOT MATCH THE HASHES`、构建失败。锁里的 sdist hash 因此永不会被用到,
#    留着无害(锁文件本身不改,保持 README §8 的再生成命令可逐字复现)。
#    第二处装的是本地 Agent .whl,本来就不走 sdist;同样加上,免得日后有人把路径改成
#    源码目录 / sdist 时静默走构建。
# 🔴 只 `--upgrade pip`,不装 `wheel`:发行版 venv 只**安装**预编译 .whl(锁里 15 个包与
#    Agent wheel 都有对应 wheel),装 .whl 不需要 `wheel` 包(它只在 build sdist 时才要)。
#    带上 `wheel` 会把它和它的依赖 `packaging` 留进 venv —— 两个 Agent 运行期从不 import
#    的包,污染「pip freeze 与锁一致」这条自检并平白扩大供应链面。
RUN ${PY} -m venv /opt/qtrade/agent/venv \
    && /opt/qtrade/agent/venv/bin/python -m pip install --no-cache-dir --upgrade pip \
    && /opt/qtrade/agent/venv/bin/python -m pip install --no-cache-dir --require-hashes \
        --only-binary=:all: -r /opt/qtrade/agent/requirements.lock \
    && /opt/qtrade/agent/venv/bin/python -m pip install --no-cache-dir --no-deps \
        --only-binary=:all: /opt/qtrade/agent/dist/qtrade_agent-*.whl \
    && /opt/qtrade/agent/venv/bin/python -c "import qtrade_agent, importlib.resources as r; \
        assert (r.files('qtrade_agent.store') / 'schema_agent.sql').is_file(), 'schema_agent.sql 没进包'; \
        import json; \
        caps = [p.name for p in r.files('qtrade_agent.capabilities').iterdir() if p.name.endswith('.json')]; \
        assert len(caps) >= 16, f'能力目录只有 {len(caps)} 个,缺目录会让 expand_allow_ops([\"*\"]) 把高危 op 全放行'; \
        print('Agent 装好,能力目录', len(caps), '个')" \
    && /opt/qtrade/agent/venv/bin/python -m pip check

# ── 构建期自检:Python 必须是正式版(不是 rc / a / b)────────────────────────
# jammy 源里的 python3.11 是 3.11.0rc1(D-03)。把「拿到候选版」这件事挡在构建期,
# 而不是等发行版装到客户机器上才发现。
RUN /opt/qtrade/agent/venv/bin/python -c "import sys; \
        v = sys.version.split()[0]; \
        assert v.replace('.', '').isdigit(), f'Python 不是正式版:{v}(带 rc/a/b 后缀的预发布版不得进发行版)'; \
        assert sys.version_info[:2] >= (3, 11), f'Python {v} 低于 pyproject 的 requires-python >= 3.11'; \
        assert sys.version_info.releaselevel == 'final', f'Python releaselevel={sys.version_info.releaselevel}(须 final)'; \
        print('Python 正式版自检通过:', v)"

# ── 构建期自检:机型档案库按**默认配置**就能加载,且条数 = 库文件条数 ──────────
# 🔴 这一条专防「文件落错目录」:Agent 读不到库文件时**不会报错**,它回落到
#    device_profiles.py 里那 10 条内置保底清单并只记一条 ERROR ——「机型只剩几条」
#    这种故障要等到真机上新建企点账号才发作,必须在构建期就挡住(R6-62 Ⅶ②)。
#    断言走的是 `AgentConfig()` 的默认值(= 05 §7 的路径),不是硬编码路径,
#    所以配置默认值与落点任一侧漂了都会红。
RUN /opt/qtrade/agent/venv/bin/python -c "import json; \
        from qtrade_agent.config import AgentConfig; \
        from qtrade_agent import device_profiles as dp; \
        cfg = AgentConfig(); \
        lib = dp.load(cfg); \
        want = len(json.load(open(cfg.device_profiles.library, encoding='utf-8'))['templates']); \
        assert lib.source == dp.SOURCE_LIBRARY, f'机型档案库没按默认配置加载到({cfg.device_profiles.library}):source={lib.source} error={lib.error} —— 落点与 05 §7 对不上,Agent 会回落内置小清单'; \
        assert len(lib) == want, f'加载到 {len(lib)} 条,库文件里是 {want} 条'; \
        print('机型档案库自检通过:', cfg.device_profiles.library, len(lib), '条')"

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
#    `qtrade-agent.service` **不在这里 enable** —— §2.7.3 ②(e) 明写它由首启 oneshot
#    `systemctl enable --now qtrade-agent` 拉起,时点在 docker load(a)与 agent --init-db(d)
#    之后((d)(e) 的顺序由 00 §15g R6-60 (a) 裁决对调)。
RUN systemctl enable qtrade-docker-config.service \
    && systemctl enable qtrade-firstboot.service \
    && systemctl enable qtrade-container-janitor.service \
    && systemctl enable docker.service

# ── 版本标记 ────────────────────────────────────────────────────────────────
# /etc/qtrade/contents.json(G-10)由 build-rootfs.sh 在 export 之后写入 —— 它要含
# 镜像 tar 的 sha256,而那两个 tar 这时候还没进来。这里只留一个人眼可读的版本串。
RUN echo "$ROOTFS_VERSION" > /etc/qtrade/rootfs-version

# 明确不设 CMD:见文件头,这棵树是给 `wsl --import` 的,不是给 `docker run` 的。
