import asyncio

from custom_components.casa import (
    CasaAdminDeviceView,
    CasaAdminQueueUpdateView,
    _normalize_reported_fields,
    _split_wireguard_from_profile,
)
from tests.fakes import FakeHass, FakeRequest, bind_view, make_user


def test_legacy_immersive_triple_is_split():
    out = _normalize_reported_fields({"immersive_level": "2,custom,#ff0000"})
    assert out["immersive_level"] == "2"
    assert out["theme_color_mode"] == "custom"
    assert out["custom_color"] == "#ff0000"


def test_explicit_keys_win_over_triple():
    out = _normalize_reported_fields({"immersive_level": "3,custom,#ff0000", "theme_color_mode": "inherit"})
    assert out["immersive_level"] == "3" and out["theme_color_mode"] == "inherit"


def test_types_are_coerced_for_a_lossless_form_round_trip():
    out = _normalize_reported_fields({
        "immersive_level": 2, "cache_control_hours": 48, "require_alias": "false",
        "allow_wireguard": 1, "welcome_url": None, "allowed_pages": "/*",
    })
    assert out["immersive_level"] == "2"
    assert out["cache_control_hours"] == "48"
    assert out["require_alias"] is False and out["allow_wireguard"] is True
    assert out["welcome_url"] == ""
    assert out["allow_all_pages"] is True


def test_wireguard_keys_become_their_own_update():
    wg = {"profiles": [{"id": "w1", "config": "[Interface]\nA", "excluded_wifi": "Home"}]}
    fields, payload = _split_wireguard_from_profile(
        {"default_dashboard": "/x", "wireguard_profile_id": "w1", "wireguard_config": "", "allow_wireguard": True}, wg)
    assert fields == {"default_dashboard": "/x", "allow_wireguard": True}
    assert payload == {"config": "[Interface]\nA", "excluded_wifi": "Home"}
    fields, payload = _split_wireguard_from_profile({"wireguard_config": "", "wireguard_profile_id": ""}, wg)
    assert fields == {} and payload is None


def _hass():
    hass = FakeHass()
    hass.casa["stored_data"]["users"]["u1"] = {"username": "alice", "devices": {
        "D1": {"provisioning_fields": {"host_url": "https://h", "default_dashboard": "/old"}},
    }}
    return hass


def test_force_device_changes_merges_and_splits_wireguard():
    hass = _hass()
    view = bind_view(CasaAdminDeviceView(hass))
    body = {"device_id": "D1", "provisioning_fields": {"default_dashboard": "/new", "wireguard_config": "[Interface]\nB"}}
    status, resp = asyncio.run(view.put(FakeRequest(make_user("admin", admin=True), body)))
    assert status == 200
    stored = hass.casa["stored_data"]["users"]["u1"]["devices"]["D1"]["provisioning_fields"]
    assert stored["host_url"] == "https://h" and stored["default_dashboard"] == "/new"
    entries = hass.casa["qu_data"]["updates"]["D1"]
    assert [(e["type"], e["action"]) for e in entries] == [("profile", "update"), ("wireguard", "update")]
    assert entries[0]["payload"]["fields"] == {"default_dashboard": "/new"}
    assert entries[1]["payload"]["config"] == "[Interface]\nB"


def test_template_apply_splits_wireguard():
    hass = _hass()
    hass.casa["pp_data"]["profiles"] = [{"id": "t1", "name": "T", "fields": {"wireguard_config": "[Interface]\nC"}}]
    view = bind_view(CasaAdminQueueUpdateView(hass))
    body = {"device_id": "D1", "update_type": "profile", "profile_id": "t1"}
    status, resp = asyncio.run(view.post(FakeRequest(make_user("admin", admin=True), body)))
    assert status == 200 and resp["queued"] == 1
    entries = hass.casa["qu_data"]["updates"]["D1"]
    assert [e["type"] for e in entries] == ["wireguard"]
