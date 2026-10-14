"""Existing local E2E database, current frozen source, production adapters."""
from pathlib import Path
import argparse,asyncio,logging,sys
SOURCE=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(SOURCE/"src"))
def main():
 parser=argparse.ArgumentParser();parser.add_argument("--data",required=True);parser.add_argument("--release-id",required=True);args=parser.parse_args()
 data=Path(args.data)
 if not (data/"agent.db").is_file() or not (data/"api.token").is_file():raise RuntimeError("Existing E2E data required; refusing to create a replacement database")
 from qtrade_agent.main import load_config,_attach_screen,install_log_masking
 from qtrade_agent.app import AgentApp
 import uvicorn
 from contextlib import asynccontextmanager
 cfg_path=data/"agent.toml";cfg=load_config(str(cfg_path))
 if Path(cfg.db.path).resolve()!=data.resolve()/"agent.db" or cfg.api.port!=17650:raise RuntimeError("Unexpected runtime data or port")
 logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s %(message)s")
 install_log_masking()
 agent=AgentApp(cfg,db_path=cfg.db.path,config_path=str(cfg_path),data_dir=str(data),adbkeyboard_apk=str(data/"apk/ADBKeyboard.apk")).open()
 api=agent.create_api()
 @api.get("/__local_e2e/version")
 def local_version():return {"source_fingerprint":args.release_id,"sts_settings":True,"runtime_source":str(SOURCE)}
 @asynccontextmanager
 async def lifespan(_app):
  await agent.start()
  try:yield
  finally:await agent.stop()
 api.router.lifespan_context=lifespan
 _attach_screen(agent,cfg)
 server=uvicorn.Server(uvicorn.Config(api,host=cfg.api.bind,port=cfg.api.port,ws=cfg.api.ws_impl,log_config=None,timeout_graceful_shutdown=30))
 async def shutdown():server.should_exit=True
 agent.shutdown_hook=shutdown
 server.run()
if __name__=="__main__":main()
