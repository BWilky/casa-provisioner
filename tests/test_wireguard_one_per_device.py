"""One WireGuard client per device: reuse is tracked on the device record and
refused unless the admin explicitly allows sharing."""
import asyncio
import time

from custom_components.casa import (
    CasaAdminDeviceView,
    CasaAdminQueueUpdateView,
    CasaWireGuardProfilesView,
    _stash_pending_profile,
    _wireguard_conflict,
    _wireguard_holders,
    _wireguard_key_fingerprint,
)
from tests.fakes import FakeHass, FakeRequest, bind_view, make_user

CONF_A = "[Interface]\nPrivateKey = AAAA=\nAddress = 10.0.0.2/32\n\n[Peer]\nPublicKey = SRV=\n"
CONF_B = "[Interface]\nPrivateKey = BBBB=\nAddress = 10.0.0.3/32\n\n[Peer]\nPublicKey = SRV=\n"


def _hass():
    hass = FakeHass()
    hass.casa["stored_data"]["users"]["u1"] = {"username": "alice", "devices": {
        "D1": {"alias": "Alice phone", "provisioning_fields": {}},
        "D2": {"provisioning_fields": {}},
    }}
    return hass


def _queue(hass, body):
    view = bind_view(CasaAdminQueueUpdateView(hass))
    return asyncio.run(view.post(FakeRequest(make_user("admin", admin=True), body)))


def _wg(device_id, config, **extra):
    return {"device_id": device_id, "update_type": "wireguard", "action": "update", "wireguard_config": config, **extra}


def _devices(hass):
    return hass.casa["stored_data"]["users"]["u1"]["devices"]


def test_fingerprint_follows_the_private_key_only():
    same_key = CONF_A.replace("10.0.0.2", "10.9.9.9").replace("PrivateKey = ", "privatekey=")
    assert _wireguard_key_fingerprint(CONF_A) == _wireguard_key_fingerprint(same_key)
    assert _wireguard_key_fingerprint(CONF_A) != _wireguard_key_fingerprint(CONF_B)
    assert "AAAA" not in _wireguard_key_fingerprint(CONF_A)
    assert _wireguard_key_fingerprint("[Interface]\nAddress = 1.2.3.4") is None
    assert _wireguard_key_fingerprint("") is None


def test_first_push_is_recorded_and_second_device_is_refused():
    hass = _hass()
    status, _ = _queue(hass, _wg("D1", CONF_A))
    assert status == 200
    assert _devices(hass)["D1"]["wireguard_key_fp"] == _wireguard_key_fingerprint(CONF_A)

    status, resp = _queue(hass, _wg("D2", CONF_A))
    assert status == 409 and resp["code"] == "wireguard_in_use"
    assert resp["holders"] == ["Alice phone (alice)"] and "Alice phone" in resp["message"]
    assert "D2" not in hass.casa["qu_data"].get("updates", {})
    assert "wireguard_key_fp" not in _devices(hass)["D2"]


def test_same_device_can_be_sent_its_own_config_again():
    hass = _hass()
    assert _queue(hass, _wg("D1", CONF_A))[0] == 200
    assert _queue(hass, _wg("D1", CONF_A))[0] == 200


def test_allow_shared_overrides_and_records_both():
    hass = _hass()
    _queue(hass, _wg("D1", CONF_A))
    status, _ = _queue(hass, _wg("D2", CONF_A, allow_shared=True))
    assert status == 200
    assert _devices(hass)["D2"]["wireguard_key_fp"] == _devices(hass)["D1"]["wireguard_key_fp"]


def test_one_config_to_several_devices_at_once_is_refused():
    hass = _hass()
    body = {"device_ids": ["D1", "D2"], "update_type": "wireguard", "action": "update", "wireguard_config": CONF_A}
    status, resp = _queue(hass, body)
    assert status == 409 and "2 devices" in resp["message"]
    assert hass.casa["qu_data"].get("updates", {}) == {}


def test_revoke_frees_the_config():
    hass = _hass()
    _queue(hass, _wg("D1", CONF_A))
    status, _ = _queue(hass, {"device_id": "D1", "update_type": "wireguard", "action": "revoke"})
    assert status == 200 and "wireguard_key_fp" not in _devices(hass)["D1"]
    assert _queue(hass, _wg("D2", CONF_A))[0] == 200


def test_template_apply_is_checked_too():
    hass = _hass()
    _queue(hass, _wg("D1", CONF_A))
    hass.casa["pp_data"]["profiles"] = [{"id": "t1", "name": "T", "fields": {"wireguard_config": CONF_A}}]
    status, resp = _queue(hass, {"device_id": "D2", "update_type": "profile", "profile_id": "t1"})
    assert status == 409 and resp["code"] == "wireguard_in_use"


def test_device_editor_push_is_checked_before_anything_is_stored():
    hass = _hass()
    _queue(hass, _wg("D1", CONF_A))
    view = bind_view(CasaAdminDeviceView(hass))
    body = {"device_id": "D2", "provisioning_fields": {"default_dashboard": "/x", "wireguard_config": CONF_A}}
    status, resp = asyncio.run(view.put(FakeRequest(make_user("admin", admin=True), body)))
    assert status == 409 and resp["code"] == "wireguard_in_use"
    assert _devices(hass)["D2"]["provisioning_fields"] == {}
    assert "D2" not in hass.casa["qu_data"].get("updates", {})

    body["allow_shared"] = True
    status, _ = asyncio.run(view.put(FakeRequest(make_user("admin", admin=True), body)))
    assert status == 200 and _devices(hass)["D2"]["wireguard_key_fp"]


def test_configs_without_a_private_key_are_not_tracked():
    hass = _hass()
    assert _queue(hass, _wg("D1", "[Interface]\nX"))[0] == 200
    assert _queue(hass, _wg("D2", "[Interface]\nX"))[0] == 200
    assert "wireguard_key_fp" not in _devices(hass)["D1"]


def test_profiles_listing_reports_who_uses_each():
    hass = _hass()
    hass.casa["wg_data"] = {"profiles": [{"id": "w1", "alias": "A", "config": CONF_A}, {"id": "w2", "alias": "B", "config": CONF_B}]}
    _queue(hass, _wg("D1", CONF_A))
    view = bind_view(CasaWireGuardProfilesView(hass))
    status, resp = asyncio.run(view.get(FakeRequest(make_user("admin", admin=True), None)))
    assert status == 200
    by_id = {p["id"]: p for p in resp["profiles"]}
    assert by_id["w1"]["used_by"] == ["Alice phone (alice)"] and by_id["w2"]["used_by"] == []
    assert "used_by" not in hass.casa["wg_data"]["profiles"][0]


def test_unredeemed_setup_link_holds_the_config_until_it_expires():
    hass = _hass()
    fp = _wireguard_key_fingerprint(CONF_A)
    stash = dict(profile_id=None, profile_name=None, device_alias="New iPad", expiration_hours=None)
    _stash_pending_profile(hass, "u9", wireguard_key_fp=fp, valid_until=time.time() + 600, **stash)
    stored = hass.casa["stored_data"]
    assert _wireguard_holders(stored, fp) == ["New iPad (awaiting setup)"]
    # A regenerated link for the same account is not sharing.
    assert _wireguard_conflict(stored, CONF_A, exclude_user_id="u9") is None
    assert _wireguard_conflict(stored, CONF_A, exclude_user_id="u1")["code"] == "wireguard_in_use"
    stored["pending_profiles"]["u9"]["valid_until"] = time.time() - 1
    assert _wireguard_holders(stored, fp) == []
