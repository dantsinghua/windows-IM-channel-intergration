"""Use the cloud's existing egress proxy and CA inside our disposable Android VM.

This preserves TLS verification and the platform's destination allowlist. It does
not add routes for native TCP protocols that ignore Android's HTTP proxy.
"""
import os
from pathlib import Path
import shlex
import socket
import subprocess
from urllib.parse import urlsplit

SOURCE = Path(__file__).resolve().parent
PAYLOAD = Path('/workspace/.qtrade-linux-debug/vm/payload')


def main():
    proxy = urlsplit(os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy', ''))
    if not proxy.hostname:
        print('No platform proxy binding; no Android network settings changed.')
        return
    if proxy.scheme != 'http' or proxy.username or proxy.password:
        raise SystemExit('This helper requires the provided HTTP CONNECT proxy without URL credentials.')
    address = socket.gethostbyname(proxy.hostname)
    port = proxy.port or 80
    cert = Path(os.environ['CODEX_PROXY_CERT'])
    data = cert.read_bytes()
    if b'PRIVATE KEY' in data or data.count(b'BEGIN CERTIFICATE') != 1:
        raise SystemExit('Expected one public proxy CA certificate')
    name = subprocess.run(['openssl', 'x509', '-in', str(cert), '-subject_hash_old', '-noout'],
                          check=True, capture_output=True, text=True).stdout.strip() + '.0'
    target = PAYLOAD / name
    target.write_bytes(data)
    target.chmod(0o644)
    # Validate the same approved destination from the VM before altering Android.
    command = shlex.join(['curl', '--fail', '--silent', '--show-error', '--head',
                          '--proxy', f'http://{address}:{port}', '--cacert', '/mnt/payload/' + name,
                          '--connect-timeout', '8', '--max-time', '20', '--output', '/dev/null',
                          '--write-out', 'VM HTTPS via platform proxy: %{http_code}\n',
                          'https://deb.debian.org/debian/dists/trixie/InRelease'])
    subprocess.run(['bash', str(SOURCE / 'vm-ssh.sh'), command], check=True)
    remote = '/system/etc/security/cacerts/' + name
    check = subprocess.run(['bash', str(SOURCE / 'vm-ssh.sh'), shlex.join(
        ['docker', 'exec', 'qtrade-redroid', 'cat', remote])], capture_output=True)
    if check.returncode == 0 and check.stdout != data:
        raise SystemExit('Existing Android certificate with this hash differs; refusing to overwrite it')
    if check.returncode:
        subprocess.run(['bash', str(SOURCE / 'vm-ssh.sh'), shlex.join(
            ['docker', 'cp', '/mnt/payload/' + name, 'qtrade-redroid:' + remote])], check=True)
        subprocess.run(['bash', str(SOURCE / 'vm-ssh.sh'), shlex.join(
            ['docker', 'exec', 'qtrade-redroid', 'chmod', '644', remote])], check=True)
    subprocess.run(['bash', str(SOURCE / 'vm-ssh.sh'), shlex.join(
        ['docker', 'exec', 'qtrade-redroid', 'settings', 'put', 'global', 'http_proxy', f'{address}:{port}'])], check=True)
    print('Android uses the platform proxy and trusts its public CA. TLS verification remains enabled.')


if __name__ == '__main__':
    main()
