"""企点画面流执行体(02 #34 / #35 / #101、04 H07 与 §2.7.4「scrcpy 转发」):scrcpy-server 4.1。

拉起(每账号一个 server,端口 = 00 §3 ``16500 + NN``):
``adb push <jar> /data/local/tmp/scrcpy-server.jar`` → ``adb forward tcp:165NN localabstract:scrcpy`` →
``adb shell CLASSPATH=… app_process / com.genymobile.scrcpy.Server 4.1 tunnel_forward=true …`` →
依次连两条 TCP 到 ``127.0.0.1:165NN``:第一条视频、第二条控制。**两条都连上之后** server 才开始出流头
(4.1 ``DesktopConnection.open``:``tunnel_forward`` 下按 video→audio→control 顺序 accept 完才返回,
dummy 字节在 accept 视频后立刻写;``SurfaceEncoder`` 之后才写流头)⇒ 读 dummy → 连控制 → 再读头。

视频 socket(big-endian,4.1 ``device/Streamer.java``):1 字节 dummy → 4 字节 codec id(``send_stream_meta``)
→ 包序列,每包 12 字节头 + 数据:
- ``pts_flags``(8 字节)bit63 置位 = **session 包**:头 12 字节就是 ``int flags(bit31=1, bit0=client resize)
  + int 宽 + int 高``,没有数据段;每次编码会话(重)开都会来一个,分辨率变了 ⇒ 重发首帧 JSON。
- 否则 ``pts_flags`` + 4 字节包长 + Annex-B;bit62 = 配置包(SPS/PPS)、bit61 = 关键帧、低 61 位 PTS 微秒。
真机字节(2026-09-26 redroid 11 探针):``80000000 000002d0 00000500``(session,720×1280,无载荷)→
``4000000000000000 00000021`` + 33 B SPS/PPS → ``2000000000000000|pts`` + 关键帧 → delta ……
转给 #34 时去掉包长、PTS 转毫秒;配置包缓存下来,拼在每个关键帧前面发(02 #34「SPS/PPS 随每个 IDR 重发」)。

关键帧:``i-frame-interval`` 在真机软件编码器上**无效**(整条流只有开头一个关键帧),画面静止时 server 也**一帧不出**。
⇒ 新 WS 接入 / ``resume`` / 慢客户端丢帧重对齐时,向控制 socket 发 ``TYPE_RESET_VIDEO``(=17,无额外字段),
server 重置编码器、再出 session + 配置包 + 关键帧;订阅者只从关键帧开始收。``key_wait_s``(3 s)内没来 ⇒ 重拉 server。

健康(04 H07):server 进程在 + 视频 socket 未 EOF + 控制 socket 未 EOF ⇒ 健康;任一断 ⇒ 重建。
「没有帧」**不是**故障(静止画面本来就 0 帧);``scrcpy_frame_timeout_s`` 只管「开流后等首个关键帧」。

控制 socket:``touch/scroll/key/text`` 编成 scrcpy 控制消息写进去,**不经任何 shell**。

模型(规格 A.2):同账号一个 server,多条 WS 共享;``focus*`` 同时只 1 条由路由层保证。
``profile`` 切换 / #101 / H07 自愈 = 重建 forward + server(每次都重新 ``adb push`` jar:4.1 启动后自删 jar),
并给已连 WS 发 ``{type:'restart'}`` 让它重连。
``pause`` / ``resume``(**待安琳裁决 A.2 的实现口径**):4.1 下视频 socket 一断 server 就退出、控制也跟着断
(真机坐实),所以 ``pause`` = 只停止向这条 WS 转发帧、两条 socket 都不动(静止时 server 本就不编码,开销可忽略);
``resume`` = 发一次 RESET_VIDEO 让画面立刻刷新。
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Any, AsyncIterator, Callable, Optional, Union

from . import screen_adb
from .screen_adb import AsyncAdb, keycode_of

log = logging.getLogger("qtrade.screen.scrcpy")

REMOTE_JAR = "/data/local/tmp/scrcpy-server.jar"
SERVER_CLASS = "com.genymobile.scrcpy.Server"
CODEC_H264 = 0x68323634                  # "h264"
# 4.1 device/Streamer.java:PACKET_FLAG_SESSION / CONFIG / KEY_FRAME
FLAG_SESSION = 1 << 63
FLAG_CONFIG = 1 << 62
FLAG_KEY = 1 << 61
PTS_MASK = FLAG_KEY - 1
# 4.1 Streamer.writeDisableStream:codec id 位置写 0 = 流被关、1 = server 配置出错
CODEC_DISABLED = 0
CODEC_ERROR = 1

# 控制消息类型(scrcpy ControlMessage)
MSG_INJECT_KEYCODE = 0
MSG_INJECT_TEXT = 1
MSG_INJECT_TOUCH = 2
MSG_INJECT_SCROLL = 3
# 4.1 ControlMessage.TYPE_RESET_VIDEO;ControlMessageReader 里 createEmpty ⇒ 整条消息只有 1 字节类型号
MSG_RESET_VIDEO = 17
RESET_VIDEO = bytes([MSG_RESET_VIDEO])
KEY_ACTIONS = {"down": 0, "up": 1}                       # KeyEvent.ACTION_*
TOUCH_ACTIONS = {"down": 0, "up": 1, "move": 2}          # MotionEvent.ACTION_*
BUTTON_PRIMARY = 1
TEXT_CHUNK_MAX = 300                                     # scrcpy 单条 INJECT_TEXT 上限(字节)
SCROLL_NOTCH_PX = 100.0                                  # 浏览器 wheel 一格 ≈ 100 px ⇒ scrcpy 滚动量 1.0
# 4.1 ControlMessageReader.parseInjectScrollEvent:i16FixedPointToFloat(v) * 16,实际范围 [-16, 16];
# 官方客户端 control_msg.c 编码前先 /16 ⇒ 这里同样先 /16 再编定点
SCROLL_RANGE = 16.0

#: 02 §7.1 ``[adapters.qidian] stream_profiles`` 缺省四档
DEFAULT_STREAM_PROFILES = {"thumb": "540p@5", "thumb10": "540p@10", "focus": "720p@30", "focus15": "720p@15"}
#: 仍按 1 s 传给编码器(无害,server 接受),但真机软件编码器 ``OMX.google.h264.encoder`` 不按它出周期关键帧——
#: **不能依赖**;要关键帧一律发 RESET_VIDEO(见模块说明)。
I_FRAME_INTERVAL_S = 1

Target = tuple[str, int]                                 # (adb serial, 165NN)
FrameItem = Union[tuple[int, bytes], dict[str, Any]]


class ScrcpyError(RuntimeError):
    pass


# ══════════════════════════════════════════════════════════════════ 纯函数(协议字节)
def parse_profile(spec: str) -> tuple[int, int]:
    """``"540p@5"`` → ``(540, 5)``。"""
    res, _, fps = str(spec).strip().lower().partition("@")
    short = int(res.rstrip("p"))
    return short, int(fps)


def profile_params(spec: str) -> dict[str, int]:
    """档位 → scrcpy 的 ``max_size``(长边)/ ``max_fps`` / ``video_bit_rate``。竖屏 9:16:540p ⇒ 960、720p ⇒ 1280。"""
    short, fps = parse_profile(spec)
    long_side = (short * 16 // 9 + 7) // 8 * 8
    bit_rate = max(800_000, int(short * long_side * fps * 0.12))
    return {"max_size": long_side, "max_fps": fps, "video_bit_rate": bit_rate}


def server_args(version: str, spec: str) -> list[str]:
    p = profile_params(spec)
    return [
        version, "tunnel_forward=true", "video=true", "audio=false", "control=true", "video_codec=h264",
        f"max_size={p['max_size']}", f"max_fps={p['max_fps']}", f"video_bit_rate={p['video_bit_rate']}",
        f"video_codec_options=i-frame-interval={I_FRAME_INTERVAL_S}",
        "send_frame_meta=true", "send_stream_meta=true", "send_device_meta=false", "send_dummy_byte=true",
    ]


def _coord(v: Any, size: int) -> int:
    """归一化 0~1 → 视频像素;大于 1 视为已是视频像素。"""
    f = float(v if v is not None else 0)
    px = f * size if 0 <= f <= 1 else f
    return max(0, min(size - 1, int(round(px))))


def _i16fp(v: float) -> int:
    """scrcpy ``sc_float_to_i16fp``:[-1, 1] → 定点 i16(向零截断),满值 0x7FFF。"""
    v = max(-1.0, min(1.0, v))
    return max(-0x8000, min(0x7FFF, int(v * 0x8000)))


def encode_touch(action: str, pointer: int, x: int, y: int, w: int, h: int) -> bytes:
    code = TOUCH_ACTIONS[action]
    pressure = 0 if action == "up" else 0xFFFF
    buttons = 0 if action == "up" else BUTTON_PRIMARY
    return struct.pack(">BBqiiHHHII", MSG_INJECT_TOUCH, code, int(pointer), x, y, w, h, pressure,
                       BUTTON_PRIMARY, buttons)


def encode_scroll(x: int, y: int, w: int, h: int, hscroll: float, vscroll: float) -> bytes:
    """``hscroll`` / ``vscroll`` 是 scrcpy 滚动量(格,[-16, 16]);先 /16 归一化再编定点(服务端再 ×16)。"""
    return struct.pack(">BiiHHhhI", MSG_INJECT_SCROLL, x, y, w, h,
                       _i16fp(hscroll / SCROLL_RANGE), _i16fp(vscroll / SCROLL_RANGE), 0)


def encode_key(action: str, keycode: int) -> bytes:
    return struct.pack(">BBIII", MSG_INJECT_KEYCODE, KEY_ACTIONS[action], keycode, 0, 0)


def encode_text(text: str) -> list[bytes]:
    """按 UTF-8 字符边界切成 ≤300 字节的几条 INJECT_TEXT。"""
    out: list[bytes] = []
    chunk = b""
    for ch in text:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > TEXT_CHUNK_MAX:
            out.append(chunk)
            chunk = b""
        chunk += b
    if chunk:
        out.append(chunk)
    return [struct.pack(">BI", MSG_INJECT_TEXT, len(c)) + c for c in out]


def control_bytes(msg: dict[str, Any], width: int, height: int) -> list[bytes]:
    """#34 控制帧 → scrcpy 控制消息字节;不认识 / 参数不全回空列表(不猜默认值)。"""
    kind = msg.get("type")
    if kind == "touch":
        action = str(msg.get("action") or "")
        if action not in TOUCH_ACTIONS:
            return []
        pointer = msg.get("pointer")
        pointer = int(pointer) if isinstance(pointer, (int, float)) and not isinstance(pointer, bool) else 0
        return [encode_touch(action, pointer, _coord(msg.get("x"), width), _coord(msg.get("y"), height),
                             width, height)]
    if kind == "scroll":
        x = _coord(msg.get("x", 0.5), width)
        y = _coord(msg.get("y", 0.5), height)
        dx = float(msg.get("dx") or 0) / SCROLL_NOTCH_PX
        dy = float(msg.get("dy") or 0) / SCROLL_NOTCH_PX
        # 浏览器 deltaY>0 = 往下翻(内容上移);Android AXIS_VSCROLL>0 = 往上 ⇒ 取反;水平同理
        return [encode_scroll(x, y, width, height, -dx, -dy)]
    if kind == "key":
        action = str(msg.get("action") or "down")
        code = keycode_of(msg.get("keycode"))
        if action not in KEY_ACTIONS or code is None:
            return []
        return [encode_key(action, code)]
    if kind == "text":
        text = msg.get("text")
        return encode_text(text) if isinstance(text, str) and text else []
    return []


def parse_session(hdr: bytes) -> Optional[tuple[int, int]]:
    """12 字节包头若是 session 包(bit63)⇒ ``(宽, 高)``;否则 None(4.1 客户端 ``sc_demuxer_parse_session``)。"""
    flags, w, h = struct.unpack(">III", hdr)
    if not flags & 0x80000000:
        return None
    return w, h


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """读一包:session 包返回 ``(pts_flags, 12 字节头)``(无数据段);其余返回 ``(pts_flags, 数据)``。"""
    hdr = await reader.readexactly(12)
    pts_flags, size = struct.unpack(">QI", hdr)
    if pts_flags & FLAG_SESSION:
        return pts_flags, hdr
    return pts_flags, await reader.readexactly(size)


def store_target_resolver(store) -> Callable[[str], Target]:
    """账号 → ``(adb serial, 165NN)``;serial / 端口没落库就按 00 §3 由序号推导(不回退到任何固定设备)。"""
    def target_of(account_id: str) -> Target:
        row = store.get_account_full(account_id)
        if row is None or row.get("deleted_ms"):
            raise LookupError(f"账号不存在:{account_id}")
        if row.get("channel") != "qidian":
            raise LookupError(f"{account_id} 不是企点账号,没有画面流")
        seq = row.get("seq")
        serial = row.get("adb_serial") or (f"127.0.0.1:{16000 + int(seq)}" if seq else None)
        port = row.get("stream_port") or (16500 + int(seq) if seq else None)
        if not serial or not port:
            raise LookupError(f"{account_id} 查不到 adb serial / 画面端口")
        return str(serial), int(port)
    return target_of


# ══════════════════════════════════════════════════════════════════ 订阅者 / 通道
class _Sub:
    QUEUE_MAX = 90

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Optional[FrameItem]] = asyncio.Queue()
        self.waiting_key = True
        self.paused = False
        self.detached = False

    def offer(self, pts_ms: int, data: bytes, key: bool) -> None:
        """``pause`` 中不转发(socket 不动,A.2 待裁决口径);没对齐到关键帧前只丢不送。"""
        if self.paused or self.detached:
            return
        if not key and (self.waiting_key or self.queue.qsize() >= self.QUEUE_MAX):
            self.waiting_key = True                     # 慢客户端:丢到下一个关键帧重新对齐(通道发 RESET_VIDEO 要)
            return
        if key:
            self.waiting_key = False
        self.queue.put_nowait((pts_ms, data))

    def renew(self, meta: dict[str, Any]) -> None:
        """新 session:发新首帧 JSON,之后从下一个关键帧开始送。"""
        if self.detached:
            return
        self.waiting_key = True
        self.queue.put_nowait(meta)

    def end(self, last: Optional[dict[str, Any]] = None) -> None:
        if self.detached:
            return
        self.detached = True
        if last is not None:
            self.queue.put_nowait(last)
        self.queue.put_nowait(None)


class _Channel:
    """一个账号的 scrcpy-server:视频读循环、控制写、看门狗(H07)。"""

    def __init__(self, backend: "ScrcpyBackend", account_id: str, serial: str, port: int, profile: str) -> None:
        self.b = backend
        self.account_id = account_id
        self.serial = serial
        self.port = port
        self.profile = profile
        self.width = 0
        self.height = 0
        self.config: bytes = b""
        self.subs: set[_Sub] = set()
        self.lock = asyncio.Lock()
        self.alive = False
        self.closed = False
        self.forward_ok = False
        self.last_key: Optional[tuple[int, bytes]] = None   # 最近关键帧(已拼配置包)
        self._key_seen = asyncio.Event()
        self._key_wait: Optional[asyncio.Task] = None
        self._proc: Any = None
        self._vr: Optional[asyncio.StreamReader] = None
        self._vw: Optional[asyncio.StreamWriter] = None
        self._cr: Optional[asyncio.StreamReader] = None
        self._cw: Optional[asyncio.StreamWriter] = None
        self._tasks: list[asyncio.Task] = []
        self._heal_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._ctrl_lock = asyncio.Lock()
        self._proc_tail: list[str] = []

    # ---------------------------------------------------------------- meta
    def meta(self) -> dict[str, Any]:
        return {"codec": "h264", "width": self.width, "height": self.height, "profile": self.profile,
                "fps": parse_profile(self.b.profiles[self.profile])[1], "seq0": 0}

    # ---------------------------------------------------------------- 起停(调用方持 self.lock)
    async def _start_io(self) -> None:
        adb = self.b.adb
        rc, out = await adb.run(self.serial, "push", self.b.server_jar, REMOTE_JAR, timeout=30.0)
        if rc != 0:
            raise ScrcpyError(f"push scrcpy-server 失败 rc={rc}: {out.decode('utf-8', 'replace')[-200:]}")
        rc, out = await adb.run(self.serial, "forward", f"tcp:{self.port}", "localabstract:scrcpy", timeout=10.0)
        self.forward_ok = rc == 0
        if rc != 0:
            raise ScrcpyError(f"adb forward tcp:{self.port} 失败 rc={rc}: {out.decode('utf-8', 'replace')[-200:]}")
        self._proc_tail = []
        self._proc = await adb.spawn(self.serial, "shell", f"CLASSPATH={REMOTE_JAR}", "app_process", "/",
                                     SERVER_CLASS, *server_args(self.b.server_version, self.b.profiles[self.profile]))
        self._tasks.append(asyncio.create_task(self._drain_proc(), name=f"scrcpy-log:{self.account_id}"))
        self._vr, self._vw = await self._connect_video()
        # 4.1:server 要把控制 socket 也 accept 完才出流头 ⇒ 必须先连控制,再读头(X1)
        self._cr, self._cw = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.port),
                                                    self.b.connect_timeout_s)
        tmo = self.b.connect_timeout_s
        (codec,) = struct.unpack(">I", await asyncio.wait_for(self._vr.readexactly(4), tmo))
        if codec in (CODEC_DISABLED, CODEC_ERROR):
            raise ScrcpyError(f"scrcpy-server 关了视频流(codec_id={codec}): {' | '.join(self._proc_tail[-5:])}")
        if codec != CODEC_H264:
            raise ScrcpyError(f"scrcpy 回的编码不是 h264(codec_id=0x{codec:08x})")
        size = parse_session(await asyncio.wait_for(self._vr.readexactly(12), tmo))
        if size is None:
            raise ScrcpyError("scrcpy 视频流头之后第一包不是 session 包")
        w, h = size
        if not w or not h:
            raise ScrcpyError(f"scrcpy session 包尺寸无效 {w}x{h}")
        self.width, self.height = w, h
        self.config = b""
        self.last_key = None
        self.alive = True
        self._tasks += [asyncio.create_task(self._read_video(), name=f"scrcpy-video:{self.account_id}"),
                        asyncio.create_task(self._drain_control(), name=f"scrcpy-ctrl:{self.account_id}"),
                        asyncio.create_task(self._watch_proc(), name=f"scrcpy-proc:{self.account_id}")]
        # 新 server 自己会出首关键帧(真机 0.27 s);scrcpy_frame_timeout_s 内没来才算 H07
        self._want_key(reset=False, timeout=self.b.frame_timeout_s, why="first_keyframe")
        log.info("画面流已拉起 account=%s serial=%s port=%d %dx%d profile=%s",
                 self.account_id, self.serial, self.port, w, h, self.profile)

    async def _connect_video(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """forward 模式下 server 没 listen 时 adb 也会先接住连接再立刻关 ⇒ 以读到 dummy 字节为准,读不到就重试。"""
        deadline = self.b.clock() + self.b.connect_timeout_s
        last: Optional[BaseException] = None
        while self.b.clock() < deadline:
            if getattr(self._proc, "returncode", None) is not None:
                raise ScrcpyError(f"scrcpy-server 提前退出 rc={self._proc.returncode}: {' | '.join(self._proc_tail[-5:])}")
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.port), 1.0)
                try:
                    if len(await asyncio.wait_for(r.readexactly(1), 1.0)) == 1:
                        return r, w
                except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError) as e:
                    last = e
                w.close()
            except (OSError, asyncio.TimeoutError) as e:
                last = e
            await asyncio.sleep(self.b.connect_retry_s)
        raise ScrcpyError(f"连不上 scrcpy 视频 socket 127.0.0.1:{self.port}: {last!r}")

    async def _stop_io(self, *, remove_forward: bool) -> None:
        self.alive = False
        me = asyncio.current_task()
        for t in self._tasks:
            if t is not me:
                t.cancel()
        for t in self._tasks:
            if t is not me:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._tasks = []
        self._key_wait = None
        for w in (self._vw, self._cw):
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass
        self._vr = self._vw = self._cr = self._cw = None
        proc, self._proc = self._proc, None
        if proc is not None and getattr(proc, "returncode", None) is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), 3.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            except Exception as e:
                log.warning("收 scrcpy-server 进程出错 account=%s: %s", self.account_id, e)
        if remove_forward:
            await self.b.adb.run(self.serial, "forward", "--remove", f"tcp:{self.port}", timeout=10.0)

    # ---------------------------------------------------------------- 后台任务
    async def _drain_proc(self) -> None:
        stream = getattr(self._proc, "stdout", None)
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            self._proc_tail = (self._proc_tail + [text])[-20:]
            log.debug("scrcpy-server[%s] %s", self.account_id, text)

    async def _drain_control(self) -> None:
        """设备→Agent 的控制消息(剪贴板等)读掉丢弃,免得 socket 缓冲塞满;读到 EOF = server 断了 ⇒ H07 重建。"""
        reader = self._cr
        assert reader is not None
        why: Any = "EOF"
        try:
            while await reader.read(4096):
                pass
        except (ConnectionError, OSError) as e:
            why = e
        if self.alive:
            log.warning("画面流控制 socket 断了 account=%s: %r ⇒ 重建(H07)", self.account_id, why)
            self._spawn_heal("control_eof")

    async def _watch_proc(self) -> None:
        """scrcpy-server 进程退出 ⇒ H07 重建(真机:视频 socket 一断 server 就 rc=0 退出)。"""
        proc = self._proc
        if proc is None:
            return
        rc = await proc.wait()
        if self.alive:
            log.warning("scrcpy-server 进程退出 account=%s rc=%s: %s ⇒ 重建(H07)", self.account_id, rc,
                        " | ".join(self._proc_tail[-5:]))
            self._spawn_heal("server_exit")

    async def _read_video(self) -> None:
        reader = self._vr
        assert reader is not None
        try:
            while True:
                pts_flags, data = await read_packet(reader)
                if pts_flags & FLAG_SESSION:
                    self._on_session(data)
                    continue
                if pts_flags & FLAG_CONFIG:
                    self.config = data
                    continue
                key = bool(pts_flags & FLAG_KEY)
                pts_ms = (pts_flags & PTS_MASK) // 1000
                payload = self.config + data if key else data
                if key:
                    self.last_key = (pts_ms, payload)
                    self._key_seen.set()
                for sub in list(self.subs):
                    sub.offer(pts_ms, payload, key)
                if not key and any(s.waiting_key and not s.paused and not s.detached for s in self.subs):
                    self.request_keyframe("resync")      # 有订阅者掉队:不会再有自然关键帧,主动要
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
            if not self.alive:
                return
            log.warning("画面流视频 socket 断了 account=%s: %r ⇒ 重建(H07)", self.account_id, e)
            self._spawn_heal("video_eof")

    def _on_session(self, hdr: bytes) -> None:
        """编码会话重开(4.1 每次 ``SurfaceEncoder`` 重配都发):尺寸变了 ⇒ 更新宽高、清配置缓存,
        给已连 WS 重发首帧 JSON 并从下一个关键帧重新对齐;尺寸没变只清缓存(新 SPS/PPS 会紧跟着来)。"""
        size = parse_session(hdr)
        self.config = b""
        # 编码器重开后自带配置包 + 关键帧:期间掉队的订阅者等它就行,不再另发 RESET_VIDEO
        self._want_key(reset=False, timeout=self.b.key_wait_s, why="session")
        if size is None or not size[0] or not size[1] or size == (self.width, self.height):
            return
        self.width, self.height = size
        log.info("画面流分辨率变化 account=%s ⇒ %dx%d", self.account_id, *size)
        meta = self.meta()
        for sub in list(self.subs):
            sub.renew(meta)

    # ---------------------------------------------------------------- 关键帧
    def request_keyframe(self, why: str) -> None:
        """要一个新关键帧:发 RESET_VIDEO,``key_wait_s`` 内没来 ⇒ 重拉 server(订阅者留着,收新首帧 JSON)。"""
        self._want_key(reset=True, timeout=self.b.key_wait_s, why=why)

    def _want_key(self, *, reset: bool, timeout: float, why: str) -> None:
        if not self.alive or (self._key_wait is not None and not self._key_wait.done()):
            return                                        # 已在等关键帧:来一个就够所有等待者用
        self._key_seen.clear()
        if reset and self._cw is not None:
            self._cw.write(RESET_VIDEO)                   # 同步整条写入,不会与 send_control 的消息交错
        task = asyncio.create_task(self._await_key(timeout, why), name=f"scrcpy-key:{self.account_id}")
        self._key_wait = task
        self._tasks.append(task)

    async def _await_key(self, timeout: float, why: str) -> None:
        try:
            await asyncio.wait_for(self._key_seen.wait(), timeout)
            return
        except asyncio.TimeoutError:
            pass
        if not self.alive:
            return
        first = why == "first_keyframe"
        log.warning("画面流 %.1fs 内没等到关键帧 account=%s why=%s ⇒ 重拉 server", timeout, self.account_id, why)
        # 首关键帧没来 = 开流失败(H07,通知客户端重连);RESET_VIDEO 没回 = 退回重拉,已连 WS 留着收新首帧 JSON
        self._spawn_heal(f"no_keyframe:{why}", notify=first)

    def _spawn_heal(self, why: str, *, notify: bool = True) -> None:
        if self._heal_task is not None and not self._heal_task.done():
            return
        self._heal_task = asyncio.create_task(self.restart(notify=notify, why=why),
                                              name=f"scrcpy-heal:{self.account_id}")

    # ---------------------------------------------------------------- 对外动作
    async def start(self) -> None:
        async with self.lock:
            try:
                await self._start_io()
            except BaseException:
                await self._stop_io(remove_forward=False)
                raise

    async def restart(self, *, notify: bool, profile: Optional[str] = None, why: str = "") -> bool:
        """重建 forward + server。``notify`` ⇒ 已连 WS 收 ``{type:'restart'}`` 并摘下(客户端重连);否则保留订阅者续流。"""
        async with self.lock:
            if self.closed:
                return False
            if notify:
                subs, self.subs = list(self.subs), set()
                for s in subs:
                    s.end({"type": "restart"})
            await self._stop_io(remove_forward=True)
            if profile is not None:
                self.profile = profile
            try:
                await self._start_io()
            except Exception as e:
                log.warning("画面流重建失败 account=%s why=%s: %s", self.account_id, why, e)
                await self._stop_io(remove_forward=False)
                for s in list(self.subs):                   # 留着的订阅者也摘下:让客户端重连,重连时如实拿 4503
                    s.end({"type": "restart"})
                self.subs.clear()
                self._close_locked()
                return False
            for s in self.subs:
                s.waiting_key = True
                s.queue.put_nowait(self.meta())
            if not self.subs:
                self._schedule_idle_stop()
            return True

    async def send_control(self, blobs: list[bytes]) -> None:
        if not blobs:
            return
        async with self._ctrl_lock:
            w = self._cw
            if w is None or not self.alive:
                raise ScrcpyError("控制 socket 未连上")
            for b in blobs:
                w.write(b)
            await w.drain()

    def attach(self, sub: _Sub) -> None:
        """新订阅者从关键帧开始收。通道已出过关键帧 ⇒ 发 RESET_VIDEO 要个新的:缓存的 ``last_key`` 之后
        可能已有 delta,拿它当起点会花屏;还在等首关键帧 ⇒ 不用发,首关键帧就是它的起点。"""
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        self.subs.add(sub)
        if self.last_key is not None:
            self.request_keyframe("attach")

    def detach(self, sub: _Sub) -> None:
        self.subs.discard(sub)
        if not self.subs and not self.closed:
            self._schedule_idle_stop()

    def _schedule_idle_stop(self) -> None:
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_stop(), name=f"scrcpy-idle:{self.account_id}")

    async def _idle_stop(self) -> None:
        await asyncio.sleep(self.b.idle_stop_s)
        async with self.lock:
            if self.subs or self.closed:
                return
            await self._stop_io(remove_forward=True)
            self._close_locked()

    def _close_locked(self) -> None:
        self.closed = True
        if self.b.channels.get(self.account_id) is self:
            self.b.channels.pop(self.account_id, None)

    async def close(self) -> None:
        async with self.lock:
            for s in list(self.subs):
                s.end()
            self.subs.clear()
            await self._stop_io(remove_forward=True)
            self._close_locked()
        for t in (self._idle_task, self._heal_task):
            if t is not None and t is not asyncio.current_task() and not t.done():
                t.cancel()


# ══════════════════════════════════════════════════════════════════ 会话 / 后端
class ScrcpySession:
    """一条 #34 WS 对应的会话:``meta`` / ``frames()`` / ``control(msg)`` / ``close()``(routes_ext2 的 StreamBackend 协议)。"""

    def __init__(self, backend: "ScrcpyBackend", channel: _Channel, sub: _Sub) -> None:
        self._b = backend
        self._ch = channel
        self._sub = sub
        self.meta = channel.meta()

    async def frames(self) -> AsyncIterator[FrameItem]:
        """``(pts_ms, Annex-B)`` 元组;中途 ``dict`` = 要原样发 JSON 的控制消息(新 meta / ``{type:'restart'}``)。"""
        while True:
            item = await self._sub.queue.get()
            if item is None:
                return
            yield item

    async def control(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        ch = self._ch
        if kind == "pong":
            return
        if kind == "pause":
            # A.2 待裁决口径:只停转发,两条 socket 都不动(4.1 断视频 ⇒ server 退出、控制也断)
            self._sub.paused = True
            return
        if kind == "resume":
            if self._sub.paused:
                self._sub.paused = False
                self._sub.waiting_key = True
                ch.request_keyframe("resume")             # 画面立刻刷新,不等下一次重绘
            return
        if kind == "profile":
            new = str(msg.get("profile") or "")
            if new in self._b.profiles and new != ch.profile:
                await ch.restart(notify=True, profile=new, why="profile")
            return
        blobs = control_bytes(msg, ch.width, ch.height)
        if not blobs:
            # 不认识的键名 / 动作一律丢弃,不落成任何默认键
            log.warning("画面流控制帧丢弃(参数不全或不认识)account=%s type=%s keycode=%r action=%r",
                        ch.account_id, kind, msg.get("keycode"), msg.get("action"))
            return
        await ch.send_control(blobs)

    async def close(self) -> None:
        self._sub.end()
        self._ch.detach(self._sub)


class ScrcpyBackend:
    """``agent.stream_backend`` 的真机实现(替换原 ``AdbScreenBackend``)。

    接口:``open(account_id, *, profile) -> ScrcpySession``、``restart(account_id)``(#101 / H07 同一实现)、
    ``capture_png(account_id)``(#33 企点截图,经适配器、走总线)、``inject(account_id, msg)``(#35 REST 兜底)、``aclose()``。
    """

    def __init__(self, target_of: Callable[[str], Target], *, adb: Optional[Any] = None,
                 server_jar: str = "/opt/qtrade/scrcpy/scrcpy-server", server_version: str = "4.1",
                 profiles: Optional[dict[str, str]] = None, frame_timeout_s: float = 10.0,
                 key_wait_s: float = 3.0, idle_stop_s: float = 5.0, connect_timeout_s: float = 5.0,
                 connect_retry_s: float = 0.1,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._target_of = target_of
        self.adb = adb if adb is not None else AsyncAdb()
        self.server_jar = server_jar
        self.server_version = server_version
        self.profiles = dict(profiles or DEFAULT_STREAM_PROFILES)
        self.frame_timeout_s = float(frame_timeout_s)      # 只管开流后等首关键帧(「无帧」不是故障)
        self.key_wait_s = float(key_wait_s)                # RESET_VIDEO 之后等关键帧,超时重拉 server
        self.idle_stop_s = float(idle_stop_s)
        self.connect_timeout_s = float(connect_timeout_s)
        self.connect_retry_s = float(connect_retry_s)
        self.clock = clock
        self.channels: dict[str, _Channel] = {}
        self._open_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _rank(spec: str) -> tuple[int, int]:
        return parse_profile(spec)

    async def open(self, account_id: str, *, profile: str) -> ScrcpySession:
        if profile not in self.profiles:
            raise ScrcpyError(f"未知档位 {profile!r}")
        lock = self._open_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            ch = self.channels.get(account_id)
            if ch is None or ch.closed:
                serial, port = self._target_of(account_id)
                ch = _Channel(self, account_id, serial, port, profile)
                await ch.start()
                self.channels[account_id] = ch
            elif self._rank(self.profiles[profile]) > self._rank(self.profiles[ch.profile]):
                if not await ch.restart(notify=True, profile=profile, why="upgrade"):
                    raise ScrcpyError("画面流按新档位重拉失败")
            sub = _Sub()
            ch.attach(sub)
            return ScrcpySession(self, ch, sub)

    async def restart(self, account_id: str) -> dict[str, Any]:
        """#101 / H07:有在跑的画面流 ⇒ 真重建 forward + server、已连 WS 收 ``{type:'restart'}``;
        没人在看 ⇒ 只重建 forward,``stream_restarted:false``(没有 server 可重拉,不假装)。"""
        ch = self.channels.get(account_id)
        if ch is not None and not ch.closed:
            ok = await ch.restart(notify=True, why="api")
            return {"forward_rebuilt": bool(ch.forward_ok and ok), "stream_restarted": bool(ok)}
        serial, port = self._target_of(account_id)
        await self.adb.run(serial, "forward", "--remove", f"tcp:{port}", timeout=10.0)
        rc, _ = await self.adb.run(serial, "forward", f"tcp:{port}", "localabstract:scrcpy", timeout=10.0)
        return {"forward_rebuilt": rc == 0, "stream_restarted": False}

    async def capture_png(self, account_id: str) -> bytes:
        serial, _port = self._target_of(account_id)
        return await screen_adb.screencap_png(self.adb, serial)

    async def inject(self, account_id: str, msg: dict[str, Any]) -> dict[str, Any]:
        serial, _port = self._target_of(account_id)
        return await screen_adb.adb_input(self.adb, serial, msg)

    async def aclose(self) -> None:
        for ch in list(self.channels.values()):
            try:
                await ch.close()
            except Exception as e:
                log.warning("收画面流出错 account=%s: %s", ch.account_id, e)
