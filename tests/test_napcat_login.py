"""NapCat 4.18.28 登录 HTTP/PNG 契约；全部是假 HTTP 和内存 PNG。"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qtrade_agent.adapters.qq import login
from test_qq_first_login_common import first_login_rig, no_external_io, png

ROW = {'id': 'qq01', 'seq': 1, 'channel': 'qq'}


@pytest.fixture(autouse=True)
def guarded(no_external_io):
    pass


async def test_refresh_authenticates_loopback_and_returns_new_png_not_url(monkeypatch):
    old, new = png(1), png(2)
    images = iter([old, old, b'incomplete write', new])
    calls = []
    async def request(port, path, body, credential=''):
        calls.append((port, path, body, credential))
        return {'Credential': 'fake-credential'} if path == 'auth/login' else {'qrcodeurl': 'https://fake.example/scan'}
    async def read_png(row):
        return next(images)
    monkeypatch.setattr(login, 'asyncio', SimpleNamespace(wait_for=login.asyncio.wait_for, sleep=AsyncMock(), CancelledError=login.asyncio.CancelledError))
    backend = login.NapCatLoginBackend(token_for=lambda row: 'fake-token', read_png=read_png, request=request)
    assert await backend.qrcode(ROW, refresh=True) == new
    assert calls == [(16301, 'auth/login', {'hash': hashlib.sha256(b'fake-token.napcat').hexdigest()}, ''),
                     (16301, 'QQLogin/RefreshQRcode', {}, 'fake-credential')]


async def test_get_qrcode_also_reads_png_and_does_not_fetch_scan_url():
    calls = []
    async def request(port, path, body, credential=''):
        calls.append(path)
        return {'Credential': 'fake-credential'} if path == 'auth/login' else {'qrcode': 'https://fake.example/scan'}
    backend = login.NapCatLoginBackend(token_for=lambda row: 'fake-token', read_png=AsyncMock(return_value=png(9)), request=request)
    assert await backend.qrcode(ROW, refresh=False) == png(9)
    assert calls == ['auth/login', 'QQLogin/GetQQLoginQrcode']


@pytest.mark.parametrize('result', [{}, {'qrcodeurl': ''}, {'qrcodeurl': 'fake', 'restarting': True}])
async def test_upstream_not_ready_is_visible_fixed_error(result):
    async def request(port, path, body, credential=''):
        return {'Credential': 'fake-credential'} if path == 'auth/login' else result
    backend = login.NapCatLoginBackend(token_for=lambda row: 'fake-token', read_png=AsyncMock(return_value=png()), request=request)
    with pytest.raises(login.NapCatLoginError, match='qrcode_not_ready'):
        await backend.qrcode(ROW, refresh=True)


async def test_stale_png_never_reissued_after_refresh(monkeypatch):
    async def request(port, path, body, credential=''):
        return {'Credential': 'fake-credential'} if path == 'auth/login' else {'qrcodeurl': 'fake'}
    monkeypatch.setattr(login, 'asyncio', SimpleNamespace(wait_for=login.asyncio.wait_for, sleep=AsyncMock(), CancelledError=login.asyncio.CancelledError))
    backend = login.NapCatLoginBackend(token_for=lambda row: 'fake-token', read_png=AsyncMock(return_value=png()), request=request)
    with pytest.raises(login.NapCatLoginError, match='qrcode_not_ready'):
        await backend.qrcode(ROW, refresh=True)


@pytest.mark.parametrize('auth', [{}, {'Credential': 'fake', 'require2FA': True}, {'Credential': 123}])
async def test_incomplete_or_two_factor_webui_auth_denies_qr(auth):
    request = AsyncMock(return_value=auth)
    backend = login.NapCatLoginBackend(token_for=lambda row: 'fake-token', read_png=AsyncMock(return_value=png()), request=request)
    with pytest.raises(login.NapCatLoginError, match='webui_auth_unavailable'):
        await backend.qrcode(ROW, refresh=True)
    assert request.await_count == 1


@pytest.mark.parametrize('row', [dict(ROW, seq=0), dict(ROW, seq=99), dict(ROW, id='qq02'), dict(ROW, channel='qidian')])
async def test_invalid_account_never_requests_webui(row):
    request = AsyncMock()
    backend = login.NapCatLoginBackend(token_for=lambda row: 'fake-token', read_png=AsyncMock(), request=request)
    with pytest.raises(login.NapCatLoginError):
        await backend.qrcode(row, refresh=True)
    request.assert_not_awaited()


@pytest.mark.parametrize('body', [b'\x89PNG\r\n\x1a\n', png()[:-4], png() + b'trailing', b'x' * (1024 * 1024 + 1)])
def test_truncated_or_oversized_png_is_rejected(body):
    assert not login.valid_png(body)


async def test_http_uses_loopback_no_proxy_redirect_and_bounded_response(monkeypatch):
    captured = {}
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, size):
            captured['read_size'] = size
            return json.dumps({'code': 0, 'data': {'Credential': 'fake'}}).encode()
    class Opener:
        def open(self, request, timeout):
            captured.update(url=request.full_url, timeout=timeout, data=json.loads(request.data), headers=dict(request.headers))
            return Response()
    def opener(*handlers):
        captured['handlers'] = handlers
        return Opener()
    monkeypatch.setattr(login.urllib.request, 'build_opener', opener)
    assert await login._post(16301, 'auth/login', {'hash': 'fake'}) == {'Credential': 'fake'}
    assert captured['url'] == 'http://127.0.0.1:16301/api/auth/login'
    assert captured['read_size'] == 65537 and captured['timeout'] == 12
    assert captured['handlers'][0].proxies == {}
    assert captured['handlers'][1].redirect_request(None, None, None, None, None, 'https://elsewhere') is None


async def test_upstream_error_body_and_token_never_escape(monkeypatch, caplog):
    class Opener:
        def open(self, *args, **kwargs):
            raise RuntimeError('fake-private-token-and-base64')
    monkeypatch.setattr(login.urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(login.NapCatLoginError, match='^webui_unavailable$') as error:
        await login._post(16301, 'QQLogin/GetQQLoginQrcode', {}, 'fake-private-token')
    assert 'fake-private' not in str(error.value) + caplog.text


@pytest.mark.parametrize('raw', ['invalid-base64', 'oversize'])
async def test_runtime_qr_read_rejects_invalid_or_oversized_payload(first_login_rig, monkeypatch, raw):
    import base64
    r = first_login_rig
    value = raw if raw == 'invalid-base64' else base64.b64encode(b'x' * (login.MAX_QR_BYTES + 1)).decode()
    read = AsyncMock(return_value=value)
    monkeypatch.setattr(r.containers, 'exec', read)
    with pytest.raises(login.NapCatLoginError, match='^qrcode_file_unavailable$'):
        await r.agent.runtime.read_napcat_qrcode(r.accounts.get(r.id))
    name, command = read.call_args.args
    assert name == f'qtrade-{r.id}'
    assert '/app/napcat/cache/qrcode.png' in command
    assert str(login.MAX_QR_BYTES + 1) in command


@pytest.mark.parametrize('account_env,expected', [('123456789', '123456789'), ('', ''), (None, None)])
async def test_docker_inspect_exposes_only_account_environment(monkeypatch, account_env, expected):
    from dataclasses import asdict
    from qtrade_agent.runtime import backends
    env = ['TOKEN=fake-private-token', 'PASSWORD=fake-password']
    if account_env is not None:
        env.append('ACCOUNT=' + account_env)
    result = {'Id': 'a' * 64, 'Config': {'Env': env}, 'State': {'Running': True, 'ExitCode': 0}}
    run = AsyncMock(return_value=(0, json.dumps(result)))
    monkeypatch.setattr(backends, '_run', run)
    info = await backends.DockerCliBackend().inspect('qtrade-qq01')
    assert info.account_env == expected
    assert 'fake-private-token' not in json.dumps(asdict(info))
    assert 'fake-password' not in repr(info)
    assert run.call_args.args[0] == ['docker', 'inspect', '-f', '{{json .}}', 'qtrade-qq01']
