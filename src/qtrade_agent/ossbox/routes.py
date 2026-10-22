"""Administrator-only configuration, authentication and local directory preview."""
from fastapi import Request

from ..api.auth import ApiError
from .errors import OssboxSettingsError


def register_ossbox_settings(app, *, agent, prefix, principal):
    def settings(request):
        if "contract" in request.query_params:
            raise OssboxSettingsError("reporting_contract_invalid")
        active = getattr(agent, "collector_reporting_settings", None)
        if active is None:
            raise OssboxSettingsError("collector_not_configured", unavailable=True)
        return active

    async def view(active):
        from .institution import InstitutionCollectorHost

        public = await active.view_async()
        saved = active.saved()
        host = getattr(agent, "xunjia_v22_host", None)
        status = host.status() if isinstance(host, InstitutionCollectorHost) else {}
        same = bool(status) and (saved is None or saved["revision"] == status.get("applied_revision"))
        applied = bool(status.get("active") and same)
        return {"ok": True, "group": "ossbox", "mode": "its_relay", "relay": public,
                "state": active.state, "applied": applied, "restart_required": bool(status and not same),
                "inquiry_configured": applied, "catalog_configured": applied, "institution_runtime": status}

    def failure(exc):
        if exc.unavailable:
            message = "认证配置或服务暂不可用，请检查配置后重试"
            if exc.reason == "file_auth_rejected":
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
            return await view(settings(request))
        except OssboxSettingsError as exc:
            raise failure(exc) from None

    @app.put(prefix + "/settings/ossbox")
    async def put_ossbox(request: Request):
        actor = principal(request, "admin").actor
        value = await body(request)
        try:
            active = settings(request)
            await active.save(value, actor=actor)
            if getattr(agent, "_started", False) and getattr(agent, "xunjia_v22_host", None) is None:
                await agent._configure_xunjia_v22()
            return await view(active)
        except OssboxSettingsError as exc:
            raise failure(exc) from None

    @app.post(prefix + "/settings/ossbox/test")
    async def test_ossbox(request: Request):
        principal(request, "admin")
        value = await body(request)
        try:
            return {"ok": True, **await settings(request).test(value)}
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
