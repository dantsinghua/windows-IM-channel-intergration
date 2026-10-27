"""Retired slots must not be relabelled as Qidian in status responses."""
from types import SimpleNamespace
from qtrade_agent.ossbox.institution import InstitutionCollectorHost


def test_deleted_wechat_slot_does_not_generate_qidian_presence():
    host = InstitutionCollectorHost.__new__(InstitutionCollectorHost)
    host.rows = lambda: [{"id": "wx07", "channel": "wechat", "self_uid": "synthetic-wx"}]
    host.config = {"contract": "its-relay-path-v1"}
    host.active = True
    host.workbench = "synthetic"
    host.applied_revision = "revision"
    host.last_directory_at = 1
    host.hosts = {}
    host.errors = {}
    host.histories = {}
    host.presence = {("wx06", "synthetic-wx"): {"state": "offline"},
                     ("wx07", "synthetic-wx"): {"state": "online"}}
    data = host.status()["native_presence"]
    assert len(data) == 1
    assert data["wechat:synthetic-wx"]["state"] == "online"
    assert data["wechat:synthetic-wx"]["channel"] == "wechat"


def test_retained_slot_with_changed_uid_does_not_publish_old_identity():
    host = InstitutionCollectorHost.__new__(InstitutionCollectorHost)
    host.rows = lambda: [{"id": "wx07", "channel": "wechat", "self_uid": "new-uid"}]
    host.config = {"contract": "its-relay-path-v1"}
    host.active = True
    host.workbench = "synthetic"
    host.applied_revision = "revision"
    host.last_directory_at = 1
    host.hosts = {}
    host.errors = {}
    host.histories = {}
    host.presence = {("wx07", "old-uid"): {"state": "online"}}
    assert host.status()["native_presence"] == {}
