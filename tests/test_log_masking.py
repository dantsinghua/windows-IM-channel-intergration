"""D-1(e2e-rootfs-2):``MaskQuerySecrets`` 必须能与 uvicorn 真实的 ``AccessFormatter`` / ``DefaultFormatter`` 共存。

旧实现命中时把 ``record.args`` 清成 ``()`` ⇒ ``AccessFormatter`` 解包五元组失败 ⇒ 每条带 ``?token=`` 的访问行变成
「--- Logging error ---」回溯、原访问行丢失。这里全部走**完整格式化链路**(真 handler + 真 formatter),不只调 ``getMessage()``。
"""
from __future__ import annotations

import io
import logging

import pytest
from uvicorn.logging import AccessFormatter, DefaultFormatter

from qtrade_agent.main import MaskQuerySecrets

TOK = "S3cr3t-Tok-9f8e7d"
PW = "Pw-Plain-0a1b2c"


@pytest.fixture()
def rig(capsys):
    """独立 logger(不碰全局 uvicorn logger)+ 真 formatter;返回 (emit, 读输出, 读 stderr)。"""
    made: list[logging.Logger] = []

    def build(name: str, formatter: logging.Formatter):
        lg = logging.getLogger(f"test.logmask.{name}.{len(made)}")
        lg.handlers[:] = []
        lg.propagate = False
        lg.setLevel(logging.DEBUG)
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setFormatter(formatter)
        lg.addHandler(h)
        lg.addFilter(MaskQuerySecrets())
        made.append(lg)
        return lg, buf

    assert logging.raiseExceptions                    # 保证格式化出错会走 handleError 打「Logging error」
    yield build
    for lg in made:
        lg.handlers[:] = []
        lg.filters[:] = []


def _no_logging_error(capsys) -> None:
    err = capsys.readouterr().err
    assert "Logging error" not in err and "Traceback" not in err, err


# ────────────────────────────────────────────── uvicorn.access:五元组 args
@pytest.mark.parametrize("path,expect", [
    (f"/api/v1/screens/x.png?token={TOK}", "/api/v1/screens/x.png?token=***"),
    (f"/api/v1/x?token={TOK}&limit=5", "/api/v1/x?token=***&limit=5"),
    (f"/api/v1/x?password={PW}&secret={TOK}&access_token={TOK}", "/api/v1/x?password=***&secret=***&access_token=***"),
    (f"/api/v1/x?a=1&Token={TOK}", "/api/v1/x?a=1&Token=***"),
])
def test_access_formatter_full_chain_masks_and_keeps_fields(rig, capsys, path, expect):
    lg, buf = rig("access", AccessFormatter('%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                                            use_colors=False))
    lg.info('%s - "%s %s HTTP/%s" %d', "10.1.2.3:51234", "GET", path, "1.1", 200)
    out = buf.getvalue()
    _no_logging_error(capsys)
    assert TOK not in out and PW not in out
    assert out.strip() == f'INFO:     10.1.2.3:51234 - "GET {expect} HTTP/1.1" 200 OK'


def test_access_formatter_colored_and_non_secret_path_untouched(rig, capsys):
    lg, buf = rig("access-color", AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=True))
    lg.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "POST", f"/api/v1/y?token={TOK}", "1.1", 401)
    lg.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:2", "GET", "/api/v1/z?limit=5", "1.1", 404)
    out = buf.getvalue()
    _no_logging_error(capsys)
    lines = out.splitlines()
    assert len(lines) == 2 and TOK not in out
    assert "token=***" in lines[0] and "POST" in lines[0] and "401" in lines[0] and "127.0.0.1:1" in lines[0]
    assert "/api/v1/z?limit=5" in lines[1] and "404" in lines[1]


# ────────────────────────────────────────────── uvicorn.error:`%s` 模板
def test_default_formatter_ws_handshake_masked(rig, capsys):
    lg, buf = rig("error", DefaultFormatter("%(levelprefix)s %(message)s", use_colors=False))
    lg.info('%s - "WebSocket %s" [accepted]', "127.0.0.1:50000", f"/api/v1/events?token={TOK}&x=1")
    lg.info('%s - "WebSocket %s" 403', "127.0.0.1:50001", f"/api/v1/events?token={TOK}")
    out = buf.getvalue()
    _no_logging_error(capsys)
    assert TOK not in out
    assert out.splitlines() == [
        'INFO:     127.0.0.1:50000 - "WebSocket /api/v1/events?token=***&x=1" [accepted]',
        'INFO:     127.0.0.1:50001 - "WebSocket /api/v1/events?token=***" 403',
    ]


def test_default_formatter_secret_in_template_or_split(rig, capsys):
    """凭据写在模板字面量里、或被模板与参数拆开(``"?token=%s", tok``)——仍须遮且不抛。"""
    lg, buf = rig("error-split", DefaultFormatter("%(message)s", use_colors=False))
    lg.warning(f"GET /x?token={TOK} from %s", "1.2.3.4")          # 字面量里有凭据 + 另有参数
    lg.warning("GET /x?token=%s&n=%d", TOK, 3)                     # 模板与参数拆开
    lg.warning(f"plain /x?password={PW}")                          # 无参数
    lg.warning("dict %(p)s", {"p": f"/x?secret={TOK}"})            # 映射型 args
    out = buf.getvalue()
    _no_logging_error(capsys)
    assert TOK not in out and PW not in out
    assert out.splitlines() == [
        "GET /x?token=*** from 1.2.3.4",
        "GET /x?token=***&n=3",
        "plain /x?password=***",
        "dict /x?secret=***",
    ]


def test_non_str_arg_containing_secret_is_masked_ints_kept(rig, capsys):
    class _Url:
        def __str__(self) -> str:
            return f"/x?token={TOK}"

    lg, buf = rig("error-obj", DefaultFormatter("%(message)s", use_colors=False))
    lg.info("%s -> %d", _Url(), 7)
    out = buf.getvalue()
    _no_logging_error(capsys)
    assert out.strip() == "/x?token=*** -> 7"


def test_installed_on_real_uvicorn_loggers_full_chain(capsys):
    """与 main() 同顺序:先 ``uvicorn.Config``(dictConfig 装上真 formatter)再 ``install_log_masking``,
    然后经 uvicorn 自己的 handler/formatter 输出。"""
    import uvicorn

    from qtrade_agent.main import install_log_masking
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).propagate, logging.getLogger(n).level,
                 list(logging.getLogger(n).filters)) for n in names}
    try:
        uvicorn.Config(app=lambda *a: None, log_level="info", use_colors=False)
        install_log_masking()
        acc, err = logging.getLogger("uvicorn.access"), logging.getLogger("uvicorn.error")
        assert any(isinstance(h.formatter, AccessFormatter) for h in acc.handlers)
        capsys.readouterr()
        acc.info('%s - "%s %s HTTP/%s" %d', "9.9.9.9:9", "GET", f"/api/v1/q?token={TOK}", "1.1", 200)
        err.info('%s - "WebSocket %s" [accepted]', "9.9.9.9:10", f"/api/v1/events?token={TOK}")
        cap = capsys.readouterr()
        text = cap.out + cap.err
        assert "Logging error" not in text and "Traceback" not in text and TOK not in text
        assert '9.9.9.9:9 - "GET /api/v1/q?token=*** HTTP/1.1" 200 OK' in text
        assert '9.9.9.9:10 - "WebSocket /api/v1/events?token=***" [accepted]' in text
    finally:
        for n, (h, prop, lvl, flt) in saved.items():
            lg = logging.getLogger(n)
            lg.handlers[:], lg.propagate, lg.filters[:] = h, prop, flt
            lg.setLevel(lvl)
