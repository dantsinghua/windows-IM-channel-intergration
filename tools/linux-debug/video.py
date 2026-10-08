"""Pinned scrcpy video/control transport. TCP sockets stay inside the VM runner."""
import asyncio
import contextlib
import hashlib
from pathlib import Path
import secrets
import shlex
import struct

VERSION = '3.3.4'
SHA256 = '8588238c9a5a00aa542906b6ec7e6d5541d9ffb9b5d0f6e1bc0e365e2303079e'
ARCHIVE = Path('/workspace/.qtrade-linux-debug/vm/payload/scrcpy-server-v' + VERSION)
ANDROID_JAR = '/data/local/tmp/qtrade-scrcpy-server.jar'
PROTOCOL = 'qtrade-scrcpy-v1'


def control_packet(message, width, height):
    """Only expose touch, a small navigation-key allowlist and ASCII input."""
    kind = message.get('type')
    if kind == 'touch':
        action = {'down': 0, 'up': 1, 'move': 2, 'cancel': 3}.get(message.get('action'))
        x, y = message.get('x'), message.get('y')
        if action is None or type(x) is not int or type(y) is not int or not (0 <= x < width and 0 <= y < height):
            raise ValueError('无效的触控坐标')
        pressure = 0 if action in (1, 3) else 65535
        return struct.pack('>BBQiiHHHII', 2, action, 0, x, y, width, height, pressure, 0, 0)
    if kind == 'key':
        key = {'home': 3, 'back': 4, 'enter': 66, 'delete': 67, 'select_all': 29}.get(message.get('key'))
        if key is None:
            raise ValueError('不支持的按键')
        meta = 0x1000 if message.get('key') == 'select_all' else 0
        return b''.join(struct.pack('>BBIII', 0, action, key, 0, meta) for action in (0, 1))
    if kind == 'text':
        text = message.get('text')
        if not isinstance(text, str) or not 1 <= len(text) <= 512 or any(ord(c) < 32 or ord(c) > 126 for c in text):
            raise ValueError('此入口支持 1–512 个 ASCII 字符')
        data = text.encode()
        return struct.pack('>BI', 1, len(data)) + data
    raise ValueError('不支持的控制操作')


class ScrcpySession:
    def __init__(self, device):
        self.device = device
        self.scid = f'{secrets.randbelow(0x7fffffff):08x}'
        self.port = None
        self.processes = []
        self.server = self.video = self.control = None
        self.width = self.height = 0
        self.log_task = None
        self.logs = b''

    async def adb(self, *args):
        return await asyncio.to_thread(self.device.run, *args, timeout=20)

    async def spawn(self, *args, input=False):
        proc = await asyncio.create_subprocess_exec(*args,
            stdin=asyncio.subprocess.PIPE if input else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self.processes.append(proc)
        return proc

    async def start(self):
        if not ARCHIVE.is_file() or hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() != SHA256:
            raise RuntimeError('请先运行 python install-video.py 准备已校验的 scrcpy 服务端')
        await self.adb('push', '/data/payload/' + ARCHIVE.name, ANDROID_JAR)
        raw_port = await self.adb('forward', 'tcp:0', 'localabstract:scrcpy_' + self.scid)
        self.port = int(raw_port.strip())
        if not 1 <= self.port <= 65535:
            raise RuntimeError('无效的设备转发端口')
        options = ['env', 'CLASSPATH=' + ANDROID_JAR, 'app_process', '/',
                   'com.genymobile.scrcpy.Server', VERSION, 'scid=' + self.scid,
                   'log_level=warn', 'audio=false', 'tunnel_forward=true',
                   'max_size=720', 'max_fps=15', 'video_bit_rate=1500000',
                   'video_codec=h264', 'video_encoder=OMX.google.h264.encoder',
                   'video_codec_options=i-frame-interval=1', 'send_device_meta=false',
                   'clipboard_autosync=false', 'power_on=false', 'cleanup=false']
        pidfile = '/data/local/tmp/qtrade-scrcpy-' + self.scid + '.pid'
        script = 'echo $$ > ' + pidfile + '; exec ' + shlex.join(options)
        prefix = ['docker', 'exec', self.device.container, '/opt/platform-tools/adb', '-s', self.device.serial]
        self.server = await self.spawn(*prefix, 'shell', shlex.join(['sh', '-c', script]))
        async def drain_logs():
            while data := await self.server.stdout.read(4096):
                self.logs = (self.logs + data)[-4096:]
        self.log_task = asyncio.create_task(drain_logs())
        # adb forward may accept then immediately close while the Android socket
        # is not listening yet; wait for the real abstract socket before dialing.
        for _ in range(100):
            sockets = await self.adb('shell', 'cat /proc/net/unix')
            if ('@scrcpy_' + self.scid).encode() in sockets:
                break
            if self.server.returncode is not None:
                raise RuntimeError('scrcpy 服务端启动失败')
            await asyncio.sleep(.1)
        else:
            raise RuntimeError('scrcpy socket 未就绪')
        # Bash's TCP redirection avoids installing another tunnel service. No TTY.
        opening = f'for i in {{1..100}}; do exec 3<>/dev/tcp/127.0.0.1/{self.port} 2>/dev/null && break; sleep .1; done; '
        self.video = await self.spawn('docker', 'exec', self.device.container, 'bash', '-c', opening + 'exec cat <&3')
        dummy = await asyncio.wait_for(self.video.stdout.readexactly(1), 25)
        if dummy != b'\0':
            raise RuntimeError('scrcpy 握手失败')
        self.control = await self.spawn('docker', 'exec', '-i', self.device.container, 'bash', '-c',
                                       f'exec 3<>/dev/tcp/127.0.0.1/{self.port}; exec cat >&3', input=True)
        codec, self.width, self.height = struct.unpack('>III', await asyncio.wait_for(self.video.stdout.readexactly(12), 30))
        if codec != 0x68323634 or not (0 < self.width <= 4096 and 0 < self.height <= 4096):
            raise RuntimeError('设备未返回有效 H.264 视频头')
        return {'codec': 'h264', 'width': self.width, 'height': self.height,
                'profile': 'debug15', 'fps': 15, 'seq0': 0}

    async def packets(self):
        config = b''
        while True:
            # A static Android screen can legitimately stop producing frames.
            # WS ping/pong and socket EOF detect disconnects; don't kill an idle screen.
            header = await self.video.stdout.readexactly(12)
            flags_pts, size = struct.unpack('>QI', header)
            if not 0 < size <= 2 * 1024 * 1024:
                raise RuntimeError('无效的视频帧长度')
            data = await asyncio.wait_for(self.video.stdout.readexactly(size), 15)
            if flags_pts & (1 << 63):
                config = data
                yield struct.pack('>Q', 0) + config
                continue
            if flags_pts & (1 << 62):
                data = config + data
            pts_ms = (flags_pts & ((1 << 62) - 1)) // 1000
            yield struct.pack('>Q', pts_ms) + data

    async def send_control(self, message):
        packet = control_packet(message, self.width, self.height)
        self.control.stdin.write(packet)
        await asyncio.wait_for(self.control.stdin.drain(), 2)

    async def close(self):
        # Kill only this stream's recorded Android process, after matching its scid.
        pidfile = '/data/local/tmp/qtrade-scrcpy-' + self.scid + '.pid'
        script = ('pid=$(cat ' + pidfile + ' 2>/dev/null); '
                  'case "$pid" in ""|*[!0-9]*) ;; *) '
                  'if tr "\\000" " " < /proc/"$pid"/cmdline 2>/dev/null | grep -F ' + shlex.quote('scid=' + self.scid) +
                  ' >/dev/null; then kill "$pid" 2>/dev/null; fi ;; esac; rm -f ' + pidfile)
        with contextlib.suppress(Exception):
            await self.adb('shell', shlex.join(['sh', '-c', script]))
        if self.port:
            with contextlib.suppress(Exception):
                await self.adb('forward', '--remove', 'tcp:' + str(self.port))
        for proc in reversed(self.processes):
            if proc.stdin:
                proc.stdin.close()
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
        if self.log_task:
            self.log_task.cancel()
            await asyncio.gather(self.log_task, return_exceptions=True)
