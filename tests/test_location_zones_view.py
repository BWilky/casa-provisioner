import asyncio

from custom_components.casa import CasaHeartbeatView, CasaLocationZonesView
from tests.fakes import FakeHass, FakeRequest, bind_view, make_user

ANCHOR = {"id": "a1", "name": "House", "latitude": 1.0, "longitude": 2.0,
          "rings": [{"label": "home", "radius_m": 100}]}


def _hass():
    hass = FakeHass()
    hass.casa["stored_data"]["users"]["u1"] = {"username": "alice", "devices": {"D1": {}}}
    return hass


def _put(hass, anchors):
    view = bind_view(CasaLocationZonesView(hass))
    admin = make_user("admin", admin=True)

    async def go():
        hass.tasks.clear()
        res = await view.put(FakeRequest(admin, {"anchors": anchors, "stale_after_minutes": 30}))
        await asyncio.gather(*hass.tasks)
        return res

    return asyncio.run(go())


def _location_entries(hass):
    return [e for e in hass.casa["qu_data"]["updates"].get("D1", []) if e["type"] == "location"]


def test_saving_empty_list_pushes_teardown_once():
    hass = _hass()
    _put(hass, [ANCHOR])
    assert hass.casa["lz_data"]["config_version"]
    status, body = _put(hass, [])
    assert status == 200 and body["config_version"] == "" and body["queued"] == 1
    entries = _location_entries(hass)
    assert len(entries) == 1 and entries[0]["payload"] == {"anchors": [], "config_version": ""}
    hass.casa["qu_data"]["updates"].clear()
    status, body = _put(hass, [])  # unchanged: nothing re-queued
    assert body["queued"] == 0 and _location_entries(hass) == []


def _heartbeat(hass, body):
    async def hb(user_id, device_id, **kw):
        return {"owned": True, "updates": False}

    view = bind_view(CasaHeartbeatView(hass, hb))
    return asyncio.run(view.post(FakeRequest(make_user("u1"), body)))


def test_heartbeat_with_no_zones_reports_null_and_never_reconciles():
    hass = _hass()
    status, resp = _heartbeat(hass, {"device_id": "D1", "location_config_version": "deadbeef"})
    assert status == 200 and resp["location_config_version"] is None
    assert resp["updates"] is False
    assert _location_entries(hass) == []


def test_heartbeat_with_stale_version_reconciles():
    hass = _hass()
    hass.casa["lz_data"].update({"anchors": [ANCHOR], "config_version": "abcd1234"})
    status, resp = _heartbeat(hass, {"device_id": "D1", "location_config_version": "old"})
    assert resp["location_config_version"] == "abcd1234" and resp["updates"] is True
    assert len(_location_entries(hass)) == 1
