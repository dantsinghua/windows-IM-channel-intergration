"""Current shared Institution wire contracts; temporary ledgers and synthetic queues.

The real collector-to-deposit App/Hub/ODS gate remains the separate
deposit tests/contracts/deposit_native_producer.py suite.
"""
from collections import defaultdict

import pytest

from relay.file_gateway import GatewayAuth
from relay.gateway_paths import InstitutionKeys, account_paths
from relay.protocol import ProtocolError, digest
from server.institution_transport import InstitutionTransport

NOW=1800000000


@pytest.fixture
def endpoints(tmp_path):
    queues=defaultdict(list)
    class Client:
        def __init__(self,path):self.path=path;self.queue_identity=('synthetic-relay',path)
        def pending(self):return len(queues[self.path])
        def upload(self,name,blob):
            queues[self.path].append((name,blob))
            return {'file_key':name,'file_name':name}
    keys=InstitutionKeys.from_auth(GatewayAuth('synthetic-contract',b'0123456789abcdef'))
    wire=keys.binding('synthetic-wb','shared-native',channel='wechat')
    local=keys.binding('synthetic-wb','shared-native',owner_id='synthetic-authorized-user',channel='wechat')
    paths=account_paths(wire.workbench_id,wire.account_uid,channel='wechat')
    collector=InstitutionTransport(wire,Client(paths['downlink_path']),Client(paths['uplink_path']),
        tmp_path/'collector.db',role='collector',institution_owner=keys.owner_id,clock=lambda:NOW)
    service=InstitutionTransport(local,Client(paths['uplink_path']),Client(paths['downlink_path']),
        tmp_path/'service.db',role='service',institution_owner=keys.owner_id,clock=lambda:NOW)
    service.online_guard=lambda:True
    return keys,collector,service,paths,queues


def publish_event(collector):
    return collector.publish_event({'event_id':'native-original','peer_id':'synthetic-peer',
        'direction':'in','text':'synthetic original text','ext_msg_id':'native-42',
        'kind':'message','occurred_at':NOW,'channel':'wechat','identity_epoch':0,
        'native_account_uid':'shared-native'})


def command(service):
    binding=service.binding
    return {'owner_user_id':binding.owner_user_id,'account_uid':binding.account_uid,
        'workbench_id':binding.workbench_id,'channel':'wechat','command_id':'authorized-command',
        'peer_id':'synthetic-peer','control_version':1,'text':'synthetic authorized reply',
        'content_hash':digest('immutable-authority'),'expires_at':NOW+120,'transport':'oss',
        'business_version':'2.2','source_watermark':1}


def test_current_collector_and_selected_service_open_each_others_signed_packets(endpoints):
    keys,collector,service,paths,queues=endpoints
    assert publish_event(collector)==1
    [(_,event_blob)]=queues[paths['uplink_path']]
    event=service.codec.unpack(event_blob)
    assert event['owner_user_id']==service.binding.owner_user_id
    assert event['account_uid']=='shared-native' and event['channel']=='wechat'
    assert event['payload']['text']=='synthetic original text'
    assert event['payload']['source_seq']==1 and event['payload']['ext_msg_id']=='native-42'
    request=command(service)
    assert service.send(request)['status']=='accepted'
    [(_,command_blob)]=queues[paths['downlink_path']]
    received=collector.codec.unpack(command_blob)
    assert received['owner_user_id']==keys.owner_id
    assert received['payload']=={**request,'owner_user_id':keys.owner_id}
    assert b'synthetic original text' not in event_blob
    assert b'synthetic authorized reply' not in command_blob
    assert keys.seal_key not in event_blob and keys.sign_key not in command_blob


def test_current_wire_rejects_reversed_direction_and_foreign_channel_or_identity(endpoints):
    keys,collector,service,paths,queues=endpoints
    publish_event(collector);service.send(command(service))
    event_blob=queues[paths['uplink_path']][0][1]
    command_blob=queues[paths['downlink_path']][0][1]
    before={path:list(items) for path,items in queues.items()}
    with pytest.raises(ProtocolError,match='OUTBOUND_DIRECTION'):
        collector._publish(collector.codec.unpack(command_blob),command_blob)
    with pytest.raises(ProtocolError,match='OUTBOUND_DIRECTION'):
        service._publish(service.codec.unpack(event_blob),event_blob)
    for changes in ({'channel':'qidian'},{'account_uid':'other-native'},{'workbench_id':'other-wb'}):
        from relay.institution_codec import InstitutionCodec
        target={'workbench_id':'synthetic-wb','account_uid':'shared-native','channel':'wechat',**changes}
        binding=keys.binding(target['workbench_id'],target['account_uid'],owner_id='synthetic-authorized-user',channel=target['channel'])
        with pytest.raises(ProtocolError):InstitutionCodec(binding,keys.owner_id,clock=lambda:NOW).unpack(event_blob)
    assert {path:list(items) for path,items in queues.items()}==before
