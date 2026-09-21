#!/usr/bin/env bash
# 再生成 wheels.expected —— 「15 个运行期包 → 目标平台上应装上的那一个 wheel 文件」清单(E4-1)
#
# 做法:在与发行版同一套 Python(jammy + deadsnakes python3.12)的构建镜像里,按 Dockerfile 同样的
#   pip 版本(pip-bootstrap.lock)与同样的开关(--require-hashes --only-binary=:all:)对 requirements.lock
#   跑一次 `pip install --dry-run --report`,由 verify-wheels.py emit 把 pip 选中的文件名 + sha256 写成清单。
#   ⇒ 清单 = 「锁完好时 pip 在目标平台的选择」;构建期 verify-wheels.py check 要求实装与它逐包一致。
#
# 何时要跑:requirements.lock 变了(升/降任一版本、重新 compile)、pip-bootstrap.lock 的 pip 版本变了、
#   或发行版 Python 的 minor / glibc 基线变了(Dockerfile 的 ARG PY、FROM)。跑完用 git diff 人眼看一遍
#   文件名变化(尤其平台 wheel ↔ py3-none-any 之间的变化)再提交。
#
# 基础镜像:默认用 build-rootfs.sh 自己留下的 qtrade-build/rootfs:rootfs-1.0.0(有 /usr/bin/python3.12);
#   本机没有时先跑一次 build-rootfs.sh(或 --base 指定任一 jammy + deadsnakes python3.12 的镜像)。
#
# docker 纪律:只 `docker build --output type=local`(不产生镜像、不起容器、不映射端口);
#   网络只在 build 期经 http(s)_proxy 出网。
#
# 用法(仓库根):bash installer/rootfs/gen-wheel-manifest.sh [--base <image>] > installer/rootfs/wheels.expected
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE='qtrade-build/rootfs:rootfs-1.0.0'
[ "${1:-}" = "--base" ] && BASE="$2"

OUTD="$(mktemp -d)"
trap 'rm -rf "$OUTD"' EXIT

PROXY_ARGS=()
if [ -n "${http_proxy:-}" ] || [ -n "${https_proxy:-}" ]; then
    PROXY_ARGS+=(--build-arg "HTTP_PROXY=${http_proxy:-}"
                 --build-arg "HTTPS_PROXY=${https_proxy:-${http_proxy:-}}"
                 --build-arg "NO_PROXY=${no_proxy:-localhost,127.0.0.1}")
fi

docker build -q --no-cache "${PROXY_ARGS[@]}" --build-arg "BASE=$BASE" \
    --output "type=local,dest=$OUTD" -f - "$HERE" >&2 <<'DOCKERFILE'
ARG BASE
FROM ${BASE} AS gen
COPY pip-bootstrap.lock requirements.lock verify-wheels.py /w/
RUN python3.12 -m venv /tmp/v \
    && /tmp/v/bin/python -m pip install -q --no-cache-dir --require-hashes --only-binary=:all: -r /w/pip-bootstrap.lock \
    && /tmp/v/bin/python -m pip install -q --no-cache-dir --dry-run --ignore-installed --require-hashes \
        --only-binary=:all: --report /tmp/r.json -r /w/requirements.lock \
    && /tmp/v/bin/python /w/verify-wheels.py emit --report /tmp/r.json --lock /w/requirements.lock > /w/wheels.expected
FROM scratch
COPY --from=gen /w/wheels.expected /
DOCKERFILE

cat "$OUTD/wheels.expected"
