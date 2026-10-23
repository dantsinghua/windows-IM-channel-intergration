"""WSL host-network selection from adapter snapshots; no PowerShell or WMI."""
from unittest.mock import Mock

import pytest

from qtrade_winagent.backends import Adapter
from qtrade_winagent.win.netinfo import WinNet


def adapter(name="vEthernet (WSL)", description="Hyper-V Virtual Ethernet Adapter", *, ip="172.20.0.1", up=True):
    return Adapter(name=name, description=description, up=up, ipv4=ip, prefix_length=20, is_virtual=True)


def select(monkeypatch, *rows):
    net = WinNet()
    monkeypatch.setattr(net, "adapters", Mock(return_value=list(rows)))
    return net.wsl_adapter()


@pytest.mark.parametrize("suffix", ["", " #2", " #123"])
@pytest.mark.parametrize("wsl_first", [True, False])
def test_named_wsl_adapter_wins_over_default_switch_regardless_of_number_or_order(monkeypatch, suffix, wsl_first):
    target = adapter(description="Hyper-V Virtual Ethernet Adapter" + suffix)
    unrelated = adapter("vEthernet (Default Switch)", ip="172.23.176.1")
    rows = (target, unrelated) if wsl_first else (unrelated, target)
    assert select(monkeypatch, *rows) is target


@pytest.mark.parametrize("suffix", [" #x", " #2 unrelated", " Plus", "X"])
def test_description_must_be_the_hyperv_base_name_with_optional_numeric_suffix(monkeypatch, suffix):
    assert select(monkeypatch, adapter(description="Hyper-V Virtual Ethernet Adapter" + suffix)) is None


def test_multiple_active_named_wsl_adapters_are_ambiguous(monkeypatch):
    first = adapter()
    second = adapter("vEthernet (WSL alternate)", "Hyper-V Virtual Ethernet Adapter #2", ip="172.21.0.1")
    fallback = adapter("vEthernet (Default Switch)", ip="172.23.176.1")
    assert select(monkeypatch, first, second, fallback) is None


@pytest.mark.parametrize("suffix", ["", " #2"])
def test_only_one_eligible_hyperv_fallback_is_allowed_when_no_named_wsl_exists(monkeypatch, suffix):
    fallback = adapter("vEthernet (Default Switch)", "Hyper-V Virtual Ethernet Adapter" + suffix)
    assert select(monkeypatch, fallback) is fallback


def test_multiple_eligible_fallbacks_do_not_guess_the_first_adapter(monkeypatch):
    first = adapter("vEthernet (Default Switch)")
    second = adapter("vEthernet (Other)", ip="172.21.0.1")
    assert select(monkeypatch, first, second) is None


@pytest.mark.parametrize("ip,up", [(None, True), ("invalid", True), ("8.8.8.8", True), ("172.20.0.1", False)])
def test_fallback_requires_an_active_adapter_with_an_in_range_ipv4(monkeypatch, ip, up):
    assert select(monkeypatch, adapter("vEthernet (Default Switch)", ip=ip, up=up)) is None
