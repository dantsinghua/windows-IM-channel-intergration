"""Prepare a local software VM without changing the cloud host kernel or system packages.

Existing disks are never overwritten. Runtime data stays outside the checkout.
"""
import hashlib
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
from urllib.parse import urlsplit
from urllib.request import urlopen
import zipfile

SOURCE = Path(__file__).resolve().parent
DATA = Path('/workspace/.qtrade-linux-debug')
VM = DATA / 'vm'
TOOLS = 'qtrade-linux-debug-vm-tools:local'
BASE = 'debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f'
REDROID = 'redroid/redroid@sha256:60b0810684be4578733a847be3314c50b70f73bc92405b5a627ebe9b633ebb5e'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def exists(kind, name):
    return subprocess.run(['docker', kind, 'inspect', name], capture_output=True).returncode == 0


def main():
    VM.mkdir(parents=True, exist_ok=True)
    DATA.chmod(0o700)
    (VM / 'payload').mkdir(exist_ok=True)
    (DATA / 'android').mkdir(exist_ok=True)
    if not (DATA / 'platform-tools/adb').is_file():
        # SHA-1 is the checksum published by Google's official repository2-3.xml.
        context = ssl.create_default_context(cafile=os.getenv('SSL_CERT_FILE'))
        with urlopen('https://dl.google.com/android/repository/platform-tools_r37.0.1-linux.zip', context=context, timeout=120) as response:
            archive = response.read()
        if hashlib.sha1(archive).hexdigest() != '477254aa5f903c15cf51001717bdf347fb6b53e0':
            raise RuntimeError('Official Android SDK checksum mismatch')
        with tempfile.TemporaryFile() as f:
            f.write(archive)
            f.seek(0)
            with zipfile.ZipFile(f) as z:
                for item in z.infolist():
                    target = (DATA / item.filename).resolve()
                    if not target.is_relative_to(DATA.resolve()):
                        raise RuntimeError('Unexpected SDK archive path')
                z.extractall(DATA)
                for item in z.infolist():
                    mode = (item.external_attr >> 16) & 0o777
                    if mode:
                        (DATA / item.filename).chmod(mode)
    if (VM / 'rootfs.img').exists():
        for name in ['vmlinuz', 'initrd.img', 'id_ed25519', 'known_hosts', 'payload/redroid11.tar']:
            if not (VM / name).is_file():
                raise RuntimeError('Existing VM is incomplete: ' + name + '; preserve its disk and diagnose before rebuilding')
        if not exists('image', TOOLS):
            raise RuntimeError('Existing disk retained, but the QEMU tools image is missing')
        print('Existing VM disk and runtime retained; no rebuild or data reset.')
        return
    if not exists('image', TOOLS):
        builder = 'qtrade-linux-debug-build'
        if exists('container', builder):
            raise RuntimeError('Build container already exists; inspect its logs before retrying')
        args = ['docker', 'run', '--name', builder, '--label', 'qtrade.debug=true']
        for name in ['http_proxy', 'https_proxy', 'no_proxy']:
            if name in os.environ:
                args += ['--env', name]
        hosts = set()
        for name in ['http_proxy', 'https_proxy']:
            host = urlsplit(os.environ.get(name, '')).hostname
            if host and host not in hosts:
                hosts.add(host)
                args += ['--add-host', host + ':' + socket.gethostbyname(host)]
        cert = os.getenv('SSL_CERT_FILE')
        if cert:
            args += ['--mount', 'type=bind,src=' + cert + ',dst=/usr/local/share/ca-certificates/cloud-proxy.crt,readonly']
        script = '''set -eu
sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources
if [ -f /usr/local/share/ca-certificates/cloud-proxy.crt ]; then
  printf 'Acquire::https::CaInfo "/usr/local/share/ca-certificates/cloud-proxy.crt";\\n' > /etc/apt/apt.conf.d/99-cloud-ca
fi
apt-get -o APT::Update::Error-Mode=any update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils e2fsprogs linux-image-amd64 docker.io docker-cli openssh-server iproute2 procps kmod ca-certificates systemd-sysv initramfs-tools curl
update-ca-certificates
rm -f /etc/apt/apt.conf.d/99-cloud-ca
touch /root/setup-complete
'''
        run(*(args + [BASE, 'sh', '-c', script]))
        run('docker', 'commit', '--change', 'ENV http_proxy= https_proxy= no_proxy=', '--change', 'CMD ["sleep", "infinity"]', builder, TOOLS)
    prep = 'qtrade-linux-debug-prep'
    if exists('container', prep):
        raise RuntimeError('Preparation container already exists; inspect it before retrying')
    run('docker', 'run', '-d', '--name', prep, '--label', 'qtrade.debug=true', TOOLS)
    if not (VM / 'id_ed25519').exists():
        run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(VM / 'id_ed25519'))
    run('docker', 'cp', str(VM / 'id_ed25519.pub'), prep + ':/tmp/debug-authorized-key')
    run('docker', 'cp', str(SOURCE / 'vm-guest.sh'), prep + ':/tmp/vm-guest.sh')
    run('docker', 'exec', prep, 'sh', '/tmp/vm-guest.sh')
    kernel = run('docker', 'exec', prep, 'sh', '-c', 'ls /boot/vmlinuz-*', capture_output=True, text=True).stdout.strip()
    if '\n' in kernel:
        raise RuntimeError('Multiple guest kernels; select explicitly before exporting')
    version = kernel.removeprefix('/boot/vmlinuz-')
    run('docker', 'cp', prep + ':' + kernel, str(VM / 'vmlinuz'))
    run('docker', 'cp', prep + ':/boot/initrd.img-' + version, str(VM / 'initrd.img'))
    key = run('docker', 'exec', prep, 'cat', '/etc/ssh/ssh_host_ed25519_key.pub', capture_output=True, text=True).stdout.split()
    (VM / 'known_hosts').write_text('[127.0.0.1]:10022 ' + key[0] + ' ' + key[1] + '\n')
    run('docker', 'export', '-o', str(VM / 'rootfs.tar'), prep)
    run('docker', 'pull', REDROID)
    run('docker', 'tag', REDROID, 'redroid/redroid:11.0.0-latest')
    run('docker', 'save', '-o', str(VM / 'payload/redroid11.tar'), 'redroid/redroid:11.0.0-latest')
    run('docker', 'run', '--rm', '--mount', 'type=bind,src=' + str(VM) + ',dst=/data', TOOLS, 'sh', '-c', '''set -eu
mkdir -p /tmp/guest-root
tar -xpf /data/rootfs.tar -C /tmp/guest-root
rm -f /tmp/guest-root/.dockerenv
printf 'nameserver 10.0.2.3\\n' > /tmp/guest-root/etc/resolv.conf
printf '127.0.0.1 localhost\\n127.0.1.1 qtrade-debug\\n' > /tmp/guest-root/etc/hosts
printf 'qtrade-debug\\n' > /tmp/guest-root/etc/hostname
truncate -s 12G /data/rootfs.img.new
mkfs.ext4 -F -q -d /tmp/guest-root /data/rootfs.img.new
mv /data/rootfs.img.new /data/rootfs.img
''')
    run('docker', 'stop', prep)
    print('VM prepared. Run bash vm-start.sh; keep the guest disk to preserve Android data.')


if __name__ == '__main__':
    main()
