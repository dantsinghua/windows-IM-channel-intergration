"""O-2(e2e-rootfs-2):WinAgent 自动发现的候选地址只收回环 / RFC1918 / 链路本地;公网地址一律丢弃、不发任何请求。

旧实现:无网关、无 host.json 时把 resolv.conf 的公网 DNS(如 ``223.5.5.5``)当「暂用首选项」,
此后带 ``Authorization: Bearer <winagent.token>`` 的请求以明文 HTTP 发往公网。
"""
from __future__ import annotations

import json
import logging

import pytest

from qtrade_agent.config import WinAgentConfig
from qtrade_agent.winagent_client import WinAgentClient, WinAgentUnavailable, base_url_candidates, resolve_base_url

TOKEN = "wa-secret-token-o2"


def _cfg(tmp_path, hint: dict | None = None) -> WinAgentConfig:
    hint_file = tmp_path / "host.json"
    if hint is not None:
        hint_file.write_text(json.dumps(hint), encoding="utf-8")
    tok = tmp_path / "winagent.token"
    tok.write_text(TOKEN, encoding="utf-8")
    return WinAgentConfig(url="", host_ip_hint_file=str(hint_file), token_file=str(tok))


def _resolv(tmp_path, *ns: str) -> str:
    p = tmp_path / "resolv.conf"
    p.write_text("".join(f"nameserver {n}\n" for n in ns), encoding="utf-8")
    return str(p)


def _recorder():
    calls: list[tuple[str, str, dict]] = []

    async def transport(method, url, headers, body, timeout_s):
        calls.append((method, url, dict(headers)))
        raise OSError("unreachable")

    return calls, transport


@pytest.mark.parametrize("ns", ["223.5.5.5", "8.8.8.8", "1.1.1.1", "100.100.2.136", "2001:4860:4860::8888"])
async def test_public_resolv_only_yields_no_candidate_and_no_request(tmp_path, ns, caplog):
    caplog.set_level(logging.WARNING, logger="qtrade.winagent")
    cfg = _cfg(tmp_path)
    rc = _resolv(tmp_path, ns)
    assert base_url_candidates(cfg, resolv_conf=rc, gateway=lambda: None) == []
    assert resolve_base_url(cfg, resolv_conf=rc, gateway=lambda: None) is None
    calls, transport = _recorder()
    cl = WinAgentClient(cfg, transport=transport, resolv_conf=rc, gateway=lambda: None)
    assert await cl.discover() is None
    assert cl.base_url is None
    assert await cl.ping() is None and await cl.health() is None               # 既有降级:不可达即 None
    with pytest.raises(WinAgentUnavailable) as ei:
        await cl.request("GET", "/wa/v1/time")
    assert ei.value.reason == "no_address"                                     # 既有原因码,不新造
    assert calls == []                                                         # 一个请求都没发出
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("hint", [{"host_ip": "203.0.113.7"}, {"winagent_base_url": "http://198.51.100.9:17610"},
                                  {"winagent_base_url": "http://evil.example.com:17610"}])
def test_public_or_hostname_hint_and_gateway_rejected(tmp_path, hint):
    cfg = _cfg(tmp_path, hint)
    rc = _resolv(tmp_path, "8.8.4.4")
    assert base_url_candidates(cfg, resolv_conf=rc, gateway=lambda: "52.1.2.3") == []


def test_local_ranges_kept_public_dropped_order_preserved(tmp_path):
    cfg = _cfg(tmp_path, {"host_ip": "8.8.8.8"})
    rc = _resolv(tmp_path, "10.255.255.254")
    assert base_url_candidates(cfg, resolv_conf=rc, gateway=lambda: "172.23.16.1") == [
        ("gateway", "http://172.23.16.1:17610"), ("resolv_conf", "http://10.255.255.254:17610")]
    for ok in ("127.0.0.1", "10.0.0.1", "172.16.0.1", "172.31.255.254", "192.168.3.1", "169.254.1.2"):
        assert base_url_candidates(_cfg(tmp_path), resolv_conf=_resolv(tmp_path), gateway=lambda ok=ok: ok) == [
            ("gateway", f"http://{ok}:17610")]
    for bad in ("172.32.0.1", "172.15.255.255", "11.0.0.1", "0.0.0.0", "255.255.255.255"):
        assert base_url_candidates(_cfg(tmp_path), resolv_conf=_resolv(tmp_path), gateway=lambda bad=bad: bad) == []


async def test_public_candidate_skipped_local_one_used(tmp_path):
    """公网候选排在前面也不会被探:探活请求只打到私网候选。"""
    cfg = _cfg(tmp_path, {"host_ip": "223.5.5.5"})
    calls, transport = _recorder()
    cl = WinAgentClient(cfg, transport=transport, resolv_conf=_resolv(tmp_path, "8.8.8.8"), gateway=lambda: "172.20.0.5")
    assert await cl.discover() == "http://172.20.0.5:17610"
    assert calls and all(u.startswith("http://172.20.0.5:17610/") for _m, u, _h in calls)


def test_explicit_url_not_filtered(tmp_path):
    """``[winagent] url`` 是运维显式配置(不是自动发现的候选),原样采用。"""
    cfg = WinAgentConfig(url="http://winagent.lan:17610/", host_ip_hint_file=str(tmp_path / "none.json"))
    assert base_url_candidates(cfg, resolv_conf=_resolv(tmp_path, "8.8.8.8")) == [("url", "http://winagent.lan:17610")]
