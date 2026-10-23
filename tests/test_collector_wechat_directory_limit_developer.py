"""Bounded native WeChat session discovery; real client/host, fake HTTP edge only."""
import asyncio
from types import MethodType, SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from qtrade_agent.adapters.wechat.client import WeChatWinAgent
from qtrade_agent.app import AgentApp
from qtrade_agent.ossbox.institution import InstitutionCollectorHost
from qtrade_agent.store import Store
from relay.file_gateway import GatewayAuth
from relay.gateway_paths import CONTRACT, InstitutionKeys


class NativeHTTP:
    def __init__(self, sessions):
        self.rows = sessions
        self.uid = 'wxid_synthetic_current'
        self.requests = []
        self.switch_after_sessions = False

    async def request(self, method, path, **kwargs):
        assert method == 'GET', 'directory reads must never perform native actions'
        self.requests.append(path)
        route = urlsplit(path)
        if route.path == '/wa/v1/wechat/status':
            return 200, {'enabled': True, 'wechat': {'running': True, 'logged_in': True, 'wxid': self.uid},
                         'chatlog': {'running': True, 'key_ok': True}}
        assert route.path == '/wa/v1/wechat/sessions'
        count = int(parse_qs(route.query).get('limit', ['100'])[0])
        if self.switch_after_sessions:
            self.uid = 'wxid_another_login'
        return 200, {'sessions': self.rows[:count]}


def sessions(count, *, include_filehelper=False):
    rows = [{'userName': f'wxid_native_peer_{index}', 'nickName': f'Native peer {index}'} for index in range(count)]
    if include_filehelper:
        rows.append({'userName': 'filehelper', 'nickName': '文件传输助手'})
    return rows


def app_for(store, edge):
    app = SimpleNamespace(store=store, wechat_client=WeChatWinAgent(edge),
                          accounts=SimpleNamespace(busy=lambda _: False),
                          xunjia_v22_host=SimpleNamespace(loop=asyncio.get_running_loop()))
    app._xunjia_institution_presence = MethodType(AgentApp._xunjia_institution_presence, app)
    app._xunjia_wechat_directory = MethodType(AgentApp._xunjia_wechat_directory, app)
    return app


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path/'account.db')).open()
    value.ensure_account('wx01', 'wechat', state='running', self_uid='wxid_synthetic_current')
    yield value
    value.close()


@pytest.mark.parametrize('peer_count', [201, 4998])
def test_more_than_100_native_sessions_include_the_real_filehelper_record_at_the_end(store, peer_count):
    async def run():
        edge = NativeHTTP(sessions(peer_count, include_filehelper=True))
        app = app_for(store, edge)
        row = store.get_account('wx01')
        raw = await app._xunjia_wechat_directory(row)
        assert len(raw) == peer_count + 1 and raw[-1]['userName'] == 'filehelper'
        assert edge.requests == ['/wa/v1/wechat/status', '/wa/v1/wechat/sessions?limit=5000', '/wa/v1/wechat/status']
        contacts = await asyncio.to_thread(AgentApp._xunjia_institution_friends, app, row)
        assert len(contacts) == len(edge.rows)
        assert contacts[-1] == {'native_id': 'filehelper', 'peer_id': 'wxpeer-ZmlsZWhlbHBlcg',
                                'kind': 'private', 'name': '文件传输助手'}
    asyncio.run(run())


@pytest.mark.parametrize('native_count', [5000, 5001])
def test_limit_reached_retains_exact_previous_publication_timestamp_and_pending_sync(store, tmp_path, native_count):
    async def run():
        edge = NativeHTTP(sessions(1, include_filehelper=True))
        app = app_for(store, edge)
        uploaded = []
        class Client:
            def __init__(self, path): self.path = path
            def upload(self, name, blob): uploaded.append((self.path, name, blob))
        now = [1800000000.0]
        host = InstitutionCollectorHost(store=store, bus=None, loop=asyncio.get_running_loop(),
            keys=InstitutionKeys.from_auth(GatewayAuth('synthetic', b'0123456789abcdef')),
            config={'workbench_name': 'Synthetic WeChat desk', 'contract': CONTRACT},
            settings=SimpleNamespace(workbench_id='synthetic-wb'), data_dir=tmp_path,
            legacy_inbox_disabled=lambda: True,
            friends_provider=lambda row: AgentApp._xunjia_institution_friends(app, row),
            organization_provider=lambda _: pytest.fail('WeChat metadata must not read Qidian organization'),
            client_factory=Client, clock=lambda: now[0])
        app.xunjia_v22_host = host
        host.active = True
        try:
            await asyncio.to_thread(host.metadata_once)
            previous_uploads = list(uploaded)
            assert len(previous_uploads) == 1
            publication_key = 'friends:wechat:wxid_synthetic_current'
            previous_stamp = host.publisher.last(publication_key)
            edge.rows = sessions(native_count)
            now[0] += 60
            host.publisher.request_sync('wechat:wxid_synthetic_current')
            pending = host.publisher.pending_sync()
            await asyncio.to_thread(host.metadata_once)
            assert host.errors.get('contacts:wxid_synthetic_current') == 'WECHAT_DIRECTORY_TRUNCATED'
            assert uploaded == previous_uploads
            assert host.publisher.last(publication_key) == previous_stamp
            assert host.publisher.pending_sync() == pending
        finally:
            host.active = False
    asyncio.run(run())


def test_native_identity_fence_still_runs_after_a_large_directory_read(store):
    async def run():
        edge = NativeHTTP(sessions(201, include_filehelper=True))
        edge.switch_after_sessions = True
        app = app_for(store, edge)
        with pytest.raises(RuntimeError, match='native_identity_unverified'):
            await app._xunjia_wechat_directory(store.get_account('wx01'))
        assert edge.requests[-1] == '/wa/v1/wechat/status'
        assert any(path == '/wa/v1/wechat/sessions?limit=5000' for path in edge.requests)
    asyncio.run(run())
