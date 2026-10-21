"""Safe diagnostic codes: transport, authentication, and readiness are distinct."""
from __future__ import annotations

import json
import socket

class StartupFailure(RuntimeError):
    def __init__(self, reason: str, stage: str, *, native_code: int = 0, http_status: int = 0):
        super().__init__(f"{stage}: {reason}")
        self.reason = reason
        self.stage = stage
        self.native_code = native_code
        self.http_status = http_status

def http_reason(status: int, component: str) -> str:
    return {401: f"{component}_AUTH_FAILED", 403: f"{component}_FORBIDDEN",
            404: f"{component}_API_NOT_FOUND", 429: f"{component}_OVERLOADED",
            503: f"{component}_NOT_READY"}.get(status, f"{component}_HTTP_{status}")

def exception_reason(error: Exception) -> str:
    if isinstance(error, StartupFailure):
        return error.reason
    if isinstance(error, json.JSONDecodeError) or isinstance(error, UnicodeError):
        return "RESPONSE_INVALID"
    if isinstance(error, FileNotFoundError):
        return "FILE_MISSING"
    if isinstance(error, PermissionError):
        return "ACCESS_DENIED"
    if isinstance(error, TimeoutError):
        return "CONNECTION_TIMEOUT"
    if isinstance(error, ConnectionRefusedError):
        return "CONNECTION_REFUSED"
    if isinstance(error, socket.gaierror):
        return "DNS_LOOKUP_FAILED"
    native = getattr(error, "winerror", None) or getattr(error, "errno", 0)
    return {5: "ACCESS_DENIED", 126: "NATIVE_DEPENDENCY_MISSING",
            193: "EXECUTABLE_ARCHITECTURE_INVALID", 1223: "UAC_CANCELLED",
            10048: "PORT_IN_USE", 10061: "CONNECTION_REFUSED",
            10060: "CONNECTION_TIMEOUT"}.get(native, "STARTUP_OPERATION_FAILED")

def health_reason(status: int, health: object, version_matches: bool) -> str:
    if status != 200:
        return http_reason(status, "AGENT")
    if not isinstance(health, dict) or health.get("ok") is not True:
        return "AGENT_HEALTH_INVALID"
    wa = health.get("winagent")
    if not isinstance(wa, dict):
        return "WINAGENT_STATUS_UNKNOWN"
    if wa.get("online") is not True:
        return "WINAGENT_UNREACHABLE"
    if wa.get("user_agent") is None:
        return "USER_AGENT_STATUS_UNKNOWN"
    if wa.get("user_agent") is not True:
        return "USER_AGENT_NOT_READY"
    if health.get("dockerd") is not True:
        return "DOCKER_NOT_READY"
    if not version_matches:
        return "SOURCE_VERSION_MISMATCH"
    return ""
