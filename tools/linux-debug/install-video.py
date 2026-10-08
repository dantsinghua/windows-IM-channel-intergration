"""Download the fixed upstream scrcpy server, checking its published SHA-256."""
import hashlib
from urllib.request import urlopen
from video import ARCHIVE, SHA256, VERSION

if not ARCHIVE.is_file():
    with urlopen(f'https://github.com/Genymobile/scrcpy/releases/download/v{VERSION}/scrcpy-server-v{VERSION}', timeout=60) as response:
        data = response.read(2 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit('scrcpy checksum mismatch; no file installed')
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE.write_bytes(data)
if hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() != SHA256:
    raise SystemExit('Cached scrcpy checksum mismatch; stop and inspect the file')
print('Verified official scrcpy server', VERSION)
