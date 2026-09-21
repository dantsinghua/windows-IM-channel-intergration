"""验收:uvicorn 访问日志 / WS 握手日志里的敏感查询参数遮蔽(e2e-rootfs-2 D-1 的验收用例)。

规格:
- `02` §3.4.7 握手段(🔴 R6-68 ⑪):「uvicorn 访问日志会把握手 URL 原样打进 INFO,生产入口挂日志过滤器把
  `token`/`secret`/`password`/`access_token` 查询参数的值遮成 `***`」;
- `00` §11.2 [NOLOG]:日志不记密码(令牌同理)。
- 挂载入口:`qtrade_agent.main.install_log_masking()`(公开函数;docstring:「uvicorn.Config 构造时会 dictConfig 它自己的
  logger ⇒ 必须在那之后挂」)。

做法 —— **走完整的真实格式化链路**,不是只调 `record.getMessage()`:用 uvicorn 自带的 `LOGGING_CONFIG`
(`uvicorn.logging.AccessFormatter` / `DefaultFormatter` + `StreamHandler`)真构造一次 `uvicorn.Config`,再按上面的顺序挂过滤器,
然后按 uvicorn 源码里**逐字相同的格式串与参数形状**打日志(h11/httptools 的访问行、websockets 实现的握手行)。
「遮蔽器把日志行弄成 `--- Logging error ---` 回溯」这种失败只有经过真 formatter 才看得见,这正是本文件要守的。
令牌均为合成值。
"""
from __future__ import annotations

import copy
import io
import logging

import pytest

TOKEN = "SYNTH-tok-9f3Ac71e"              # 合成令牌
PASSWORD = "SYNTH-pw-Qx82"
SECRET = "SYNTH-sec-44Lm"
ACCESS_TOKEN = "SYNTH-at-0Zk5"
ALL_SECRETS = (TOKEN, PASSWORD, SECRET, ACCESS_TOKEN)

# uvicorn/protocols/http/h11_impl.py 与 httptools_impl.py 的访问行(逐字):
ACCESS_FMT = '%s - "%s %s HTTP/%s" %d'
# uvicorn/protocols/websockets/websockets_impl.py 的握手行(逐字):
WS_ACCEPTED_FMT = '%s - "WebSocket %s" [accepted]'
WS_403_FMT = '%s - "WebSocket %s" 403'

_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def uv_logs(capsys):
    """真 uvicorn 日志配置 + 产品遮蔽器;产出 ``read()`` → 本用例打出的 stdout+stderr 全文(含 handler 的 Logging error)。"""
    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    from qtrade_agent.main import install_log_masking

    saved = {n: (lg.handlers[:], lg.filters[:], lg.level, lg.propagate, lg.disabled)
             for n, lg in ((n, logging.getLogger(n)) for n in _NAMES)}
    raise_exc = logging.raiseExceptions
    logging.raiseExceptions = True                         # 生产缺省:formatter 抛错 ⇒ handler 往 stderr 打「--- Logging error ---」
    try:
        async def app(scope, receive, send):              # 仅供 uvicorn.Config 构造,不会被调用
            return None

        cfg = copy.deepcopy(LOGGING_CONFIG)
        buf = io.StringIO()                                 # 两个 handler 的输出流(formatter / handler 类仍是 uvicorn 原配)
        for h in cfg["handlers"].values():
            h["stream"] = buf
        uvicorn.Config(app=app, log_config=cfg)            # 真 dictConfig
        install_log_masking()
        capsys.readouterr()

        def read() -> str:
            err = capsys.readouterr().err                  # handler.handleError 的「--- Logging error ---」回溯打在 sys.stderr
            return buf.getvalue() + err

        yield read
    finally:
        logging.raiseExceptions = raise_exc
        for n, (handlers, filters, level, propagate, disabled) in saved.items():
            lg = logging.getLogger(n)
            lg.handlers[:] = handlers
            lg.filters[:] = filters
            lg.setLevel(level)
            lg.propagate = propagate
            lg.disabled = disabled


def _access(path: str, status: int = 200, method: str = "GET") -> None:
    logging.getLogger("uvicorn.access").info(ACCESS_FMT, "127.0.0.1:50123", method, path, "1.1", status)


def _ws(fmt: str, path: str) -> None:
    logging.getLogger("uvicorn.error").info(fmt, "127.0.0.1:50124", path)


# ─────────────────────────────────────────────────── HTTP 访问日志(uvicorn.access + AccessFormatter)

def test_LM01_access_log_with_token_formats_without_logging_error(uv_logs):
    """带 `?token=` 的访问行经真 `AccessFormatter` 格式化:不得出现 `--- Logging error ---` / Traceback(D-1 的原始症状)。"""
    _access(f"/api/v1/media/m1?token={TOKEN}&limit=5")
    out = uv_logs()
    assert "Logging error" not in out and "Traceback" not in out, out


def test_LM02_access_log_masks_token_value(uv_logs):
    """02 §3.4.7 / R6-68 ⑪:令牌明文 0 命中,值遮成 `***`。"""
    _access(f"/api/v1/media/m1?token={TOKEN}&limit=5")
    out = uv_logs()
    assert TOKEN not in out, out
    assert "token=***" in out, out


def test_LM03_access_log_keeps_method_path_version_status_and_client(uv_logs):
    """遮蔽只动敏感参数的值:访问行其余信息(客户端、方法、去掉令牌后的路径与其余参数、HTTP 版本、状态码)仍在,且恰好一行。"""
    _access(f"/api/v1/media/m1?token={TOKEN}&limit=5", status=206)
    lines = [ln for ln in uv_logs().splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    line = lines[0]
    for piece in ("127.0.0.1:50123", '"GET /api/v1/media/m1?', "limit=5", "HTTP/1.1", "206"):
        assert piece in line, (piece, line)


def test_LM04_access_log_masks_all_four_secret_params(uv_logs):
    """R6-68 ⑪ 点名的四个参数 `token`/`secret`/`password`/`access_token` 全部遮成 `***`,无一明文。"""
    _access(f"/api/v1/x?token={TOKEN}&password={PASSWORD}&secret={SECRET}&access_token={ACCESS_TOKEN}&page=2", method="POST")
    out = uv_logs()
    assert "Logging error" not in out, out
    assert not [s for s in ALL_SECRETS if s in out], out
    for k in ("token", "password", "secret", "access_token"):
        assert f"{k}=***" in out, (k, out)
    assert "page=2" in out and '"POST /api/v1/x?' in out, out


def test_LM05_access_log_without_secrets_is_formatted_verbatim(uv_logs):
    """对照:不带敏感参数的访问行照常输出(遮蔽器不得破坏普通行)。"""
    _access("/api/v1/accounts?limit=20")
    out = uv_logs()
    assert "Logging error" not in out, out
    assert '127.0.0.1:50123 - "GET /api/v1/accounts?limit=20 HTTP/1.1" 200' in out, out


# ─────────────────────────────────────────────────── WS 握手日志(uvicorn.error + DefaultFormatter)

def test_LM06_ws_handshake_log_formats_without_error_and_masks_token(uv_logs):
    """02 §3.4.7:浏览器 WS 只能用 `?token=` 握手 ⇒ 握手行经真 `DefaultFormatter`:不抛错、令牌 0 命中、出现 `***`。"""
    _ws(WS_ACCEPTED_FMT, f"/api/v1/events?token={TOKEN}")
    out = uv_logs()
    assert "Logging error" not in out and "Traceback" not in out, out
    assert TOKEN not in out, out
    assert "token=***" in out, out


def test_LM07_ws_handshake_log_keeps_path_and_outcome(uv_logs):
    """握手行其余信息仍在:客户端、`WebSocket /api/v1/events`、结果(`[accepted]` / `403`)。"""
    _ws(WS_ACCEPTED_FMT, f"/api/v1/events?token={TOKEN}")
    _ws(WS_403_FMT, f"/api/v1/events?token={TOKEN}")
    out = uv_logs()
    assert TOKEN not in out, out
    assert '127.0.0.1:50124 - "WebSocket /api/v1/events?' in out, out
    assert "[accepted]" in out and '" 403' in out, out
