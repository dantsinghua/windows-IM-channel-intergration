"""qtrade-agent 进程入口:``python -m qtrade_agent.main [--config /etc/qtrade/agent.toml] [--db PATH]``。

uvicorn 单 worker、``ws="websockets"``(02 §2.2:不许 auto)。
"""
from __future__ import annotations

import argparse
import logging
import sys
from contextlib import asynccontextmanager

from .config import AgentConfig


def load_config(path: str | None) -> AgentConfig:
    if not path:
        return AgentConfig()
    try:
        import tomllib
    except ImportError:                                 # Python < 3.11
        import tomli as tomllib                         # type: ignore
    with open(path, "rb") as f:
        return AgentConfig.from_toml_dict(tomllib.load(f))


def build(cfg: AgentConfig, db_path: str | None = None):
    from .app import AgentApp
    agent = AgentApp(cfg, db_path=db_path).open()
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qtrade-agent")
    ap.add_argument("--config", default="/etc/qtrade/agent.toml")
    ap.add_argument("--db", default=None)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-5s %(name)-24s %(message)s")
    import os
    cfg = load_config(args.config if os.path.exists(args.config) else None)
    _agent, api = build(cfg, args.db)
    import uvicorn
    uvicorn.run(api, host=cfg.api.bind, port=cfg.api.port, ws=cfg.api.ws_impl, workers=1, log_level=args.log_level.lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
