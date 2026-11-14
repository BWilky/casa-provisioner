"""Rotating one device's key (admin action) and on an owner change
(security Phase 4, G1)."""
import asyncio

import custom_components.casa as casa
from custom_components.casa import CasaAdminRegenerateKeyView, _move_device_record
from custom_components.casa.device_keys import ensure_device_key, key_id
from tests.fakes import FakeHass, FakeRequest, bind_view, make_user

SITE = "s" * 64


def _hass(monkeypatch):
    hass = FakeHass()
    sd = hass.casa["stored_data"]
    sd["device_key"] = SITE
    sd["users"]["u1"] = {"username": "alice", "devices": {"D1": {"push_token": "a" * 64}, "D2": {}}}
    nudged = []

    async def fake_nudge(_hass, _session, _sd, dinfo, command="request_heartbeat"):
        nudged.append(dinfo)

    monkeypatch.setattr(casa, "_nudge_device_checkin", fake_nudge)
    hass.nudged = nudged
    return hass


def _devices(hass):
    return hass.casa["stored_data"]["users"]["u1"]["devices"]


def _rotate(hass, body, admin=True):
    view = bind_view(CasaAdminRegenerateKeyView(hass))
    return asyncio.run(view.post(FakeRequest(make_user("admin", admin=admin), body)))


def test_admin_rotates_one_device(monkeypatch):
    hass = _hass(monkeypatch)
    old = ensure_device_key(_devices(hass)["D1"])
    other = ensure_device_key(_devices(hass)["D2"])
    status, body = _rotate(hass, {"device_id": "D1"})
    assert status == 200
    new = _devices(hass)["D1"]["device_key"]
    assert new != old and body == {"status": "ok", "device_id": "D1", "device_key_id": key_id(new)}
    assert _devices(hass)["D2"]["device_key"] == other
    assert hass.casa["stored_data"]["device_key"] == SITE
    assert hass.nudged == [_devices(hass)["D1"]]


def test_rotating_a_device_without_a_key_creates_one(monkeypatch):
    hass = _hass(monkeypatch)
    status, body = _rotate(hass, {"device_id": "D2"})
    assert status == 200 and body["device_key_id"] == key_id(_devices(hass)["D2"]["device_key"])


def test_rotate_requires_admin(monkeypatch):
    hass = _hass(monkeypatch)
    status, _ = _rotate(hass, {"device_id": "D1"}, admin=False)
    assert status == 403


def test_rotate_unknown_device_404(monkeypatch):
    hass = _hass(monkeypatch)
    status, _ = _rotate(hass, {"device_id": "NOPE"})
    assert status == 404
    assert hass.casa["stored_data"]["device_key"] == SITE


def test_site_rotation_unchanged_without_device_id(monkeypatch):
    hass = _hass(monkeypatch)
    status, body = _rotate(hass, {})
    assert status == 200 and hass.casa["stored_data"]["device_key"] != SITE
    assert body["device_key_id"] == key_id(hass.casa["stored_data"]["device_key"])


def test_moving_a_record_rotates_its_key(monkeypatch):
    hass = _hass(monkeypatch)
    sd = hass.casa["stored_data"]
    old = ensure_device_key(_devices(hass)["D1"])
    dest = {}
    info = _move_device_record(sd, hass.casa["qu_data"], "D1", "u1", dest)
    assert info is dest["D1"] and info["device_key"] != old


def test_moving_a_record_without_a_key_leaves_it_keyless(monkeypatch):
    hass = _hass(monkeypatch)
    dest = {}
    info = _move_device_record(hass.casa["stored_data"], hass.casa["qu_data"], "D2", "u1", dest)
    assert "device_key" not in info
