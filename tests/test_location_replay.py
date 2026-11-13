"""A captured location report cannot be replayed: the exact ciphertext is
accepted once. Fresh reports (new nonce) are unaffected, even in one second."""
import asyncio
import json
import time

from custom_components.casa import CasaLocationReportView
from custom_components.casa.location import encrypt_report_payload
from tests.fakes import FakeHass, FakeRequest, bind_view

KEY = "site-device-key"


import pytest


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
    sd["device_key"] = KEY
    sd["users"]["u1"] = {"username": "a", "devices": {"D1": {}}}
    return hass


def _post(hass, blob):
    view = bind_view(CasaLocationReportView(hass))
    return asyncio.run(view.post(FakeRequest(None, {"device_id": "D1", "payload": blob})))[0]


def _blob(ts=None):
    inner = {"state": "away", "config_version": "", "ts": ts or int(time.time())}
    return encrypt_report_payload(json.dumps(inner), KEY, "D1")


def test_replayed_report_is_rejected():
    hass = _hass()
    blob = _blob()
    assert _post(hass, blob) == 200
    assert _post(hass, blob) == 403


def test_two_fresh_reports_in_the_same_second_both_accepted():
    hass = _hass()
    ts = int(time.time())
    assert _post(hass, _blob(ts)) == 200
    assert _post(hass, _blob(ts)) == 200
