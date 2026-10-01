import asyncio

from custom_components.casa import (
    CasaAdminReauthDeviceView,
    _set_account_password,
)
from tests.fakes import FakeHass, FakeRequest, bind_view, make_user


def _auth_entry(eid, username, password):
    return {"id": eid, "type": "auth", "action": "reauthenticate",
            "payload": {"username": username, "password": password}}


def _hass_with_queue():
    hass = FakeHass()
    hass.auth.add_user(make_user("u1", login="kitchen"), password="old")
    hass.auth.add_user(make_user("u2", login="den"), password="denpw")
    sd = hass.casa["stored_data"]
    sd["users"]["u1"] = {"username": "kitchen", "devices": {
        "D1": {"reauth_pending": {"update_id": "e1", "target_username": "kitchen"}},
        "D2": {"reauth_pending": {"update_id": "e2", "target_username": "den"}},
    }}
    hass.casa["qu_data"]["updates"] = {
        "D1": [_auth_entry("e1", "Kitchen", "old"), {"id": "p1", "type": "profile", "payload": {}}],
        "D2": [_auth_entry("e2", "den", "denpw")],
    }
    return hass


def test_rotation_drops_stale_entries_and_markers_for_that_user_only():
    hass = _hass_with_queue()
    pw = asyncio.run(_set_account_password(hass, hass.auth.provider, "kitchen"))
    assert hass.auth.provider.data.passwords["kitchen"] == pw
    q = hass.casa["qu_data"]["updates"]
    assert [e["id"] for e in q["D1"]] == ["p1"]  # stale auth gone, profile kept
    assert [e["id"] for e in q["D2"]] == ["e2"]  # other user untouched
    devices = hass.casa["stored_data"]["users"]["u1"]["devices"]
    assert "reauth_pending" not in devices["D1"]
    assert devices["D2"]["reauth_pending"]["update_id"] == "e2"


def test_setting_same_password_keeps_matching_entries():
    hass = _hass_with_queue()
    asyncio.run(_set_account_password(hass, hass.auth.provider, "kitchen", "old"))
    assert [e["id"] for e in hass.casa["qu_data"]["updates"]["D1"]] == ["e1", "p1"]


def _reauth_hass():
    hass = FakeHass()
    hass.auth.add_user(make_user("old", login="olduser"))
    hass.auth.add_user(make_user("tgt", login="kiosk"))
    hass.casa["stored_data"]["users"]["old"] = {"username": "olduser", "devices": {
        "D1": {"refresh_token_id": "r1"}, "D2": {"refresh_token_id": "r2"},
    }}
    return hass


def _post(hass, body):
    view = bind_view(CasaAdminReauthDeviceView(hass))
    admin = make_user("admin", admin=True)
    return view.post(FakeRequest(admin, {"send_update_push": False, **body}))


def _queued_auth(hass, device_id):
    return [e for e in hass.casa["qu_data"]["updates"].get(device_id, []) if e["type"] == "auth"]


def test_double_click_reauth_queues_only_the_current_password():
    hass = _reauth_hass()
    # The first request's password save is slow, so without serialization the
    # second request would rotate and queue first, and the first would then
    # queue its already-stale password over it.
    hass.auth.provider.data.save_delays = [20]

    async def scenario():
        return await asyncio.gather(
            _post(hass, {"device_id": "D1", "user_id": "tgt"}),
            _post(hass, {"device_id": "D1", "user_id": "tgt"}),
        )

    results = asyncio.run(scenario())
    assert all(status == 200 for status, _ in results)
    entries = _queued_auth(hass, "D1")
    assert len(entries) == 1
    assert entries[0]["payload"]["password"] == hass.auth.provider.data.passwords["kiosk"]
    marker = hass.casa["stored_data"]["users"]["old"]["devices"]["D1"]["reauth_pending"]
    assert marker["update_id"] == entries[0]["id"]


def test_second_device_to_same_user_reuses_queued_password():
    hass = _reauth_hass()
    asyncio.run(_post(hass, {"device_id": "D1", "user_id": "tgt"}))
    asyncio.run(_post(hass, {"device_id": "D2", "user_id": "tgt"}))
    e1, e2 = _queued_auth(hass, "D1"), _queued_auth(hass, "D2")
    assert len(e1) == len(e2) == 1
    current = hass.auth.provider.data.passwords["kiosk"]
    assert e1[0]["payload"]["password"] == e2[0]["payload"]["password"] == current
