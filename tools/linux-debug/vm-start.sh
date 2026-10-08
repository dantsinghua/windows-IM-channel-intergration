#!/usr/bin/env bash
set -euo pipefail
data=/workspace/.qtrade-linux-debug
if docker container inspect qtrade-linux-debug-vm >/dev/null 2>&1; then
  docker start qtrade-linux-debug-vm
  exit 0
fi
test -f "$data/vm/rootfs.img" || { echo 'Run python3 prepare-vm.py first.' >&2; exit 1; }
docker run -d --name qtrade-linux-debug-vm --label qtrade.debug=true \
  --restart unless-stopped --cpus 4 --memory 12g \
  -p 127.0.0.1:10022:10022 -p 127.0.0.1:15555:15555 \
  --mount "type=bind,src=$data/vm,dst=/data" \
  --mount "type=bind,src=$data/platform-tools,dst=/opt/platform-tools,readonly" \
  --mount "type=bind,src=$data/android,dst=/root/.android" \
  qtrade-linux-debug-vm-tools:local \
  qemu-system-x86_64 -accel tcg,thread=multi -machine q35 -cpu max -smp 4 -m 8192 \
  -kernel /data/vmlinuz -initrd /data/initrd.img \
  -append 'root=/dev/vda rw console=ttyS0 net.ifnames=0' \
  -drive file=/data/rootfs.img,format=raw,if=virtio \
  -netdev user,id=net0,hostfwd=tcp:0.0.0.0:10022-:22,hostfwd=tcp:0.0.0.0:15555-:5555 \
  -device virtio-net-pci,netdev=net0 \
  -virtfs local,path=/data/payload,mount_tag=payload,security_model=none,readonly=on \
  -nographic -no-reboot
