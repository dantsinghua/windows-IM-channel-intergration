"""Native login, relay heartbeat and reset state are independent facts."""
import asyncio
from types import SimpleNamespace
from qtrade_agent.ossbox.institution import InstitutionCollectorHost
from relay.file_gateway import FileGatewayError


def host():
    h = object.__new__(InstitutionCollectorHost)
    native = SimpleNamespace(_active=True, _intake_ok=True,
        get_session_reset_status=lambda aid: {"available": True, "account_id": aid, "reset": None})
    h.hosts = {("qd01", "10001"): native}
    h.clock = lambda: 2000
    h.last_directory_at = 1800
    h.presence = {("qd01", "10001"): {"state": "online", "confirmed_at": 1999, "checked_at": 1999}}
    h.errors = {"directory": "FILE_GATEWAY_HTTP_ERROR (HTTP 502)", "contacts:other": "PRIVATE_OTHER_ERROR"}
    return h


def test_gateway_outage_does_not_become_native_logout_or_reset():
    h = host(); status = h.get_session_reset_status("qd01")
    assert status["reset"] is None
    channel = status["channel_status"]
    assert channel["native_state"] == "online" and not channel["heartbeat_fresh"]
    assert channel["errors"] == {"directory": "FILE_GATEWAY_HTTP_ERROR (HTTP 502)"}
    assert "PRIVATE_OTHER_ERROR" not in str(status)


def test_expired_native_observation_is_not_reported_as_online():
    h = host(); h.presence[("qd01", "10001")]["confirmed_at"] = 1900
    assert h.get_session_reset_status("qd01")["channel_status"]["native_state"] == "unknown"


def test_new_heartbeat_recovers_status_without_creating_reset():
    h = host(); h.errors = {}; h.last_directory_at = 1995
    state = h.get_session_reset_status("qd01")
    assert state["channel_status"]["heartbeat_fresh"] and state["channel_status"]["intake_ready"]
    assert state["reset"] is None


def test_directory_upload_preserves_safe_http_failure_detail():
    async def run():
        h = host(); h.active = True; h._wake = asyncio.Event()
        async def reconcile(): h.active = False
        h.reconcile_accounts = reconcile
        h.settings = SimpleNamespace(view=lambda: {"workbench_name": "Synthetic"})
        h.directory_rows = lambda: []
        def fail(rows): raise FileGatewayError("FILE_GATEWAY_HTTP_ERROR", status=502, ambiguous=True)
        h.publisher = SimpleNamespace(directory=fail, name="Synthetic")
        await h._presence_loop()
        assert h.errors["directory"] == "FILE_GATEWAY_HTTP_ERROR (HTTP 502)"
        assert h.last_directory_at == 1800
    asyncio.run(run())
