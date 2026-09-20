"""``user`` —— 用户会话代理 ``qtrade-winagent-user.exe`` 的装配(02 §2.4,C-02)。

计划任务「用户登录时」以**登录用户身份**拉起,**不提权**;需要交互桌面(微信 UI 自动化)与用户自己的 WSL 登记。
它**不监听任何端口**(04 §2.6.3 R-14:防火墙规则里绝不出现会话代理 exe),只作为命名管道的**客户端**被服务调度。

方法名与 ``/wa/v1`` 一一映射(``/wa/v1/wsl/start`` ⇒ ``wsl.start``,02 §2.4.1);混合端点由服务拆好后
只把**管道半**下发过来(如 ``wsl.kernel.apply`` 只带 ``kernel_path`` + ``confirm_shutdown``)。

上线动作(02 §2.1 步 2′):按 ``winagent.toml [wsl] autostart=true`` 拉起发行版 ``qtrade``
(**随用户登录,不随开机**,C-02);失败写 ``wa_audit_log`` 并每 60s 重试 —— 重试由服务侧调度,这里只报错。
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from typing import Any, Callable, Optional

from .backends import PipeBackend, PowerBackend, WeChatBackend, WslBackend
from .config import WinAgentConfig
from .errors import NOT_READY, WaError
from .logfmt import get_logger
from .pipe import UserAgentLink
from .wechat import WeChatSession
from .wslctl import WslCtl

log = get_logger("user")


class UserAgent:
    """会话代理主体:把管道方法路由到 ``WslCtl`` / ``WeChatSession`` / ``PowerBackend``。"""

    def __init__(self, cfg: WinAgentConfig, *, pipe: PipeBackend, wsl: WslBackend,
                 wechat: Optional[WeChatBackend] = None, power: Optional[PowerBackend] = None,
                 session_id: str = "1", user_sid: str = "", version: str = "0.1.0", pid: Optional[int] = None,
                 backup_dir: Optional[str] = None, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.cfg = cfg
        self._clock = clock
        self.wslctl = WslCtl(wsl, cfg.wsl, backup_dir=backup_dir, clock=clock)
        self.wechat = WeChatSession(wechat, cfg.wechat, clock=clock) if wechat is not None else None
        self._power = power
        modules = ("wslctl",) + (("wechat",) if (wechat is not None and cfg.wechat.enabled) else ())
        self.link = UserAgentLink(pipe, cfg.ipc, pipe_name=cfg.api.pipe_user, session_id=session_id,
                                  user_sid=user_sid, version=version, pid=pid or os.getpid(), modules=modules,
                                  clock=clock)
        self._register()

    # ---------------------------------------------------------------- 方法路由
    def _register(self) -> None:
        w = self.wslctl
        self.link.on("wsl.status", lambda p: w.status())
        self.link.on("wsl.start", lambda p: w.start())
        self.link.on("wsl.stop", lambda p: w.stop())
        self.link.on("wsl.restart", lambda p: w.restart(mode=p.get("mode") or "terminate", confirm=bool(p.get("confirm"))))
        self.link.on("wsl.version", self._wsl_version)
        self.link.on("wsl.config.get", self._config_get)
        self.link.on("wsl.config.put", self._config_put)
        self.link.on("wsl.kernel.verify", self._kernel_verify)
        self.link.on("wsl.kernel.apply", self._kernel_apply)
        self.link.on("wsl.kernel.rollback", self._kernel_rollback)
        self.link.on("wsl.distro.repair", self._distro_repair)
        self.link.on("power.display", self._power_display)
        self.link.on("wechat.module", self._wechat_module)
        for method, fn in (("wechat.status", self._wechat_status), ("wechat.login.start", self._wechat_login_start),
                           ("wechat.login.cancel", self._wechat_login_cancel),
                           ("wechat.login.status", self._wechat_login_status), ("wechat.bind", self._wechat_bind),
                           ("wechat.logout", self._wechat_logout), ("wechat.key.retry", self._wechat_key_retry),
                           ("wechat.ui-visible", self._wechat_ui_visible), ("wechat.send", self._wechat_send),
                           ("wechat.read", self._wechat_read), ("wechat.media", self._wechat_media),
                           ("wechat.sessions", self._wechat_sessions), ("wechat.screenshot", self._wechat_screenshot),
                           ("wechat.reinstall", self._wechat_reinstall)):
            self.link.on(method, fn)

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> dict[str, Any]:
        welcome = await self.link.connect()
        if self.cfg.wsl.autostart:                     # 02 §2.1 步 2′:上线即拉起发行版(随用户登录,不随开机)
            try:
                await self.wslctl.start()
            except Exception as e:                     # 失败不阻断上线,由服务侧按 60s 重试
                log.warning("autostart 拉起发行版失败", extra={"op": "wsl.start", "code": "FAILED", "kv": {"err": repr(e)}})
        return welcome

    async def run(self) -> None:
        await self.link.run()

    async def stop(self) -> None:
        await self.link.stop()

    # ---------------------------------------------------------------- wsl
    async def _wsl_version(self, p: dict[str, Any]) -> Any:
        return await self.wslctl._wsl.version()        # noqa: SLF001 —— WslCtl 拥有后端,这里只读一个版本串

    async def _config_get(self, p: dict[str, Any]) -> Any:
        return self.wslctl.config_get()

    async def _config_put(self, p: dict[str, Any]) -> Any:
        # #25 白名单四键(C-32);安装器/内核流程走 kernel.* 那几个方法,不经本入口
        return self.wslctl.config_put(dict(p.get("desired") or {}),
                                      allow_keys=("memory", "processors", "autoMemoryReclaim", "swap"))

    async def _kernel_verify(self, p: dict[str, Any]) -> Any:
        return (await self.wslctl.kernel_verify()).view()

    async def _kernel_apply(self, p: dict[str, Any]) -> Any:
        return await self.wslctl.kernel_write_and_restart(kernel_path=p.get("kernel_path"),
                                                          confirm_shutdown=bool(p.get("confirm_shutdown")))

    async def _kernel_rollback(self, p: dict[str, Any]) -> Any:
        """回滚 = 把 ``kernel=`` 改回备份值(这里以「清空 ``kernel=`` 回默认内核」为基线实现,03 §2.6.5)。"""
        return await self.wslctl.kernel_write_and_restart(kernel_path=p.get("kernel_path"),
                                                          confirm_shutdown=bool(p.get("confirm_shutdown")))

    async def _distro_repair(self, p: dict[str, Any]) -> Any:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock() / 1000))
        return await self.wslctl.distro_repair(confirm=bool(p.get("confirm")),
                                               rootfs=p.get("rootfs") or "%ProgramData%\\QTrade\\pkg\\rootfs.tar",
                                               backup_tar=p.get("backup_tar") or f"/mnt/c/ProgramData/QTrade/wsl/data-backup-{stamp}.tar")

    # ---------------------------------------------------------------- power(会话代理只负责 ES_DISPLAY_REQUIRED)
    async def _power_display(self, p: dict[str, Any]) -> Any:
        if self._power is None:
            raise WaError(NOT_READY, "会话代理未接电源后端", reason="no_power_backend")
        cur = self._power.current_requests()
        self._power.set_execution_state(system_required=bool(cur.get("system_required")), display_required=bool(p.get("on")))
        return {"applied": True, "display_required": bool(p.get("on"))}

    # ---------------------------------------------------------------- wechat
    def _wx(self) -> WeChatSession:
        if self.wechat is None:
            raise WaError(NOT_READY, "会话代理未加载微信模块", reason="wechat_module_absent")
        return self.wechat

    async def _wechat_module(self, p: dict[str, Any]) -> Any:
        """#43:``enabled`` 改动后服务通知会话代理起/停模块。"""
        self.cfg = self.cfg.with_wechat(enabled=bool(p.get("enabled")))
        if self.wechat is not None:
            self.wechat._cfg = self.cfg.wechat          # noqa: SLF001 —— 配置热更新,唯一写点
            if not self.cfg.wechat.enabled:
                await self.wechat.logout()
        return {"enabled": self.cfg.wechat.enabled}

    async def _wechat_status(self, p: dict[str, Any]) -> Any:
        return self._wx().status(screen_locked=bool(p.get("screen_locked")))

    async def _wechat_login_start(self, p: dict[str, Any]) -> Any:
        # main_wnd_class:R6-58 (at) ②——服务侧按 account_id 算好的「该 wxid 行值 / 配置默认」,这里只应用
        return await self._wx().login_start(account_id=p.get("account_id"), login_session_id=p.get("login_session_id"),
                                            main_wnd_class=p.get("main_wnd_class"))

    async def _wechat_login_cancel(self, p: dict[str, Any]) -> Any:
        return await self._wx().login_cancel(login_session_id=p.get("login_session_id"))

    async def _wechat_login_status(self, p: dict[str, Any]) -> Any:
        return await self._wx().poll()                  # 每次查询顺带推一格状态机(轮询驱动)

    async def _wechat_bind(self, p: dict[str, Any]) -> Any:
        wx = self._wx()
        s = wx.session
        if s is not None:
            s.wxid = p.get("wxid") or s.wxid
            s.account_id = p.get("account_id") or s.account_id
        # main_wnd_class:R6-58 (at) ①——把这次探测到的主窗口类名随响应带回,服务侧据此 record_main_wnd_class
        # (方向仍是「服务发请求、结果随响应带回」,本类自己不碰 DB)
        return {"ok": True, "wxid": p.get("wxid"), "account_id": p.get("account_id"),
                "main_wnd_class": wx.detected_main_wnd_class()}

    async def _wechat_logout(self, p: dict[str, Any]) -> Any:
        return await self._wx().logout()

    async def _wechat_key_retry(self, p: dict[str, Any]) -> Any:
        return await self._wx().key_retry()

    async def _wechat_ui_visible(self, p: dict[str, Any]) -> Any:
        return self._wx().ui_visible()

    async def _wechat_send(self, p: dict[str, Any]) -> Any:
        return await self._wx().send(session_name=str(p.get("session_name") or ""), text=p.get("text"),
                                     image_path=p.get("image_path"), idempotency_key=p.get("idempotency_key"),
                                     confirm_timeout_ms=p.get("confirm_timeout_ms"))

    async def _wechat_read(self, p: dict[str, Any]) -> Any:
        return await self._wx().read(talker=p.get("talker"), since_seq=p.get("since_seq"), limit=p.get("limit"))

    async def _wechat_media(self, p: dict[str, Any]) -> Any:
        """截图/媒体走 base64 装进一帧;单帧 ≤ ``[ipc] max_frame_kb``(02 §2.4.1),超限由 ``FakePipeConn``/真管道拒。"""
        raw = await self._wx().media(str(p.get("key") or ""))
        return {"b64": base64.b64encode(raw).decode("ascii"), "content_type": "image/jpeg"}

    async def _wechat_sessions(self, p: dict[str, Any]) -> Any:
        return {"sessions": await self._wx().sessions(keyword=p.get("keyword"), limit=int(p.get("limit") or 100))}

    async def _wechat_screenshot(self, p: dict[str, Any]) -> Any:
        raw = await self._wx().screenshot(screen_locked=bool(p.get("screen_locked")))
        return {"b64": base64.b64encode(raw).decode("ascii"), "content_type": "image/png"}

    async def _wechat_reinstall(self, p: dict[str, Any]) -> Any:
        return await self._wx().reinstall(installer=p.get("installer"))
