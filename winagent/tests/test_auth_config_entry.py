"""Invalid local configuration must fail before native side effects."""
from unittest.mock import Mock
import pytest
from qtrade_winagent import main_user

@pytest.mark.parametrize("raw",[b"[wsl]\nautostart = false[wechat]\n",b"\xff\xfe"] )
def test_invalid_config_is_controlled_exit_without_starting_agent(tmp_path,monkeypatch,caplog,raw):
 p=tmp_path/"invalid.toml"; p.write_bytes(raw)
 monkeypatch.setattr(main_user,"current_user_sid",lambda:"S-1-5-21-1111-2222-3333-1001")
 monkeypatch.setattr(main_user,"user_log_dir",lambda:str(tmp_path/"logs"))
 logger=Mock(); monkeypatch.setattr(main_user,"setup_logging",logger)
 build=Mock(); monkeypatch.setattr(main_user,"build_user_agent",build)
 assert main_user.main(["--root",str(tmp_path),"--config",str(p)])==2
 build.assert_not_called(); logger.assert_called_once()
 assert "CONFIG_INVALID" in caplog.text or "configuration" in caplog.text.lower()

def test_missing_explicit_config_never_falls_back_to_live_defaults(tmp_path,monkeypatch):
 monkeypatch.setattr(main_user,"current_user_sid",lambda:"S-1-5-21-1111-2222-3333-1001")
 monkeypatch.setattr(main_user,"user_log_dir",lambda:str(tmp_path/"logs"))
 monkeypatch.setattr(main_user,"setup_logging",Mock())
 build=Mock(side_effect=AssertionError("must not construct native side effects"))
 monkeypatch.setattr(main_user,"build_user_agent",build)
 assert main_user.main(["--root",str(tmp_path),"--config",str(tmp_path/"absent.toml")])==2
 build.assert_not_called()