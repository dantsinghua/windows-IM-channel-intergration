"""ACL policy is based on well-known SIDs rather than localized account names."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qtrade_winagent.win import dpapi


def security_stub(monkeypatch, *, protected=True, aces=None):
    if aces is None:
        aces = [((0, 0), 0x1F01FF, "S-1-5-18"), ((0, 0), 0x1F01FF, "S-1-5-32-544")]
    acl = SimpleNamespace(GetAceCount=lambda: len(aces), GetAce=lambda index: aces[index])
    sd = SimpleNamespace(GetSecurityDescriptorControl=lambda: (0x1000 if protected else 0, 1),
                         GetSecurityDescriptorDacl=lambda: acl)
    sec = SimpleNamespace(SDDL_REVISION_1=1, SE_FILE_OBJECT=1, DACL_SECURITY_INFORMATION=4,
                          PROTECTED_DACL_SECURITY_INFORMATION=0x80000000,
                          ConvertStringSecurityDescriptorToSecurityDescriptor=Mock(return_value=sd),
                          SetNamedSecurityInfo=Mock(), GetNamedSecurityInfo=Mock(return_value=sd),
                          ConvertSidToStringSid=lambda sid: sid)
    monkeypatch.setitem(sys.modules, "win32security", sec)
    monkeypatch.setattr(dpapi, "require_windows", lambda *_: None)
    return sec


def test_acl_apply_and_readback_use_well_known_sids(monkeypatch, tmp_path):
    sec = security_stub(monkeypatch)
    path = str(tmp_path / "熵目录/entropy.bin")
    dpapi.WinCrypto().tighten_acl(path)
    sec.ConvertStringSecurityDescriptorToSecurityDescriptor.assert_called_once_with(
        "D:P(A;;FA;;;SY)(A;;FA;;;BA)", 1)
    args = sec.SetNamedSecurityInfo.call_args.args
    assert args[0] == path
    assert args[2] == 0x80000004
    sec.GetNamedSecurityInfo.assert_called_once()


@pytest.mark.parametrize("protected,aces", [
    (False, None),
    (True, []),
    (True, [((0, 0), 0x1F01FF, "S-1-5-18")]),
    (True, [((0, 0), 0x1F01FF, "S-1-5-18"), ((0, 0), 0x1F01FF, "S-1-1-0")]),
    (True, [((0, 16), 0x1F01FF, "S-1-5-18"), ((0, 0), 0x1F01FF, "S-1-5-32-544")]),
    (True, [((0, 0), 0x1F01FF, "S-1-5-18"), ((0, 0), 0x120089, "S-1-5-32-544")]),
])
def test_acl_readback_rejects_weakened_or_inherited_permissions(monkeypatch, protected, aces):
    security_stub(monkeypatch, protected=protected, aces=aces)
    assert dpapi.WinCrypto().acl_is_tight("fixture") is False


def test_acl_write_success_with_bad_readback_is_not_reported_as_secure(monkeypatch):
    security_stub(monkeypatch, protected=False)
    with pytest.raises(PermissionError):
        dpapi.WinCrypto().tighten_acl("fixture")
