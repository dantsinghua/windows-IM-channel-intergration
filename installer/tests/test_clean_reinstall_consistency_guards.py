"""Negative controls for the R6-101/102 replacement contract; no source writes."""
from pathlib import Path

import pytest

import test_spec_consistency as contract
import test_signing_consistency as signing


@pytest.mark.parametrize("relative,old,new,check", [
    ("engine/qtrade-setup-engine.iss", "/QT_ACCEPT_CLEAN_REINSTALL=", "/QT_UNDOCUMENTED=", "switch"),
    ("engine/qtrade-setup-engine.iss", "RunStep('clean-reinstall')", "RunStep('upgrade')", "states"),
    ("engine/qtrade-setup-engine.iss", "OptAcceptCleanReinstall := True", "OptAcceptCleanReinstall := False", "consent"),
    ("sfx-stub/qtrade-sfx.patch", "kQTradeExitDiskLow = 26", "kQTradeExitDiskLow = 0", "disk"),
    ("engine/qtrade-setup-engine.iss", "Uninstallable=no", "Uninstallable=yes", "uninstall"),
    ("engine/run-step.ps1", "'FULL_INSTALLER_REQUIRED'", "'UNGUARDED_UPGRADE'", "upgrade"),
    ("engine/precheck-disk.cmd", "exit /b 41", "exit /b 0", "legacy"),
    ("engine/precheck-disk.cmd", "exit /b 41", "move fixture live\nexit /b 41", "move"),
    ("sfx-stub/qtrade-sfx.patch", "SHGetFolderPathW(NULL, CSIDL_COMMON_APPDATA", "GetEnvironmentVariableW(NULL, CSIDL_COMMON_APPDATA", "known-folder"),
    ("build/build.ps1", "$Stub -eq 'official') { throw", "$Stub -eq 'official') { Write-Warning", "official"),
    ("engine/run-step.ps1", "$aclRes = Set-QtInstallRootAcl -Path $paths.Root", "$aclRes = BypassedAcl -Path $paths.Root", "acl"),
    ("engine/run-step.ps1", "'E_INSTALL_ACL_HARDEN_FAILED'", "'E_INSTALL_INTERNAL'", "acl-exit"),
    ("sfx-stub/qtrade-sfx.patch", "+++ b/CPP/7zip/Bundles/SFXSetup/QTradeStage.h", "+++ b/CPP/7zip/Bundles/SFXSetup/Unexpected.h", "patch-scope"),
    ("build/build.ps1", "'*.ps1', '*.cmd'", "'*.ps1'", "cmd-copy"),
    ("signing/QTrade.Signing.psm1", "GetFolderPath('LocalApplicationData')", "GetFolderPath('UserProfile')", "signing-default"),
])
def test_updated_contract_rejects_a_broken_copy(monkeypatch, relative, old, new, check):
    path = contract.INSTALLER_ROOT / relative
    real_read = contract.read
    text = real_read(path)
    assert old in text, f"negative control no longer matches {path}"
    mutated = text.replace(old, new)
    monkeypatch.setattr(contract, "read", lambda p: mutated if Path(p) == path else real_read(p))
    real_bytes_read = contract._read_bytes_text
    monkeypatch.setattr(contract, "_read_bytes_text", lambda p: mutated if Path(p) == path else real_bytes_read(p))
    checks = {
        "switch": lambda: contract.test_iss_declares_every_spec_switch(real_read(contract.DOC_03), mutated),
        "states": contract.test_iss_drives_every_state,
        "consent": contract.test_iss_has_interactive_branches,
        "disk": contract.test_ruling_1_sfx_disk_maps_to_26,
        "uninstall": lambda: contract.test_iss_uninstallrun_has_runonceid(mutated),
        "upgrade": contract.test_upgrade_wires_image_load_step,
        "legacy": contract.test_precheck_stages_payload_to_programdata,
        "move": contract.test_precheck_does_not_move_its_own_directory,
        "known-folder": contract.test_patch_adds_installpath_key,
        "official": lambda: contract.test_r6_58_cm_selfbuilt_stub_is_primary_and_fallback_warns(real_read(contract.DOC_03)),
        "acl": contract.test_payload_staged_hardens_acl_before_everything_else,
        "acl-exit": contract.test_acl_failure_blocks_the_install,
        "patch-scope": contract.test_patch_embeds_explicit_manifest,
        "cmd-copy": lambda: signing.TestBuildIntegration().test_cmd_not_signed(mutated),
        "signing-default": lambda: signing.TestPrivateKeyNeverLeaves().test_cert_dir_is_outside_repo(mutated),
    }
    with pytest.raises((AssertionError, ValueError)):
        checks[check]()


def assert_finished_firewall_warning(iss: str):
    done = iss.index("RunStep(ST_DONE)")
    warning = iss.index("FirewallWarning := JsonStr(LastStepJson, 'firewall_message');", done)
    assert done < warning
    assert "if (CurPageID = wpFinished) and (FirewallWarning <> '') then" in iss
    assert "WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + FirewallWarning;" in iss


def test_policy_warning_is_bound_to_the_finished_page():
    assert_finished_firewall_warning(contract.read(contract.ISS))


@pytest.mark.parametrize("old,new", [
    ("JsonStr(LastStepJson, 'firewall_message')", "JsonStr(LastStepJson, 'unused_message')"),
    ("CurPageID = wpFinished", "CurPageID = wpWelcome"),
    ("+ #13#10#13#10 + FirewallWarning;", "+ #13#10#13#10;"),
])
def test_missing_policy_warning_display_is_detected(old, new):
    text = contract.read(contract.ISS)
    assert old in text
    with pytest.raises((AssertionError, ValueError)):
        assert_finished_firewall_warning(text.replace(old, new))
