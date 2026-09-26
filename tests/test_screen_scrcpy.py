"""画面流执行体 ``screen_scrcpy.ScrcpyBackend`` 与 ``screen_adb`` 的开发者测试。

全部用假件:假 adb(只记命令、不起真进程)+ 假 scrcpy-server(``asyncio.start_server``,跑在独立线程的事件循环里,
发 dummy 字节 / codec meta / 帧包,收控制消息)。**不碰真 docker / adb / 任何设备。**
断言落在协议字节上:首帧 JSON、PTS 与标志位、配置包缓存补发、控制消息的逐字节布局、断线/无帧重建。
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
    CODEC_H264, FLAG_CONFIG, FLAG_KEY, REMOTE_JAR, SERVER_CLASS, ScrcpyBackend, ScrcpyError, control_bytes,
    encode_text, profile_params, store_target_resolver,
)

JAR = "/opt/qtrade/scrcpy/scrcpy-server"
SERIAL = "127.0.0.1:16001"
SPS_PPS = b"\x00\x00\x00\x01\x67SPS\x00\x00\x00\x01\x68PPS"


# ══════════════════════════════════════════════════ 假件
def pkt(pts_flags: int, data: bytes) -> bytes:
    return struct.pack(">QI", pts_flags, len(data)) + data


class FakeScrcpyServer:
    """假 scrcpy-server:偶数号连接 = 视频(dummy + codec meta + 脚本包),奇数号 = 控制(全收下)。"""

    def __init__(self, width: int = 540, height: int = 960, codec: int = CODEC_H264) -> None:
        self.width, self.height, self.codec = width, height, codec
        self.script: list[bytes] = []                 # 每条视频连接一建立就发的包
        self.conns = 0
        self.ctrl = bytearray()
        self.video_eof = 0                            # 视频连接被对端(Agent)关掉的次数
        self._video_w: Optional[asyncio.StreamWriter] = None
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
        idx = self.conns
        self.conns += 1
        if idx % 2 == 0:
            self._video_w = w
            w.write(b"\x00" + struct.pack(">III", self.codec, self.width, self.height) + b"".join(self.script))
            await w.drain()
            await r.read()                             # 等 Agent 关视频 socket(暂停 / 收尾)
            self.video_eof += 1
        else:
            while True:
                b = await r.read(4096)
                if not b:
                    break
                self.ctrl += b
        w.close()

    def _call(self, coro_fn) -> Any:
        async def go():
            return await coro_fn()
        return asyncio.run_coroutine_threadsafe(go(), self.loop).result(5)

    def push(self, *packets: bytes) -> None:
        async def go():
            assert self._video_w is not None
            self._video_w.write(b"".join(packets))
            await self._video_w.drain()
        self._call(go)

    def drop_video(self) -> None:
        """模拟 server 端视频断线(scrcpy-server 崩了 / 容器里被杀)。"""
        async def go():
            assert self._video_w is not None
            self._video_w.close()
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
    def __init__(self) -> None:
        self.returncode: Optional[int] = None
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_eof()
        self._done = asyncio.Event()

    def terminate(self) -> None:
        self.returncode = 0
        self._done.set()

    kill = terminate

    async def wait(self) -> int:
        await self._done.wait()
        return 0


class FakeAdbRunner:
    """只记 argv;``wm size`` / ``screencap`` 回预置值,其余一律 rc=0。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.spawned: list[tuple[str, ...]] = []
        self.procs: list[FakeProc] = []
        self.png = b"\x89PNG\r\n\x1a\nREAL"
        self.wm = b"Physical size: 720x1280\n"
        self.fail: set[str] = set()

    async def run(self, serial: str, *args: str, timeout: float = 15.0) -> tuple[int, bytes]:
        self.calls.append((serial, *args))
        if args and args[0] in self.fail:
            return 1, b"boom"
        if args[:1] == ("exec-out",):
            return 0, self.png
        if args[:2] == ("shell", "wm size"):
            return 0, self.wm
        return 0, b""

    async def spawn(self, serial: str, *args: str) -> FakeProc:
        self.spawned.append((serial, *args))
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


def test_scroll_bytes():
    b = control_bytes({"type": "scroll", "x": 0.5, "y": 0.5, "dx": 0, "dy": 100}, 540, 960)
    assert b == [bytes([3]) + (270).to_bytes(4, "big") + (480).to_bytes(4, "big") + (540).to_bytes(2, "big")
                 + (960).to_bytes(2, "big") + (0).to_bytes(2, "big", signed=True)
                 + (-0x8000).to_bytes(2, "big", signed=True) + bytes(4)]
    assert len(b[0]) == 21
    up = control_bytes({"type": "scroll", "x": 0.5, "y": 0.5, "dx": -50, "dy": -300}, 540, 960)[0]
    assert struct.unpack(">hh", up[13:17]) == (0x4000, 0x7FFF)  # 左滑半格、往上满值(钳到 0x7FFF)


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
        assert s.meta == {"codec": "h264", "width": 540, "height": 960, "profile": "thumb", "fps": 5, "seq0": 0}
        assert adb.calls[0] == (SERIAL, "push", JAR, REMOTE_JAR)
        assert adb.calls[1] == (SERIAL, "forward", f"tcp:{srv.port}", "localabstract:scrcpy")
        argv = adb.spawned[0]
        assert argv[:6] == (SERIAL, "shell", f"CLASSPATH={REMOTE_JAR}", "app_process", "/", SERVER_CLASS)
        opts = argv[6:]
        assert opts[0] == "4.1"
        for want in ("tunnel_forward=true", "video=true", "audio=false", "control=true", "video_codec=h264",
                     "max_size=960", "max_fps=5", "send_frame_meta=true", "send_codec_meta=true",
                     "send_device_meta=false", "send_dummy_byte=true"):
            assert want in opts
        assert not any(c[1:3] == ("shell", "am") or "am start" in " ".join(c) for c in adb.calls)   # 开流不拉企点
        await wait_for(lambda: srv.conns == 2)                     # 视频 + 控制两条
    finally:
        await s.close()
        await be.aclose()


async def test_open_rejects_non_h264(srv):
    srv.codec = 0x68323635                                        # "h265"
    be = make_backend(srv, FakeAdbRunner())
    with pytest.raises(ScrcpyError):
        await be.open("qd01", profile="thumb")
    assert be.channels == {}


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


async def test_second_subscriber_shares_server_and_starts_at_next_keyframe(srv):
    srv.script = [pkt(FLAG_CONFIG, SPS_PPS), pkt(FLAG_KEY | 1_000_000, b"K1")]
    adb = FakeAdbRunner()
    be = make_backend(srv, adb)
    s1 = await be.open("qd01", profile="thumb")
    try:
        assert await next_item(s1) == (1000, SPS_PPS + b"K1")
        s2 = await be.open("qd01", profile="thumb")
        assert len(adb.spawned) == 1                             # 同账号共用一个 server
        srv.push(pkt(1_100_000, b"P2"), pkt(FLAG_KEY | 1_200_000, b"K2"))
        assert await next_item(s1) == (1100, b"P2")
        assert await next_item(s2) == (1200, SPS_PPS + b"K2")    # 新连接:缓存的配置包 + 下一个关键帧
        assert await next_item(s1) == (1200, SPS_PPS + b"K2")    # 每个 IDR 都带 SPS/PPS
        await s2.close()
    finally:
        await s1.close()
        await be.aclose()


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
        assert bytes(srv.ctrl[:32]) == control_bytes({"type": "touch", "action": "down", "x": 0.5, "y": 0.5, "pointer": 0}, 540, 960)[0]
        assert bytes(srv.ctrl[32:]) == control_bytes({"type": "touch", "action": "up", "x": 0.5, "y": 0.5, "pointer": 0}, 540, 960)[0]
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


# ══════════════════════════════════════════════════ 断线 / 无帧 / 暂停 / 换档 / #101
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
        assert len(adb.spawned) == 2 and s2.meta["width"] == 540
        await s2.close()
    finally:
        await s.close()
        await be.aclose()


async def test_no_frame_watchdog_rebuilds(srv):
    adb = FakeAdbRunner()
    be = make_backend(srv, adb, frame_timeout_s=0.3, idle_stop_s=5)
    s = await be.open("qd01", profile="focus")
    try:
        assert await next_item(s, timeout=5) == {"type": "restart"}
        await wait_for(lambda: len(adb.spawned) == 2)             # 先通知、再重拉(客户端重连时在锁上等拉起完)
    finally:
        await s.close()
        await be.aclose()


async def test_paused_subscribers_are_not_watched(srv):
    adb = FakeAdbRunner()
    be = make_backend(srv, adb, frame_timeout_s=0.2)
    s = await be.open("qd01", profile="thumb")
    try:
        await s.control({"type": "pause"})
        await asyncio.sleep(0.8)
        assert len(adb.spawned) == 1                              # 后台流不检查 H07
    finally:
        await s.close()
        await be.aclose()


async def test_pause_drops_video_keeps_control_then_resume_restarts(srv):
    srv.script = [pkt(FLAG_CONFIG, SPS_PPS), pkt(FLAG_KEY | 1_000_000, b"K1")]
    adb = FakeAdbRunner()
    be = make_backend(srv, adb)
    s = await be.open("qd01", profile="thumb")
    try:
        assert await next_item(s) == (1000, SPS_PPS + b"K1")
        await s.control({"type": "pause"})
        await wait_for(lambda: srv.video_eof == 1)                # 视频 socket 断了
        await s.control({"type": "key", "keycode": 4, "action": "down"})
        await wait_for(lambda: len(srv.ctrl) == 14)               # 控制 socket 还在
        assert len(adb.spawned) == 1
        await s.control({"type": "resume"})
        assert len(adb.spawned) == 2                              # 恢复 = 重拉 server,已连 WS 不断
        meta = await next_item(s)
        assert isinstance(meta, dict) and meta["codec"] == "h264" and meta["width"] == 540
        assert await next_item(s) == (1000, SPS_PPS + b"K1")
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
        s2 = await be.open("qd01", profile="focus")
        assert s2.meta["profile"] == "focus" and s2.meta["fps"] == 30 and len(adb.spawned) == 2
        await s2.close()
    finally:
        await s.close()
        await be.aclose()


async def test_restart_api_with_and_without_live_stream(srv):
    adb = FakeAdbRunner()
    be = make_backend(srv, adb, idle_stop_s=5)
    assert await be.restart("qd01") == {"forward_rebuilt": True, "stream_restarted": False}   # 没人在看:只重建 forward
    assert adb.spawned == []
    s = await be.open("qd01", profile="focus")
    try:
        assert await be.restart("qd01") == {"forward_rebuilt": True, "stream_restarted": True}
        assert await next_item(s) == {"type": "restart"} and len(adb.spawned) == 2
    finally:
        await s.close()
        await be.aclose()


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
    from tests.test_api_ext2 import P, TOK_R
    srv.script = [pkt(FLAG_CONFIG, SPS_PPS), pkt(FLAG_KEY | 7_000_123, b"\x00\x00\x00\x01\x65IDR")]
    rig.agent.stream_backend = make_backend(srv, FakeAdbRunner())
    with rig.client.websocket_connect(f"{P}/accounts/qd01/stream?token={TOK_R}&profile=thumb",
                                      subprotocols=["qtrade-scrcpy-v1"]) as ws:
        meta = ws.receive_json()
        assert meta == {"codec": "h264", "width": 540, "height": 960, "profile": "thumb", "fps": 5, "seq0": 0}
        frame = ws.receive_bytes()
        assert struct.unpack(">Q", frame[:8])[0] == 7000 and frame[8:] == SPS_PPS + b"\x00\x00\x00\x01\x65IDR"
        ws.send_json({"type": "touch", "action": "down", "x": 0.5, "y": 0.5, "pointer": 0})
        ws.send_json({"type": "touch", "action": "up", "x": 0.5, "y": 0.5, "pointer": 0})
        for _ in range(100):
            if len(srv.ctrl) >= 64:
                break
            time.sleep(0.02)
        assert len(srv.ctrl) == 64 and srv.ctrl[1] == 0 and srv.ctrl[33] == 1


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
