import logging
import pytest
from qtrade_agent.operational_log import attach_file_log, detach_file_log

def test_daily_log_redacts_credentials_and_rotates(tmp_path):
    path=tmp_path/"logs"/"agent.log"
    handler=attach_file_log(str(path))
    logger=logging.getLogger("qtrade.synthetic")
    logger.setLevel(logging.INFO)
    try:
        logger.warning("trace_id=synthetic access_token=secret-value Authorization: Bearer hidden-value ?token=hidden-query")
        handler.flush()
        text=path.read_text()
        assert "trace_id=synthetic" in text
        assert all(secret not in text for secret in ("secret-value","hidden-value","hidden-query"))
        assert handler.backupCount==7
        handler.doRollover()
        logger.warning("next-day")
        handler.flush()
        assert "next-day" in path.read_text()
    finally: detach_file_log(handler)

def test_log_failure_is_not_silently_ignored(tmp_path):
    parent=tmp_path/"not-directory"
    parent.write_text("x")
    with pytest.raises(OSError): attach_file_log(str(parent/"agent.log"))

def test_relative_log_location_rejected():
    with pytest.raises(ValueError,match="LOG_PATH_NOT_ABSOLUTE"): attach_file_log("agent.log")
