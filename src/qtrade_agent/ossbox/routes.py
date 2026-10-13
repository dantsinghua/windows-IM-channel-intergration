"""Administrator-only settings and a read-only STS authentication check."""
from fastapi import Request

from ..api.auth import ApiError
from .settings import OssboxSettingsError


def register_ossbox_settings(app, *, agent, prefix, principal):
    def view():
        settings = agent.oss_settings
        saved = settings.saved()
        return {"ok": True, "group": "ossbox", "data": settings.view(),
                "restart_required": bool(saved and saved["revision"] != settings.applied_revision),
                "inquiry_configured": agent.xunjia_inbox is not None,
                "catalog_configured": agent.catalog_publisher is not None}

    def failure(exc):
        if exc.unavailable:
            message = "认证配置或服务暂不可用，请检查配置后重试"
            if exc.reason == "sts_http_401":
                message = "认证失败，请核对业务口令、SM4 密钥和本机时间"
            return ApiError(503, "NOT_READY", message, reason=exc.reason, retryable=True)
        return ApiError(400, "INVALID_ARGS", "请检查认证地址、密钥及对象存储配置", reason=exc.reason)

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
            return view()
        except OssboxSettingsError as exc:
            raise failure(exc) from None

    @app.put(prefix + "/settings/ossbox")
    async def put_ossbox(request: Request):
        actor = principal(request, "admin").actor
        value = await body(request)
        try:
            await agent.oss_settings.save(value, actor=actor)
            return view()
        except OssboxSettingsError as exc:
            raise failure(exc) from None

    @app.post(prefix + "/settings/ossbox/test")
    async def test_ossbox(request: Request):
        principal(request, "admin")
        value = await body(request)
        try:
            return {"ok": True, **await agent.oss_settings.test(value)}
        except OssboxSettingsError as exc:
            raise failure(exc) from None
