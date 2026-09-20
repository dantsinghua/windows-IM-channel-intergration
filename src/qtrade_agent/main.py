"""qtrade-agent 进程入口:``python -m qtrade_agent.main [--config /etc/qtrade/agent.toml] [--db PATH] [--init-db]``。

uvicorn 单 worker、``ws="websockets"``(02 §2.2:不许 auto)。
``--init-db`` = 首启建库分支(03 §2.7.3 ②(d)):只建库 + 迁移到最新 + 写 ``schema_version`` 就退出。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import asynccontextmanager

from .config import AgentConfig

log = logging.getLogger("qtrade.main")

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
    _agent, api = build(cfg, args.db, config_path=args.config if has_cfg else None)
    import uvicorn
    uvicorn.run(api, host=cfg.api.bind, port=cfg.api.port, ws=cfg.api.ws_impl, workers=1, log_level=args.log_level.lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
