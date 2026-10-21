"""Precise, synthetic startup classifications; no network or native processes."""
import importlib.util
import json
import socket
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location("portable_diagnostics",Path(__file__).resolve().parents[1]/"scripts/local_e2e/diagnostics.py")
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

@pytest.mark.parametrize("status,expected",[(401,"AGENT_AUTH_FAILED"),(403,"AGENT_FORBIDDEN"),(404,"AGENT_API_NOT_FOUND"),(503,"AGENT_NOT_READY")])
def test_http_errors_preserve_their_component_and_cause(status,expected):
    assert module.health_reason(status,{},False)==expected

@pytest.mark.parametrize("error,expected",[(TimeoutError("synthetic-secret"),"CONNECTION_TIMEOUT"),(ConnectionRefusedError(),"CONNECTION_REFUSED"),(PermissionError(),"ACCESS_DENIED"),(FileNotFoundError(),"FILE_MISSING"),(socket.gaierror(),"DNS_LOOKUP_FAILED")])
def test_transport_errors_are_not_reported_as_authentication(error,expected):
    assert module.exception_reason(error)==expected

def test_unknown_session_status_is_not_mislabeled_as_not_logged_in():
    health={"ok":True,"dockerd":True,"winagent":{"online":True,"user_agent":None}}
    assert module.health_reason(200,health,True)=="USER_AGENT_STATUS_UNKNOWN"

def test_source_version_mismatch_is_explicit_and_never_ready():
    health={"ok":True,"dockerd":True,"winagent":{"online":True,"user_agent":True}}
    assert module.health_reason(200,health,False)=="SOURCE_VERSION_MISMATCH"
    assert module.health_reason(200,health,True)==""
