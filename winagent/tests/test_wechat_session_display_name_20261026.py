"""Session previews are not authoritative contact or group display names."""
import asyncio
from copy import deepcopy

import pytest

from qtrade_winagent.win.wechat import WinWeChat


GROUP = "45686445455@chatroom"


class NativeDirectory:
    """Fake Chatlog read boundary; no network, files, UI, or account access."""

    def __init__(self, sessions, contacts=(), rooms=(), *, fuzzy=False):
        self.sessions = deepcopy(sessions)
        self.contacts = deepcopy(list(contacts))
        self.rooms = deepcopy(list(rooms))
        self.fuzzy = fuzzy
        self.calls = []

    def __call__(self, path, query, **kwargs):
        self.calls.append((path, dict(query)))
        assert path in {"/api/v1/session", "/api/v1/contact", "/api/v1/chatroom"}
        rows = {"/api/v1/session": self.sessions, "/api/v1/contact": self.contacts,
                "/api/v1/chatroom": self.rooms}[path]
        keyword = query.get("keyword")
        if keyword and not self.fuzzy:
            rows = [row for row in rows if any(keyword in str(value) for value in row.values())]
        offset = int(query.get("offset", 0))
        limit = int(query.get("limit", 100))
        return deepcopy(rows[offset:offset + limit])


def backend(monkeypatch, source):
    value = WinWeChat()
    monkeypatch.setattr(value, "_get_items", source)
    return value


def read(value, *, keyword=None, limit=100):
    return asyncio.run(value.list_sessions(keyword=keyword, limit=limit))


@pytest.mark.parametrize("source", ["contact", "chatroom"])
def test_group_name_comes_from_matching_native_metadata_not_last_speaker(monkeypatch, source):
    original = {"userName": GROUP, "nickName": "灰灰", "content": "last preview", "nOrder": 123}
    metadata = {"UserName" if source == "contact" else "Name": GROUP, "NickName": "Greydan"}
    edge = NativeDirectory([original], contacts=[metadata] if source == "contact" else [],
                           rooms=[metadata] if source == "chatroom" else [])
    row = read(backend(monkeypatch, edge))[0]
    assert row.get("display_name") == "Greydan"
    assert {key: row[key] for key in original} == original
    assert edge.sessions == [original]


@pytest.mark.parametrize("metadata", [
    {"UserName": GROUP, "Remark": "资金群备注", "NickName": "Greydan"},
    {"userName": GROUP, "remark": "资金群备注", "nickName": "Greydan"},
    {"username": GROUP, "remark": "资金群备注", "nickname": "Greydan"},
])
def test_exact_group_contact_remark_takes_precedence(monkeypatch, metadata):
    edge = NativeDirectory([{"userName": GROUP, "nickName": "speaker"}], contacts=[metadata])
    assert read(backend(monkeypatch, edge))[0].get("display_name") == "资金群备注"


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_empty_contact_name_does_not_hide_matching_chatroom_name(monkeypatch, empty):
    edge = NativeDirectory([{"userName": GROUP, "nickName": "speaker"}],
                           contacts=[{"UserName": GROUP, "Remark": empty, "NickName": empty}],
                           rooms=[{"Name": GROUP, "NickName": "Greydan"}])
    assert read(backend(monkeypatch, edge))[0].get("display_name") == "Greydan"


@pytest.mark.parametrize("metadata", [[], [{"UserName": GROUP, "Remark": "", "NickName": ""}]])
def test_group_without_usable_metadata_falls_back_to_stable_id(monkeypatch, metadata):
    edge = NativeDirectory([{"userName": GROUP, "nickName": "灰灰"}], contacts=metadata)
    row = read(backend(monkeypatch, edge))[0]
    assert row.get("display_name") == GROUP
    assert row["nickName"] == "灰灰"


def test_fuzzy_foreign_id_and_missing_id_names_are_never_borrowed(monkeypatch):
    edge = NativeDirectory([{"userName": GROUP, "nickName": "speaker"}], fuzzy=True,
                           contacts=[{"UserName": "other@chatroom", "NickName": "Wrong"},
                                     {"NickName": "Unidentified"}],
                           rooms=[{"Name": "other@chatroom", "NickName": "Wrong"}])
    assert read(backend(monkeypatch, edge))[0].get("display_name") == GROUP


def test_same_name_different_ids_remain_separate_and_in_session_order(monkeypatch):
    ids = ["second@chatroom", GROUP]
    edge = NativeDirectory([{"userName": key, "nickName": "speaker"} for key in ids],
                           rooms=[{"Name": key, "NickName": "Shared"} for key in reversed(ids)])
    rows = read(backend(monkeypatch, edge))
    assert [(row["userName"], row.get("display_name")) for row in rows] == [(key, "Shared") for key in ids]


def test_name_change_refreshes_without_changing_native_id_or_preview(monkeypatch):
    edge = NativeDirectory([{"userName": GROUP, "nickName": "speaker"}],
                           rooms=[{"Name": GROUP, "NickName": "Old group"}])
    value = backend(monkeypatch, edge)
    before = read(value)[0]
    edge.rooms[0]["NickName"] = "New group"
    after = read(value)[0]
    assert (before.get("display_name"), after.get("display_name")) == ("Old group", "New group")
    assert before["userName"] == after["userName"] == GROUP
    assert before["nickName"] == after["nickName"] == "speaker"


def test_special_characters_are_displayed_literally(monkeypatch):
    name = '资金 & <群> "A" / α 💬'
    edge = NativeDirectory([{"userName": GROUP, "nickName": "speaker"}],
                           rooms=[{"Name": GROUP, "NickName": name}])
    assert read(backend(monkeypatch, edge))[0].get("display_name") == name


def test_session_keyword_limit_order_and_membership_are_preserved(monkeypatch):
    ids = ["keep-b@chatroom", "keep-a@chatroom", "keep-c@chatroom", "omit@chatroom"]
    edge = NativeDirectory([{"userName": key, "nickName": "speaker"} for key in ids],
                           rooms=[{"Name": key, "NickName": "Named " + key} for key in ids])
    rows = read(backend(monkeypatch, edge), keyword="keep", limit=2)
    assert [(row["userName"], row.get("display_name")) for row in rows] == [
        (key, "Named " + key) for key in ids[:2]]
    assert [(path, query) for path, query in edge.calls if path == "/api/v1/session"] == [
        ("/api/v1/session", {"keyword": "keep", "limit": 2})]


def test_metadata_beyond_default_page_can_resolve_requested_group(monkeypatch):
    rooms = [{"Name": f"filler-{i}@chatroom", "NickName": f"Filler {i}"} for i in range(201)]
    rooms.append({"Name": GROUP, "NickName": "Greydan"})
    edge = NativeDirectory([{"userName": GROUP, "nickName": "speaker"}], rooms=rooms)
    assert read(backend(monkeypatch, edge), limit=1)[0].get("display_name") == "Greydan"


def test_private_contact_prefers_current_remark_over_session_preview(monkeypatch):
    edge = NativeDirectory([{"userName": "wxid_peer", "nickName": "old nickname"}],
                           contacts=[{"UserName": "wxid_peer", "Remark": "交易联系人", "NickName": "New nickname"}])
    row = read(backend(monkeypatch, edge))[0]
    assert row.get("display_name") == "交易联系人"
    assert row["userName"] == "wxid_peer"
    assert row["nickName"] == "old nickname"


def test_empty_session_page_stays_empty_without_inventing_contacts(monkeypatch):
    edge = NativeDirectory([], contacts=[{"UserName": "wxid_peer", "NickName": "Contact only"}])
    assert read(backend(monkeypatch, edge)) == []


@pytest.mark.parametrize("failed_source", ["/api/v1/contact", "/api/v1/chatroom"])
@pytest.mark.parametrize("code", ["TIMEOUT", "NOT_READY"])
def test_one_metadata_source_failure_still_uses_the_other_source(monkeypatch, failed_source, code):
    from qtrade_winagent.errors import WaError

    edge = NativeDirectory([{"userName": GROUP, "nickName": "speaker"}],
                           contacts=[{"UserName": GROUP, "NickName": "Greydan"}],
                           rooms=[{"Name": GROUP, "NickName": "Greydan"}])

    def fetch(path, query, **kwargs):
        if path == failed_source:
            raise WaError(code, "synthetic metadata failure")
        return edge(path, query, **kwargs)

    assert read(backend(monkeypatch, fetch))[0].get("display_name") == "Greydan"


def test_both_metadata_failures_preserve_session_and_fall_back_to_id(monkeypatch):
    from qtrade_winagent.errors import WaError

    original = {"userName": GROUP, "nickName": "speaker", "content": "preview", "nOrder": 456}

    def fetch(path, query, **kwargs):
        if path == "/api/v1/session":
            return [dict(original)]
        raise WaError("TIMEOUT", "synthetic metadata timeout")

    row = read(backend(monkeypatch, fetch))[0]
    assert row == {**original, "display_name": GROUP}


def test_session_read_failure_is_not_converted_to_an_empty_success(monkeypatch):
    from qtrade_winagent.errors import WaError

    error = WaError("NOT_READY", "synthetic session failure")

    def fetch(path, query, **kwargs):
        assert path == "/api/v1/session"
        raise error

    with pytest.raises(WaError) as caught:
        read(backend(monkeypatch, fetch))
    assert caught.value is error


def test_metadata_multiple_pages_find_target_after_first_full_page(monkeypatch):
    rooms = [{"Name": f"filler-{i}@chatroom", "NickName": f"Filler {i}"} for i in range(1501)]
    rooms.append({"Name": GROUP, "NickName": "Greydan"})
    edge = NativeDirectory([{"userName": GROUP, "nickName": "speaker"}], rooms=rooms)
    assert read(backend(monkeypatch, edge), limit=1)[0].get("display_name") == "Greydan"


def test_metadata_ignoring_offset_stops_when_page_has_no_new_id(monkeypatch):
    calls = []

    def fetch(path, query, **kwargs):
        if path == "/api/v1/session":
            return [{"userName": GROUP, "nickName": "speaker"}]
        if path == "/api/v1/contact":
            return []
        calls.append(dict(query))
        assert len(calls) <= 3, "metadata pagination must stop rather than exhaust repeated pages"
        return [{"Name": f"other-{i}@chatroom", "NickName": "Other"}
                for i in range(int(query.get("limit", 100)))]

    assert read(backend(monkeypatch, fetch))[0].get("display_name") == GROUP
    assert len(calls) <= 2


def test_metadata_requests_share_bounded_deadline_and_keep_sessions(monkeypatch):
    from threading import local
    from types import SimpleNamespace
    from qtrade_winagent.win import wechat as module

    clock = local()
    starts = []
    original = {"userName": GROUP, "nickName": "speaker"}

    def monotonic():
        return getattr(clock, "elapsed", 0.0)

    def fetch(path, query, **kwargs):
        if path == "/api/v1/session":
            return [dict(original)]
        start = monotonic()
        timeout = kwargs.get("timeout_s")
        starts.append((start, timeout))
        assert timeout is not None and 0 < timeout <= 2.0
        assert start < 2.0, "no additional metadata request after enrichment budget expires"
        assert timeout <= 2.0 - start
        clock.elapsed = start + min(0.75, timeout)
        offset = int(query.get("offset", 0))
        return [{"UserName": f"other-{offset + i}", "NickName": "Other"}
                for i in range(int(query.get("limit", 100)))]

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=monotonic))
    assert read(backend(monkeypatch, fetch))[0] == {**original, "display_name": GROUP}
    assert starts
    assert all(start + min(0.75, timeout) <= 2.0 for start, timeout in starts)


def test_slow_metadata_headers_and_body_obey_total_deadline_and_release_resources(monkeypatch):
    """Real HTTP parsing over socketpair catches inactivity-only timeout guards."""
    import http.client
    import json
    import socket
    import threading
    import time

    stopped = threading.Event()
    clients, servers, responses, senders = [], [], [], []
    readers = set()
    requests = []
    original_begin = http.client.HTTPResponse.begin
    original_read = http.client.HTTPResponse.read

    def begin(response, *args, **kwargs):
        responses.append(response)
        readers.add(threading.current_thread())
        return original_begin(response, *args, **kwargs)

    def receive(response, *args, **kwargs):
        readers.add(threading.current_thread())
        return original_read(response, *args, **kwargs)

    def serve(server):
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                block = server.recv(4096)
                if not block:
                    return
                request += block
            target = request.split(b" ", 2)[1]
            requests.append(target)
            if target.startswith(b"/api/v1/contact"):
                # Each gap is shorter than the socket timeout. Header and body
                # phases together must still share one absolute deadline.
                chunks = [b"HTTP/1.1 200 OK\r\nContent-L", b"ength: 7\r\nX-Slow: ", b"x\r\n\r\n"]
                chunks.extend(bytes([value]) for value in b"[     ]")
                for chunk in chunks:
                    if stopped.wait(0.4):
                        return
                    server.sendall(chunk)
            else:
                if target.startswith(b"/api/v1/session"):
                    rows = [{"userName": GROUP, "nickName": "speaker"}]
                else:
                    assert target.startswith(b"/api/v1/chatroom")
                    rows = [{"Name": GROUP, "NickName": "Greydan"}]
                body = json.dumps(rows).encode("utf-8")
                server.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                               + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
        except OSError:
            # An absolute-deadline implementation closes the peer mid-stream.
            pass
        finally:
            server.close()

    def connect(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *args, **kwargs):
        assert address == ("127.0.0.1", 5030), "synthetic transport must not access another host"
        client, server = socket.socketpair()
        class PairClient(socket.socket):
            def setsockopt(self, level, option, *values):
                # Linux socketpair is AF_UNIX: TCP_NODELAY has no meaning here.
                if self.family == getattr(socket, "AF_UNIX", None) and (level, option) == (socket.IPPROTO_TCP, socket.TCP_NODELAY):
                    return
                return super().setsockopt(level, option, *values)
        client = PairClient(client.family, client.type, client.proto, fileno=client.detach())
        client.settimeout(timeout if isinstance(timeout, (int, float)) else 2.0)
        server.settimeout(5.0)
        clients.append(client)
        servers.append(server)
        sender = threading.Thread(target=serve, args=(server,), name="synthetic-chatlog", daemon=True)
        senders.append(sender)
        sender.start()
        return client

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(http.client.HTTPResponse, "begin", begin)
    monkeypatch.setattr(http.client.HTTPResponse, "read", receive)
    try:
        started = time.monotonic()
        rows = read(WinWeChat())
        elapsed = time.monotonic() - started
        assert elapsed < 2.8, f"metadata exceeded its 2s total budget: {elapsed:.3f}s"
        assert rows == [{"userName": GROUP, "nickName": "speaker", "display_name": "Greydan"}]
        assert any(target.startswith(b"/api/v1/contact") for target in requests)
        assert responses and all(response.isclosed() for response in responses)
        assert clients and all(client.fileno() == -1 for client in clients)
        assert readers and all(not reader.is_alive() for reader in readers)
    finally:
        stopped.set()
        for sock in clients + servers:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        for sender in senders:
            sender.join(timeout=1.0)
        assert all(not sender.is_alive() for sender in senders)
