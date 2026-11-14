"""The heartbeat hands a device its own key, and only to the device's own
pinned session (security Phase 4, G1)."""
import asyncio
import logging

from custom_components.casa import CasaHeartbeatView
from custom_components.casa.device_keys import key_id
from tests.fakes import FakeHass, FakeRequest, bearer, bind_view, make_user

SITE = "s" * 64


def _hass():
    hass = FakeHass()
    sd = hass.casa["stored_data"]
    sd["device_key"] = SITE
    sd["users"]["u1"] = {"username": "alice", "devices": {"D1": {"refresh_token_id": "rt1"}}}
    return hass


def _record(hass):
    return hass.casa["stored_data"]["users"]["u1"]["devices"]["D1"]


def _beat(hass, token, result=None, on_call=None):
    async def heartbeat(*a, **kw):
        if on_call:
            on_call()
        return dict(result or {"owned": True})

    view = bind_view(CasaHeartbeatView(hass, heartbeat))
    req = FakeRequest(make_user("u1"), {"device_id": "D1"}, headers=bearer(token))
    out = asyncio.run(view.post(req))
    return out


def _json(out):
    status, body = out
    assert status == 200
    return body


def test_pinned_session_gets_own_key():
    hass = _hass()
    body = _json(_beat(hass, "rt1"))
    rec = _record(hass)
    assert body["device_key"] == rec["device_key"] != SITE
    assert body["device_key_id"] == key_id(rec["device_key"])
    assert rec["device_key_delivered_at"]


def test_key_is_stable_across_heartbeats():
    hass = _hass()
    first = _json(_beat(hass, "rt1"))["device_key"]
    assert _json(_beat(hass, "rt1"))["device_key"] == first


def test_unpinned_session_gets_null_key():
    hass = _hass()
    body = _json(_beat(hass, "rt2"))
    assert body["device_key"] is None and body["device_key_id"] is None
    assert "device_key" not in _record(hass)


def test_not_owned_gets_null_key():
    hass = _hass()
    body = _json(_beat(hass, "rt1", result={"owned": False}))
    assert body["device_key"] is None and body["device_key_id"] is None
    assert "device_key" not in _record(hass)


def test_resigned_in_phone_still_gets_its_key():
    hass = _hass()
    first = _json(_beat(hass, "rt1"))["device_key"]

    def repin():  # what _may_repin does for the same phone's new session
        _record(hass)["refresh_token_id"] = "rt2"

    assert _json(_beat(hass, "rt2", on_call=repin))["device_key"] == first


def test_key_not_logged(caplog):
    hass = _hass()
    with caplog.at_level(logging.DEBUG):
        body = _json(_beat(hass, "rt1"))
    assert body["device_key"] not in caplog.text
