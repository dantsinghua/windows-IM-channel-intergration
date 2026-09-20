#!/usr/bin/env bash
# QTrade 安装器 —— kcheck 微型发行版构建
#
# 规格:docs/03 §2.6.1(`KERNEL_STAGED`)
#   「预先导入 kcheck(W14/§2.6.8 第 5 条):在**官方内核**上
#     `wsl --import qtrade-kcheck %ProgramData%\QTrade\wsl\kcheck kcheck-rootfs.tar --version 2`
#     (busybox ~3 MB;`/etc/wsl.conf` 关 systemd/interop/automount;`Invoke-Wsl` 超时 120 s)」
#
# 这个发行版**只**用来回答一个问题:换上自编内核后 WSL2 还能不能起来一个发行版。
# 所以它要满足三条,缺一不可:
#   ① 极小 —— 它在切内核前后各被拉起一次,体积直接决定这两次的耗时;
#   ② 无 systemd —— 验证的是「内核能起 VM + 起发行版 init」,systemd 起不来会把
#      「内核坏了」误报成「发行版坏了」,原因码与回滚路径全错(§2.6.7 表 B);
#   ③ 无 interop / 无 automount —— 这两样都要 9p / Windows 侧配合,验证内核时把它们
#      拉进来等于给判据加两个无关的失败源。
#
# 产物:kcheck-rootfs.tar(未压缩 tar;`wsl --import` 吃 tar,压缩与否由外层 7z 管)
#
# 用法:
#   ./build-kcheck.sh --out <产物根>/rootfs/out/kcheck-rootfs.tar
#   ./build-kcheck.sh --out … --busybox /path/to/busybox-x86_64     # 离线:用已下好的 busybox
#
# 🔴 busybox 是**静态链接**二进制(no libc 依赖),所以这个 rootfs 里不需要任何动态库、
#    不需要 ld.so —— 这是它能只有 ~1 MB 的原因,也是别用 alpine 的理由(alpine 的 busybox
#    动态链接 musl,得把 /lib/ld-musl-x86_64.so.1 一起带上)。
set -euo pipefail

BUSYBOX_URL='https://busybox.net/downloads/binaries/1.35.0-x86_64-linux-musl/busybox'
# 官方 busybox.net 发布件,2026-09-20 实测值。换版本时连这行一起改,别只改 URL。
BUSYBOX_SHA256='6e123e7f3202a8c1e9b1f94d8941580a25135382b99e8d3e34fb858bba311348'
BUSYBOX_VERSION='1.35.0'

OUT=''
BUSYBOX_SRC=''

while [ $# -gt 0 ]; do
    case "$1" in
        --out)     OUT="$2"; shift 2 ;;
        --busybox) BUSYBOX_SRC="$2"; shift 2 ;;
        -h|--help) sed -n '1,30p' "$0"; exit 0 ;;
        *) echo "未知参数:$1" >&2; exit 2 ;;
    esac
done

[ -n "$OUT" ] || { echo "必须给 --out <kcheck-rootfs.tar 路径>" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── 1. 取 busybox ───────────────────────────────────────────────────────────
BB="$WORK/busybox"
if [ -n "$BUSYBOX_SRC" ]; then
    cp -f "$BUSYBOX_SRC" "$BB"
else
    echo "[1/4] 下载 busybox $BUSYBOX_VERSION(静态)"
    curl -fsSL --max-time 300 -o "$BB" "$BUSYBOX_URL"
fi

# 校验 —— 这个二进制会成为 kcheck 的 init,不校验等于让内核验证步跑一个来路不明的 init。
actual="$(sha256sum "$BB" | cut -d' ' -f1)"
if [ "$actual" != "$BUSYBOX_SHA256" ]; then
    echo "busybox sha256 不符:期望 $BUSYBOX_SHA256,实得 $actual" >&2
    exit 1
fi
chmod 0755 "$BB"

# 静态性复核:动态链接的 busybox 进了这个 rootfs 会在 `wsl --import` 之后
# 以「exec format error / No such file or directory」失败,而那个报错看起来像内核问题。
if command -v file >/dev/null 2>&1; then
    if ! file "$BB" | grep -q 'statically linked'; then
        echo "busybox 不是静态链接,kcheck 里没有 ld.so 会起不来" >&2
        exit 1
    fi
fi

# ── 2. 铺 rootfs 目录树 ─────────────────────────────────────────────────────
echo "[2/4] 铺 rootfs 目录树"
R="$WORK/rootfs"
mkdir -p "$R"/{bin,sbin,etc,proc,sys,dev,tmp,root,run,var/log,usr/bin,usr/sbin}
chmod 1777 "$R/tmp"

cp -f "$BB" "$R/bin/busybox"

# busybox 的 applet 符号链接。`busybox --install -s` 要求目标在 PATH 上且自己可执行,
# 在交叉/无权限环境里不稳;这里按 `busybox --list` 自己建链接,行为确定且不依赖运行环境。
for applet in $("$BB" --list); do
    ln -sf /bin/busybox "$R/bin/$applet"
done
ln -sf /bin/busybox "$R/init"

# ── 3. 配置文件 ─────────────────────────────────────────────────────────────
echo "[3/4] 写 /etc/wsl.conf 等配置"

# 🔴 §2.6.1 逐条:关 systemd、关 interop、关 automount。
cat > "$R/etc/wsl.conf" <<'EOF'
# kcheck —— 内核验证用微型发行版(docs/03 §2.6.1)
# 三项全关的理由见 build-kcheck.sh 文件头:验证的是内核,不是 systemd/9p/interop。
[boot]
systemd=false

[interop]
enabled=false
appendWindowsPath=false

[automount]
enabled=false

[user]
default=root
EOF

cat > "$R/etc/passwd" <<'EOF'
root:x:0:0:root:/root:/bin/sh
EOF

cat > "$R/etc/group" <<'EOF'
root:x:0:
EOF

cat > "$R/etc/shadow" <<'EOF'
root:*:19000:0:99999:7:::
EOF
chmod 0640 "$R/etc/shadow"

cat > "$R/etc/hostname" <<'EOF'
qtrade-kcheck
EOF

# `uname -r` 是 §2.6.4 的判据(「与 manifest version 逐字相等」),走的是内核接口不是
# 这里的文件;这个 os-release 只是让人 `wsl -d qtrade-kcheck -- cat /etc/os-release`
# 时能一眼看出这是哪个包造的,排障用。
cat > "$R/etc/os-release" <<EOF
NAME="QTrade kcheck"
ID=qtrade-kcheck
PRETTY_NAME="QTrade kernel check (busybox $BUSYBOX_VERSION)"
VERSION_ID="$BUSYBOX_VERSION"
EOF

cat > "$R/etc/profile" <<'EOF'
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PS1='kcheck:\w# '
EOF

# ── 4. 打 tar ───────────────────────────────────────────────────────────────
echo "[4/4] 打包 $OUT"
mkdir -p "$(dirname "$OUT")"
# --numeric-owner:不把构建机的 /etc/passwd 名字写进去(导入后 uid 才是确定的 0)
# --owner/--group=0 :构建机上是普通用户跑的,产物里必须是 root 持有
tar -C "$R" --numeric-owner --owner=0 --group=0 -cf "$OUT" .

size=$(stat -c %s "$OUT")
sha=$(sha256sum "$OUT" | cut -d' ' -f1)
echo
echo "kcheck-rootfs.tar 完成"
echo "  路径   : $OUT"
echo "  体积   : $size B ($((size / 1024 / 1024)) MB)"
echo "  sha256 : $sha"
echo "  busybox: $BUSYBOX_VERSION ($BUSYBOX_SHA256)"
echo
echo "安装器怎么用它(§2.6.1,KERNEL_STAGED 步末尾、**官方内核**上):"
echo '  wsl.exe --import qtrade-kcheck %ProgramData%\QTrade\wsl\kcheck kcheck-rootfs.tar --version 2'
