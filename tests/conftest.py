import pytest


@pytest.fixture(autouse=True)
def never_touch_real_system_settings(monkeypatch, tmp_path):
    """Tests run with a temp config and with the macOS system proxy disabled for good."""
    import maxnet.net as net
    import maxnet.sysproxy as sysproxy
    monkeypatch.setattr(net, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(sysproxy, "SUPPORTED", False)
