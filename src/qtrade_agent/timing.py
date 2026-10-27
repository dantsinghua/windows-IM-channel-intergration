"""Content-free local phase timing; diagnostic failures never affect execution."""
from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar

log = logging.getLogger("qtrade.timing")
_identity = ContextVar("qtrade_timing_identity", default=("", ""))


def _emit(data):
    try:
        log.info("chain_timing %s", json.dumps(data, separators=(",", ":")))
    except Exception:
        # Diagnostics cannot change native send or confirmation outcomes.
        pass


@contextmanager
def timing_context(trace_id, account_id):
    token = _identity.set((trace_id or "", account_id or ""))
    try:
        yield
    finally:
        _identity.reset(token)


def timing_event(stage, *, status, trace_id=None, account_id=None):
    trace, account = _identity.get()
    _emit({"stage": stage, "phase": "event", "status": status,
           "trace_id": trace if trace_id is None else trace_id,
           "account_id": account if account_id is None else account_id})


@contextmanager
def timing_span(stage, *, trace_id=None, account_id=None):
    trace, account = _identity.get()
    data = {"stage": stage, "trace_id": trace if trace_id is None else trace_id,
            "account_id": account if account_id is None else account_id}
    started = time.monotonic()
    _emit({**data, "phase": "start", "status": "started"})
    try:
        yield data
    finally:
        error_type = sys.exc_info()[0]
        result = {**data, "phase": "end", "status": "returned",
                  "elapsed_ms": round(max(0.0, time.monotonic() - started) * 1000, 3)}
        if error_type is not None:
            result.update(status="error", error_type=error_type.__name__)
        _emit(result)
