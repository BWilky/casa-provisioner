from custom_components.casa import _reprovision_service_data
from tests.fakes import FakeHass

ORIGIN = "http://192.168.1.21:8123"


def _hass(templates=()):
    hass = FakeHass()
    hass.casa["pp_data"]["profiles"] = list(templates)
    return hass


def test_seed_uses_reported_fields_and_live_template():
    hass = _hass([{"id": "t1", "name": "Mobile Owners", "fields": {"host_url": "https://t"}}])
    info = {"alias": "Kitchen iPad", "provisioning_profile_id": "t1",
            "provisioning_fields": {"host_url": "https://h", "default_dashboard": "/d", "bogus": 1}}
    data = _reprovision_service_data(hass, info, ORIGIN)
    assert data["method"] == "qr"
    assert data["deauthenticate_existing"] is False
    assert data["host_url"] == "https://h"
    assert data["default_dashboard"] == "/d"
    assert "bogus" not in data
    assert data["profile"] == "t1"
    assert data["device_alias"] == "Kitchen iPad"


def test_seed_normalizes_legacy_reports():
    data = _reprovision_service_data(_hass(), {"provisioning_fields": {
        "host_url": "https://h", "immersive_level": "2,custom,#ff0000"}}, ORIGIN)
    assert data["immersive_level"] == "2"
    assert data["theme_color_mode"] == "custom"


def test_seed_without_reports_uses_template_host():
    hass = _hass([{"id": "t1", "name": "T", "fields": {"host_url": "https://t"}}])
    data = _reprovision_service_data(hass, {"provisioning_profile_id": "t1"}, ORIGIN)
    assert data["profile"] == "t1"
    assert "host_url" not in data  # the template supplies it via get_field


def test_seed_falls_back_to_defaults_and_origin():
    data = _reprovision_service_data(_hass(), {"provisioning_profile_id": "deleted"}, ORIGIN)
    assert "profile" not in data
    assert data["host_url"] == ORIGIN
    assert "device_alias" not in data
