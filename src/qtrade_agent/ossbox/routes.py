"""Administrator-only configuration, authentication and local directory preview."""
import os
from fastapi import Request

from ..api.auth import ApiError
from .settings import OssboxSettingsError


def register_ossbox_settings(app, *, agent, prefix, principal):
    def settings(body=None, contract=None):
        local = getattr(agent, "collector_reporting_settings", None)
        legacy = getattr(agent, "relay_settings", None)
        if contract not in (None, "legacy", "collector"):
            raise OssboxSettingsError("reporting_contract_invalid")
        if contract == "legacy":
            if legacy is None or getattr(legacy, "config_file", None) is None:
                raise OssboxSettingsError("relay_not_configured", unavailable=True)
            return legacy
        if contract == "collector":
            if local is None:
                raise OssboxSettingsError("collector_not_configured", unavailable=True)
            return local
        if local is not None:
            if (os.environ.get("XUNJIA_INSTITUTION_COLLECTOR") == "1"
                    or isinstance(body, dict) and bool({"workbench_name", "base_url"} & body.keys())):
                return local
            # Saving an independent draft never captures an active legacy
            # client's GET or two-key rotation. Explicit selectors keep both
            # editors reachable without changing the running host.
            if (legacy is not None and getattr(legacy, "config_file", None) is not None
                    and getattr(agent, "xunjia_v22_host", None) is not None):
                return legacy
            if local.saved() is not None or legacy is None or getattr(legacy, "config_file", None) is None:
                return local
        return legacy or agent.oss_settings

    async def view(active=None):
        active = settings() if active is None else active
        legacy = getattr(agent, "relay_settings", None)
        metadata = {"legacy_available": legacy is not None and getattr(legacy, "config_file", None) is not None}
        public = await active.view_async() if hasattr(active, "view_async") else active.view()
        saved = active.saved()
        local = active is getattr(agent, "collector_reporting_settings", None)
        if local and os.environ.get("XUNJIA_INSTITUTION_COLLECTOR")=="1":
            from .institution import InstitutionCollectorHost
            host=getattr(agent,"xunjia_v22_host",None)
            status=host.status() if isinstance(host,InstitutionCollectorHost) else {}
            same=bool(status) and (saved is None or saved["revision"]==status.get("applied_revision"))
            applied=bool(status.get("active") and same)
            return {**metadata,"ok":True,"group":"ossbox","mode":"its_relay","relay":public,
                    "state":active.state,"applied":applied,"restart_required":bool(status and not same),
                    "inquiry_configured":applied,"catalog_configured":applied,"institution_runtime":status}
        if local:
            return {**metadata, "ok": True, "group": "ossbox", "mode": "its_relay", "relay": public,
                    "state": active.state, "applied": False, "restart_required": False,
                    "inquiry_configured": False, "catalog_configured": False}
        relay = active is getattr(agent, "relay_settings", None)
        result = {**metadata, "ok": True, "group": "ossbox",
                "restart_required": bool(saved and saved["revision"] != (
                    active.applied_revision or getattr(active, "baseline_revision", None))),
                "inquiry_configured": agent.xunjia_inbox is not None or getattr(agent, 'xunjia_v22_host', None) is not None,
                "catalog_configured": agent.catalog_publisher is not None or getattr(agent, 'xunjia_v22_host', None) is not None}
        if relay:
            result.update(mode="its_relay", relay=public,
                          applied=bool(saved and saved["revision"] == active.applied_revision))
        else:
            result["data"] = public
        return result

    def failure(exc):
        if exc.unavailable:
            message = "认证配置或服务暂不可用，请检查配置后重试"
            if exc.reason in ("sts_http_401", "file_auth_rejected"):
                message = "认证失败，请核对 key、sm4_key 和本机时间"
            return ApiError(503, "NOT_READY", message, reason=exc.reason, retryable=True)
        return ApiError(400, "INVALID_ARGS", "请检查认证配置字段", reason=exc.reason)

    async def body(request):
        try:
            value = await request.json()
        except ValueError:
            raise ApiError(400, "INVALID_ARGS", "请求体须为 JSON 对象", reason="bad_body") from None
        if not isinstance(value, dict):
            raise ApiError(400, "INVALID_ARGS", "请求体须为 JSON 对象", reason="bad_body")
        return value

    @app.get(prefix + "/settings/ossbox")
    async def get_ossbox(request: Request):
        principal(request, "admin")
        try:
            return await view(settings(contract=request.query_params.get("contract")))
        except OssboxSettingsError as exc:
            raise failure(exc) from None

    @app.put(prefix + "/settings/ossbox")
    async def put_ossbox(request: Request):
        actor = principal(request, "admin").actor
        value = await body(request)
        try:
            active = settings(value, request.query_params.get("contract"))
            await active.save(value, actor=actor)
            if active is getattr(agent,"collector_reporting_settings",None) and os.environ.get("XUNJIA_INSTITUTION_COLLECTOR")=="1" and getattr(agent,"_started",False) and getattr(agent,"xunjia_v22_host",None) is None:
                await agent._configure_xunjia_v22()
            return await view(active)
        except OssboxSettingsError as exc:
            raise failure(exc) from None

    @app.post(prefix + "/settings/ossbox/test")
    async def test_ossbox(request: Request):
        principal(request, "admin")
        value = await body(request)
        try:
            return {"ok": True, **await settings(value, request.query_params.get("contract")).test(value)}
        except OssboxSettingsError as exc:
            raise failure(exc) from None

    @app.get(prefix + "/collector/catalog")
    async def collector_catalog(request: Request):
        principal(request, "admin")
        active = getattr(agent, "collector_reporting_settings", None)
        if active is None:
            raise ApiError(503, "NOT_READY", "采集端目录尚未配置", reason="collector_not_configured")
        try:
            return {"ok": True, "data": active.catalog()}
        except OssboxSettingsError as exc:
            raise failure(exc) from None
