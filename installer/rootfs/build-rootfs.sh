#!/usr/bin/env bash
# QTrade 发行版 qtrade 的 rootfs.tar —— 构建编排
#
# 规格:docs/03 §2.7(发行版导入与首启)、§2.2.1(manifest / rootfs_contents / G-10 / G-11)、
#       docs/02 §2.1(Agent 进程部署)、docs/05 §2.5.1 + §7(设备身份档案库与其落盘路径)
#
# 产物:rootfs.tar —— 给 `wsl --import qtrade … --version 2` 吃的完整文件系统。
#       同时把 G-10 的内容清单写进 rootfs 内的 /etc/qtrade/contents.json,
#       并在构建目录留一份同内容的 contents.json 供 CI 填进 manifest.rootfs_contents
#       (§2.2.1:「两处必须逐字一致」)。
#
# 🔴 docker 使用纪律(这台机器上有 12 个在用的容器:qtrade-redroid / Dify / MySQL / GaussDB …):
#      · 只 build / create / export / save / load / pull;
#      · 镜像 tag 一律 `qtrade-build/` 前缀,临时容器一律 `qtrade-build-` 前缀,结束即删;
#      · **绝不** stop / restart / rm 任何不是本脚本建的容器,不 `docker system prune`,
#        不重启 docker,不 `wsl --shutdown`。
#
# 用法:
#   ./build-rootfs.sh --source-root /mnt/c/Users/anlin/qtrade-payload \
#                     --images-dir  ~/work/qtrade-payload-build/images \
#                     --out         /mnt/c/Users/anlin/qtrade-payload/rootfs/out/rootfs.tar
#
#   --skip-images   不追加预载镜像(出一个只验流程的瘦 rootfs;contents 里两项标 absent)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SOURCE_ROOT=''
IMAGES_DIR=''
OUT=''
ROOTFS_VERSION='rootfs-1.0.0'
SKIP_IMAGES=0
KEEP_WORK=0

REDROID_REF='redroid/redroid:11.0.0-latest'
NAPCAT_REF='mlikiowa/napcat-docker:v4.18.28'

while [ $# -gt 0 ]; do
    case "$1" in
        --source-root) SOURCE_ROOT="$2"; shift 2 ;;
        --images-dir)  IMAGES_DIR="$2";  shift 2 ;;
        --out)         OUT="$2";         shift 2 ;;
        --version)     ROOTFS_VERSION="$2"; shift 2 ;;
        --redroid-ref) REDROID_REF="$2"; shift 2 ;;
        --napcat-ref)  NAPCAT_REF="$2";  shift 2 ;;
        --skip-images) SKIP_IMAGES=1;    shift ;;
        --keep-work)   KEEP_WORK=1;      shift ;;
        -h|--help)     sed -n '1,30p' "$0"; exit 0 ;;
        *) echo "未知参数:$1" >&2; exit 2 ;;
    esac
done

[ -n "$SOURCE_ROOT" ] || { echo "必须给 --source-root(载荷产物根)" >&2; exit 2; }
[ -n "$OUT" ]         || { echo "必须给 --out(rootfs.tar 路径)" >&2; exit 2; }
[ "$SKIP_IMAGES" = 1 ] || [ -n "$IMAGES_DIR" ] || { echo "必须给 --images-dir(或 --skip-images)" >&2; exit 2; }

IMAGE_TAG="qtrade-build/rootfs:${ROOTFS_VERSION}"
CONTAINER="qtrade-build-export-$$"

# 🔴 工作目录放 WSL 原生盘,**不能**放 /mnt/c —— rootfs 有 ~3.5 GB,
#    9p 上 docker export 与 tar --append 都会慢一个量级。产物最后才拷去 Windows 侧。
WORK="${TMPDIR:-/var/tmp}/qtrade-rootfs-build.$$"
mkdir -p "$WORK"
cleanup() {
    # 只删自己建的容器,且用 -f 之前先确认名字就是我们那个(纪律见文件头)
    if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
        docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    fi
    [ "$KEEP_WORK" = 1 ] || rm -rf "$WORK"
}
trap cleanup EXIT

say() { echo "==> $*"; }
die() { echo "!! $*" >&2; exit 1; }
sha() { sha256sum "$1" | cut -d' ' -f1; }

# ── 1. 备料 ─────────────────────────────────────────────────────────────────
say "[1/6] 备料到 $WORK/payload"
P="$WORK/payload"
mkdir -p "$P"/{platform-tools,agent-wheel}

# Agent wheel
shopt -s nullglob
wheels=("$SOURCE_ROOT"/dist/qtrade_agent-*.whl)
shopt -u nullglob
[ ${#wheels[@]} -gt 0 ] || die "找不到 Agent wheel:$SOURCE_ROOT/dist/qtrade_agent-*.whl(先在仓库根跑 pip wheel . -w dist --no-deps)"
[ ${#wheels[@]} -eq 1 ] || die "$SOURCE_ROOT/dist 下有 ${#wheels[@]} 个 wheel,装哪个不确定;只留一个"
AGENT_WHEEL="${wheels[0]}"
cp -f "$AGENT_WHEEL" "$P/agent-wheel/"
AGENT_WHEEL_NAME="$(basename "$AGENT_WHEEL")"

# 依赖锁(R-1)—— 与 Dockerfile 的 `COPY payload/requirements.lock` 对应。
# 运行期依赖的带 hash 锁(uv pip compile 生成),Dockerfile 用 --require-hashes 安装,
# 让「同一份 wheel 不同日期重建出的依赖集合一致、可复现」;下面登记进 contents.json。
LOCK_SRC="$HERE/requirements.lock"
[ -r "$LOCK_SRC" ] || die "缺 $LOCK_SRC(先在 installer/rootfs 跑 uv pip compile 生成,见 README「依赖上锁」)"
cp -f "$LOCK_SRC" "$P/requirements.lock"
LOCK_PKGS="$(grep -cE '^[a-zA-Z0-9].*==' "$P/requirements.lock")"
say "    依赖锁 $LOCK_PKGS 个运行期包 → /opt/qtrade/agent/requirements.lock"

# wheel 清单(E4-1)—— 与 Dockerfile 的 `COPY payload/wheels.expected` 对应:锁里每个包在目标平台
# 「应装上的那一个 wheel 文件」(文件名 + sha256)。Dockerfile 装完依赖后由 verify-wheels.py 逐包断言
# 实装与之一致(否则构建失败);清单本身随包进 rootfs 并登记进 contents.json(wheel_manifest)。
# 再生成:gen-wheel-manifest.sh(见 README §8)。另两件只在构建期用、不留进 rootfs:
# pip-bootstrap.lock(pip 钉版本 + hash,E3-O1)、verify-wheels.py(断言脚本)。
MANIFEST_SRC="$HERE/wheels.expected"
for f in "$MANIFEST_SRC" "$HERE/pip-bootstrap.lock" "$HERE/verify-wheels.py"; do
    [ -r "$f" ] || die "缺 $f(见 README §8)"
done
cp -f "$MANIFEST_SRC" "$P/wheels.expected"
cp -f "$HERE/pip-bootstrap.lock" "$HERE/verify-wheels.py" "$P/"
MANIFEST_PKGS="$(grep -cvE '^[[:space:]]*(#|$)' "$P/wheels.expected")"
[ "$MANIFEST_PKGS" = "$LOCK_PKGS" ] \
    || die "wheels.expected 有 $MANIFEST_PKGS 个包,requirements.lock 有 $LOCK_PKGS 个 —— 锁变了清单没跟着再生成?(gen-wheel-manifest.sh)"
say "    wheel 清单 $MANIFEST_PKGS 个包 → /opt/qtrade/agent/wheels.expected"
# 版本号从文件名取(qtrade_agent-<ver>-py3-none-any.whl)
AGENT_VERSION="$(printf '%s' "$AGENT_WHEEL_NAME" | sed -E 's/^qtrade_agent-([^-]+)-.*/\1/')"

# platform-tools —— 🔴 G-11:必须与 Windows 侧 pkg/adb 同版本。
# Windows 侧的在 $SOURCE_ROOT/third_party/platform-tools(.exe),rootfs 要的是 Linux 版。
PT_LINUX="$SOURCE_ROOT/third_party/platform-tools-linux"
[ -d "$PT_LINUX" ] || die "缺 $PT_LINUX(Linux 版 platform-tools;须与 third_party/platform-tools 的 Pkg.Revision 相同)"
cp -a "$PT_LINUX/." "$P/platform-tools/"
ADB_VERSION="$(grep -E '^Pkg.Revision=' "$P/platform-tools/source.properties" | cut -d= -f2 | tr -d '\r')"
[ -n "$ADB_VERSION" ] || die "读不出 Linux platform-tools 的 Pkg.Revision"

# G-11 硬闸:两边版本不等就别出包了 —— 版本不同的 adb 客户端连上 Agent 的 adb server
# 会把它杀掉,企点账号集体 offline(04 §2.7.4),而这种故障现场极难归因。
PT_WIN_PROPS="$SOURCE_ROOT/third_party/platform-tools/source.properties"
if [ -r "$PT_WIN_PROPS" ]; then
    ADB_WIN_VERSION="$(grep -E '^Pkg.Revision=' "$PT_WIN_PROPS" | cut -d= -f2 | tr -d '\r')"
    [ "$ADB_VERSION" = "$ADB_WIN_VERSION" ] \
        || die "G-11 违例:rootfs 内 adb=$ADB_VERSION,Windows 侧 pkg/adb=$ADB_WIN_VERSION,必须同版本"
    say "    G-11 OK:两侧 platform-tools 同为 $ADB_VERSION"
else
    echo "    warn:读不到 $PT_WIN_PROPS,G-11 版本一致性未能在此校验(CI 须比对 manifest lock_with)" >&2
fi

# scrcpy-server —— 与 Windows 侧 scrcpy 客户端同版本(协议按版本严格匹配)
SCRCPY_SERVER="$SOURCE_ROOT/third_party/scrcpy/scrcpy-server"
[ -r "$SCRCPY_SERVER" ] || die "缺 $SCRCPY_SERVER"
cp -f "$SCRCPY_SERVER" "$P/scrcpy-server"
SCRCPY_VERSION="$(cat "$SOURCE_ROOT/third_party/scrcpy/.version" 2>/dev/null || echo unknown)"

# frida-server(android x86_64)
shopt -s nullglob
fridas=("$SOURCE_ROOT"/third_party/frida/frida-server-*-android-x86_64)
shopt -u nullglob
[ ${#fridas[@]} -eq 1 ] || die "$SOURCE_ROOT/third_party/frida/ 下要恰好一个 frida-server-*-android-x86_64(现有 ${#fridas[@]} 个)"
FRIDA_BIN="${fridas[0]}"
FRIDA_NAME="$(basename "$FRIDA_BIN")"
FRIDA_VERSION="$(printf '%s' "$FRIDA_NAME" | sed -E 's/^frida-server-([^-]+)-android.*/\1/')"
# 🔴 按**带版本号的原名**放进构建上下文 —— contents.json 里登记的就是这个名字
# (§2.2.1 的 `/opt/qtrade/frida/frida-server-<ver>-android-x86_64`),而 Agent 的 runtime
# 是按 contents 里的 path 去找它的。落成光秃秃的 `frida-server` 两边就对不上了。
# Dockerfile 那行 COPY 由下面的 sed 改成通配 `payload/frida-server-*`。
cp -f "$FRIDA_BIN" "$P/$FRIDA_NAME"

APK_SRC="$SOURCE_ROOT/third_party/apk/ADBKeyboard.apk"
[ -r "$APK_SRC" ] || die "缺 $APK_SRC"
cp -f "$APK_SRC" "$P/ADBKeyboard.apk"
APK_VERSION="$(cat "$SOURCE_ROOT/third_party/apk/.version" 2>/dev/null || echo unknown)"

# 机型档案库 —— 🔴 落点 = docs/05 §7 `[device_profiles] library` 的
# `/opt/qtrade/agent/data/device_profiles.json`(裁决 00 §15g R6-62 Ⅶ②,文档不改)。
# Dockerfile 的 COPY 与下面 contents.json 的 path 必须与它、以及 Agent 侧的配置默认值
# `src/qtrade_agent/config.py` `DeviceProfilesConfig.library` 三处逐字同值 ——
# 落错目录不会报错,Agent 只是回落到内置 10 条小清单并记一条 ERROR(构建期有自检拦它)。
cp -f "$HERE/profiles/device_profiles.json" "$P/device_profiles.json"
PROFILES_VERSION="$(jq -r '.version' "$P/device_profiles.json")"
PROFILES_COUNT="$(jq -r '.templates | length' "$P/device_profiles.json")"
say "    机型档案库 $PROFILES_VERSION,$PROFILES_COUNT 条 → /opt/qtrade/agent/data/device_profiles.json"

# ── 2. docker build ─────────────────────────────────────────────────────────
say "[2/6] docker build $IMAGE_TAG"
# Dockerfile 里的 COPY payload/frida-server 指的是**带版本名的那个文件**,统一成通配:
# 构建上下文里就叫 frida-server-*,Dockerfile 用 `COPY payload/frida-server* /opt/qtrade/frida/`。
cp -f "$HERE/Dockerfile" "$WORK/Dockerfile"
sed -i 's|^COPY payload/frida-server .*|COPY payload/frida-server-* /opt/qtrade/frida/|' "$WORK/Dockerfile"
cp -a "$HERE/files" "$WORK/files"

# 构建期要连 archive.ubuntu.com 与 PyPI。本机经 Windows 侧代理出网,所以把 shell 里的
# http_proxy/https_proxy 透传给构建容器(HTTP_PROXY/HTTPS_PROXY/NO_PROXY 是 docker 的
# 预定义 build-arg,不必在 Dockerfile 里 ARG 声明,也不会留进最终镜像的 ENV)。
# 🔴 NO_PROXY 必带 localhost/127.0.0.1,否则容器内对本地的请求会被送去代理然后超时。
PROXY_ARGS=()
if [ -n "${http_proxy:-}" ] || [ -n "${https_proxy:-}" ]; then
    PROXY_ARGS+=(--build-arg "HTTP_PROXY=${http_proxy:-}"
                 --build-arg "HTTPS_PROXY=${https_proxy:-${http_proxy:-}}"
                 --build-arg "NO_PROXY=${no_proxy:-localhost,127.0.0.1}")
    say "    经代理构建:${https_proxy:-$http_proxy}"
fi

docker build \
    --build-arg "ROOTFS_VERSION=$ROOTFS_VERSION" \
    "${PROXY_ARGS[@]}" \
    -t "$IMAGE_TAG" \
    -f "$WORK/Dockerfile" \
    "$WORK"

# ── 3. export ───────────────────────────────────────────────────────────────
say "[3/6] docker create + export → rootfs-base.tar"
BASE_TAR="$WORK/rootfs-base.tar"
docker create --name "$CONTAINER" "$IMAGE_TAG" /bin/true >/dev/null
docker export -o "$BASE_TAR" "$CONTAINER"
docker rm -f "$CONTAINER" >/dev/null
say "    base tar $(stat -c %s "$BASE_TAR") B"

# ── 4. 预载镜像 + contents.json ─────────────────────────────────────────────
say "[4/6] 生成 /etc/qtrade/contents.json(G-10)并备预载镜像"
STAGE="$WORK/stage"
mkdir -p "$STAGE/etc/qtrade" "$STAGE/var/lib/qtrade/images"

img_json() {  # $1=ref $2=tar(可空)
    local ref="$1" tar="$2" digest='' sha='' size=0
    digest="$(docker image inspect "$ref" --format '{{index .RepoDigests 0}}' 2>/dev/null | cut -d@ -f2 || true)"
    if [ -n "$tar" ] && [ -r "$tar" ]; then
        sha="$(sha "$tar")"; size="$(stat -c %s "$tar")"
    fi
    jq -n --arg ref "$ref" --arg d "$digest" \
          --arg tar "${tar:+/var/lib/qtrade/images/$(basename "$tar")}" \
          --arg sha "$sha" --argjson size "$size" \
        '{ref:$ref} + (if $d=="" then {} else {digest:$d} end)
                    + (if $tar=="" then {present:false} else {tar:$tar, sha256:$sha, size:$size} end)'
}

REDROID_TAR=''; NAPCAT_TAR=''
if [ "$SKIP_IMAGES" = 0 ]; then
    REDROID_TAR="$IMAGES_DIR/redroid-11.tar"
    NAPCAT_TAR="$IMAGES_DIR/napcat.tar"
    [ -r "$REDROID_TAR" ] || die "缺预载镜像 $REDROID_TAR(docker save $REDROID_REF -o …)"
    [ -r "$NAPCAT_TAR" ]  || die "缺预载镜像 $NAPCAT_TAR(docker save $NAPCAT_REF -o …)"
    cp -f "$REDROID_TAR" "$NAPCAT_TAR" "$STAGE/var/lib/qtrade/images/"
else
    say "    --skip-images:不追加预载镜像"
fi

jq -n \
    --arg wheel_path "/opt/qtrade/agent/dist/$AGENT_WHEEL_NAME" \
    --arg wheel_ver "$AGENT_VERSION" --arg wheel_sha "$(sha "$AGENT_WHEEL")" \
    --arg lock_sha "$(sha "$P/requirements.lock")" --argjson lock_pkgs "$LOCK_PKGS" \
    --arg manifest_sha "$(sha "$P/wheels.expected")" --argjson manifest_pkgs "$MANIFEST_PKGS" \
    --argjson redroid "$(img_json "$REDROID_REF" "$REDROID_TAR")" \
    --argjson napcat  "$(img_json "$NAPCAT_REF"  "$NAPCAT_TAR")" \
    --arg adb_ver "$ADB_VERSION" --arg adb_sha "$(sha "$P/platform-tools/adb")" \
    --arg scrcpy_ver "$SCRCPY_VERSION" --arg scrcpy_sha "$(sha "$P/scrcpy-server")" \
    --arg frida_name "$FRIDA_NAME" --arg frida_ver "$FRIDA_VERSION" --arg frida_sha "$(sha "$P/$FRIDA_NAME")" \
    --arg apk_ver "$APK_VERSION" --arg apk_sha "$(sha "$P/ADBKeyboard.apk")" \
    --arg prof_ver "$PROFILES_VERSION" --arg prof_sha "$(sha "$P/device_profiles.json")" \
    --arg rootfs_ver "$ROOTFS_VERSION" --arg built "$(date -Is)" \
    '{
        rootfs_version: $rootfs_ver,
        built_at: $built,
        agent_wheel:     { path: $wheel_path, version: $wheel_ver, sha256: $wheel_sha },
        requirements_lock: { path: "/opt/qtrade/agent/requirements.lock", packages: $lock_pkgs, sha256: $lock_sha },
        wheel_manifest:  { path: "/opt/qtrade/agent/wheels.expected", packages: $manifest_pkgs, sha256: $manifest_sha },
        redroid_image:   $redroid,
        napcat_image:    $napcat,
        adb:             { path: "/opt/qtrade/platform-tools/adb", version: $adb_ver, sha256: $adb_sha },
        scrcpy_server:   { path: "/opt/qtrade/scrcpy/scrcpy-server", version: $scrcpy_ver, sha256: $scrcpy_sha },
        frida_server:    { path: ("/opt/qtrade/frida/" + $frida_name), version: $frida_ver, sha256: $frida_sha },
        adbkeyboard_apk: { path: "/opt/qtrade/apk/ADBKeyboard.apk", version: $apk_ver, sha256: $apk_sha },
        device_profiles: { path: "/opt/qtrade/agent/data/device_profiles.json", version: $prof_ver, sha256: $prof_sha }
     }' > "$STAGE/etc/qtrade/contents.json"

# CI 要把同一份填进 manifest.rootfs_contents(§2.2.1「两处必须逐字一致」),
# 所以在构建目录旁边也留一份,别让人再从 tar 里抠。
cp -f "$STAGE/etc/qtrade/contents.json" "$(dirname "$OUT")/contents.json" 2>/dev/null \
    || { mkdir -p "$(dirname "$OUT")"; cp -f "$STAGE/etc/qtrade/contents.json" "$(dirname "$OUT")/contents.json"; }

# ── 5. 合成 rootfs.tar ──────────────────────────────────────────────────────
say "[5/6] tar --append 合成 $OUT"
# `docker export` 的 tar 是无压缩的流式 tar,可以直接 --append(比解包重打快一个量级,
# 也不会在解包/重打的过程中丢 uid/mode)。
cp -f "$BASE_TAR" "$WORK/rootfs.tar"
tar --numeric-owner --owner=0 --group=0 -C "$STAGE" -rf "$WORK/rootfs.tar" .

mkdir -p "$(dirname "$OUT")"
mv -f "$WORK/rootfs.tar" "$OUT"

# ── 6. 交代 ─────────────────────────────────────────────────────────────────
say "[6/6] 完成"
SIZE="$(stat -c %s "$OUT")"
SHA="$(sha "$OUT")"
echo
echo "rootfs.tar"
echo "  路径          : $OUT"
echo "  体积          : $SIZE B ($((SIZE / 1024 / 1024)) MB)"
echo "  sha256        : $SHA"
echo "  rootfs_version: $ROOTFS_VERSION"
echo "  contents.json : $(dirname "$OUT")/contents.json  ← CI 填进 manifest.rootfs_contents(G-10,两处逐字一致)"
echo
echo "安装器怎么用它(§2.7.2):"
echo '  wsl.exe --import qtrade %ProgramData%\QTrade\wsl\distro rootfs.tar --version 2'
echo
echo "构建产物镜像 $IMAGE_TAG 还在本地;确认不再需要时自行 docker rmi(本脚本不替你删,"
echo "免得下次构建又从头跑一遍 apt/pip)。"
