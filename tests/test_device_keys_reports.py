"""Location reports and notification-button presses decrypt under the
device's own key; the shared site key works only until the device has
proven it holds its own (security Phase 4, G1)."""
import asyncio
import json
import time

import pytest

from custom_components.casa import CasaLocationReportView, CasaNotificationActionView
from custom_components.casa.actions import create_pending
from custom_components.casa.device_keys import ensure_device_key
from custom_components.casa.location import ACTION_INFO, encrypt_report_payload
from tests.fakes import FakeHass, FakeRequest, bind_view

SITE = "s" * 64
ACTS = [{"action": "IGNORE", "title": "Ignore"}]


@pytest.fixture(autouse=True)
def _dispatcher(monkeypatch):
    import sys
    import types
    mod = sys.modules.get("homeassistant.helpers.dispatcher") or types.ModuleType("homeassistant.helpers.dispatcher")
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.dispatcher", mod)
    monkeypatch.setattr(mod, "async_dispatcher_send", lambda *a, **k: None, raising=False)


def _hass():
    hass = FakeHass()
    sd = hass.casa["stored_data"]
    sd["device_key"] = SITE
    sd["users"]["u1"] = {"username": "alice", "devices": {"D1": {}}}
    return hass


def _record(hass):
    return hass.casa["stored_data"]["users"]["u1"]["devices"]["D1"]


def _report(hass, key):
    inner = {"state": "away", "config_version": "", "ts": int(time.time())}
    blob = encrypt_report_payload(json.dumps(inner), key, "D1")
    view = bind_view(CasaLocationReportView(hass))
    return asyncio.run(view.post(FakeRequest(None, {"device_id": "D1", "payload": blob})))[0]


def _press(hass, key):
    nid = create_pending(hass.casa["na_data"], device_id="D1", user_id="u1", username="alice",
                         actions=ACTS, tag="t", action_data={})
    inner = {"ts": int(time.time()), "notification_id": nid, "action": "IGNORE"}
    blob = encrypt_report_payload(json.dumps(inner), key, "D1", info=ACTION_INFO)
    view = bind_view(CasaNotificationActionView(hass))
    return asyncio.run(view.post(FakeRequest(None, {"device_id": "D1", "payload": blob})))[0]


def test_report_under_device_key_accepted_and_confirms():
    hass = _hass()
    own = ensure_device_key(_record(hass))
    assert _report(hass, own) == 200
    assert _record(hass)["device_key_confirmed_at"]


def test_old_app_report_under_site_key_still_works():
    hass = _hass()
    assert _report(hass, SITE) == 200
    assert _record(hass)["legacy_key_uses"] == 1


def test_report_under_site_key_after_confirm_rejected():
    hass = _hass()
    own = ensure_device_key(_record(hass))
    assert _report(hass, own) == 200
    assert _report(hass, SITE) == 403


def test_report_works_without_a_site_key():
    hass = _hass()
    hass.casa["stored_data"].pop("device_key")
    own = ensure_device_key(_record(hass))
    assert _report(hass, own) == 200


def test_press_under_device_key_runs_action():
    hass = _hass()
    own = ensure_device_key(_record(hass))
    assert _press(hass, own) == 200
    assert _record(hass)["device_key_confirmed_at"]


def test_old_app_press_under_site_key_still_works():
    hass = _hass()
    assert _press(hass, SITE) == 200


def test_another_devices_key_cannot_press():
    hass = _hass()
    hass.casa["stored_data"]["users"]["u1"]["devices"]["D2"] = {}
    other = ensure_device_key(hass.casa["stored_data"]["users"]["u1"]["devices"]["D2"])
    ensure_device_key(_record(hass))
    assert _press(hass, other) == 403
