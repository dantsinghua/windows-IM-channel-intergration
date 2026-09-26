"""画面流执行体 ``screen_scrcpy.ScrcpyBackend`` 与 ``screen_adb`` 的开发者测试。

全部用假件:假 adb(只记命令、不起真进程)+ 假 scrcpy-server 4.1(``asyncio.start_server``,跑在独立线程的事件循环里,
按 2026-09-26 真机探针的字节与行为造:首连直接关 → 视频 accept 写 dummy → accept 控制 → 写 codec id / 12 字节 session /
33 B 配置包 / 19588 B 关键帧;静止画面不出帧;RESET_VIDEO 回新关键帧;视频被关 ⇒ 关控制并退出)。
**不碰真 docker / adb / 任何设备。**
断言落在协议字节上:首帧 JSON、PTS 与标志位、session 包换尺寸、配置包缓存补发、控制消息的逐字节布局、
新订阅者 / resume 发 RESET_VIDEO、健康判据(静止不重建、socket / 进程断才重建)。

协议依据 = scrcpy **v4.1** 官方源码(随包 ``installer/out/payload/pkg/scrcpy/scrcpy-server`` 的 dex 里
版本串 ``4.1``、有 ``send_stream_meta``、无 ``send_codec_meta``,与下列源码一致):

- ``server/.../device/DesktopConnection.java`` ``open()`` 56-116:``tunnel_forward`` 下
  ``LocalServerSocket("scrcpy")`` 依次 ``accept`` video → audio → control(关掉的跳过),dummy 字节在**第一条
  accept 后立即**写;三条都 accept 完 ``open()`` 才返回。
- ``server/.../Server.java`` 105:先 ``DesktopConnection.open(...)``,之后才建 ``Streamer`` / 起编码器。
- ``server/.../video/SurfaceEncoder.java`` 102 ``streamer.writeVideoHeader()``;146 每次编码会话开始
  ``streamer.writeSessionMeta(w, h, isClientResize)``(分辨率变化 / 重配会再来一次)。
- ``server/.../device/Streamer.java``:
  17-19 ``PACKET_FLAG_SESSION = 1L << 63``、``PACKET_FLAG_CONFIG = 1L << 62``、``PACKET_FLAG_KEY_FRAME = 1L << 61``;
  48-55 ``writeVideoHeader``:只写 4 字节 ``codec.getId()``(需 ``send_stream_meta``);
  57-66 ``writeDisableStream``:codec id 位置写 0(流被关)/ 1(配置错误);
  91-105 ``writeSessionMeta``:12 字节 ``int flags = (int)(PACKET_FLAG_SESSION >> 32) | (clientResize ? 1 : 0)``
  + ``int width`` + ``int height``,**没有数据段**;
  107-124 ``writeFrameMeta``:``long ptsAndFlags``(配置包 = ``PACKET_FLAG_CONFIG``,否则 pts | KEY_FRAME)+ ``int size``。
- ``server/.../Options.java`` 559-561 认 ``send_stream_meta``;571-572 不认识的键只 ``Ln.w("Unknown server option")``。
- ``app/src/demuxer.c`` 14-17、127-137:客户端同样按 MSB 判 session 包,``width = read32be(&header[4])``、
  ``height = read32be(&header[8])``;230-254 头之后第一包必须是 session 包。
- ``server/.../control/ControlMessageReader.java`` 121-128 ``parseInjectScrollEvent``:
  ``Binary.i16FixedPointToFloat(readShort()) * 16``(注释:实际范围 [-16, 16]);``util/Binary.java`` 34-37
  ``i16FixedPointToFloat(v) = v == 0x7fff ? 1f : v / 0x1p15f``;``app/src/control_msg.c`` 130-137 客户端先 ``/ 16``
  再 ``sc_float_to_i16fp``(``app/src/util/binary.h`` 84-93:``(int32_t)(f * 0x1p15f)`` 向零截断,0x8000 钳成 0x7fff)。
- ``ControlMessageReader`` 72-78 keycode 14 B、106-109 text ``>BI``+UTF-8(``INJECT_TEXT_MAX_LENGTH = 300``)、
  111-119 touch 32 B;``control/ControlMessage.java`` 10-13 类型号 0/1/2/3。
"""
from __future__ import annotations

import asyncio
import json
import shlex
import struct
import threading
import time
from typing import Any, Optional

import pytest

from qtrade_agent import screen_adb
from qtrade_agent.screen_scrcpy import (
    CODEC_H264, FLAG_CONFIG, FLAG_KEY, FLAG_SESSION, REMOTE_JAR, RESET_VIDEO, SERVER_CLASS, ScrcpyBackend,
    ScrcpyError, control_bytes, encode_text, parse_session, profile_params, read_packet, server_args,
    store_target_resolver,
)

JAR = "/opt/qtrade/scrcpy/scrcpy-server"
SERIAL = "127.0.0.1:16001"
SPS_PPS = b"\x00\x00\x00\x01\x67SPS\x00\x00\x00\x01\x68PPS"


# ══════════════════════════════════════════════════ 假件
#: 2026-09-26 真机探针(redroid 11 + 随包 4.1 server)抓到的字节:session 包就是这 12 字节,**没有载荷**
REAL_SESSION = bytes.fromhex("80000000" "000002d0" "00000500")                 # 720×1280
REAL_CONFIG = b"\x00\x00\x00\x01\x67" + bytes(range(1, 21)) + b"\x00\x00\x00\x01\x68" + b"\xce\x3c\x80"   # 33 B
REAL_KEY = b"\x00\x00\x00\x01\x65" + bytes(19588 - 5)                           # 19588 B
REAL_KEY_PTS_US = 270_000                                                       # 首关键帧 0.27 s


def pkt(pts_flags: int, data: bytes) -> bytes:
    """4.1 ``writeFrameMeta``:8 字节 ``pts_flags`` + 4 字节长度 + 数据。"""
    return struct.pack(">QI", pts_flags, len(data)) + data


def session(width: int, height: int, client_resize: bool = False) -> bytes:
    """4.1 ``writeSessionMeta``:``int flags(bit31=1, bit0=client resize) + int w + int h``,无数据段。"""
    return struct.pack(">III", 0x80000000 | (1 if client_resize else 0), width, height)


def real_script() -> list[bytes]:
    """真机首段:配置包(``4000000000000000 00000021`` + 33 B)→ 关键帧(19588 B)。"""
    return [pkt(FLAG_CONFIG, REAL_CONFIG), pkt(FLAG_KEY | REAL_KEY_PTS_US, REAL_KEY)]


# 控制消息长度(4.1 ControlMessageReader):keycode 14、text 5+n、touch 32、scroll 21、RESET_VIDEO 1
_CTRL_FIXED = {0: 14, 2: 32, 3: 21, 17: 1}


class FakeScrcpyServer:
    """假 scrcpy-server 4.1,按真机行为造(``tunnel_forward=true video=true audio=false control=true``)。

    - 每次 server 会话的**第一条**连接直接关(真机:adb forward 先接住 TCP、设备端还没 listen ⇒ 读 dummy 得 EOF),
      第二条才是视频:写 dummy,等控制 accept 完才写 codec id + session 12 B + ``script``;
    - 控制 socket 按消息边界解析;收到 RESET_VIDEO(17)且 ``honor_reset`` ⇒ 在视频上补 session + 配置包 + 新关键帧;
    - Agent 关视频 socket ⇒ **关控制 socket 并「退出」**(真机 N1:视频一断 server rc=0 退出,控制跟着断)。
    静止画面:``script`` 发完就一帧不出,除非测试 ``push``。
    """

    def __init__(self, width: int = 720, height: int = 1280, codec: int = CODEC_H264) -> None:
        self.width, self.height, self.codec = width, height, codec
        self.script: list[bytes] = real_script()      # 每次会话出完头就发的包
        self.honor_reset = True
        self.conns = 0
        self.probes = 0                               # 被直接关掉的「首连」次数
        self.ctrl = bytearray()                       # 控制 socket 收到的全部字节
        self.msgs: list[int] = []                     # 按边界解析出的控制消息类型
        self.resets = 0
        self.video_eof = 0                            # 视频连接被 Agent 关掉的次数
        self.exits = 0                                # 「server 退出」次数(视频断 ⇒ 退出)
        self.exit_cb: Optional[Any] = None
        self.events: list[str] = []
        self._reset_pts = 5_000_000
        self._expect_video = True
        self._probe_pending = True
        self._cur: dict[str, Any] = {}
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.server = self.loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
        self.port = self.server.sockets[0].getsockname()[1]
        self._ready.set()
        self.loop.run_forever()

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.conns += 1
        if self._expect_video and self._probe_pending:
            self._probe_pending = False
            self.probes += 1
            self.events.append("probe_closed")
            w.close()
            return
        if self._expect_video:
            await self._video(r, w)
        else:
            await self._control(r, w)

    async def _video(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self._expect_video = False
        sess: dict[str, Any] = {"vw": w, "cw": None, "ctrl_up": asyncio.Event()}
        self._cur = sess
        self.events.append("accept_video")
        w.write(b"\x00")                               # DesktopConnection.open:accept 视频后立刻写 dummy
        await w.drain()
        waiter = asyncio.ensure_future(sess["ctrl_up"].wait())
        eof = asyncio.ensure_future(r.read())
        await asyncio.wait({waiter, eof}, return_when=asyncio.FIRST_COMPLETED)
        if waiter.done():                              # open() 返回 ⇒ 编码器起来 ⇒ 头 + session + 帧
            self.events.append("header")
            w.write(struct.pack(">I", self.codec) + session(self.width, self.height) + b"".join(self.script))
            await w.drain()
        else:
            waiter.cancel()
        await eof                                      # 等 Agent 关视频 socket
        self.video_eof += 1
        # 真机 N1:视频 socket 断 ⇒ server 退出,控制 socket 被服务端关
        if sess["cw"] is not None:
            sess["cw"].close()
        self.exits += 1
        if self.exit_cb is not None:
            self.exit_cb()
        w.close()

    async def _control(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self._expect_video = True
        self._probe_pending = True                     # 下一次 server 会话的首连又会被关
        sess = self._cur
        sess["cw"] = w
        self.events.append("accept_control")
        sess["ctrl_up"].set()
        buf = bytearray()
        while True:
            try:
                b = await r.read(4096)
            except (ConnectionError, OSError):
                break
            if not b:
                break
            self.ctrl += b
            buf += b
            self._parse(buf, sess)
        w.close()

    def _parse(self, buf: bytearray, sess: dict[str, Any]) -> None:
        while buf:
            t = buf[0]
            if t == 1:
                if len(buf) < 5:
                    return
                n = 5 + struct.unpack(">I", bytes(buf[1:5]))[0]
            else:
                n = _CTRL_FIXED.get(t, len(buf))
            if len(buf) < n:
                return
            del buf[:n]
            self.msgs.append(t)
            if t == 17:
                self.resets += 1
                if self.honor_reset:                   # Controller.resetVideo ⇒ 编码器重开:session + 配置包 + 关键帧
                    self._reset_pts += 1_000_000
                    sess["vw"].write(session(self.width, self.height) + pkt(FLAG_CONFIG, REAL_CONFIG)
                                     + pkt(FLAG_KEY | self._reset_pts, b"\x00\x00\x00\x01\x65RESET"))

    def _call(self, coro_fn) -> Any:
        async def go():
            return await coro_fn()
        return asyncio.run_coroutine_threadsafe(go(), self.loop).result(5)

    def push(self, *packets: bytes) -> None:
        async def go():
            vw = self._cur["vw"]
            vw.write(b"".join(packets))
            await vw.drain()
        self._call(go)

    def drop_video(self) -> None:
        """服务端关视频 socket(scrcpy-server 崩了 / 被杀)。"""
        async def go():
            self._cur["vw"].close()
        self._call(go)

    def drop_control(self) -> None:
        """服务端只关控制 socket(视频还开着)。"""
        async def go():
            self._cur["cw"].close()
        self._call(go)

    def close(self) -> None:
        async def go():
            self.server.close()
            me = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks() if t is not me]
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._call(go)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)


class FakeProc:
    """假 ``adb shell app_process``:不自己退出;``terminate`` / ``kill`` 或 ``exit()`` 才结束。"""

    def __init__(self) -> None:
        self.returncode: Optional[int] = None
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_eof()
        self._done = asyncio.Event()

    def terminate(self) -> None:
        self.returncode = 0
        self._done.set()

    kill = terminate
    exit = terminate

    async def wait(self) -> int:
        await self._done.wait()
        return 0


class FakeAdbRunner:
    """只记 argv;``wm size`` / ``screencap`` 回预置值,其余一律 rc=0。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.spawned: list[tuple[str, ...]] = []
        self.order: list[str] = []                    # run / spawn 的统一先后(push → forward → spawn)
        self.procs: list[FakeProc] = []
        self.png = b"\x89PNG\r\n\x1a\nREAL"
        self.wm = b"Physical size: 720x1280\n"
        self.fail: set[str] = set()

    async def run(self, serial: str, *args: str, timeout: float = 15.0) -> tuple[int, bytes]:
        self.calls.append((serial, *args))
        self.order.append(args[0] if args else "")
        if args and args[0] in self.fail:
            return 1, b"boom"
        if args[:1] == ("exec-out",):
            return 0, self.png
        if args[:2] == ("shell", "wm size"):
            return 0, self.wm
        return 0, b""

    async def spawn(self, serial: str, *args: str) -> FakeProc:
        self.spawned.append((serial, *args))
        self.order.append("spawn")
        p = FakeProc()
        self.procs.append(p)
        return p

    def cmds(self, first: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if len(c) > 1 and c[1] == first]


@pytest.fixture
def srv():
    s = FakeScrcpyServer()
    yield s
    s.close()


def make_backend(srv: FakeScrcpyServer, adb: FakeAdbRunner, **kw: Any) -> ScrcpyBackend:
    kw.setdefault("idle_stop_s", 0.05)
    kw.setdefault("connect_timeout_s", 3.0)
    return ScrcpyBackend(lambda aid: (SERIAL, srv.port), adb=adb, server_jar=JAR, **kw)


async def next_item(session, timeout: float = 3.0):
    agen = session._agen if hasattr(session, "_agen") else None
    if agen is None:
        agen = session.frames().__aiter__()
        session._agen = agen
    return await asyncio.wait_for(agen.__anext__(), timeout)


async def wait_for(cond, timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等待条件超时")


# ══════════════════════════════════════════════════ 控制消息字节(纯函数)
def test_touch_down_move_up_bytes():
    w, h = 540, 960
    down = control_bytes({"type": "touch", "action": "down", "x": 0.5, "y": 0.25, "pointer": 1}, w, h)
    assert down == [bytes([2, 0]) + (1).to_bytes(8, "big") + (270).to_bytes(4, "big") + (240).to_bytes(4, "big")
                    + (540).to_bytes(2, "big") + (960).to_bytes(2, "big") + b"\xff\xff"
                    + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")]
    assert len(down[0]) == 32
    move = control_bytes({"type": "touch", "action": "move", "x": 0.6, "y": 0.25, "pointer": 1}, w, h)[0]
    assert move[1] == 2 and struct.unpack(">i", move[10:14])[0] == 324 and move[22:24] == b"\xff\xff"
    up = control_bytes({"type": "touch", "action": "up", "x": 0.6, "y": 0.25, "pointer": 1}, w, h)[0]
    # up:action=1、pressure=0、actionButton=1、buttons=0
    assert up[1] == 1 and up[22:24] == b"\x00\x00" and up[24:28] == (1).to_bytes(4, "big") and up[28:32] == bytes(4)


def test_touch_rejects_unknown_action_and_clamps_coords():
    assert control_bytes({"type": "touch", "action": "tap", "x": 0.1, "y": 0.1}, 540, 960) == []
    b = control_bytes({"type": "touch", "action": "down", "x": 1.0, "y": 0.0}, 540, 960)[0]
    assert struct.unpack(">ii", b[10:18]) == (539, 0)            # 右边界钳到 w-1


def _server_scroll(v: int) -> float:
    """4.1 服务端解码:``Binary.i16FixedPointToFloat(v) * 16``。"""
    return (1.0 if v == 0x7FFF else v / 0x8000) * 16


def test_scroll_bytes():
    """wheel 一格(100 px)= scrcpy 滚动量 1.0;编码前先 /16(4.1 服务端再 ×16),设备上正好滚 1 格。"""
    b = control_bytes({"type": "scroll", "x": 0.5, "y": 0.5, "dx": 0, "dy": 100}, 540, 960)
    assert b == [bytes([3]) + (270).to_bytes(4, "big") + (480).to_bytes(4, "big") + (540).to_bytes(2, "big")
                 + (960).to_bytes(2, "big") + (0).to_bytes(2, "big", signed=True)
                 + (-0x0800).to_bytes(2, "big", signed=True) + bytes(4)]
    assert len(b[0]) == 21
    assert _server_scroll(-0x0800) == -1.0                        # 往下一格(Android vscroll 取反)
    up = control_bytes({"type": "scroll", "x": 0.5, "y": 0.5, "dx": -50, "dy": -300}, 540, 960)[0]
    h, v = struct.unpack(">hh", up[13:17])
    assert (h, v) == (0x0400, 0x1800) and (_server_scroll(h), _server_scroll(v)) == (0.5, 3.0)
    big = control_bytes({"type": "scroll", "x": 0.5, "y": 0.5, "dx": 5000, "dy": -5000}, 540, 960)[0]
    assert struct.unpack(">hh", big[13:17]) == (-0x8000, 0x7FFF)  # 超过 ±16 格钳到定点满值


def test_console_key_names_map_to_keycodes():
    """控制台发 Android 键名串、每次按键 down + up 两帧 ⇒ 各一条 INJECT_KEYCODE。"""
    want = {"ENTER": 66, "BACK": 4, "HOME": 3, "DEL": 67, "FORWARD_DEL": 112, "TAB": 61, "ESCAPE": 111,
            "DPAD_UP": 19, "DPAD_DOWN": 20, "DPAD_LEFT": 21, "DPAD_RIGHT": 22, "SPACE": 62, "MENU": 82}
    for name, code in want.items():
        down = control_bytes({"type": "key", "keycode": name, "action": "down"}, 1, 1)
        up = control_bytes({"type": "key", "keycode": name, "action": "up"}, 1, 1)
        assert down == [struct.pack(">BBIII", 0, 0, code, 0, 0)] and up == [struct.pack(">BBIII", 0, 1, code, 0, 0)]


def test_key_bytes_and_names():
    assert control_bytes({"type": "key", "keycode": "BACK", "action": "down"}, 1, 1) == \
        [bytes([0, 0]) + (4).to_bytes(4, "big") + bytes(8)]
    assert control_bytes({"type": "key", "keycode": 66, "action": "up"}, 1, 1) == \
        [bytes([0, 1]) + (66).to_bytes(4, "big") + bytes(8)]
    assert control_bytes({"type": "key", "keycode": "KEYCODE_HOME", "action": "down"}, 1, 1)[0][2:6] == (3).to_bytes(4, "big")
    assert control_bytes({"type": "key", "keycode": "ROTATE", "action": "down"}, 1, 1) == []   # 不认识不落成返回键
    assert control_bytes({"type": "key", "keycode": 4, "action": "long"}, 1, 1) == []


def test_text_bytes_and_chunking():
    raw = "a;$(reboot)`x`|&>"
    assert control_bytes({"type": "text", "text": raw}, 1, 1) == [bytes([1]) + len(raw).to_bytes(4, "big") + raw.encode()]
    parts = encode_text("中" * 101)                               # 303 字节 ⇒ 300 + 3,按字符边界切
    assert [struct.unpack(">I", p[1:5])[0] for p in parts] == [300, 3]
    assert b"".join(p[5:] for p in parts).decode("utf-8") == "中" * 101
    assert control_bytes({"type": "text", "text": ""}, 1, 1) == []


def test_profile_params_follow_stream_profiles():
    assert profile_params("540p@5") == {"max_size": 960, "max_fps": 5, "video_bit_rate": 800_000}
    p = profile_params("720p@30")
    assert p["max_size"] == 1280 and p["max_fps"] == 30 and p["video_bit_rate"] > 800_000


# ══════════════════════════════════════════════════ 拉起 / 首帧 / 帧包
async def test_open_runs_push_forward_server_and_meta(srv):
    adb = FakeAdbRunner()
    be = make_backend(srv, adb)
    s = await be.open("qd01", profile="thumb")
    try:
        assert s.meta == {"codec": "h264", "width": 720, "height": 1280, "profile": "thumb", "fps": 5, "seq0": 0}
        assert adb.calls[0] == (SERIAL, "push", JAR, REMOTE_JAR)
        assert adb.calls[1] == (SERIAL, "forward", f"tcp:{srv.port}", "localabstract:scrcpy")
        assert adb.order == ["push", "forward", "spawn"]          # 缺口 G4:spawn 必须在 forward 之后
        argv = adb.spawned[0]
        assert argv[:6] == (SERIAL, "shell", f"CLASSPATH={REMOTE_JAR}", "app_process", "/", SERVER_CLASS)
        opts = argv[6:]
        assert opts[0] == "4.1"
        for want in ("tunnel_forward=true", "video=true", "audio=false", "control=true", "video_codec=h264",
                     "max_size=960", "max_fps=5", "send_frame_meta=true", "send_stream_meta=true",
                     "send_device_meta=false", "send_dummy_byte=true"):
            assert want in opts
        assert not any(o.startswith("send_codec_meta") for o in opts)   # 4.1 不认旧名,只会 warn
        assert not any(c[1:3] == ("shell", "am") or "am start" in " ".join(c) for c in adb.calls)   # 开流不拉企点
        await wait_for(lambda: srv.conns == 3)                     # 首连被关 + 视频 + 控制
        # 真机:首连读 dummy 得 EOF ⇒ 重连;4.1 控制连上后才出头
        assert srv.events == ["probe_closed", "accept_video", "accept_control", "header"]
    finally:
        await s.close()
        await be.aclose()


async def test_open_rejects_non_h264(srv):
    srv.codec = 0x68323635                                        # "h265"
    be = make_backend(srv, FakeAdbRunner())
    with pytest.raises(ScrcpyError):
        await be.open("qd01", profile="thumb")
    assert be.channels == {}


async def test_open_fails_when_server_disables_stream(srv):
    """4.1 ``writeDisableStream``:codec id 位置写 1 = server 配置出错 ⇒ 如实报错、不留通道。"""
    srv.codec = 1
    be = make_backend(srv, FakeAdbRunner())
    with pytest.raises(ScrcpyError, match="关了视频流"):
        await be.open("qd01", profile="thumb")
    assert be.channels == {}


def test_flag_bits_match_scrcpy_41():
    assert (FLAG_SESSION, FLAG_CONFIG, FLAG_KEY) == (1 << 63, 1 << 62, 1 << 61)
    assert RESET_VIDEO == b"\x11"                                 # ControlMessage.TYPE_RESET_VIDEO = 17,无额外字段


async def test_fake_server_first_connect_eof_then_header_only_after_control(srv):
    """假 server 自检(照真机):首连直接关;第二条读到 dummy;只连视频不连控制 ⇒ 等不到头;连上控制才出
    codec id + 12 字节 session(与真机字节逐字相同)。"""
    r0, w0 = await asyncio.open_connection("127.0.0.1", srv.port)
    assert await asyncio.wait_for(r0.read(1), 2) == b""            # 首连:EOF
    w0.close()
    r, w = await asyncio.open_connection("127.0.0.1", srv.port)
    try:
        assert await asyncio.wait_for(r.readexactly(1), 2) == b"\x00"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(r.readexactly(4), 0.3)
        r2, w2 = await asyncio.open_connection("127.0.0.1", srv.port)
        hdr = await asyncio.wait_for(r.readexactly(16), 2)
        assert struct.unpack(">I", hdr[:4])[0] == CODEC_H264 and hdr[4:] == REAL_SESSION
        w2.close()
    finally:
        w.close()


async def test_fake_server_video_close_closes_control_like_real_device(srv):
    """假 server 自检(真机 N1):Agent 关视频 socket ⇒ server 退出、控制 socket 被服务端关。"""
    r0, w0 = await asyncio.open_connection("127.0.0.1", srv.port)
    await asyncio.wait_for(r0.read(1), 2)
    w0.close()
    r, w = await asyncio.open_connection("127.0.0.1", srv.port)
    await asyncio.wait_for(r.readexactly(1), 2)
    cr, cw = await asyncio.open_connection("127.0.0.1", srv.port)
    await asyncio.wait_for(r.readexactly(16), 2)
    w.close()
    assert await asyncio.wait_for(cr.read(1), 2) == b""            # 控制被关
    await wait_for(lambda: srv.exits == 1)
    cw.close()


def test_real_device_session_is_12_bytes_without_payload():
    """编号 1:真机字节 ``80000000 000002d0 00000500`` 自身就是 session 包(宽 = pts_flags 低 32 位、高 = size 字段),
    紧跟的就是配置包头 ``4000000000000000 00000021``——读 session 不能再吞 12 字节载荷。"""
    assert parse_session(REAL_SESSION) == (720, 1280)
    pts_flags, size = struct.unpack(">QI", REAL_SESSION)
    assert pts_flags & FLAG_SESSION and (pts_flags & 0xFFFFFFFF, size) == (720, 1280)
    stream = REAL_SESSION + b"".join(real_script()) + pkt(300_000, b"\x00\x00\x00\x01\x41D")
    assert stream[12:24] == bytes.fromhex("4000000000000000" "00000021")

    async def go():
        rd = asyncio.StreamReader()
        rd.feed_data(stream)
        rd.feed_eof()
        f0, d0 = await read_packet(rd)
        assert f0 & FLAG_SESSION and d0 == REAL_SESSION           # session:返回的就是这 12 字节
        f1, d1 = await read_packet(rd)
        assert f1 == FLAG_CONFIG and d1 == REAL_CONFIG and len(d1) == 33
        f2, d2 = await read_packet(rd)
        assert f2 == FLAG_KEY | REAL_KEY_PTS_US and len(d2) == 19588
        f3, d3 = await read_packet(rd)
        assert (f3, d3) == (300_000, b"\x00\x00\x00\x01\x41D")
    asyncio.run(go())


async def test_real_device_byte_sequence_end_to_end(srv):
    """真机字节序列经 Agent:首帧 JSON 宽高取自 12 字节 session;首个二进制帧 = 33 B 配置包 + 19588 B 关键帧。"""
    be = make_backend(srv, FakeAdbRunner())
    s = await be.open("qd01", profile="focus")
    try:
        assert (s.meta["width"], s.meta["height"]) == (720, 1280)
        assert await next_item(s) == (270, REAL_CONFIG + REAL_KEY)
        srv.push(pkt(310_000, b"\x00\x00\x00\x01\x41D1"))
        assert await next_item(s) == (310, b"\x00\x00\x00\x01\x41D1")
    finally:
        await s.close()
        await be.aclose()


async def test_session_packet_midstream_resends_meta_with_new_size(srv):
    """分辨率变化:4.1 再发 session 包 + 新配置包 + 关键帧 ⇒ 已连 WS 先收新首帧 JSON,再从新关键帧开始。"""
    srv.script = [pkt(FLAG_CONFIG, SPS_PPS), pkt(FLAG_KEY | 1_000_000, b"K1")]
    be = make_backend(srv, FakeAdbRunner())
    s = await be.open("qd01", profile="focus")
    try:
        assert s.meta["width"] == 720 and s.meta["height"] == 1280
        assert await next_item(s) == (1000, SPS_PPS + b"K1")
        new_cfg = b"\x00\x00\x00\x01\x67NEW"
        srv.push(session(1280, 720), pkt(2_000_000, b"P-old-session"), pkt(FLAG_CONFIG, new_cfg),
                 pkt(FLAG_KEY | 2_100_000, b"K2"))
        meta = await next_item(s)
        assert meta == {"codec": "h264", "width": 1280, "height": 720, "profile": "focus", "fps": 30, "seq0": 0}
        assert await next_item(s) == (2100, new_cfg + b"K2")          # 换尺寸后的 P 帧不送、旧 SPS 不拼
        await s.control({"type": "touch", "action": "down", "x": 1.0, "y": 1.0, "pointer": 0})
        await wait_for(lambda: len(srv.ctrl) == 32)
        assert struct.unpack(">iiHH", bytes(srv.ctrl[10:22])) == (1279, 719, 1280, 720)   # 坐标按新尺寸
        srv.push(session(1280, 720), pkt(FLAG_CONFIG, new_cfg), pkt(FLAG_KEY | 3_000_000, b"K3"))
        assert await next_item(s) == (3000, new_cfg + b"K3")          # 尺寸没变的 session 不重发首帧
    finally:
        await s.close()
        await be.aclose()


async def test_packets_pts_flags_and_config_prepended_to_keyframe(srv):
    srv.script = [pkt(FLAG_CONFIG, SPS_PPS),
                  pkt(1_000_000, b"\x00\x00\x00\x01\x41P-before-key"),        # 关键帧之前的 P 帧:新连接不送
                  pkt(FLAG_KEY | 2_500_999, b"\x00\x00\x00\x01\x65IDR"),
                  pkt(3_000_000, b"\x00\x00\x00\x01\x41P1")]
    be = make_backend(srv, FakeAdbRunner())
    s = await be.open("qd01", profile="thumb")
    try:
        assert await next_item(s) == (2500, SPS_PPS + b"\x00\x00\x00\x01\x65IDR")   # 微秒 → 毫秒,标志位清掉
        assert await next_item(s) == (3000, b"\x00\x00\x00\x01\x41P1")               # 去掉 4 字节包长
    finally:
        await s.close()
        await be.aclose()


# ══════════════════════════════════════════════════ 新观看者 / 关键帧(编号 3)
async def test_new_subscriber_sends_reset_video_and_only_gets_frames_after_keyframe(srv):
    """真机只有开头一个关键帧 ⇒ 第二个订阅者接入立刻发 RESET_VIDEO;它在新关键帧之前的 delta 一律不收。"""
    srv.script = [pkt(FLAG_CONFIG, SPS_PPS), pkt(FLAG_KEY | 1_000_000, b"K1")]
    srv.honor_reset = False                                       # 先不回,好观察「关键帧前不收」
    adb = FakeAdbRunner()
    be = make_backend(srv, adb)
    s1 = await be.open("qd01", profile="thumb")
    try:
        assert await next_item(s1) == (1000, SPS_PPS + b"K1")
        assert srv.resets == 0                                    # 首个订阅者:新 server 自带首关键帧,不用要
        s2 = await be.open("qd01", profile="thumb")
        assert len(adb.spawned) == 1                             # 同账号共用一个 server
        await wait_for(lambda: srv.resets == 1)
        assert bytes(srv.ctrl) == b"\x11"                         # RESET_VIDEO 就 1 字节
        srv.push(pkt(1_100_000, b"P2"))
        assert await next_item(s1) == (1100, b"P2")
        assert s2._sub.queue.qsize() == 0                          # 关键帧前的 delta 不给新订阅者
        # server 重置编码器:session(同尺寸)+ 新配置包 + 关键帧
        cfg2 = b"\x00\x00\x00\x01\x67CFG2"
        srv.push(session(720, 1280), pkt(FLAG_CONFIG, cfg2), pkt(FLAG_KEY | 1_200_000, b"K2"),
                 pkt(1_300_000, b"P3"))
        assert await next_item(s2) == (1200, cfg2 + b"K2")        # 新订阅者首个二进制帧 = 配置包 + 关键帧
        assert await next_item(s2) == (1300, b"P3")
        assert await next_item(s1) == (1200, cfg2 + b"K2")        # 老订阅者照常收(每个 IDR 都带 SPS/PPS)
        assert srv.resets == 1 and len(adb.spawned) == 1
        await s2.close()
    finally:
        await s1.close()
        await be.aclose()


async def test_new_subscriber_gets_reset_keyframe_from_server(srv):
    """server 如实响应 RESET_VIDEO(假 server 照 4.1 Controller.resetVideo 补 session + 配置包 + 关键帧)。"""
    be = make_backend(srv, FakeAdbRunner())
    s1 = await be.open("qd01", profile="thumb")
    try:
        assert await next_item(s1) == (270, REAL_CONFIG + REAL_KEY)
        s2 = await be.open("qd01", profile="thumb")
        assert await next_item(s2) == (6000, REAL_CONFIG + b"\x00\x00\x00\x01\x65RESET")
        assert srv.msgs == [17]
        await s2.close()
    finally:
        await s1.close()
        await be.aclose()


async def test_reset_video_without_keyframe_falls_back_to_server_restart(srv):
    """RESET_VIDEO 之后 ``key_wait_s`` 内没关键帧 ⇒ 重拉 server(重推 jar);订阅者都留着,收新首帧 JSON + 新关键帧。"""
    srv.honor_reset = False
    adb = FakeAdbRunner()
    be = make_backend(srv, adb, key_wait_s=0.3, idle_stop_s=5)
    s1 = await be.open("qd01", profile="thumb")
    try:
        assert await next_item(s1) == (270, REAL_CONFIG + REAL_KEY)
        s2 = await be.open("qd01", profile="thumb")
        await wait_for(lambda: len(adb.spawned) == 2)
        assert len(adb.cmds("push")) == 2
        for s in (s2, s1):
            meta = await next_item(s)
            assert isinstance(meta, dict) and meta["codec"] == "h264" and meta["width"] == 720
            assert await next_item(s) == (270, REAL_CONFIG + REAL_KEY)
        await s2.close()
    finally:
        await s1.close()
        await be.aclose()


async def test_slow_subscriber_resync_requests_reset_video(srv, monkeypatch):
    """慢客户端队列满 ⇒ 丢到关键帧重对齐;真机没有周期关键帧 ⇒ 主动发 RESET_VIDEO。"""
    from qtrade_agent import screen_scrcpy
    monkeypatch.setattr(screen_scrcpy._Sub, "QUEUE_MAX", 2)
    srv.honor_reset = False
    be = make_backend(srv, FakeAdbRunner())
    s = await be.open("qd01", profile="thumb")
    try:
        await wait_for(lambda: s._sub.queue.qsize() == 1)         # 首关键帧(不取,模拟慢)
        srv.push(pkt(1_000_000, b"D1"), pkt(1_100_000, b"D2"), pkt(1_200_000, b"D3"))
        await wait_for(lambda: srv.resets == 1)
        assert s._sub.waiting_key
    finally:
        await s.close()
        await be.aclose()


# ══════════════════════════════════════════════════ RESET 节流 / 掉队(第三轮验收 B1)
def test_sub_queue_hard_cap_counts_keyframes_and_clears_backlog(monkeypatch):
    """队列硬上限含关键帧:满了先清积压(JSON 留着),关键帧以自己为新起点,delta 丢到下一个关键帧。"""
    from qtrade_agent import screen_scrcpy
    monkeypatch.setattr(screen_scrcpy._Sub, "QUEUE_MAX", 3)
    sub = screen_scrcpy._Sub()
    meta = {"codec": "h264", "width": 720}
    sub.renew(meta)                                               # 1(JSON)
    assert sub.offer(1, b"K1", True) is False                    # 2
    assert sub.offer(2, b"D1", False) is False                   # 3 = 满
    assert sub.offer(3, b"D2", False) is True                    # 掉队:清积压,等关键帧
    assert sub.waiting_key and sub.queue.qsize() == 1 and sub.queue.get_nowait() == meta
    assert sub.offer(4, b"D3", False) is False and sub.queue.qsize() == 0
    for i in range(3):
        sub.offer(10 + i, b"K" if i == 0 else b"D", i == 0)
    assert sub.queue.qsize() == 3
    assert sub.offer(20, b"K2", True) is True                    # 满了来关键帧:不突破上限,替换全部旧积压
    assert sub.queue.qsize() == 1 and sub.queue.get_nowait() == (20, b"K2") and not sub.waiting_key


async def test_slow_subscriber_reset_count_and_queue_are_bounded(srv, monkeypatch):
    """B1 复现口径:QUEUE_MAX=5,快订阅者正常取、慢订阅者一帧不取,30 fps 连推 2 s delta。
    改前 RESET 56 次、慢订阅者队列涨到 61;改后 RESET ≤ 2(缺省最小间隔 2 s:首次立即 + 至多一次到点),
    慢订阅者队列任何时刻 ≤ 5,快订阅者 60 个 delta 一个不少。"""
    from qtrade_agent import screen_scrcpy
    monkeypatch.setattr(screen_scrcpy._Sub, "QUEUE_MAX", 5)
    be = make_backend(srv, FakeAdbRunner(), lag_evict_count=1000)   # 只看节流与上限,不让它被摘
    fast = await be.open("qd01", profile="focus")
    got: list[Any] = []

    async def consume() -> None:
        while True:
            got.append(await next_item(fast, timeout=10))

    reader = asyncio.create_task(consume())
    await wait_for(lambda: len(got) == 1)                         # 首关键帧到了,再接入慢订阅者
    slow = await be.open("qd01", profile="focus")
    try:
        await wait_for(lambda: srv.resets == 1)                   # slow 接入:首次 RESET 不延迟
        await wait_for(lambda: slow._sub.queue.qsize() == 1)      # slow 拿到 RESET 关键帧后就不取了
        base = srv.resets
        peak = 0
        for i in range(60):
            await asyncio.to_thread(srv.push, pkt(10_000_000 + i * 33_333, b"D%02d" % i))
            await asyncio.sleep(1 / 30)
            peak = max(peak, slow._sub.queue.qsize())
        await asyncio.sleep(0.2)
        peak = max(peak, slow._sub.queue.qsize())
        assert srv.resets - base <= 2, srv.resets
        assert peak <= 5
        deltas = [x[1] for x in got if isinstance(x, tuple) and x[1].startswith(b"D")]
        assert deltas == [b"D%02d" % i for i in range(60)]        # 快订阅者不受慢订阅者牵连
    finally:
        reader.cancel()
        await slow.close()
        await fast.close()
        await be.aclose()


async def test_consecutive_lag_evicts_subscriber_with_restart(srv, monkeypatch):
    """10 s 内掉队 ≥ 3 次 ⇒ 摘下该订阅者、给它发 {type:'restart'}(客户端重连);其它观看者照常、server 不重拉。"""
    from qtrade_agent import screen_scrcpy
    monkeypatch.setattr(screen_scrcpy._Sub, "QUEUE_MAX", 3)
    adb = FakeAdbRunner()
    be = make_backend(srv, adb, reset_min_interval_s=0.2)
    fast = await be.open("qd01", profile="thumb")
    slow = await be.open("qd01", profile="thumb")
    ch = be.channels["qd01"]
    got: list[Any] = []

    async def consume() -> None:
        while True:
            got.append(await next_item(fast, timeout=10))

    reader = asyncio.create_task(consume())
    try:
        i = 0
        while slow._sub in ch.subs and i < 300:
            await asyncio.to_thread(srv.push, pkt(10_000_000 + i * 33_333, b"D%03d" % i))
            await asyncio.sleep(1 / 30)
            i += 1
        assert slow._sub not in ch.subs and slow._sub.detached
        assert len(slow._sub.lags) == 3
        items = []
        while not slow._sub.queue.empty():
            items.append(slow._sub.queue.get_nowait())
        assert items[-2:] == [{"type": "restart"}, None]
        assert all(not isinstance(x, tuple) or x[1] != b"" for x in items)
        n = len(got)
        await asyncio.to_thread(srv.push, pkt(99_000_000, b"AFTER"))
        await wait_for(lambda: any(isinstance(x, tuple) and x[1] == b"AFTER" for x in got[n:]))
        assert fast._sub in ch.subs and len(adb.spawned) == 1   # 其它观看者照常,server 没重拉
    finally:
        reader.cancel()
        await slow.close()
        await fast.close()
        await be.aclose()


async def test_reset_throttle_does_not_delay_first_attach_and_merges_later_ones(srv):
    """节流不影响首次接入:本 server 第一次 RESET 立即发;间隔内再来的接入 / resume 合并成一次、到点再发,
    且都从那个关键帧开始收。"""
    be = make_backend(srv, FakeAdbRunner(), reset_min_interval_s=0.8)
    s1 = await be.open("qd01", profile="thumb")
    try:
        assert await next_item(s1) == (270, REAL_CONFIG + REAL_KEY)          # 首个订阅者:不发 RESET
        t0 = time.monotonic()
        s2 = await be.open("qd01", profile="thumb")
        assert await next_item(s2, timeout=0.5) == (6000, REAL_CONFIG + b"\x00\x00\x00\x01\x65RESET")
        assert srv.resets == 1 and time.monotonic() - t0 < 0.5             # 立即,不等节流
        s3 = await be.open("qd01", profile="thumb")
        await s1.control({"type": "pause"})
        await s1.control({"type": "resume"})
        s4 = await be.open("qd01", profile="thumb")
        await asyncio.sleep(0.3)
        assert srv.resets == 1                                    # 间隔内:不发,合并等待
        for s in (s3, s4):
            assert await next_item(s, timeout=2) == (7000, REAL_CONFIG + b"\x00\x00\x00\x01\x65RESET")
        assert srv.resets == 2 and time.monotonic() - t0 >= 0.75  # 合并成 1 次,到点才发
        assert await next_item(s1) == (6000, REAL_CONFIG + b"\x00\x00\x00\x01\x65RESET")   # 老订阅者两次都收
        for s in (s2, s1):
            assert await next_item(s) == (7000, REAL_CONFIG + b"\x00\x00\x00\x01\x65RESET")
        await asyncio.sleep(1.0)
        assert srv.resets == 2                                    # 没人等了就不再发
        for s in (s4, s3, s2):
            await s.close()
    finally:
        await s1.close()
        await be.aclose()


async def test_reset_throttle_deferred_reset_skipped_when_nobody_waits_at_deadline(srv):
    """第四轮验收 G-a / P3:间隔内来的接入被延后;到点前它走了(没人在等关键帧)⇒ 到点**不发** RESET。"""
    be = make_backend(srv, FakeAdbRunner(), reset_min_interval_s=0.8)
    s1 = await be.open("qd01", profile="thumb")
    try:
        assert await next_item(s1) == (270, REAL_CONFIG + REAL_KEY)
        s2 = await be.open("qd01", profile="thumb")
        assert await next_item(s2) == (6000, REAL_CONFIG + b"\x00\x00\x00\x01\x65RESET")
        assert await next_item(s1) == (6000, REAL_CONFIG + b"\x00\x00\x00\x01\x65RESET")
        assert srv.resets == 1
        s3 = await be.open("qd01", profile="thumb")               # 间隔内:延后
        await asyncio.sleep(0.2)
        assert srv.resets == 1
        await s3.close()                                          # 到点前走了
        await asyncio.sleep(1.0)                                  # 越过节流到点时刻
        assert srv.resets == 1                                    # 没人等 ⇒ 不发
        await s2.close()
    finally:
        await s1.close()
        await be.aclose()


async def test_reset_throttle_new_server_first_reset_not_delayed_by_old_throttle(srv):
    """第四轮验收 G-a / P4:#101 重拉后的新 server,首次 RESET 立即发,不被旧 server 的节流时刻延后。"""
    be = make_backend(srv, FakeAdbRunner(), reset_min_interval_s=5.0, idle_stop_s=5)
    s1 = await be.open("qd01", profile="thumb")
    try:
        assert await next_item(s1) == (270, REAL_CONFIG + REAL_KEY)
        s2 = await be.open("qd01", profile="thumb")
        assert await next_item(s2) == (6000, REAL_CONFIG + b"\x00\x00\x00\x01\x65RESET")
        assert srv.resets == 1                                    # 旧 server 的节流时刻 = 刚才
        assert (await be.restart("qd01"))["stream_restarted"] is True
        ch = be.channels["qd01"]
        await wait_for(lambda: ch.last_key is not None and not ch._key_pending())   # 新 server 首关键帧已到
        t0 = time.monotonic()
        s3 = await be.open("qd01", profile="thumb")
        assert await next_item(s3, timeout=2) == (7000, REAL_CONFIG + b"\x00\x00\x00\x01\x65RESET")
        assert srv.resets == 2 and time.monotonic() - t0 < 1.0    # 立即,不等旧节流的 5 s
        await s3.close()
        await s2.close()
    finally:
        await s1.close()
        await be.aclose()


async def test_end_to_end_41_open_meta_frames_and_control_bytes():
    """端到端(4.1 顺序):假 server 首连关、再 accept 两条才出头 → ``open`` → 首帧 JSON 宽高来自 session 包 →
    配置包 + 关键帧 → touch / scroll / key / text 落到控制 socket 的逐字节内容。"""
    srv = FakeScrcpyServer(width=576, height=1024)
    srv.script = [pkt(FLAG_CONFIG, SPS_PPS), pkt(FLAG_KEY | 42_000_777, b"\x00\x00\x00\x01\x65IDR")]
    be = make_backend(srv, FakeAdbRunner())
    try:
        s = await be.open("qd01", profile="focus")
        try:
            assert srv.events == ["probe_closed", "accept_video", "accept_control", "header"]
            assert s.meta == {"codec": "h264", "width": 576, "height": 1024, "profile": "focus", "fps": 30, "seq0": 0}
            assert await next_item(s) == (42000, SPS_PPS + b"\x00\x00\x00\x01\x65IDR")
            for m in ({"type": "touch", "action": "down", "x": 0.25, "y": 0.5, "pointer": 3},
                      {"type": "touch", "action": "up", "x": 0.25, "y": 0.5, "pointer": 3},
                      {"type": "scroll", "x": 0.5, "y": 0.5, "dx": 0, "dy": -200},
                      {"type": "key", "keycode": "ENTER", "action": "down"},
                      {"type": "text", "text": "你好"}):
                await s.control(m)
            want = (
                struct.pack(">BBqiiHHHII", 2, 0, 3, 144, 512, 576, 1024, 0xFFFF, 1, 1)
                + struct.pack(">BBqiiHHHII", 2, 1, 3, 144, 512, 576, 1024, 0, 1, 0)
                + struct.pack(">BiiHHhhI", 3, 288, 512, 576, 1024, 0, 0x1000, 0)   # 上滚 2 格:2/16*32768
                + struct.pack(">BBIII", 0, 0, 66, 0, 0)
                + struct.pack(">BI", 1, 6) + "你好".encode("utf-8"))
            await wait_for(lambda: len(srv.ctrl) >= len(want))
            assert bytes(srv.ctrl) == want
            assert srv.msgs == [2, 2, 3, 0, 1]
            assert _server_scroll(0x1000) == 2.0
        finally:
            await s.close()
    finally:
        await be.aclose()
        srv.close()


# ══════════════════════════════════════════════════ 控制 socket
async def test_control_socket_bytes_long_press_and_no_shell(srv):
    adb = FakeAdbRunner()
    be = make_backend(srv, adb)
    s = await be.open("qd01", profile="focus")
    try:
        n_calls = len(adb.calls)
        await s.control({"type": "touch", "action": "down", "x": 0.5, "y": 0.5, "pointer": 0})
        await asyncio.sleep(0.3)                                  # 长按:down 与 up 之间什么都不发
        await wait_for(lambda: len(srv.ctrl) == 32)
        await s.control({"type": "touch", "action": "up", "x": 0.5, "y": 0.5, "pointer": 0})
        await wait_for(lambda: len(srv.ctrl) == 64)
        assert bytes(srv.ctrl[:32]) == control_bytes({"type": "touch", "action": "down", "x": 0.5, "y": 0.5, "pointer": 0}, 720, 1280)[0]
        assert bytes(srv.ctrl[32:]) == control_bytes({"type": "touch", "action": "up", "x": 0.5, "y": 0.5, "pointer": 0}, 720, 1280)[0]
        await s.control({"type": "key", "keycode": "BACK", "action": "down"})
        await s.control({"type": "scroll", "x": 0.5, "y": 0.5, "dx": 0, "dy": 100})
        await s.control({"type": "text", "text": "pw;$(id)"})
        await s.control({"type": "pong"})
        await wait_for(lambda: len(srv.ctrl) == 64 + 14 + 21 + 5 + 8)
        tail = bytes(srv.ctrl[64:])
        assert tail[:14] == bytes([0, 0]) + (4).to_bytes(4, "big") + bytes(8)
        assert tail[14] == 3 and tail[35:] == bytes([1]) + (8).to_bytes(4, "big") + b"pw;$(id)"
        assert len(adb.calls) == n_calls                          # 注入全程不经 adb / shell
    finally:
        await s.close()
        await be.aclose()


# ══════════════════════════════════════════════════ 健康判据(编号 2)/ 暂停(编号 4)/ 换档 / #101
async def test_static_screen_15s_without_frames_is_healthy(srv):
    """编号 2:静止画面 0 帧是常态。首关键帧之后 15 s 一帧不出(> 缺省 scrcpy_frame_timeout_s=10),
    server / 两条 socket 都在 ⇒ 不重建、不给 WS 发 restart。"""
    adb = FakeAdbRunner()
    alerts, ev = recording_alerts()
    be = make_backend(srv, adb, frame_timeout_s=10.0, idle_stop_s=30, alerts=alerts)
    s = await be.open("qd01", profile="focus")
    try:
        assert await next_item(s) == (270, REAL_CONFIG + REAL_KEY)
        await asyncio.sleep(15.0)
        assert ev == [] and not alerts.active                     # 静止 15 s:不产生 H07 告警
        assert be.channels["qd01"].h07_ok()                       # H07 口径:前台 + 进程在 + 两条 socket 未 EOF ⇒ ok
        assert len(adb.spawned) == 1 and len(adb.cmds("push")) == 1
        assert s._sub.queue.qsize() == 0 and not s._sub.detached  # 没收到 restart
        assert srv.video_eof == 0 and srv.exits == 0 and srv.conns == 3
        srv.push(pkt(15_500_000, b"D-after-touch"))               # 触摸后才出帧:照常送达
        assert await next_item(s) == (15500, b"D-after-touch")
    finally:
        await s.close()
        await be.aclose()


async def test_no_first_keyframe_within_frame_timeout_rebuilds(srv):
    """``scrcpy_frame_timeout_s`` 只管开流后等首关键帧:没来 ⇒ H07 重建(WS 收 restart)。"""
    srv.script = []
    adb = FakeAdbRunner()
    be = make_backend(srv, adb, frame_timeout_s=0.3, idle_stop_s=5)
    s = await be.open("qd01", profile="focus")
    try:
        assert await next_item(s, timeout=5) == {"type": "restart"}
        await wait_for(lambda: len(adb.spawned) == 2)
        assert len(adb.cmds("push")) == 2
    finally:
        await s.close()
        await be.aclose()


async def test_video_eof_rebuilds_forward_and_server_and_sends_restart(srv):
    srv.script = [pkt(FLAG_CONFIG, SPS_PPS), pkt(FLAG_KEY | 1_000_000, b"K1")]
    adb = FakeAdbRunner()
    be = make_backend(srv, adb, idle_stop_s=5)
    s = await be.open("qd01", profile="focus")
    try:
        assert await next_item(s) == (1000, SPS_PPS + b"K1")
        srv.drop_video()
        assert await next_item(s) == {"type": "restart"}          # 02 #101 / H07:已连 WS 收 restart 后重连
        with pytest.raises(StopAsyncIteration):
            await next_item(s)
        await wait_for(lambda: len(adb.spawned) == 2)
        assert (SERIAL, "forward", "--remove", f"tcp:{srv.port}") in adb.calls
        assert len(adb.cmds("forward")) >= 3 and len(adb.cmds("push")) == 2     # forward 与 server 都重建
        s2 = await be.open("qd01", profile="focus")               # 客户端重连:接到重建后的 server
        assert len(adb.spawned) == 2 and s2.meta["width"] == 720
        await s2.close()
    finally:
        await s.close()
        await be.aclose()


async def test_control_eof_rebuilds(srv):
    """编号 2:控制 socket 被服务端关(视频还开着)⇒ 也算断,H07 重建。"""
    adb = FakeAdbRunner()
    be = make_backend(srv, adb, idle_stop_s=5)
    s = await be.open("qd01", profile="focus")
    try:
        assert await next_item(s) == (270, REAL_CONFIG + REAL_KEY)
        srv.drop_control()
        assert await next_item(s) == {"type": "restart"}
        await wait_for(lambda: len(adb.spawned) == 2)
        assert len(adb.cmds("push")) == 2
    finally:
        await s.close()
        await be.aclose()


async def test_server_process_exit_rebuilds(srv):
    """编号 2:scrcpy-server 进程退出(socket 还没察觉)⇒ H07 重建。"""
    adb = FakeAdbRunner()
    be = make_backend(srv, adb, idle_stop_s=5)
    s = await be.open("qd01", profile="focus")
    try:
        assert await next_item(s) == (270, REAL_CONFIG + REAL_KEY)
        adb.procs[0].exit()
        assert await next_item(s) == {"type": "restart"}
        await wait_for(lambda: len(adb.spawned) == 2)
    finally:
        await s.close()
        await be.aclose()


# ══════════════════════════════════════════════════ H07 告警(02 §3.7 H07_SCRCPY_STALLED;04 H07 / A5-06)
def recording_alerts():
    """真 ``Alerts`` + 只记事件的假 events:``ev`` = ``(state, code, subject, severity, evidence)`` 序列。"""
    from qtrade_agent.alerts import Alerts

    ev: list[tuple[str, str, str, str, dict[str, Any]]] = []

    class Events:
        def emit(self, family, *, payload, account_id=None, now_ms=None):
            assert family == "alert" and account_id == "qd01"
            ev.append((payload["state"], payload["code"], payload["subject"], payload["severity"], dict(payload["evidence"])))

    return Alerts(Events(), clock=lambda: int(time.time() * 1000)), ev


@pytest.mark.parametrize("kill,reason", [("proc", "server_exit"), ("video", "video_eof"), ("control", "control_eof")])
async def test_h07_kill_server_warns_once_then_resolves_after_rebuild(srv, kill, reason):
    """A5-06:前台账号 server 退出 / 视频或控制 socket EOF ⇒ H07 warn **一条**(subject=account:qd01);
    重建成功(新 server 两条 socket 连上、流头读到)⇒ resolved,active 清空。"""
    adb = FakeAdbRunner()
    alerts, ev = recording_alerts()
    be = make_backend(srv, adb, idle_stop_s=5, alerts=alerts)
    s = await be.open("qd01", profile="focus")
    try:
        assert await next_item(s) == (270, REAL_CONFIG + REAL_KEY)
        assert ev == []
        {"proc": lambda: adb.procs[0].exit(), "video": srv.drop_video, "control": srv.drop_control}[kill]()
        assert await next_item(s) == {"type": "restart"}
        await wait_for(lambda: len(ev) == 2)
        assert ev[0] == ("firing", "H07_SCRCPY_STALLED", "account:qd01", "warn", {"reason": reason})
        assert ev[1][:4] == ("resolved", "H07_SCRCPY_STALLED", "account:qd01", "warn")
        assert len(adb.spawned) == 2 and not alerts.active
        await asyncio.sleep(0.2)
        assert len(ev) == 2                                        # 同一次故障只一条 firing
    finally:
        await s.close()
        await be.aclose()


async def test_h07_rebuild_failure_keeps_warn_with_reason(srv):
    """重建失败 ⇒ 不 resolve,保持 warn,evidence 带失败原因。"""
    adb = FakeAdbRunner()
    alerts, ev = recording_alerts()
    be = make_backend(srv, adb, idle_stop_s=5, alerts=alerts)
    s = await be.open("qd01", profile="focus")
    try:
        assert await next_item(s) == (270, REAL_CONFIG + REAL_KEY)
        adb.fail.add("push")
        adb.procs[0].exit()
        assert await next_item(s) == {"type": "restart"}
        await wait_for(lambda: "qd01" not in be.channels)
        await asyncio.sleep(0.1)
        assert [e[0] for e in ev] == ["firing"]                    # 只一条 firing、没有 resolved
        a = alerts.active[("H07_SCRCPY_STALLED", "account:qd01")]
        assert a.severity == "warn" and a.evidence["reason"] == "server_exit" and a.evidence["rebuild"] == "failed"
        assert "push" in a.evidence["error"]
    finally:
        await s.close()
        await be.aclose()


async def test_h07_background_or_unwatched_account_does_not_alert(srv):
    """04 H07「后台账号不检查」:只剩 pause 的订阅者(后台)或没人在看 ⇒ server 退出照常重建,但不产生告警。"""
    adb = FakeAdbRunner()
    alerts, ev = recording_alerts()
    be = make_backend(srv, adb, idle_stop_s=5, alerts=alerts)
    s = await be.open("qd01", profile="thumb")
    try:
        assert await next_item(s) == (270, REAL_CONFIG + REAL_KEY)
        await s.control({"type": "pause"})
        assert not be.channels["qd01"].h07_ok()                    # 后台:H07 不检查(unknown,不给 ok)
        adb.procs[0].exit()
        assert await next_item(s) == {"type": "restart"}
        await wait_for(lambda: len(adb.spawned) == 2)
        await asyncio.sleep(0.1)
        assert ev == [] and not alerts.active
        s2 = await be.open("qd01", profile="thumb")                # 再接一个,然后走掉 ⇒ 没人在看
        await s2.close()
        adb.procs[1].exit()
        await wait_for(lambda: len(adb.spawned) == 3)
        await asyncio.sleep(0.1)
        assert ev == [] and not alerts.active
    finally:
        await s.close()
        await be.aclose()


@pytest.mark.parametrize("broken", ["video", "control", "proc"])
async def test_h07_ok_false_once_socket_eof_or_server_gone(srv, broken):
    """``h07_ok``(#72 的 ok 判据)逐项看:视频 / 控制 socket 一 EOF、或 server 进程一退,当场就不再是 ok。"""
    be = make_backend(srv, FakeAdbRunner(), idle_stop_s=5)
    s = await be.open("qd01", profile="focus")
    try:
        assert await next_item(s) == (270, REAL_CONFIG + REAL_KEY)
        ch = be.channels["qd01"]
        assert ch.h07_ok()
        if broken == "proc":
            ch._proc.returncode = 0                               # 只改返回码,不让看门狗先跑
        else:
            (ch._vr if broken == "video" else ch._cr).feed_eof()
        assert not ch.h07_ok()                                    # 同步判,重建还没来得及跑
    finally:
        await s.close()
        await be.aclose()


def test_h07_health_check_per_account_follows_channel(rig):
    """#72 ``checks.accounts.<id>.H07``:前台且健康 ⇒ ok;firing ⇒ firing;没有通道 / 后台 ⇒ unknown。"""
    from tests.test_api_ext2 import H, P, TOK_R
    from qtrade_agent.alerts import H07_SCRCPY_STALLED

    class Ch:
        def __init__(self, ok):
            self.ok = ok

        def h07_ok(self):
            return self.ok

    class Be:
        channels = {"qd01": Ch(True), "qd02": Ch(False)}

    rig.agent.stream_backend = Be()

    def h07():
        r = rig.client.get(f"{P}/system/health", headers=H(TOK_R))
        return {aid: v["H07"] for aid, v in r.json()["checks"]["accounts"].items()}

    got = h07()
    assert got["qd01"] == "ok" and got.get("qd02", "unknown") == "unknown"
    rig.agent.alerts.firing(H07_SCRCPY_STALLED, subject="account:qd01", account_id="qd01", evidence={"reason": "server_exit"})
    assert h07()["qd01"] == "firing"


async def test_pause_keeps_both_sockets_and_resume_sends_reset_video(srv):
    """编号 4(A.2 待裁决口径):pause = 只停止向这条 WS 转发,两条 socket 都不动、server 不退;
    resume = 发 RESET_VIDEO,从新关键帧开始收。"""
    adb = FakeAdbRunner()
    be = make_backend(srv, adb)
    s = await be.open("qd01", profile="thumb")
    try:
        assert await next_item(s) == (270, REAL_CONFIG + REAL_KEY)
        await s.control({"type": "pause"})
        srv.push(pkt(400_000, b"D-paused"))
        await s.control({"type": "key", "keycode": 4, "action": "down"})
        await wait_for(lambda: len(srv.ctrl) == 14)               # 控制 socket 照常可用
        await asyncio.sleep(0.2)
        assert s._sub.queue.qsize() == 0                          # 暂停中不转发
        assert srv.video_eof == 0 and srv.exits == 0 and srv.conns == 3 and len(adb.spawned) == 1
        await s.control({"type": "resume"})
        await wait_for(lambda: srv.resets == 1)
        assert srv.msgs == [0, 17]
        assert await next_item(s) == (6000, REAL_CONFIG + b"\x00\x00\x00\x01\x65RESET")   # 画面立刻刷新
        assert len(adb.spawned) == 1                              # 没重拉 server
        await s.control({"type": "resume"})                       # 没暂停时 resume 不重复要关键帧
        await asyncio.sleep(0.1)
        assert srv.resets == 1
    finally:
        await s.close()
        await be.aclose()


async def test_profile_switch_restarts_with_new_params(srv):
    adb = FakeAdbRunner()
    be = make_backend(srv, adb, idle_stop_s=5)
    s = await be.open("qd01", profile="thumb")
    try:
        await s.control({"type": "profile", "profile": "focus"})
        assert await next_item(s) == {"type": "restart"}
        await wait_for(lambda: len(adb.spawned) == 2)
        assert "max_size=1280" in adb.spawned[-1] and "max_fps=30" in adb.spawned[-1]
        assert len(adb.cmds("push")) == 2
        s2 = await be.open("qd01", profile="focus")
        assert s2.meta["profile"] == "focus" and s2.meta["fps"] == 30 and len(adb.spawned) == 2
        await s2.close()
    finally:
        await s.close()
        await be.aclose()


async def test_restart_api_with_and_without_live_stream(srv):
    """编号 5:#101 重建每次都先重推 jar(4.1 启动后自删 jar),再 forward、再拉 server。"""
    adb = FakeAdbRunner()
    be = make_backend(srv, adb, idle_stop_s=5)
    assert await be.restart("qd01") == {"forward_rebuilt": True, "stream_restarted": False}   # 没人在看:只重建 forward
    assert adb.spawned == []
    s = await be.open("qd01", profile="focus")
    try:
        assert await be.restart("qd01") == {"forward_rebuilt": True, "stream_restarted": True}
        assert await next_item(s) == {"type": "restart"} and len(adb.spawned) == 2
        verbs = [c[1] for c in adb.calls]
        assert verbs.count("push") == 2
        last = len(verbs) - 1 - verbs[::-1].index("push")
        assert adb.calls[last] == (SERIAL, "push", JAR, REMOTE_JAR)
        assert adb.calls[last + 1] == (SERIAL, "forward", f"tcp:{srv.port}", "localabstract:scrcpy")
    finally:
        await s.close()
        await be.aclose()


def test_server_args_keep_i_frame_interval_but_it_is_not_relied_on():
    """编号 5:``i-frame-interval=1`` 留着(server 接受、无害),但关键帧一律靠 RESET_VIDEO,不靠它。"""
    assert "video_codec_options=i-frame-interval=1" in server_args("4.1", "720p@30")


async def test_last_subscriber_close_stops_server_and_removes_forward(srv):
    adb = FakeAdbRunner()
    be = make_backend(srv, adb, idle_stop_s=0.05)
    s = await be.open("qd01", profile="thumb")
    await s.close()
    await wait_for(lambda: be.channels == {})
    assert adb.procs[0].returncode is not None
    assert adb.calls[-1] == (SERIAL, "forward", "--remove", f"tcp:{srv.port}")


async def test_push_failure_raises_and_leaves_no_channel(srv):
    adb = FakeAdbRunner()
    adb.fail.add("push")
    be = make_backend(srv, adb)
    with pytest.raises(ScrcpyError):
        await be.open("qd01", profile="thumb")
    assert be.channels == {} and adb.spawned == []


# ══════════════════════════════════════════════════ 账号 → serial / 165NN
class _Store:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows

    def get_account_full(self, aid: str) -> Optional[dict[str, Any]]:
        return self.rows.get(aid)


def test_target_resolver_derives_ports_and_never_falls_back():
    t = store_target_resolver(_Store({
        "qd07": {"channel": "qidian", "seq": 7, "adb_serial": None, "stream_port": None},
        "qd02": {"channel": "qidian", "seq": 2, "adb_serial": "127.0.0.1:16002", "stream_port": 16502},
        "qq01": {"channel": "qq", "seq": 1},
    }))
    assert t("qd07") == ("127.0.0.1:16007", 16507)
    assert t("qd02") == ("127.0.0.1:16002", 16502)
    for bad in ("qd99", "qq01"):
        with pytest.raises(LookupError):
            t(bad)


# ══════════════════════════════════════════════════ screen_adb:#33 截图 / #35 REST 兜底
async def test_adb_input_text_is_shell_quoted():
    adb = FakeAdbRunner()
    nasty = "a; reboot $(id) `x` | cat > /sdcard/p & echo 'q'"
    assert await screen_adb.adb_input(adb, SERIAL, {"type": "text", "text": nasty}) == {"ok": True}
    serial, verb, cmd = adb.calls[-1]
    assert (serial, verb) == (SERIAL, "shell")
    assert shlex.split(cmd) == ["input", "text", nasty]           # 设备 shell 拆出来还是原样一个参数


async def test_adb_input_tap_swipe_key():
    adb = FakeAdbRunner()
    await screen_adb.adb_input(adb, SERIAL, {"type": "tap", "x": 0.5, "y": 0.25})
    assert adb.calls[-1] == (SERIAL, "shell", "input tap 360 320")        # 归一化按 wm size 720x1280 换算
    await screen_adb.adb_input(adb, SERIAL, {"type": "swipe", "x": 10, "y": 20, "x2": 30, "y2": 400})
    assert adb.calls[-1] == (SERIAL, "shell", "input swipe 10 20 30 400 120")        # duration_ms 缺省 120
    await screen_adb.adb_input(adb, SERIAL, {"type": "swipe", "x": 10, "y": 20, "x2": 10, "y2": 20, "duration_ms": 800})
    assert adb.calls[-1] == (SERIAL, "shell", "input swipe 10 20 10 20 800")         # 同点 + 时长 = 长按
    await screen_adb.adb_input(adb, SERIAL, {"type": "swipe", "x": 1, "y": 2, "x2": 3, "y2": 4, "duration_ms": 99999})
    assert adb.calls[-1][-1].endswith(" 5000")                                        # 上限 5000
    with pytest.raises(ValueError):
        await screen_adb.adb_input(adb, SERIAL, {"type": "swipe", "x": 1, "y": 2, "x2": 3, "y2": 4, "duration_ms": "1;reboot"})
    await screen_adb.adb_input(adb, SERIAL, {"type": "key", "keycode": "BACK"})
    assert adb.calls[-1] == (SERIAL, "shell", "input keyevent 4")
    with pytest.raises(ValueError):
        await screen_adb.adb_input(adb, SERIAL, {"type": "key", "keycode": "4; reboot"})


async def test_screencap_only_returns_png():
    adb = FakeAdbRunner()
    assert await screen_adb.screencap_png(adb, SERIAL) == adb.png
    assert adb.calls[-1] == (SERIAL, "exec-out", "screencap", "-p")
    adb.png = b"error: device offline"
    assert await screen_adb.screencap_png(adb, SERIAL) == b""


async def test_async_adb_uses_agent_server_port_and_no_shell(tmp_path):
    """真 ``AsyncAdb``:argv 固定带 ``-P 16000 -s <serial>``;用一个假 adb 脚本验证走的是 exec 而不是 shell。"""
    fake = tmp_path / "adb"
    fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    fake.chmod(0o755)
    rc, out = await screen_adb.AsyncAdb(str(fake), 16000).run(SERIAL, "shell", "input text 'a b'")
    assert rc == 0 and out.decode().splitlines() == ["-P", "16000", "-s", SERIAL, "shell", "input text 'a b'"]


# ══════════════════════════════════════════════════ 接到 #34 WS / #33 / #101 路由
@pytest.fixture
def rig(tmp_path):
    from tests.test_api_ext2 import Rig
    r = Rig(tmp_path)
    yield r
    r.close()


def test_ws_route_end_to_end_with_scrcpy_backend(rig, srv):
    """#34 全链:WS 首帧 JSON = codec meta;二进制帧 = 8 字节 PTS(ms)+ SPS/PPS + IDR;控制帧落到控制 socket。"""
    from tests.test_api_ext2 import P, TOK_W
    srv.script = [pkt(FLAG_CONFIG, SPS_PPS), pkt(FLAG_KEY | 7_000_123, b"\x00\x00\x00\x01\x65IDR")]
    rig.agent.stream_backend = make_backend(srv, FakeAdbRunner())
    with rig.client.websocket_connect(f"{P}/accounts/qd01/stream?token={TOK_W}&profile=thumb",
                                      subprotocols=["qtrade-scrcpy-v1"]) as ws:
        meta = ws.receive_json()
        assert meta == {"codec": "h264", "width": 720, "height": 1280, "profile": "thumb", "fps": 5, "seq0": 0}
        frame = ws.receive_bytes()
        assert struct.unpack(">Q", frame[:8])[0] == 7000 and frame[8:] == SPS_PPS + b"\x00\x00\x00\x01\x65IDR"
        ws.send_json({"type": "touch", "action": "down", "x": 0.5, "y": 0.5, "pointer": 0})
        ws.send_json({"type": "touch", "action": "up", "x": 0.5, "y": 0.5, "pointer": 0})
        for _ in range(100):
            if len(srv.ctrl) >= 64:
                break
            time.sleep(0.02)
        assert len(srv.ctrl) == 64 and srv.ctrl[1] == 0 and srv.ctrl[33] == 1


def test_ws_send_timeout_closes_ws_and_session(rig, monkeypatch):
    """#34 单帧 send 卡住超过 STREAM_SEND_TIMEOUT_S ⇒ 按掉线处理:摘订阅(session.close)、以 4408(客户端接收超时,R6-74)关 WS。"""
    from starlette.websockets import WebSocket, WebSocketDisconnect

    from qtrade_agent.api import routes_ext2
    from tests.test_api_ext2 import FakeStreamBackend, P, TOK_R

    async def stuck(self, data):                                  # 客户端不读、TCP 窗口满
        await asyncio.sleep(3600)

    monkeypatch.setattr(routes_ext2, "STREAM_SEND_TIMEOUT_S", 0.3)
    monkeypatch.setattr(WebSocket, "send_bytes", stuck)
    be = FakeStreamBackend()
    rig.agent.stream_backend = be
    with rig.client.websocket_connect(f"{P}/accounts/qd01/stream?token={TOK_R}&profile=focus") as ws:
        assert ws.receive_json()["codec"] == "h264"
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_bytes()
        assert ei.value.code == 4408 == routes_ext2.WS_CLOSE_RECV_TIMEOUT
    assert be.sessions[0].closed


def test_ws_rotate_control_frame_is_rejected(rig):
    """02 #34 控制帧逐字口径:没有 ``rotate``。"""
    from starlette.websockets import WebSocketDisconnect

    from qtrade_agent.api.routes_ext2 import STREAM_CONTROL_TYPES
    from tests.test_api_ext2 import FakeStreamBackend, P, TOK_R
    assert STREAM_CONTROL_TYPES == ("touch", "key", "scroll", "text", "pause", "resume", "profile", "pong")
    rig.agent.stream_backend = FakeStreamBackend()
    with rig.client.websocket_connect(f"{P}/accounts/qd01/stream?token={TOK_R}&profile=thumb") as ws:
        ws.receive_json()
        ws.send_json({"type": "rotate"})
        with pytest.raises(WebSocketDisconnect) as ei:
            for _ in range(5):
                ws.receive_bytes()
    assert ei.value.code == 4400


def test_ws_restart_item_is_forwarded_and_frees_focus(rig):
    """执行体下发 ``{type:'restart'}`` ⇒ 原样发给客户端,且 focus 位立即让出(重连不撞 4410)。"""
    from tests.test_api_ext2 import FakeStreamBackend, FakeStreamSession, P, TOK_R

    class RestartingBackend(FakeStreamBackend):
        async def open(self, account_id, *, profile):
            s = FakeStreamSession([(10, b"\x00\x00\x00\x01A")])
            s._frames = [(10, b"\x00\x00\x00\x01A"), {"type": "restart"}]      # type: ignore[list-item]
            self.sessions.append(s)
            return s

    rig.agent.stream_backend = RestartingBackend()
    with rig.client.websocket_connect(f"{P}/accounts/qd01/stream?token={TOK_R}&profile=focus") as ws1:
        ws1.receive_json()
        ws1.receive_bytes()
        assert ws1.receive_json() == {"type": "restart"}
        with rig.client.websocket_connect(f"{P}/accounts/qd01/stream?token={TOK_R}&profile=focus") as ws2:
            assert ws2.receive_json()["codec"] == "h264"             # 没被 4410 拒


def test_restart_stream_endpoint_uses_backend(rig):
    from tests.test_api_ext2 import H, P, TOK_W
    calls: list[str] = []

    class Be:
        async def restart(self, account_id):
            calls.append(account_id)
            return {"forward_rebuilt": True, "stream_restarted": True}

    rig.store.upsert_runtime("qd01", kind="redroid", adb_serial="127.0.0.1:16001", stream_port=16501)
    rig.agent.stream_backend = Be()
    r = rig.client.post(f"{P}/accounts/qd01/runtime/restart-stream", headers=H(TOK_W))
    assert r.status_code == 200 and r.json()["stream_restarted"] is True and r.json()["forward_rebuilt"] is True
    assert calls == ["qd01"]


def test_qidian_screenshot_goes_through_bus_state_check(rig):
    """#33 企点截图走适配器 + 总线(不再有绕过 ``_submit_op`` 的快路):账号停了就拿不到图。"""
    from tests.test_api_ext2 import H, P, TOK_R
    shots: list[str] = []

    async def grab(aid: str) -> bytes:
        shots.append(aid)
        return b"\x89PNG\r\n\x1a\nLIVE"

    rig.agent.adapters["qidian"].screenshot_fn = grab
    r = rig.client.get(f"{P}/accounts/qd01/screenshot", headers=H(TOK_R))
    assert r.status_code == 200 and r.content == b"\x89PNG\r\n\x1a\nLIVE"
    assert rig.store.get_command_result(r.headers["X-QT-Trace-Id"]) is not None      # 走了总线,有指令留痕
    rig.store.transition("qd01", "stopped")
    r = rig.client.get(f"{P}/accounts/qd01/screenshot", headers=H(TOK_R))
    assert r.status_code == 503 and r.json()["error"]["reason"] == "bad_state"
    assert shots == ["qd01"]                                      # 停了的账号根本没去抓图


def test_qidian_login_promote_is_gone(rig):
    """A2:``self_uid`` 非空不等于在线,不再有每 15 s 把 login_required 升 running 的调度。"""
    from qtrade_agent.healthloop import HealthLoop
    assert not hasattr(HealthLoop, "promote_qidian_login")
    assert "qidian_login_promote" not in rig.agent.scheduler.jobs
    assert "health_adb" in rig.agent.scheduler.jobs                 # 调度确实已登记(不是空表误判)


def test_contacts_non_maindb_error_is_503_not_500(tmp_path):
    """contacts:主库读取里冒出非 ``MainDbError`` 的异常(adb 超时之类)⇒ 503 maindb_unavailable,不冒 500。"""
    from tests.test_api_ext2 import H, P, TOK_R, Rig

    class Boom:
        def exists(self) -> bool:
            raise TimeoutError("adb 超时")

    r = Rig(tmp_path)
    try:
        r.agent._maindb_factory = lambda uid, acct: Boom()
        resp = r.client.get(f"{P}/accounts/qd01/contacts", headers=H(TOK_R))
        assert resp.status_code == 503 and resp.json()["error"]["reason"] == "maindb_unavailable", resp.text
    finally:
        r.close()


def test_main_attach_screen_skips_without_jar(tmp_path):
    """jar 不在 ⇒ 不装配(#34 如实 4503),也不回退到任何固定设备。"""
    from qtrade_agent.config import AgentConfig, QidianAdapterConfig
    from qtrade_agent.main import _attach_screen

    class A:
        stream_backend = None

    a = A()
    _attach_screen(a, AgentConfig(qidian=QidianAdapterConfig(scrcpy_server_path=str(tmp_path / "nope"))))
    assert a.stream_backend is None
    assert json.dumps(AgentConfig().qidian.stream_profiles) == json.dumps(
        {"thumb": "540p@5", "thumb10": "540p@10", "focus": "720p@30", "focus15": "720p@15"})


def test_stream_input_duration_and_inject_errors_map_to_4xx_5xx(rig):
    """#35:``duration_ms`` 非数字 ⇒ 400;执行体报参数错 ⇒ 400、adb 失败 ⇒ 503,都不冒 500。"""
    from tests.test_api_ext2 import H, P, TOK_W

    class Be:
        async def inject(self, account_id, msg):
            if msg.get("keycode") == "NOPE":
                raise ValueError("不认识的 keycode")
            raise RuntimeError("adb input 失败 rc=1")

    rig.agent.stream_backend = Be()
    r = rig.client.post(f"{P}/accounts/qd01/stream/input", headers=H(TOK_W),
                        json={"type": "swipe", "x": 1, "y": 2, "x2": 1, "y2": 2, "duration_ms": "long"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_duration"
    r = rig.client.post(f"{P}/accounts/qd01/stream/input", headers=H(TOK_W), json={"type": "key", "keycode": "NOPE"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_input"
    r = rig.client.post(f"{P}/accounts/qd01/stream/input", headers=H(TOK_W), json={"type": "tap", "x": 1, "y": 2})
    assert r.status_code == 503 and r.json()["error"]["reason"] == "inject_failed"
