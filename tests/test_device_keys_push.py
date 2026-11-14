"""Pushes to a device are encrypted with its own key once it has it, the site
key before that (security Phase 4, G1)."""
import asyncio
import json

import custom_components.casa as casa
from custom_components.casa import CasaAdminSummaryView, _send_encrypted_update_push
from custom_components.casa.device_keys import ensure_device_key, key_id
from tests.fakes import FakeHass, FakeRequest, bind_view, make_user

SITE = "s" * 64


def _hass():
    hass = FakeHass()
    sd = hass.casa["stored_data"]
    sd["device_key"] = SITE
    sd["users"]["u1"] = {"username": "alice", "devices": {"D1": {"push_token": "a" * 64}}}
    return hass


def _record(hass):
    return hass.casa["stored_data"]["users"]["u1"]["devices"]["D1"]


def _decrypt(enc, key, did="D1"):
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    raw = base64.b64decode(enc)
    k = HKDF(algorithm=hashes.SHA256(), length=32, salt=did.encode(), info=b"casa-update-v1").derive(key.encode())
    return json.loads(AESGCM(k).decrypt(raw[:12], raw[12:], None))


def _push(hass, monkeypatch):
    sent = []

    async def fake_relay(_hass, _session, payload):
        sent.append(payload)
        return True

    monkeypatch.setattr(casa, "_send_push_to_relay", fake_relay)
    sd = hass.casa["stored_data"]
    ok = asyncio.run(_send_encrypted_update_push(hass, sd, None, "D1", _record(hass), "U1", "app", "clear_cache_reload", {}))
    assert ok and len(sent) == 1
    return sent[0]["data"]


def test_push_uses_site_key_before_delivery(monkeypatch):
    hass = _hass()
    ensure_device_key(_record(hass))
    data = _push(hass, monkeypatch)
    assert data["device_key_id"] == key_id(SITE)
    assert _decrypt(data["update_payload"], SITE)["id"] == "U1"


def test_push_uses_device_key_once_delivered(monkeypatch):
    hass = _hass()
    own = ensure_device_key(_record(hass))
    _record(hass)["device_key_delivered_at"] = "2026-11-13T00:00:00"
    data = _push(hass, monkeypatch)
    assert data["device_key_id"] == key_id(own)
    assert _decrypt(data["update_payload"], own)["id"] == "U1"


def test_push_ready_without_site_key_once_delivered():
    hass = _hass()
    hass.casa["stored_data"].pop("device_key")
    ensure_device_key(_record(hass))
    _record(hass)["device_key_delivered_at"] = "t"
    status, body = asyncio.run(bind_view(CasaAdminSummaryView(hass)).get(FakeRequest(make_user("admin", admin=True))))
    assert status == 200
    rows = [d for d in body["devices"] if d.get("device_id") == "D1"]
    assert rows and rows[0]["push_ready"] is True


def test_admin_summary_has_no_device_keys():
    hass = _hass()
    own = ensure_device_key(_record(hass))
    status, body = asyncio.run(bind_view(CasaAdminSummaryView(hass)).get(FakeRequest(make_user("admin", admin=True))))
    assert status == 200
    text = json.dumps(body)
    assert own not in text and SITE not in text
