"""Current AgentApp assembly with injected host; no service, account or network."""
import asyncio
from types import SimpleNamespace
import pytest
from conftest import make_rig
from qtrade_agent.ossbox.institution import InstitutionCollectorHost


@pytest.mark.parametrize("version", ["1", "2.2", "invalid-version"])
def test_open_cannot_install_retired_transport_for_any_version(tmp_path, monkeypatch, version):
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", version)
    monkeypatch.setenv("OSS_ENDPOINT", "https://obsolete.synthetic.invalid")
    monkeypatch.setenv("OSS_BUCKET", "obsolete-synthetic-bucket")
    rig = make_rig(tmp_path)
    try:
        for name in ("xunjia_inbox", "catalog_publisher", "_xunjia_oss_box", "relay_settings", "oss_settings"):
            assert not hasattr(rig.agent, name)
        assert rig.agent.xunjia_v22_host is None
        assert not rig.agent._started and not rig.agent.scheduler._started
    finally:
        asyncio.run(rig.agent.stop())


def test_current_host_receives_event_hook_and_shutdown_without_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    monkeypatch.setenv("XUNJIA_INSTITUTION_COLLECTOR", "1")
    calls = []
    class Host(InstitutionCollectorHost):
        def __init__(self): pass
        async def start(self): calls.append("host-start")
        async def stop(self): calls.append("host-stop")
        def observe(self, **kwargs): calls.append("host-observe")
    host = Host()
    async def factory(cls, settings, **kwargs):
        assert kwargs["legacy_inbox_disabled"]() is True
        assert "box" not in kwargs
        assert kwargs["store"] is settings.store
        calls.append("host-configure")
        return host
    monkeypatch.setattr(InstitutionCollectorHost, "from_settings", classmethod(factory))
    async def run():
        rig = make_rig(tmp_path)
        try:
            await rig.agent._configure_xunjia_v22()
            assert rig.agent.xunjia_v22_host is host
            assert calls == ["host-configure", "host-start"]
            rig.agent.webhooks.fanout = lambda **kwargs: calls.append("webhook-fanout")
            rig.agent.events.emit("message", payload={"text": "synthetic event"}, account_id="qd90", channel="qidian")
            assert calls[-2:] == ["host-observe", "webhook-fanout"]
            assert not rig.agent._started and not rig.agent.scheduler._started
        finally:
            await rig.agent.stop()
        assert calls[-1] == "host-stop"
    asyncio.run(run())


def test_current_factory_failure_does_not_start_any_fallback(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    monkeypatch.setenv("XUNJIA_INSTITUTION_COLLECTOR", "1")
    async def fail(cls, settings, **kwargs):
        raise RuntimeError("synthetic-private-upstream-canary")
    monkeypatch.setattr(InstitutionCollectorHost, "from_settings", classmethod(fail))
    async def run():
        rig = make_rig(tmp_path)
        try:
            await rig.agent._configure_xunjia_v22()
            assert rig.agent.xunjia_v22_host is None
            assert rig.agent.oss_start_error == "institution_collector_configuration_unavailable"
            assert rig.agent.collector_reporting_settings.state == "error"
            assert "synthetic-private-upstream-canary" not in caplog.text
            assert not rig.agent._started and not rig.agent.scheduler._started
        finally:
            await rig.agent.stop()
    asyncio.run(run())


def test_disabled_current_host_never_falls_back_to_retired_env_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    monkeypatch.setenv("XUNJIA_INSTITUTION_COLLECTOR", "0")
    async def forbidden(*args, **kwargs): pytest.fail("disabled host must not be assembled")
    monkeypatch.setattr(InstitutionCollectorHost, "from_settings", forbidden)
    async def run():
        rig = make_rig(tmp_path)
        try:
            await rig.agent._configure_xunjia_v22()
            assert rig.agent.xunjia_v22_host is None
            assert not rig.agent._started and not rig.agent.scheduler._started
        finally:
            await rig.agent.stop()
    asyncio.run(run())
