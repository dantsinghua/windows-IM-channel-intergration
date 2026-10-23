"""Bounded, local, daily operational logs; never dump credential fields."""
from __future__ import annotations

import copy
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import re

SECRET = re.compile(r"(?i)((?:access_token|authorization|password|secret|data_key|img_key|security_token)\s*[\"\x27:= ]+)(?:Bearer\s+)?[^\s,\"\x27}]+")
QUERY = re.compile(r"(?i)([?&](?:token|secret|password|access_token)=)[^&\s\"\x27]*")

class RedactedFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        safe = copy.copy(record)
        safe.exc_text = None
        text = super().format(safe)
        return QUERY.sub(r"\1***", SECRET.sub(r"\1***", text))

def attach_file_log(path: str) -> TimedRotatingFileHandler:
    target = Path(path)
    if not target.is_absolute():
        raise ValueError("LOG_PATH_NOT_ABSOLUTE")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if target.is_symlink():
        raise ValueError("LOG_PATH_SYMLINK")
    handler = TimedRotatingFileHandler(target, when="midnight", backupCount=7, encoding="utf-8", delay=False)
    handler.setFormatter(RedactedFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger("qtrade").addHandler(handler)
    return handler

def detach_file_log(handler: logging.Handler) -> None:
    logging.getLogger("qtrade").removeHandler(handler)
    handler.close()
