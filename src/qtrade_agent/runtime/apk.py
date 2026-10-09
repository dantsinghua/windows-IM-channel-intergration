"""企点 APK 的可信缓存与获取；仅使用配置的预期 SHA256，不从下载结果建立信任。"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ADBKEYBOARD_APK = "/opt/qtrade/apk/ADBKeyboard.apk"
QIDIAN_PACKAGE = "com.tencent.qidian"
ADBKEYBOARD_PACKAGE = "com.android.adbkeyboard"


class ApkPreparationError(RuntimeError):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code


async def download_apk(url: str, destination: Path, *, timeout_s: float = 300,
                       honor_env_proxy: bool = True) -> None:
    """流式下载到调用方独占的临时文件；取消后等待写线程退出才允许清理临时文件。"""
    cancelled = threading.Event()

    def copy() -> None:
        source = url if urllib.parse.urlsplit(url).scheme else Path(url).absolute().as_uri()
        if urllib.parse.urlsplit(source).scheme not in {"file", "http", "https"}:
            raise ValueError("不支持的 APK 来源协议")
        handlers = [] if honor_env_proxy else [urllib.request.ProxyHandler({})]
        opener = urllib.request.build_opener(*handlers)
        deadline = time.monotonic() + timeout_s
        with opener.open(source, timeout=min(timeout_s, 15)) as response, destination.open("wb") as output:
            read = getattr(response, "read1", response.read)
            while not cancelled.is_set():
                if time.monotonic() >= deadline:
                    raise TimeoutError("APK 下载超时")
                chunk = read(1024 * 1024)
                if not chunk:
                    break
                if cancelled.is_set():
                    return
                output.write(chunk)

    worker = asyncio.create_task(asyncio.to_thread(copy))
    try:
        await asyncio.wait_for(asyncio.shield(worker), timeout_s)
    finally:
        cancelled.set()
        # 不留下仍向已清理/已发布的文件写入的线程；网络单次读取另有限时。
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if worker.done() and not worker.cancelled():
            worker.exception()


async def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            await asyncio.sleep(0)
    return digest.hexdigest()


async def qidian_apk(runtime_cfg, *, honor_env_proxy: bool = True) -> Path:
    """已校验缓存优先；下载坏包只删除本次 .part，完整缓存及账号数据保持原样。"""
    expected = runtime_cfg.apk_sha256
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected) is None:
        raise ApkPreparationError("APK_UNAVAILABLE", "企点 APK 缺少可信 SHA256")
    expected = expected.lower()
    part: Path | None = None
    try:
        cache_dir = Path(runtime_cfg.apk_cache_dir)
        cached = cache_dir / f"{expected}.apk"
        if cached.is_file() and await _sha256(cached) == expected:
            return cached
        if not runtime_cfg.apk_url:
            raise ApkPreparationError("APK_UNAVAILABLE", "企点 APK 来源未配置，可信缓存不可用")
        cache_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f"{expected}.", suffix=".part", dir=cache_dir)
        os.close(fd)
        part = Path(name)
        await download_apk(runtime_cfg.apk_url, part, honor_env_proxy=honor_env_proxy)
        if await _sha256(part) != expected:
            raise ApkPreparationError("APK_UNAVAILABLE", "企点 APK SHA256 校验失败")
        part.replace(cached)
        return cached
    except ApkPreparationError:
        raise
    except Exception as e:
        # URL 可能带凭据，不把原始异常/URL 写到账号状态与事件。
        raise ApkPreparationError("APK_UNAVAILABLE", "企点 APK 获取或校验失败") from e
    finally:
        if part is not None:
            with contextlib.suppress(FileNotFoundError):
                part.unlink()
