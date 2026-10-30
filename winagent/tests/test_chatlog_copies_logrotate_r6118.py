"""R6-118 filecopy 副本清扫、实例名持久化与 chatlog-server.log 轮转(05 §2.4.10 ③④;DECISION-3 T7/T9、B2/B3)。

🔴 只用 pytest 临时目录:``WinWeChat`` 一律传 ``temp_dir=`` 并把 ``tempvol.user_temp_dir`` / ``locate`` /
``current_wxid`` 打桩到隔离目录,绝不扫描本机真实 ``%TEMP%\\filecopy_chatlog`` 或真实微信账号目录。
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

from qtrade_winagent import tempvol
from qtrade_winagent.errors import BUSY, WaError
from qtrade_winagent.win import wechat as native

FILECOPY = "filecopy_chatlog"


@pytest.fixture(autouse=True)
def _never_touch_real_temp(tmp_path, monkeypatch):
    """兜底:任何代码路径若退回「用户 %TEMP%」,只会落到本用例的隔离目录。"""
    guard = tmp_path / "guard-temp"
    guard.mkdir()
    monkeypatch.setattr(tempvol, "user_temp_dir", lambda: str(guard))
    yield guard
    assert not (guard / FILECOPY).exists() or not any((guard / FILECOPY).iterdir())


def touch(directory, name, size=10):
    path = directory / name
    path.write_bytes(b"x" * size)
    return path


def names(directory):
    return sorted(p.name for p in directory.iterdir())


# ---------------------------------------------------------------- 前缀精确匹配
@pytest.mark.parametrize("name,expected", [
    ("wxid_a_+message_0.db", "wxid_a"),
    ("wxid_ab_+message_0.db", None),                 # wxid_a 不得命中 wxid_ab…
    ("wxid_a_message_0.db", None),
    ("wxid_a+message_0.db", None),
    ("xwxid_a_+message_0.db", None),
    ("resource-0123_+message_0.db-wal", "resource-0123"),
    ("resource-0123_+message_0.db-shm", "resource-0123"),
    ("resource-01234_+message_0.db", None),
])
def test_instance_prefix_is_whole_name_plus_separator(name, expected):
    assert native.instance_of_copy(name, ["wxid_a", "resource-0123"]) == expected


# ---------------------------------------------------------------- 清扫:只删精确前缀、本层普通文件
def build_filecopy(tmp_path):
    temp = tmp_path / "temp"
    fc = temp / FILECOPY
    fc.mkdir(parents=True)
    mine = {
        "resource-cur_+message_0.db": 100, "resource-cur_+message_0.db-wal": 20, "resource-cur_+message_0.db-shm": 5,
        "wxid_cur_+message_0.db": 50, "resource-old_+message_1.db": 7, "wxid_old_+message_0.db": 3,
    }
    foreign = {
        "wxid_cure_+message_0.db": 11, "wxid_cur_message_0.db": 12, "resource-cur+x.db": 13,
        "resource-curx_+m.db": 14, "other_+message_0.db": 15, "notes.txt": 16,
    }
    for n, size in {**mine, **foreign}.items():
        touch(fc, n, size)
    (fc / "resource-cur_+dir").mkdir()                       # 同前缀的子目录:不删、不递归
    touch(fc / "resource-cur_+dir", "resource-cur_+inner.db", 9)
    (fc / "sub").mkdir()
    touch(fc / "sub", "resource-cur_+nested.db", 9)
    touch(temp, "resource-cur_+outside.db", 9)              # filecopy_chatlog 之外:不动
    return temp, fc, mine, foreign


def test_sweep_deletes_only_exact_prefixes_in_top_level_regular_files(tmp_path):
    temp, fc, mine, foreign = build_filecopy(tmp_path)
    out = native.sweep_instance_copies(str(fc), ["resource-cur", "wxid_cur", "resource-old", "wxid_old"])
    assert names(fc) == sorted(list(foreign) + ["resource-cur_+dir", "sub"])
    assert (fc / "resource-cur_+dir" / "resource-cur_+inner.db").exists()
    assert (fc / "sub" / "resource-cur_+nested.db").exists()
    assert (temp / "resource-cur_+outside.db").exists()
    assert out["before_bytes"] == sum(mine.values()) == out["freed_bytes"]
    assert out["deleted"] == len(mine) and out["failed"] == 0 and out["complete"] is True


def test_sweep_with_no_known_instances_deletes_nothing(tmp_path):
    _, fc, _, _ = build_filecopy(tmp_path)
    before = names(fc)
    out = native.sweep_instance_copies(str(fc), [])
    assert names(fc) == before and out["freed_bytes"] == 0 and out["deleted"] == 0


def test_sweep_skips_undeletable_files_and_counts_them(tmp_path, monkeypatch):
    _, fc, mine, _ = build_filecopy(tmp_path)
    busy = str(fc / "resource-cur_+message_0.db")
    real_unlink = os.unlink

    def unlink(path, *a, **kw):
        if os.path.abspath(path) == os.path.abspath(busy):
            raise PermissionError(32, "being used by another process", path)
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "remove", unlink)
    out = native.sweep_instance_copies(str(fc), ["resource-cur", "wxid_cur"])
    assert os.path.exists(busy)
    assert out["failed"] == 1
    assert out["freed_bytes"] == 20 + 5 + 50 and out["deleted"] == 3
    assert not (fc / "wxid_cur_+message_0.db").exists()       # 一个删不掉不中断其余


@pytest.mark.skipif(os.name != "nt", reason="Windows 才有「被占用不能删」的真实语义")
def test_sweep_skips_file_really_held_open_on_windows(tmp_path):
    _, fc, _, _ = build_filecopy(tmp_path)
    held = fc / "resource-cur_+message_0.db"
    with open(held, "rb"):                                    # Python 在 Windows 上不给 FILE_SHARE_DELETE
        out = native.sweep_instance_copies(str(fc), ["resource-cur"])
        assert held.exists()
    assert out["failed"] == 1 and out["deleted"] == 2 and out["freed_bytes"] == 25


def test_sweep_budget_stops_early_and_reports_incomplete(tmp_path):
    _, fc, _, _ = build_filecopy(tmp_path)
    out = native.sweep_instance_copies(str(fc), ["resource-cur"], budget_s=0)
    assert out["complete"] is False and (fc / "resource-cur_+message_0.db").exists()


# ---------------------------------------------------------------- WinWeChat:实例名(含旧名、<wxid>)持久化与清扫
def make_account(tmp_path, wxid):
    data_root = tmp_path / "wxdata"
    (data_root / "xwechat_files" / wxid / "db_storage" / "message").mkdir(parents=True)
    touch(data_root / "xwechat_files" / wxid / "db_storage" / "message", "message_0.db", 1)
    account_root = os.path.realpath(str(data_root / "xwechat_files" / wxid))
    return str(data_root), native.resource_namespace(account_root)


def backend(tmp_path, monkeypatch, *, wxid=None, data_root=None):
    b = native.WinWeChat(chatlog_dir=str(tmp_path / "pkg"), chatlog_work_dir=str(tmp_path / "work"),
                         temp_dir=str(tmp_path / "temp"))
    if wxid is None:
        def no_account():
            raise RuntimeError("微信未定位(测试)")
        monkeypatch.setattr(b, "locate", no_account)
        monkeypatch.setattr(b, "current_wxid", lambda: None)
    else:
        monkeypatch.setattr(b, "locate", lambda: {"installed": True, "data_root": data_root})
        monkeypatch.setattr(b, "current_wxid", lambda: wxid)
    return b


def test_backend_sweeps_current_old_and_wxid_prefixes_and_persists_names(tmp_path, monkeypatch):
    data_root, ns = make_account(tmp_path, "wxid_cur")
    temp = tmp_path / "temp"
    fc = temp / FILECOPY
    fc.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    (work / native.INSTANCE_RECORD).write_text('{"version": 1, "names": ["resource-old", "wxid_old"]}', encoding="utf-8")
    for n in (f"{ns}_+message_0.db", f"{ns}_+message_0.db-wal", "wxid_cur_+message_0.db",
              "resource-old_+message_0.db", "wxid_old_+message_0.db", "wxid_cure_+message_0.db", "other_+x.db"):
        touch(fc, n)
    b = backend(tmp_path, monkeypatch, wxid="wxid_cur", data_root=data_root)
    out = b.chatlog_sweep_copies(budget_s=15.0)
    assert names(fc) == ["other_+x.db", "wxid_cure_+message_0.db"]
    assert out["deleted"] == 5 and out["failed"] == 0 and out["freed_bytes"] == 50

    # 重启后(新对象)定位不到账号:靠持久化的实例名仍能证明归属
    for n in (f"{ns}_+message_9.db", "wxid_cur_+message_9.db", "resource-old_+message_9.db"):
        touch(fc, n)
    b2 = backend(tmp_path, monkeypatch)
    out2 = b2.chatlog_sweep_copies(budget_s=15.0)
    assert names(fc) == ["other_+x.db", "wxid_cure_+message_0.db"]
    assert out2["deleted"] == 3


def test_backend_ignores_invalid_names_in_instance_record(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    fc = temp / FILECOPY
    fc.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    (work / native.INSTANCE_RECORD).write_text(
        '{"version": 1, "names": ["", "..", "a b", "' + "x" * 129 + '", 7, "good-1"]}', encoding="utf-8")
    for n in ("_+evil.db", ".._+evil.db", "good-1_+m.db", "x" * 129 + "_+m.db"):
        touch(fc, n)
    out = backend(tmp_path, monkeypatch).chatlog_sweep_copies(budget_s=15.0)
    assert names(fc) == sorted(["_+evil.db", ".._+evil.db", "x" * 129 + "_+m.db"])
    assert out["deleted"] == 1


def test_backend_corrupt_instance_record_does_not_crash_or_overdelete(tmp_path, monkeypatch):
    fc = tmp_path / "temp" / FILECOPY
    fc.mkdir(parents=True)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / native.INSTANCE_RECORD).write_text("{not json", encoding="utf-8")
    touch(fc, "other_+x.db")
    out = backend(tmp_path, monkeypatch).chatlog_sweep_copies(budget_s=15.0)
    assert names(fc) == ["other_+x.db"] and out["deleted"] == 0


def test_backend_does_not_sweep_while_its_chatlog_runs(tmp_path, monkeypatch):
    data_root, ns = make_account(tmp_path, "wxid_cur")
    fc = tmp_path / "temp" / FILECOPY
    fc.mkdir(parents=True)
    touch(fc, f"{ns}_+message_0.db")
    b = backend(tmp_path, monkeypatch, wxid="wxid_cur", data_root=data_root)
    b._chatlog_process = SimpleNamespace(returncode=None, pid=4242)     # noqa: SLF001 —— 假进程,不触真进程
    out = b.chatlog_sweep_copies(budget_s=15.0)
    assert (fc / f"{ns}_+message_0.db").exists() and out["freed_bytes"] == 0


def test_backend_without_temp_dir_uses_session_user_temp(tmp_path, monkeypatch, _never_touch_real_temp):
    """临时目录 = 会话代理(chatlog 继承)的用户 %TEMP%;此处已打桩到隔离目录。"""
    data_root, ns = make_account(tmp_path, "wxid_cur")
    fc = _never_touch_real_temp / FILECOPY
    fc.mkdir()
    touch(fc, f"{ns}_+message_0.db")
    b = native.WinWeChat(chatlog_dir=str(tmp_path / "pkg"), chatlog_work_dir=str(tmp_path / "work"))
    monkeypatch.setattr(b, "locate", lambda: {"installed": True, "data_root": data_root})
    monkeypatch.setattr(b, "current_wxid", lambda: "wxid_cur")
    out = b.chatlog_sweep_copies(budget_s=15.0)
    assert out["deleted"] == 1 and not any(fc.iterdir())


# ---------------------------------------------------------------- 回收事实:本实例副本量、归属、临时卷
def test_recycle_facts_count_only_current_instance_and_report_volume(tmp_path, monkeypatch):
    fc = tmp_path / "temp" / FILECOPY
    fc.mkdir(parents=True)
    touch(fc, "resource-cur_+message_0.db", 300)
    touch(fc, "resource-cur_+message_0.db-wal", 30)
    touch(fc, "resource-old_+message_0.db", 1000)
    touch(fc, "other_+x.db", 5000)
    b = backend(tmp_path, monkeypatch)
    b._namespace = "resource-cur"                                       # noqa: SLF001 —— 本次 server 的实例名
    monkeypatch.setattr(native.tempvol, "sample",
                        lambda path=None: {"volume": "C:", "free_bytes": 123, "total_bytes": 999})
    facts = b.chatlog_recycle_facts()
    assert facts["copy_bytes"] == 330 and facts["copy_files"] == 2
    assert facts["temp_volume"] == "C:" and facts["temp_free_bytes"] == 123
    assert facts["running"] is False and facts["owned"] is False


@pytest.mark.parametrize("current", ["serving", "not_started"])
def test_recycle_facts_count_known_previous_instances_including_old_wxid(tmp_path, monkeypatch, current):
    """DECISION-3 C1 / B2:判据 (a)/(c) 的「本实例副本」= 当前实例前缀 + 已记录的上一实例前缀(含旧 ``<wxid>``)。

    ``not_started``:代理刚重启、尚未拉起 server,只凭持久化记录也要计入(否则 (c) 的「副本非空」会漏判)。
    """
    temp = tmp_path / "temp"
    fc = temp / FILECOPY
    fc.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    (work / native.INSTANCE_RECORD).write_text('{"version": 1, "names": ["resource-old", "wxid_old"]}', encoding="utf-8")
    counted = {"resource-old_+message_0.db": 1000, "resource-old_+message_0.db-wal": 7, "wxid_old_+message_0.db": 200}
    if current == "serving":
        counted.update({"resource-cur_+message_0.db": 300, "wxid_cur_+message_0.db": 40})
    foreign = {
        "wxid_olde_+message_0.db": 5000, "wxid_old_message_0.db": 11, "resource-oldx_+m.db": 13,
        "xresource-old_+m.db": 17, "other_+x.db": 19, "wxid_cure_+m.db": 23,
    }
    for n, size in {**counted, **foreign}.items():
        touch(fc, n, size)
    (fc / "resource-old_+dir").mkdir()                       # 同前缀的子目录与目录外文件都不算
    touch(fc / "resource-old_+dir", "resource-old_+inner.db", 29)
    touch(temp, "wxid_old_+outside.db", 31)
    b = backend(tmp_path, monkeypatch)
    if current == "serving":
        b._namespace = "resource-cur"                                   # noqa: SLF001 —— 本次 server 的实例名
        b._server_account = ("wxid_cur", "c:\\synthetic\\wxid_cur")     # noqa: SLF001 —— 本次 server 的 <wxid>
    monkeypatch.setattr(native.tempvol, "sample",
                        lambda path=None: {"volume": "C:", "free_bytes": 1, "total_bytes": 2})
    facts = b.chatlog_recycle_facts()
    assert facts["copy_bytes"] == sum(counted.values())
    assert facts["copy_files"] == len(counted)


@pytest.mark.parametrize("verdict,owned", [(True, True), (False, False), ("raise", False)])
def test_recycle_facts_owned_only_when_identity_verified(tmp_path, monkeypatch, verdict, owned):
    b = backend(tmp_path, monkeypatch)
    b._chatlog_process = SimpleNamespace(returncode=None, pid=4242)     # noqa: SLF001
    b._chatlog_mode = "server"                                          # noqa: SLF001

    def matches(pid):
        if verdict == "raise":
            raise WaError(BUSY, "无法核实", reason="wechat_server_owner_unverified")
        return verdict

    monkeypatch.setattr(b, "_server_process_matches", matches)
    monkeypatch.setattr(native.tempvol, "sample", lambda path=None: {"volume": "C:", "free_bytes": 1, "total_bytes": 2})
    facts = b.chatlog_recycle_facts()
    assert facts["running"] is True and facts["mode"] == "server" and facts["owned"] is owned


def test_recycle_facts_key_mode_is_never_owned_server(tmp_path, monkeypatch):
    b = backend(tmp_path, monkeypatch)
    b._chatlog_process = SimpleNamespace(returncode=None, pid=4242)     # noqa: SLF001
    b._chatlog_mode = "key"                                             # noqa: SLF001
    monkeypatch.setattr(b, "_server_process_matches", lambda pid: True)
    monkeypatch.setattr(native.tempvol, "sample", lambda path=None: {"volume": "C:", "free_bytes": 1, "total_bytes": 2})
    facts = b.chatlog_recycle_facts()
    assert facts["mode"] == "key" and facts["owned"] is False


# ---------------------------------------------------------------- ④ 日志轮转:单文件 ≤ 上限、份数受限、最旧删除
def sizes(log):
    d = os.path.dirname(log)
    base = os.path.basename(log)
    return {n: os.path.getsize(os.path.join(d, n)) for n in os.listdir(d) if n == base or n.startswith(base + ".")}


def test_sink_exact_limit_does_not_rotate_one_more_byte_does(tmp_path):
    log = str(tmp_path / "chatlog-server.log")
    sink = native._LogSink(log, 100, 2)                                 # noqa: SLF001
    for _ in range(4):
        sink.write(b"a" * 24 + b"\n")                                    # 4 × 25 = 100 = 上限
    assert sizes(log) == {"chatlog-server.log": 100}
    sink.write(b"b\n")
    sink.close()
    got = sizes(log)
    assert got["chatlog-server.log.1"] == 100 and got["chatlog-server.log"] == 2


def test_sink_keeps_at_most_backups_and_drops_oldest(tmp_path):
    log = str(tmp_path / "chatlog-server.log")
    sink = native._LogSink(log, 100, 2)                                 # noqa: SLF001
    for i in range(20):
        sink.write(f"{i:02d}".encode() + b"-" * 57 + b"\n")              # 60 B/行 ⇒ 每行一份
    sink.close()
    got = sizes(log)
    assert set(got) == {"chatlog-server.log", "chatlog-server.log.1", "chatlog-server.log.2"}
    assert all(v <= 100 for v in got.values())
    read = lambda n: open(os.path.join(tmp_path, n), "rb").read()[:2]    # noqa: E731
    assert (read("chatlog-server.log.2"), read("chatlog-server.log.1"), read("chatlog-server.log")) == (b"17", b"18", b"19")


def test_sink_default_limits_match_config():
    from qtrade_winagent.config import WechatConfig
    b = native.WinWeChat()
    cfg = WechatConfig()
    assert (b._log_max_bytes, b._log_backups) == (cfg.chatlog_log_max_bytes, cfg.chatlog_log_backups) \
        == (64 * 1024 * 1024, 5)                                         # noqa: SLF001


def test_oversized_existing_log_rotated_to_tail_lines_on_next_start(tmp_path):
    log = tmp_path / "chatlog-server.log"
    lines = [f"line-{i:04d}-".encode() + b"z" * 20 + b"\n" for i in range(100)]   # 每行 31 B,共 3100 B
    log.write_bytes(b"".join(lines))
    (tmp_path / "chatlog-server.log.1").write_bytes(b"older\n")
    assert native.rotate_oversized_log(str(log), 1000, 3) is True
    assert not log.exists() or log.stat().st_size == 0
    tail = (tmp_path / "chatlog-server.log.1").read_bytes()
    assert 0 < len(tail) <= 1000 and tail.endswith(b"line-0099-" + b"z" * 20 + b"\n")
    assert tail.startswith(b"line-")                                    # 只留整行
    assert (tmp_path / "chatlog-server.log.2").read_bytes() == b"older\n"


@pytest.mark.parametrize("size", [999, 1000])
def test_log_within_limit_is_left_alone(tmp_path, size):
    log = tmp_path / "chatlog-server.log"
    log.write_bytes(b"q" * (size - 1) + b"\n")
    assert native.rotate_oversized_log(str(log), 1000, 3) is False
    assert log.stat().st_size == size and not (tmp_path / "chatlog-server.log.1").exists()


# ---------------------------------------------------------------- 泵:脱敏、轮转、写失败不阻塞、超长行不卡
async def pump(log, data: bytes, *, limit=2 ** 16, max_bytes=0, backups=0):
    reader = asyncio.StreamReader(limit=limit)
    reader.feed_data(data)
    reader.feed_eof()
    await asyncio.wait_for(native._pump_redacted(reader, log, max_bytes=max_bytes, backups=backups), 5)  # noqa: SLF001
    return reader


async def test_pump_rotates_and_still_redacts_keys(tmp_path):
    log = str(tmp_path / "chatlog-server.log")
    secret = "a1" * 32
    data = b"".join(f"row {i:03d} DataKey:{secret}\n".encode() for i in range(40))
    await pump(log, data, max_bytes=300, backups=2)
    got = sizes(log)
    assert len(got) <= 3 and all(v <= 300 for v in got.values())
    for n in got:
        assert secret not in open(os.path.join(tmp_path, n), encoding="utf-8").read()
    assert b"row 039" in open(log, "rb").read()


async def test_pump_keeps_draining_when_log_cannot_be_opened(tmp_path):
    log = tmp_path / "chatlog-server.log"
    log.mkdir()                                                          # 日志路径是目录 ⇒ 每次打开都失败
    reader = await pump(str(log), b"x\n" * 5000, max_bytes=100, backups=1)
    assert reader.at_eof()


async def test_pump_survives_rotation_failure(tmp_path):
    log = tmp_path / "chatlog-server.log"
    (tmp_path / "chatlog-server.log.1").mkdir()                          # 轮转目标被占 ⇒ 改名失败
    reader = await pump(str(log), b"y" * 49 + b"\n" + b"z" * 49 + b"\n" + b"w" * 49 + b"\n", max_bytes=60, backups=1)
    assert reader.at_eof()
    assert log.exists() and log.stat().st_size <= 60


async def test_pump_drops_overlong_line_and_continues(tmp_path):
    log = tmp_path / "chatlog-server.log"
    reader = await pump(str(log), b"A" * 5000 + b"\nafter-long\n", limit=256, max_bytes=1000, backups=1)
    assert reader.at_eof()
    assert b"after-long" in log.read_bytes()


async def test_fake_chatlog_flood_is_not_blocked_by_rotation(tmp_path):
    """真子进程(假 chatlog:python 打 2 MB 输出)经 spawn_redacted 落日志:进程按时退出、每份 ≤ 上限、份数受限。"""
    log = str(tmp_path / "chatlog-server.log")
    code = "import sys\nfor i in range(20000): sys.stdout.write('%06d ' % i + 'k' * 93 + '\\n')\nsys.stdout.flush()"
    p = await native.spawn_redacted([sys.executable, "-c", code], cwd=str(tmp_path), log_path=log,
                                    max_bytes=64 * 1024, backups=3)
    assert await asyncio.wait_for(p.wait(), 30) == 0
    for _ in range(100):
        if not native._PUMPS:                                            # noqa: SLF001
            break
        await asyncio.sleep(0.05)
    got = sizes(log)
    assert len(got) <= 4 and all(v <= 64 * 1024 for v in got.values())
    assert b"019999" in open(log, "rb").read()


async def test_fake_chatlog_flood_not_blocked_when_log_unwritable(tmp_path):
    log = tmp_path / "chatlog-server.log"
    log.mkdir()
    code = "import sys\nfor i in range(20000): sys.stdout.write('k' * 99 + '\\n')\nsys.stdout.flush()"
    p = await native.spawn_redacted([sys.executable, "-c", code], cwd=str(tmp_path), log_path=str(log),
                                    max_bytes=1024, backups=1)
    assert await asyncio.wait_for(p.wait(), 30) == 0
