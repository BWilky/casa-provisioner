"""Relay rejections are acted on at once (not at the next daily reconcile),
and a site_id is only kept once the relay has accepted it."""
import asyncio
import time
from types import SimpleNamespace

import custom_components.casa as casa
from custom_components.casa import _handle_relay_send_rejection, _register_site
from tests.fakes import FakeHass


class _Resp:
    def __init__(self, status, body=None):
        self.status, self._body = status, body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, **kw):
        return self._body

    async def text(self):
        return ""


class _Store:
    def __init__(self):
        self.saved = 0

    async def async_save(self, data):
        self.saved += 1


def _session(monkeypatch, status, body=None):
    class _Session:
        def post(self, url, json=None, timeout=None):
            return _Resp(status, body)
    monkeypatch.setattr(casa, "async_get_clientsession", lambda hass: _Session())


def test_register_site_failure_keeps_no_site_id(monkeypatch):
    _session(monkeypatch, 500)
    hass = SimpleNamespace(data={"casa": {}})
    data = {}
    assert asyncio.run(_register_site(hass, data, _Store())) is False
    assert "site_id" not in data and "site_key" not in data


def test_register_site_unreachable_keeps_no_site_id(monkeypatch):
    class _Boom:
        def post(self, *a, **kw):
            raise OSError("down")
    monkeypatch.setattr(casa, "async_get_clientsession", lambda hass: _Boom())
    hass = SimpleNamespace(data={"casa": {}})
    data = {}
    asyncio.run(_register_site(hass, data, _Store()))
    assert "site_id" not in data


def test_register_site_success_persists_id_and_key(monkeypatch):
    _session(monkeypatch, 201, {"site_key": "K"})
    hass = SimpleNamespace(data={"casa": {}})
    data = {}
    assert asyncio.run(_register_site(hass, data, _Store())) is True
    assert len(data["site_id"]) == 32 and data["site_key"] == "K"


def _hass():
    hass = FakeHass()
    sd = hass.casa["stored_data"]
    sd.update({"site_id": "S", "site_key": "K"})
    sd["users"]["u1"] = {"username": "a", "devices": {"D1": {"push_token": "p1"}, "D2": {"push_token": "p2"}}}
    sd["native_devices"]["n1"] = {"N1": {"push_token": "p3"}}
    return hass, sd


def test_registration_invalid_flags_only_that_device():
    hass, sd = _hass()
    asyncio.run(_handle_relay_send_rejection(hass, 403, '{"detail":"Device token/site ID registration invalid or expired"}', "p1"))
    assert sd["users"]["u1"]["devices"]["D1"]["needs_reregister"] is True
    assert "needs_reregister" not in sd["users"]["u1"]["devices"]["D2"]


def test_invalid_site_key_reregisters_site_and_flags_every_device(monkeypatch):
    hass, sd = _hass()
    calls = []

    async def fake_ensure(hass_, stored_data, store):
        calls.append(1)
        stored_data["site_key"] = "NEW"

    monkeypatch.setattr(casa, "_ensure_site_registration", fake_ensure)
    asyncio.run(_handle_relay_send_rejection(hass, 403, '{"detail":"Invalid site key"}', "p1"))
    assert calls == [1]
    assert all(d.get("needs_reregister") for d in sd["users"]["u1"]["devices"].values())
    assert sd["native_devices"]["n1"]["N1"]["needs_reregister"] is True
    # A burst of failed sends re-verifies once, not once per send.
    asyncio.run(_handle_relay_send_rejection(hass, 403, '{"detail":"Invalid site key"}', "p2"))
    assert calls == [1]


def test_invalid_site_key_with_unchanged_key_flags_nothing(monkeypatch):
    hass, sd = _hass()

    async def fake_ensure(hass_, stored_data, store):
        pass  # relay says our key is fine after all

    monkeypatch.setattr(casa, "_ensure_site_registration", fake_ensure)
    asyncio.run(_handle_relay_send_rejection(hass, 403, '{"detail":"Invalid site key"}', "p1"))
    assert not any(d.get("needs_reregister") for d in sd["users"]["u1"]["devices"].values())


def test_other_statuses_change_nothing():
    hass, sd = _hass()
    asyncio.run(_handle_relay_send_rejection(hass, 502, "APNs error", "p1"))
    assert "needs_reregister" not in sd["users"]["u1"]["devices"]["D1"]
