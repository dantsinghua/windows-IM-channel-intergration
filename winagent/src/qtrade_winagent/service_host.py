"""SCM hosting; importing this module never registers or starts a service."""
from __future__ import annotations

import threading
from typing import Any, Callable


def create_service_class(options: Any, *, runner: Callable, framework: type,
                         service_constants: Any) -> type:
    class QTradeService(framework):
        _svc_name_ = "QTradeWinAgent"
        _svc_display_name_ = "QTrade WinAgent"

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = threading.Event()
            self._status_lock = threading.Lock()
            self._status = service_constants.SERVICE_START_PENDING

        def _report(self, status):
            with self._status_lock:
                self._status = status
                self.ReportServiceStatus(status, waitHint=30000)

        def SvcStop(self):
            with self._status_lock:
                self.stop_event.set()
                self._status = service_constants.SERVICE_STOP_PENDING
                self.ReportServiceStatus(self._status, waitHint=30000)

        def SvcShutdown(self):
            self.SvcStop()

        def SvcInterrogate(self):
            with self._status_lock:
                self.ReportServiceStatus(self._status, waitHint=30000)

        def SvcRun(self):
            self._report(service_constants.SERVICE_START_PENDING)

            def ready():
                with self._status_lock:
                    if not self.stop_event.is_set():
                        self._status = service_constants.SERVICE_RUNNING
                        self.ReportServiceStatus(self._status, waitHint=30000)

            finished = threading.Event()

            def checkpoints():
                while not finished.wait(5):
                    with self._status_lock:
                        if self._status in (service_constants.SERVICE_START_PENDING,
                                            service_constants.SERVICE_STOP_PENDING):
                            self.ReportServiceStatus(self._status, waitHint=30000)

            heartbeat = threading.Thread(target=checkpoints, name="scm-checkpoint", daemon=True)
            heartbeat.start()
            try:
                result = runner(options, self.stop_event, ready)
            finally:
                finished.set()
                heartbeat.join()
            if result:
                raise RuntimeError("WinAgent service initialization failed")
            self._report(service_constants.SERVICE_STOP_PENDING)
            # pywin32 reports STOPPED when this method returns, and reports
            # a nonzero service exit code if an exception escapes.

    return QTradeService


def dispatch(options: Any, runner: Callable) -> int:
    import servicemanager
    import win32service
    import win32serviceutil

    service_class = create_service_class(options, runner=runner,
                                        framework=win32serviceutil.ServiceFramework,
                                        service_constants=win32service)
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(service_class)
    servicemanager.StartServiceCtrlDispatcher()
    return 0
