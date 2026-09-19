#!/usr/bin/env bash
# 在 Linux(或 WSL2)上编译带 binder 的 WSL2 内核,产出 bzImage。
# 用法: KERNEL_BRANCH=linux-msft-wsl-6.6.y ./build-kernel.sh
# 产物: out/bzImage
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
KERNEL_BRANCH="${KERNEL_BRANCH:-linux-msft-wsl-6.6.y}"
SRC="${SRC:-$HERE/WSL2-Linux-Kernel}"
OUT="$HERE/out"
JOBS="$(nproc)"

echo ">> 安装依赖(Debian/Ubuntu)"
if command -v apt-get >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y build-essential flex bison dwarves libssl-dev \
    libelf-dev bc cpio libncurses-dev git
fi

echo ">> 拉取 WSL2 内核源码 分支=$KERNEL_BRANCH"
if [ ! -d "$SRC" ]; then
  git clone --depth=1 -b "$KERNEL_BRANCH" \
    https://github.com/microsoft/WSL2-Linux-Kernel.git "$SRC"
fi

cd "$SRC"

# 崩溃转储 L2(安琳 2026-09-18 拍板:封装进内核,不改用户本地 WSL2 配置)。
# WSL 的用户态 /init 启动时把 core_pattern 设成 "|/wsl-capture-crash ...",该捕获器
# 绕过 RLIMIT_CORE,即使容器 ulimits: core=0 也照写转储(实测单个 16GB,一天 147GB)。
# ⚠️ 改内核编译期默认值无效 —— fs/coredump.c 的默认值是上游原值 "core"、微软没 patch,
# 是用户态 init 在 initcall 之后覆盖的。所以必须在 sysctl 写入路径上拦。
echo ">> 应用 QTrade patch(拒绝管道模式 core_pattern)"
for p in "$HERE"/patches/*.patch; do
  [ -e "$p" ] || continue
  if git apply --reverse --check "$p" 2>/dev/null; then
    echo "   已应用,跳过: $(basename "$p")"
  else
    git apply --check "$p" || { echo "!! patch 不适用于当前源码($p),构建中止"; exit 1; }
    git apply "$p" && echo "   已打: $(basename "$p")"
  fi
done

echo ">> 以 WSL2 官方 config 打底"
cp Microsoft/config-wsl .config

echo ">> 合并 binder 配置片段"
./scripts/kconfig/merge_config.sh -m .config "$HERE/binder.config"

# WSL 的 init 通过 hv_sock(vsock 端口 50000)连回 Windows。内核同一时刻只允许一个 G2H vsock 传输,
# virtio/vmci 的 vsock 若内置(=y)会在启动时先占住槽位,hv_sock 注册失败 -> WSL init 连不上宿主 ->
# 虚拟机立即关机,Windows 侧报 Wsl/Service/CreateInstance/CreateVm/WSAENOTCONN。
# QEMU 用的是 virtio-vsock,所以 QEMU 里测不出这个问题。
echo ">> 修正 vsock 传输(只保留 hv_sock)"
./scripts/config --file .config \
  --disable VIRTIO_VSOCKETS --disable VMWARE_VMCI_VSOCKETS --enable HYPERV_VSOCKETS
make olddefconfig

echo ">> 校验 binder 与 WSL 必需驱动"
for k in CONFIG_ANDROID_BINDER_IPC CONFIG_ANDROID_BINDERFS \
         CONFIG_HYPERV CONFIG_HYPERV_STORAGE CONFIG_HYPERV_VSOCKETS; do
  grep -q "^$k=y" .config || { echo "!! $k 未开启,构建中止"; exit 1; }
done
if grep -qE '^CONFIG_(VIRTIO_VSOCKETS|VMWARE_VMCI_VSOCKETS)=y' .config; then
  echo "!! virtio/vmci vsock 被内置,会抢占 hv_sock 导致 WSL 起不来,构建中止"; exit 1
fi

echo ">> 编译内核 (-j$JOBS)"
make -j"$JOBS" LOCALVERSION=   # LOCALVERSION= 置空:抑制 setlocalversion 在无 tag 时追加的 "+" 后缀

echo ">> 校验产物内嵌配置"
if ./scripts/extract-ikconfig arch/x86/boot/bzImage | grep -qE '^CONFIG_(VIRTIO_VSOCKETS|VMWARE_VMCI_VSOCKETS)=y'; then
  echo "!! 产物中 virtio/vmci vsock 仍为内置,拒绝输出"; exit 1
fi

# 崩溃转储 L2 必须真的编进去 —— patch 漏打时内核照样能编出来、能启动,
# 只是转储防护完全失效(现象:装完一切正常,几天后 C 盘被吃干),所以这里硬校验。
echo ">> 校验崩溃转储 L2 已编入"
if ! strings fs/coredump.o | grep -q 'qtrade: pipe core_pattern rejected'; then
  echo "!! fs/coredump.o 里找不到 QTrade 的 core_pattern 防护,patch 未生效,拒绝输出"; exit 1
fi

mkdir -p "$OUT"
cp -f "arch/x86/boot/bzImage" "$OUT/bzImage"
cp -f .config "$OUT/config-wsl-binder"
echo ">> 完成: $OUT/bzImage"
ls -lh "$OUT/bzImage"
sha256sum "$OUT/bzImage"   # 更新 apply-binder-kernel.ps1 里的 $Expected 用
