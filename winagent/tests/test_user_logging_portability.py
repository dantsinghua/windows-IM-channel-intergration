"""User-session logging never requires access to LocalSystem service logs."""
from __future__ import annotations

import ntpath
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qtrade_winagent import main_svc, main_user
from qtrade_winagent.config import WinAgentConfig


SID = "S-1-5-21-1111-2222-3333-1001"
SERVICE_ROOT = r"C:\ProgramData\QTrade\winagent"


@pytest.fixture
def user_entry(monkeypatch):
    """Stop at entry wiring: no pipe connection, WSL call, or real UserAgent."""
    monkeypatch.setattr(main_user, "current_user_sid", lambda: SID)
    monkeypatch.setattr(main_user, "read_toml", lambda _: {})
    monkeypatch.setattr(main_user, "load_cfg", lambda _: WinAgentConfig())
    build = Mock(return_value=object())
    monkeypatch.setattr(main_user, "build_user_agent", build)

    async def finished(_):
        return 0

    monkeypatch.setattr(main_user, "run_forever", finished)
    logging = Mock()
    monkeypatch.setattr(main_user, "setup_logging", logging)
    return SimpleNamespace(build=build, logging=logging)


@pytest.mark.parametrize("profile", [
    r"C:\Users\Alice\AppData\Local",
    r"E:\User Profiles\Test User\AppData\Local",
    "E:\\用户\\安琳\\AppData\\Local",
    r"\\profile-server\users\Alice\Local",
])
def test_user_logs_are_under_the_current_users_local_appdata(monkeypatch, user_entry, profile):
    monkeypatch.setenv("LOCALAPPDATA", profile)
    assert main_user.main(["--root", SERVICE_ROOT]) == 0
    user_entry.logging.assert_called_once()
    assert ntpath.normpath(user_entry.logging.call_args.args[1]) == ntpath.join(
        profile, "QTrade", "logs", "user",
    )
    assert user_entry.build.call_args.kwargs["root"] == SERVICE_ROOT
    assert user_entry.build.call_args.kwargs["fake"] is False


@pytest.mark.parametrize("profile", [None, "", " ", "relative", r"C:relative", r"\root-relative"])
def test_unavailable_user_profile_fails_before_logging_or_agent_start(monkeypatch, user_entry, capsys, profile):
    if profile is None:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
    else:
        monkeypatch.setenv("LOCALAPPDATA", profile)
    with pytest.raises(SystemExit) as error:
        main_user.main(["--root", SERVICE_ROOT])
    assert error.value.code == 2
    assert "LOCALAPPDATA" in capsys.readouterr().err
    user_entry.logging.assert_not_called()
    user_entry.build.assert_not_called()


def test_protected_service_log_is_never_touched_by_user_entry(monkeypatch, user_entry):
    profile = r"D:\Users\Alice\AppData\Local"
    monkeypatch.setenv("LOCALAPPDATA", profile)

    def deny_service_log(_level, path):
        if ntpath.normcase(path).startswith(ntpath.normcase(SERVICE_ROOT)):
            raise PermissionError("service log is owned by LocalSystem")
        assert ntpath.normpath(path) == ntpath.join(profile, "QTrade", "logs", "user")

    user_entry.logging.side_effect = deny_service_log
    assert main_user.main(["--root", SERVICE_ROOT]) == 0
    user_entry.logging.assert_called_once()
    user_entry.build.assert_called_once()


def test_default_install_root_does_not_override_the_user_log_location(monkeypatch, user_entry):
    profile = r"E:\Users\Alice\AppData\Local"
    monkeypatch.setenv("LOCALAPPDATA", profile)
    assert main_user.main([]) == 0
    assert ntpath.normpath(user_entry.logging.call_args.args[1]) == ntpath.join(profile, "QTrade", "logs", "user")
    assert user_entry.build.call_args.kwargs["root"] == main_user.DEFAULT_ROOT


def test_dev_logs_remain_stderr_without_a_user_profile(monkeypatch, user_entry, tmp_path):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    root = str(tmp_path / "dev-root")
    assert main_user.main(["--dev", "--root", root]) == 0
    assert user_entry.logging.call_args.args[1] is None
    assert user_entry.build.call_args.kwargs["fake"] is True
    assert not Path(root).exists()


def test_service_logging_remains_in_the_service_owned_root(monkeypatch):
    """The user path change must not relocate or relax the service log sink."""
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Alice\AppData\Local")
    monkeypatch.setattr(main_svc, "read_toml", lambda _: {})
    monkeypatch.setattr(main_svc, "load_cfg", lambda _: WinAgentConfig())

    class StopBeforeServiceStart(Exception):
        pass

    logging = Mock(side_effect=StopBeforeServiceStart)
    build = Mock(side_effect=AssertionError("must not construct real service dependencies"))
    monkeypatch.setattr(main_svc, "setup_logging", logging)
    monkeypatch.setattr(main_svc, "build_real_deps", build)
    with pytest.raises(StopBeforeServiceStart):
        main_svc.main(["--root", SERVICE_ROOT, "--install-user-sid", SID])
    assert ntpath.normpath(logging.call_args.args[1]) == ntpath.join(SERVICE_ROOT, "logs")
    build.assert_not_called()
