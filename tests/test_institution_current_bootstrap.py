"""Current institutional collector never imports or loads a retired transport."""
import asyncio
import builtins
import json
from types import SimpleNamespace
import pytest
from qtrade_agent.app import AgentApp
from tests.test_api_ext import Rig

RETIRED = {"qtrade_agent.ossbox." + name for name in
    ("settings", "relay_settings", "oss_http", "sts", "inbox", "catalog_publish")}


def test_current_bootstrap_uses_institution_network_without_legacy_loader(tmp_path, monkeypatch):
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    monkeypatch.setenv("XUNJIA_INSTITUTION_COLLECTOR", "1")
    network = tmp_path / "institution.json"
    network.write_text(json.dumps({"hostname": "relay.synthetic.invalid", "addresses": ["192.0.2.5"]}))
    monkeypatch.setenv("XUNJIA_INSTITUTION_CONFIG", str(network))
    monkeypatch.setenv("XUNJIA_MQ_CONNECTION_CONFIG", str(tmp_path / "must-not-read-legacy.json"))
    original = builtins.__import__
    def importing(name, *args, **kwargs):
        assert name not in RETIRED, "retired transport import: " + name
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", importing)
    rig = Rig(tmp_path)
    try:
        app = rig.agent
        for name in ("relay_settings", "xunjia_inbox", "_xunjia_oss_box", "catalog_publisher", "oss_settings"):
            assert not hasattr(app, name)
        assert app.collector_reporting_settings.view()["base_url"]=="https://its-relay.yingmi.cn"
        assert 'hostname' not in app.collector_reporting_settings._seed
        assert 'addresses' not in app.collector_reporting_settings._seed
        assert app.collector_reporting_settings.saved() is None
        assert not app._started and not app.scheduler._started
    finally:
        rig.close()


def test_retired_oss_reload_entrypoint_is_absent():
    assert not hasattr(AgentApp, "_configure_saved_ossbox")


@pytest.mark.parametrize("institution", ["0", "1"])
def test_organization_reference_is_account_scoped_and_ignores_retired_source(tmp_path, monkeypatch, institution):
    monkeypatch.setenv("XUNJIA_INSTITUTION_COLLECTOR", institution)
    monkeypatch.setenv("XUNJIA_COLLECTOR_CONFIG_FILE", "retired-transport.json")
    app = SimpleNamespace(data_dir=str(tmp_path))
    assert AgentApp._xunjia_org_config(app, "10001") == str(tmp_path / "organization/10001.json")
    assert AgentApp._xunjia_org_config(app, "10002") == str(tmp_path / "organization/10002.json")
