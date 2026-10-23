"""Run current source against an explicitly selected existing development database."""
from pathlib import Path
import argparse
import logging
import sys

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "src"))

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--api-port", required=True, type=int)
    args = parser.parse_args()
    data = Path(args.data)
    if not (data / "agent.db").is_file() or not (data / "api.token").is_file():
        raise RuntimeError("EXISTING_RUNTIME_DATA_MISSING: refusing to create a replacement database")
    from contextlib import asynccontextmanager
    import uvicorn
    from qtrade_agent.main import load_config, _attach_screen, install_log_masking
    from qtrade_agent.app import AgentApp

    cfg_path = data / "agent.toml"
    cfg = load_config(str(cfg_path))
    if Path(cfg.db.path).resolve() != data.resolve() / "agent.db" or cfg.api.port != args.api_port:
        raise RuntimeError("RUNTIME_CONFIG_MISMATCH: database or API port")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_log_masking()
    agent = AgentApp(cfg, db_path=cfg.db.path, config_path=str(cfg_path), data_dir=str(data),
                     adbkeyboard_apk=str(data / "apk/ADBKeyboard.apk")).open()
    api = agent.create_api()

    @api.get("/__local_e2e/version")
    def local_version():
        return {"source_fingerprint": args.release_id, "sts_settings": True, "runtime_source": str(SOURCE)}

    @asynccontextmanager
    async def lifespan(_app):
        await agent.start()
        try:
            yield
        finally:
            await agent.stop()

    api.router.lifespan_context = lifespan
    _attach_screen(agent, cfg)
    server = uvicorn.Server(uvicorn.Config(api, host=cfg.api.bind, port=cfg.api.port, ws=cfg.api.ws_impl,
                                         log_config=None, timeout_graceful_shutdown=30))

    async def shutdown():
        server.should_exit = True

    agent.shutdown_hook = shutdown
    server.run()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
