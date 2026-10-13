"""SCM lifecycle tests inject the entire Windows framework; no service is touched."""
from types import SimpleNamespace
import sys
from unittest.mock import Mock

import pytest

from qtrade_winagent import service_host


STATUS = SimpleNamespace(SERVICE_START_PENDING=2, SERVICE_RUNNING=4, SERVICE_STOP_PENDING=3)


class Framework:
    def __init__(self, args):
        self.args = args
        self.statuses = []

    def ReportServiceStatus(self, status, **kwargs):
        self.statuses.append(status)


def test_scm_reports_ready_only_after_application_boot_and_propagates_stop():
    options = SimpleNamespace(root="isolated-root")
    def run(received, stopped, ready):
        assert received is options
        assert svc.statuses == [STATUS.SERVICE_START_PENDING]
        assert not stopped.is_set()
        ready()
        assert svc.statuses[-1] == STATUS.SERVICE_RUNNING
        svc.SvcStop()
        assert stopped.is_set()
        assert svc.statuses[-1] == STATUS.SERVICE_STOP_PENDING
        return 0
    cls = service_host.create_service_class(options, runner=run, framework=Framework, service_constants=STATUS)
    svc = cls(["QTradeWinAgent"])
    svc.SvcRun()
    assert svc.statuses.count(STATUS.SERVICE_RUNNING) == 1


def test_scm_stop_before_ready_does_not_publish_running_later():
    def run(_options, stopped, ready):
        svc.SvcStop()
        ready()
        assert stopped.is_set()
        return 0
    cls = service_host.create_service_class({}, runner=run, framework=Framework, service_constants=STATUS)
    svc = cls([])
    svc.SvcRun()
    assert STATUS.SERVICE_RUNNING not in svc.statuses


def test_boot_failure_escapes_to_scm_without_claiming_running():
    cls = service_host.create_service_class({}, runner=lambda *_: 1, framework=Framework, service_constants=STATUS)
    svc = cls([])
    with pytest.raises(RuntimeError):
        svc.SvcRun()
    assert STATUS.SERVICE_RUNNING not in svc.statuses


def test_dispatch_uses_windows_service_dispatcher_not_foreground_uvicorn(monkeypatch):
    events = []
    manager = SimpleNamespace(
        Initialize=lambda: events.append("initialize"),
        PrepareToHostSingle=lambda cls: events.append(cls._svc_name_),
        StartServiceCtrlDispatcher=lambda: events.append("dispatch"),
    )
    monkeypatch.setitem(sys.modules, "servicemanager", manager)
    monkeypatch.setitem(sys.modules, "win32service", STATUS)
    monkeypatch.setitem(sys.modules, "win32serviceutil", SimpleNamespace(ServiceFramework=Framework))
    runner = Mock()
    assert service_host.dispatch({}, runner) == 0
    assert events == ["initialize", "QTradeWinAgent", "dispatch"]
    runner.assert_not_called()
