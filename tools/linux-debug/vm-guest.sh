#!/bin/sh
# Run only inside the disposable Debian image used to prepare our guest filesystem.
set -eu
mkdir -p /root/.ssh /etc/systemd/network /mnt/payload /var/lib/qtrade-redroid /var/lib/dbus
install -m 600 /tmp/debug-authorized-key /root/.ssh/authorized_keys
chmod 700 /root/.ssh
cat > /etc/systemd/network/20-wired.network <<'EOF'
[Match]
Driver=virtio_net
[Network]
Address=10.0.2.15/24
Gateway=10.0.2.2
DNS=10.0.2.3
EOF
chmod 644 /etc/systemd/network/20-wired.network
cat > /etc/fstab <<'EOF'
/dev/vda / ext4 defaults 0 1
payload /mnt/payload 9p trans=virtio,version=9p2000.L,ro,nofail 0 0
EOF
printf 'binder_linux\n' > /etc/modules-load.d/qtrade.conf
printf 'options binder_linux devices=binder,hwbinder,vndbinder\n' > /etc/modprobe.d/qtrade.conf
cat > /usr/local/sbin/qtrade-redroid-start <<'EOF'
#!/bin/sh
set -eu
modprobe binder_linux devices=binder,hwbinder,vndbinder
chmod 0666 /dev/binder /dev/hwbinder /dev/vndbinder
if ! docker image inspect redroid/redroid:11.0.0-latest >/dev/null 2>&1; then
  docker load -i /mnt/payload/redroid11.tar
fi
if docker container inspect qtrade-redroid >/dev/null 2>&1; then
  docker start qtrade-redroid
else
  docker run -d --name qtrade-redroid --restart unless-stopped --privileged --ulimit core=0 \
    -v /dev/binder:/dev/binder -v /dev/hwbinder:/dev/hwbinder -v /dev/vndbinder:/dev/vndbinder \
    -v /var/lib/qtrade-redroid:/data -p 5555:5555 \
    redroid/redroid:11.0.0-latest \
    androidboot.use_memfd=true androidboot.redroid_gpu_mode=guest \
    androidboot.redroid_width=720 androidboot.redroid_height=1280 \
    androidboot.redroid_dpi=240 androidboot.redroid_fps=10
fi
EOF
chmod 755 /usr/local/sbin/qtrade-redroid-start
cat > /etc/systemd/system/qtrade-redroid.service <<'EOF'
[Unit]
Description=QTrade local redroid debug instance
Requires=docker.service
After=docker.service network-online.target
RequiresMountsFor=/mnt/payload
[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=1800
ExecStart=/usr/local/sbin/qtrade-redroid-start
[Install]
WantedBy=multi-user.target
EOF
systemctl enable systemd-networkd.service ssh.service docker.service qtrade-redroid.service
truncate -s 0 /etc/machine-id
ln -sf /etc/machine-id /var/lib/dbus/machine-id
rm -f /usr/sbin/policy-rc.d
apt-get clean
