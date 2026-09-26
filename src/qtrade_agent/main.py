"""qtrade-agent 进程入口:``python -m qtrade_agent.main [--config /etc/qtrade/agent.toml] [--db PATH] [--init-db]``。

uvicorn 单 worker、``ws="websockets"``(02 §2.2:不许 auto)。
``--init-db`` = 首启建库分支(03 §2.7.3 ②(d)):只建库 + 迁移到最新 + 写 ``schema_version`` 就退出。
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from contextlib import asynccontextmanager

from .config import AgentConfig

log = logging.getLogger("qtrade.main")

#: 查询串里的凭据参数(02 §3.4.7 WS 握手允许 `?token=`)。uvicorn 在 WS 握手 / 访问日志里打**带查询串的完整路径**,
#: 不遮就把 bearer 令牌明文写进应用日志(00 §11.2 [NOLOG];backend-sec-1 同型扫描发现)。
_QUERY_SECRET = re.compile(r"(?i)([?&](?:token|secret|password|access_token)=)[^&\s\"']*")


def _mask_arg(v: object) -> object:
    """单个格式化参数:字符串直接遮;非字符串仅当其 ``str()`` 里含凭据时才换成遮后的字符串(int 等原样保留)。"""
    if isinstance(v, str):
        return _QUERY_SECRET.sub(r"\1***", v)
    try:
        s = str(v)
    except Exception:
        return v
    masked = _QUERY_SECRET.sub(r"\1***", s)
    return v if masked == s else masked


class MaskQuerySecrets(logging.Filter):
    """把日志行里 ``?token=…`` 一类查询参数的值换成 ``***``(挂在 uvicorn 的两个 logger 上)。

    🔴 不得清空/改变 ``record.args`` 的形状:uvicorn 的 ``AccessFormatter`` 固定把 args 解包成
    (client_addr, method, full_path, http_version, status_code) 五元组(e2e-rootfs-2 D-1)。
    故 msg 与 args **逐项**遮蔽、保持元组长度与非敏感项的类型。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            args = record.args
            if isinstance(args, tuple):
                record.args = tuple(_mask_arg(a) for a in args)
            elif isinstance(args, dict):
                record.args = {k: _mask_arg(a) for k, a in args.items()}
            full = record.getMessage()
            if _QUERY_SECRET.sub(r"\1***", full) == full:
                return True                     # 常见路径:凭据只在参数里(uvicorn 访问行 / WS 握手行)
            # 凭据在模板字面量里,或被模板与参数拆开(`"?token=%s", tok`)
            if isinstance(record.msg, str) and record.args:
                cand = _QUERY_SECRET.sub(r"\1***", record.msg)
                try:
                    out = cand % record.args
                except Exception:
                    out = None
                if out is not None and _QUERY_SECRET.sub(r"\1***", out) == out:
                    record.msg = cand
                    return True
            # 兜底:只剩整行格式化后替换。uvicorn.access 的模板是定值 `'%s - "%s %s HTTP/%s" %d'`、
            # 凭据只可能在 full_path 参数里,上面已处理 ⇒ 走不到这里,五元组形状不受影响。
            record.msg, record.args = _QUERY_SECRET.sub(r"\1***", full), ()
        except Exception:                       # 遮蔽本身绝不能让日志链路抛异常
            return True
        return True


def install_log_masking() -> None:
    """uvicorn.Config 构造时会 dictConfig 它自己的 logger ⇒ 必须在那之后挂。"""
    for name in ("uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, MaskQuerySecrets) for f in lg.filters):
            lg.addFilter(MaskQuerySecrets())

#: ``--init-db`` 的退出码约定(调用方 = ``qtrade-firstboot.sh``,非 0 即 ``die``)。2 是 argparse 的用法错误,不归这里排。
EXIT_OK = 0
EXIT_DB_CORRUPT = 3                 # 库损坏 / 不是 SQLite(02 §2.6)
EXIT_SCHEMA_TOO_NEW = 4             # 库 schema_version 高于本版代码上限(02 §3.8,降级不支持)
EXIT_INIT_FAILED = 5                # 其它:父目录建不出来、DDL/迁移失败、磁盘满、权限不足…

#: `/var/lib/qtrade` 在 02/03 无显式 mode 约定(rootfs 里由 `mkdir -p` 建成 0755);新建时按最小权限取 0750。
DB_DIR_MODE = 0o750


def load_config(path: str | None) -> AgentConfig:
    if not path:
        return AgentConfig()
    try:
        import tomllib
    except ImportError:                                 # Python < 3.11
        import tomli as tomllib                         # type: ignore
    with open(path, "rb") as f:
        return AgentConfig.from_toml_dict(tomllib.load(f))


def build(cfg: AgentConfig, db_path: str | None = None, config_path: str | None = None):
    from .app import AgentApp
    agent = AgentApp(cfg, db_path=db_path, config_path=config_path).open()
    api = agent.create_api()

    @asynccontextmanager
    async def lifespan(app):
        await agent.start()
        try:
            yield
        finally:
            await agent.stop()

    api.router.lifespan_context = lifespan
    return agent, api


def init_db(cfg: AgentConfig, db_path: str | None) -> int:
    """``--init-db``:建库 + 迁移到最新 + 写 ``schema_version``,然后退出(03 §2.7.3 ②(d))。

    **不起 HTTP、不起调度器、不连 WinAgent/dockerd/adb/邮箱**:只开 ``Store``,与正常启动走的是同一个
    ``Store.open()``(同一套 DDL、同一组 PRAGMA —— ``auto_vacuum=INCREMENTAL`` 必须建库时设,02 §2.8.4)。
    **幂等**:库已是最新 ⇒ 一条语句都不写、回 0;版本落后 ⇒ 跑迁移;损坏 / 版本比代码新 ⇒ 非 0 退出且**不动**该文件。
    退出码见本模块 ``EXIT_*``。
    """
    from .store import SchemaTooNew, Store, StoreCorrupt
    path = db_path or cfg.db.path
    parent = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(parent, mode=DB_DIR_MODE, exist_ok=True)
    except OSError as e:
        log.error("建库失败:父目录 %s 建不出来:%s", parent, e)
        return EXIT_INIT_FAILED
    store = Store(path, out_merge_window_s=cfg.bus.out_merge_window_s, capture_text=cfg.messages.capture_text)
    try:
        store.open()
        version = store.schema_version()
        tables = store.con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    except StoreCorrupt as e:
        log.error("建库失败:%s", e)
        return EXIT_DB_CORRUPT
    except SchemaTooNew as e:
        log.error("建库失败:%s", e)
        return EXIT_SCHEMA_TOO_NEW
    except Exception as e:                                  # DDL/迁移失败、磁盘满、权限不足…(02 §2.1 步 4:拒绝启动)
        log.error("建库失败:%s:%s", type(e).__name__, e)
        return EXIT_INIT_FAILED
    finally:
        store.close()
    log.info("agent.db 就绪:%s(schema_version=%d,表 %d 张)", path, version, tables)
    return EXIT_OK


def _attach_screen(agent, cfg: AgentConfig) -> None:
    """真机上把 #34 画面流 / #33 企点截图 / #35 注入兜底接到 scrcpy-server + adb(``screen_scrcpy.ScrcpyBackend``)。

    随包 scrcpy-server 或 adb 不在 ⇒ 保持未装配(#34 如实 4503、#33 企点 UNSUPPORTED),不造假帧。
    账号 → adb serial / 165NN 按库里 ``account_runtime`` 取、缺了按 00 §3 由序号推导;查不到账号即报错(WS 4503),
    **不回退到任何固定设备**。测试不走 main。
    """
    import shutil

    from .runtime.runtime import ADB_SERVER_PORT
    from .screen_adb import AsyncAdb
    from .screen_scrcpy import ScrcpyBackend, store_target_resolver

    jar = cfg.qidian.scrcpy_server_path
    if not os.path.isfile(jar):
        log.warning("随包 scrcpy-server 不在 %s,画面流保持未装配", jar)
        return
    if shutil.which("adb") is None:
        log.warning("找不到 adb,画面流保持未装配")
        return
    backend = ScrcpyBackend(store_target_resolver(agent.store), adb=AsyncAdb(server_port=ADB_SERVER_PORT),
                            server_jar=jar, server_version=cfg.qidian.scrcpy_server_version,
                            profiles=cfg.qidian.stream_profiles, frame_timeout_s=cfg.health.scrcpy_frame_timeout_s,
                            key_wait_s=cfg.qidian.scrcpy_key_wait_s,
                            reset_min_interval_s=cfg.qidian.scrcpy_reset_min_interval_s,
                            lag_evict_count=cfg.qidian.scrcpy_lag_evict_count,
                            lag_evict_window_s=cfg.qidian.scrcpy_lag_evict_window_s,
                            alerts=agent.alerts)
    agent.stream_backend = backend
    qidian = agent.adapters.get("qidian")
    if qidian is not None:
        qidian.screenshot_fn = backend.capture_png
    log.info("画面流已接上 scrcpy-server %s(%s)", cfg.qidian.scrcpy_server_version, jar)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qtrade-agent")
    ap.add_argument("--config", default="/etc/qtrade/agent.toml")
    ap.add_argument("--db", default=None)
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--init-db", action="store_true",
                    help="只建 agent.db 并把 schema 迁到最新就退出(幂等;不起 HTTP、不起调度器、不连外部依赖)。"
                         "首启用,见 docs/03 §2.7.3。退出码:0 成功 / 3 库损坏或不是 SQLite / "
                         "4 库 schema_version 高于本版支持上限 / 5 其它建库失败")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-5s %(name)-24s %(message)s")
    has_cfg = os.path.exists(args.config)
    cfg = load_config(args.config if has_cfg else None)
    if args.init_db:
        # `--config` 只用来取 `[db] path` 等建库参数;`[api]`/`[scheduler]` 这类运行期配置本次一律不生效。
        return init_db(cfg, args.db)
    agent, api = build(cfg, args.db, config_path=args.config if has_cfg else None)
    _attach_screen(agent, cfg)
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(api, host=cfg.api.bind, port=cfg.api.port, ws=cfg.api.ws_impl,
                                           workers=1, log_level=args.log_level.lower()))
    install_log_masking()

    async def _graceful_shutdown() -> None:
        """#83 `POST /system/shutdown` 的**生产**执行体(02 §2.6 优雅停机)。

        🔴 缺省钩子 = `AgentApp.stop()`,它只收调度器/投递器/总线/库,**不会让进程退出** —— 光有它,
        控制台点了「停止 Agent」以后 uvicorn 还在服务、`/system/health` 照回 200,与 #83 的语义对不上。
        这里在生产入口把两步接起来:先 `stop()` 收干净,再让 uvicorn 退出监听循环(测试仍注入假钩子,不退进程)。
        """
        try:
            await agent.stop()
        finally:
            server.should_exit = True

    agent.shutdown_hook = _graceful_shutdown
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
