"""A settings push must never move a provisioned device to another host or
touch the account it signs in as: a remote device cannot be reached to undo it."""
import asyncio

from custom_components.casa import CasaAdminDeviceView, CasaAdminQueueUpdateView, _pushable_fields
from custom_components.casa.const import PROCESS_PROVISIONING_FIELDS
from tests.fakes import FakeHass, FakeRequest, bind_view, make_user

WG = "[Interface]\nPrivateKey = AAAA=\n"


def _hass(template_fields):
    hass = FakeHass()
    hass.casa["stored_data"]["users"]["u1"] = {"username": "alice", "devices": {
        "D1": {"provisioning_fields": {"host_url": "https://h", "default_dashboard": "/old"}},
        "D2": {"provisioning_fields": {"host_url": "https://h"}},
    }}
    hass.casa["pp_data"]["profiles"] = [{"id": "t1", "name": "T", "fields": template_fields}]
    return hass


def _apply(hass, **extra):
    view = bind_view(CasaAdminQueueUpdateView(hass))
    body = {"update_type": "profile", "profile_id": "t1", **extra}
    return asyncio.run(view.post(FakeRequest(make_user("admin", admin=True), body)))


def _sent(hass, device_id):
    return hass.casa["qu_data"].get("updates", {}).get(device_id, [])


def test_filter_keeps_live_settings_only():
    everything = {"default_dashboard": "/x", "host_url": "https://evil", **PROCESS_PROVISIONING_FIELDS, "expiration_hours": 1}
    kept, held = _pushable_fields(everything)
    assert kept == {"default_dashboard": "/x"} and held == ["host_url"]
    kept, held = _pushable_fields({"allow_wireguard": True, "wireguard_config": WG, "wireguard_profile_id": "w"}, skip_wireguard_config=True)
    assert kept == {"allow_wireguard": True} and held == ["wireguard_config", "wireguard_profile_id"]


def test_template_apply_never_sends_host_or_login():
    hass = _hass({"host_url": "https://elsewhere", "default_dashboard": "/new", "username": "bob", "password": "pw"})
    status, resp = _apply(hass, device_id="D1")
    assert status == 200 and resp["queued"] == 1 and resp["not_applied"] == ["host_url"]
    (entry,) = _sent(hass, "D1")
    assert entry["payload"]["fields"] == {"default_dashboard": "/new"}


def test_template_that_only_sets_the_host_applies_nothing():
    hass = _hass({"host_url": "https://elsewhere"})
    status, resp = _apply(hass, device_id="D1")
    assert status == 400 and _sent(hass, "D1") == []


def test_bulk_apply_can_leave_each_devices_wireguard_config_alone():
    hass = _hass({"default_dashboard": "/new", "allow_wireguard": True, "wireguard_config": WG})
    status, resp = _apply(hass, device_ids=["D1", "D2"], skip_wireguard_config=True)
    assert status == 200 and resp["queued"] == 2 and resp["not_applied"] == ["wireguard_config"]
    for did in ("D1", "D2"):
        (entry,) = _sent(hass, did)
        assert entry["type"] == "profile"
        assert entry["payload"]["fields"] == {"default_dashboard": "/new", "allow_wireguard": True}
        assert "wireguard_key_fp" not in hass.casa["stored_data"]["users"]["u1"]["devices"][did]


def test_device_edit_never_sends_or_stores_a_new_host():
    hass = _hass({})
    view = bind_view(CasaAdminDeviceView(hass))
    body = {"device_id": "D1", "provisioning_fields": {"host_url": "https://elsewhere", "default_dashboard": "/new", "username": "bob"}}
    status, _ = asyncio.run(view.put(FakeRequest(make_user("admin", admin=True), body)))
    assert status == 200
    stored = hass.casa["stored_data"]["users"]["u1"]["devices"]["D1"]["provisioning_fields"]
    assert stored == {"host_url": "https://h", "default_dashboard": "/new"}
    (entry,) = _sent(hass, "D1")
    assert entry["payload"]["fields"] == {"default_dashboard": "/new"}


def test_device_edit_with_only_a_host_change_is_refused():
    hass = _hass({})
    view = bind_view(CasaAdminDeviceView(hass))
    body = {"device_id": "D1", "provisioning_fields": {"host_url": "https://elsewhere"}}
    status, resp = asyncio.run(view.put(FakeRequest(make_user("admin", admin=True), body)))
    assert status == 400 and "re-provision" in resp["error"]
    assert _sent(hass, "D1") == []
    assert hass.casa["stored_data"]["users"]["u1"]["devices"]["D1"]["provisioning_fields"]["host_url"] == "https://h"
