"""Service entry boundaries use no Windows backend, sockets or production root."""
import os
from unittest.mock import Mock

import pytest

from qtrade_winagent import main_svc
from qtrade_winagent.config import WinAgentConfig


@pytest.mark.parametrize("sid", ["", "not-a-sid", "S-1-5-18"])
def test_real_service_rejects_missing_or_non_install_user_sid_before_io(monkeypatch, tmp_path, sid):
    """The LocalSystem service cannot silently use its own identity for pipe trust."""
    monkeypatch.delenv("QT_INSTALL_USER_SID", raising=False)
    io = Mock(side_effect=AssertionError("service performed IO before validating install user SID"))
    monkeypatch.setattr(main_svc, "read_toml", io)
    with pytest.raises(SystemExit) as error:
        main_svc.main(["--root", str(tmp_path), "--install-user-sid", sid])
    assert error.value.code != 0
    io.assert_not_called()


def test_production_service_does_not_trust_inherited_install_sid(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_INSTALL_USER_SID", "S-1-5-21-1111-2222-3333-1001")
    io = Mock(side_effect=AssertionError("an inherited environment value is not installer trust"))
    monkeypatch.setattr(main_svc, "read_toml", io)
    with pytest.raises(SystemExit) as error:
        main_svc.main(["--root", str(tmp_path)])
    assert error.value.code == 2
    io.assert_not_called()


def test_firewall_program_is_the_service_in_installed_app_directory(monkeypatch, tmp_path):
    from qtrade_winagent.fakes import FakeFirewall
    seen = []
    monkeypatch.setattr(FakeFirewall, "put_rule", lambda self, spec: seen.append(spec) or "created")
    d = main_svc.build_real_deps(WinAgentConfig(), root=str(tmp_path),
                                install_user_sid="S-1-5-21-1111-2222-3333-1001", fake=True)
    try:
        d.netprobe.firewall_ensure()
        assert len(seen) == 1
        assert os.path.normpath(seen[0].program) == os.path.normpath(str(tmp_path / "app/qtrade-winagent-svc.exe"))
    finally:
        d.db.close()


async def test_windows_adapter_ip_is_never_claimed_as_wsl_guest_endpoint(rig):
    state = await rig.deps.netprobe.refresh()
    assert state.host_ip
    assert state.agent_base_url is None, "without a guest probe, gateway:17600 is not an Agent address"


@pytest.mark.parametrize("root", [r"D:\Trade Apps\winagent", r"E:\Offline\QTrade\winagent"])
def test_shipped_paths_follow_actual_install_root_before_windows_environment_expansion(monkeypatch, root):
    import ntpath
    monkeypatch.setattr(os.path, "expandvars", lambda text: text.replace("%ProgramData%", r"C:\ProgramData"))
    assert main_svc.expand(r"%ProgramData%\QTrade\vault", root) == ntpath.join(ntpath.dirname(root), "vault")
    assert main_svc.expand(r"%ProgramData%\QTrade\wechat\chatlog.exe", root) == ntpath.join(
        ntpath.dirname(root), "wechat", "chatlog.exe")
    assert main_svc.expand(r"F:\Explicit\data", root) == r"F:\Explicit\data"


@pytest.mark.parametrize("sid", ["S-1-5-21-1111-2222-3333-1001", "S-1-12-1-1111-2222-3333-4444"])
def test_valid_install_identity_is_passed_to_scm_dispatch_before_any_application_io(monkeypatch, tmp_path, sid):
    from qtrade_winagent import service_host
    dispatch = Mock(return_value=0)
    io = Mock(side_effect=AssertionError("foreground application IO before service dispatcher"))
    monkeypatch.setattr(service_host, "dispatch", dispatch)
    monkeypatch.setattr(main_svc, "read_toml", io)
    assert main_svc.main(["--service", "--root", str(tmp_path), "--install-user-sid", sid]) == 0
    assert dispatch.call_args.args[0].install_user_sid == sid
    assert dispatch.call_args.args[0].root == str(tmp_path)
    io.assert_not_called()
