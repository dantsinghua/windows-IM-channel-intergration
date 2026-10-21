"""Production AgentApp assembly tested with all external backends synthetic."""
import asyncio
from types import SimpleNamespace

import pytest

from conftest import make_rig
from qtrade_agent.ossbox.oss_http import OssV1Box
from qtrade_agent.ossbox.catalog_publish import CatalogPublisher
from qtrade_agent.ossbox.xunjia_v22 import XunjiaV22Host
from qtrade_agent.app import XunjiaInbox


def isolate_boxes(monkeypatch):
    box = object()
    monkeypatch.setattr(OssV1Box, "from_env", classmethod(lambda cls: box))
    monkeypatch.setattr(CatalogPublisher, "from_env", classmethod(lambda cls, *args, **kwargs: None))
    def legacy(*args, **kwargs):
        raise AssertionError("V2.2 must never construct the legacy command consumer")
    monkeypatch.setattr(XunjiaInbox, "from_env", legacy)
    return box


@pytest.mark.parametrize("version", ["2.2", "invalid-version"])
def test_real_app_open_never_installs_v1_when_v2_selected_or_version_invalid(tmp_path, monkeypatch, version):
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", version)
    isolate_boxes(monkeypatch)
    rig = make_rig(tmp_path)
    try:
        assert rig.agent.xunjia_inbox is None
        assert rig.agent._xunjia_version == ("2.2" if version == "2.2" else "disabled")
    finally:
        asyncio.run(rig.agent.stop())


def test_real_app_assembles_host_event_hook_and_shutdown_without_starting_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    isolate_boxes(monkeypatch)
    calls = []
    class Host:
        binding = SimpleNamespace(workbench_id="synthetic-wb")
        async def start(self):
            calls.append("host-start")
        async def stop(self):
            calls.append("host-stop")
        def observe(self, **kwargs):
            calls.append("host-observe")
    host = Host()
    def factory(cls, **kwargs):
        assert kwargs["legacy_inbox_disabled"]() is True
        calls.append("host-configure")
        return host
    monkeypatch.setattr(XunjiaV22Host, "from_env", classmethod(factory))
    async def run():
        rig = make_rig(tmp_path)
        try:
            assert rig.agent._started is False
            await rig.agent._configure_xunjia_v22()
            assert rig.agent.xunjia_v22_host is host and rig.agent.xunjia_inbox is None
            assert calls == ["host-configure", "host-start"]
            rig.agent.webhooks.fanout = lambda **kwargs: calls.append("webhook-fanout")
            rig.agent.events.emit("message", payload={"text": "synthetic event"}, account_id="qd90", channel="qidian")
            assert calls[-2:] == ["host-observe", "webhook-fanout"]
            assert rig.agent._started is False, "This test must not start scheduler or account recovery"
        finally:
            await rig.agent.stop()
        assert calls[-1] == "host-stop"
    asyncio.run(run())


def test_v2_configuration_failure_does_not_fall_back_to_legacy_inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    isolate_boxes(monkeypatch)
    def fail(cls, **kwargs):
        raise RuntimeError("synthetic protocol package unavailable")
    monkeypatch.setattr(XunjiaV22Host, "from_env", classmethod(fail))
    async def run():
        rig = make_rig(tmp_path)
        try:
            await rig.agent._configure_xunjia_v22()
            assert rig.agent.xunjia_inbox is None and rig.agent.xunjia_v22_host is None
            assert rig.agent.oss_start_error == "xunjia_v22_configuration_unavailable"
        finally:
            await rig.agent.stop()
    asyncio.run(run())


def test_saved_oss_profile_reload_still_cannot_enable_v1_under_v2(tmp_path, monkeypatch):
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    box = isolate_boxes(monkeypatch)
    async def run():
        rig = make_rig(tmp_path)
        async def saved_runtime():
            return {"workbench_id": "synthetic-wb", "revision": "synthetic-revision"}, box
        try:
            rig.agent.oss_settings.has_saved = lambda: True
            rig.agent.oss_settings.runtime = saved_runtime
            await rig.agent._configure_saved_ossbox()
            assert rig.agent.xunjia_inbox is None
            assert rig.agent._xunjia_oss_box is box
        finally:
            await rig.agent.stop()
    asyncio.run(run())
