#!/bin/bash
# QTrade 首启 ①:在 dockerd **首次启动之前**写 /etc/docker/daemon.json
#
# 规格:docs/03 §2.7.3「docker 地址池」+ R5-9;候选表与冲突判定归 04 §2.7.4(本脚本不重写候选表)
#
# 🔴 为什么必须在 dockerd 起来之前:dockerd 第一次起来就会按 default-address-pools
#    建 docker0(默认 172.17.0.0/16)。一旦建成,改 daemon.json 再 restart 也不会把
#    已建的 bridge 网络挪走 —— 容器网络一旦建好改段就要重建全部账号网络(§2.7.3)。
#    单元里的 `Before=docker.service` 是这条的执行保证,别把它改成 After=。
#
# 选段本身由**安装器在 Windows 侧**做(netprobe.pick_docker_pool(),看得到 Windows 路由表
# 与 VPN 下发的路由,发行版里看不到),结果经 install.env 传进来。本脚本只落地。
set -uo pipefail

ENV_FILE=/mnt/c/ProgramData/QTrade/install/install.env
DAEMON_JSON=/etc/docker/daemon.json
MARKER=/etc/qtrade/.docker-configured
LOG='logger -t qtrade-docker-config --'

# 04 §2.7.4 候选表的**第一项**。仅当 install.env 缺失/不可读时兜底 ——
# 那种情况下 172.17 很可能也是能用的(装不上 VPN 的机器占多数),而「起不来 docker」
# 一定是更坏的结果。段不合适会在 P-ENV 里看见(04 DOCKER_POOL_ALL_CONFLICT)。
FALLBACK_BASE='172.31.0.0/16'

base=''
bip=''

if [ -r "$ENV_FILE" ]; then
    # install.env 是安装器写的 KEY=VALUE 文本。只取我们认识的两个键,不 source 整个文件
    # —— source 一个 Windows 侧写的文件等于让安装器往 root shell 里注入任意命令。
    base="$(grep -E '^docker_cidr=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '\r\n "')"
    bip="$(grep -E '^docker_bip=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '\r\n "')"
    $LOG "读 install.env:docker_cidr='$base' docker_bip='$bip'"
else
    $LOG "install.env 不可读($ENV_FILE),用兜底段 $FALLBACK_BASE"
fi

# 形态校验:写一个语法不对的 CIDR 进 daemon.json,dockerd 会直接起不来。
if ! printf '%s' "$base" | grep -qE '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$'; then
    [ -n "$base" ] && $LOG "docker_cidr 形态不合法('$base'),改用 $FALLBACK_BASE"
    base="$FALLBACK_BASE"
fi

mkdir -p /etc/docker

if [ -n "$bip" ] && printf '%s' "$bip" | grep -qE '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$'; then
    cat > "$DAEMON_JSON" <<JSON
{
  "default-address-pools": [ { "base": "$base", "size": 24 } ],
  "bip": "$bip",
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "3" }
}
JSON
else
    cat > "$DAEMON_JSON" <<JSON
{
  "default-address-pools": [ { "base": "$base", "size": 24 } ],
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "3" }
}
JSON
fi
chmod 0644 "$DAEMON_JSON"

# 语法自检:dockerd 对坏 JSON 的报错发生在启动时,那时这个 oneshot 早已 exit 0、
# 现场只看见「docker 起不来」。在这里先用 jq 判一次,坏了就退回兜底配置。
if command -v jq >/dev/null 2>&1 && ! jq -e . "$DAEMON_JSON" >/dev/null 2>&1; then
    $LOG "生成的 daemon.json 不是合法 JSON,退回兜底"
    printf '{"default-address-pools":[{"base":"%s","size":24}]}\n' "$FALLBACK_BASE" > "$DAEMON_JSON"
fi

mkdir -p "$(dirname "$MARKER")"
printf 'base=%s\nbip=%s\nconfigured_at=%s\n' "$base" "$bip" "$(date -Is)" > "$MARKER"
$LOG "daemon.json 已写:base=$base bip=${bip:-<未指定>}"
exit 0
