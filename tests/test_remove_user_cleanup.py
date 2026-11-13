"""Removing a casa user leaves nothing usable behind: device records, relay
registrations, sessions, claims and password fingerprints all go."""
import asyncio
import time

import custom_components.casa as casa
from custom_components.casa import _forget_user, _forget_deleted_users
from tests.fakes import FakeHass, make_user


def _hass(monkeypatch):
    hass = FakeHass()
    hass.auth.add_user(make_user("u1", login="kiosk", tokens=["r1", "r2"]))
    hass.auth.add_user(make_user("u2", login="other", tokens=["r9"]))
    sd = hass.casa["stored_data"]
    sd["users"]["u1"] = {"username": "kiosk", "devices": {
        "D1": {"push_token": "p1", "refresh_token_id": "r1"},
        "D2": {"push_token": "p2", "refresh_token_id": "r2"},
    }}
    sd["users"]["u2"] = {"username": "other", "devices": {"D9": {"push_token": "p9", "refresh_token_id": "r9"}}}
    sd["provision_claims"] = {"u1": {"r1": time.time()}, "u2": {"r9": time.time()}}
    sd["provision_opened"] = {"u1": time.time(), "u2": time.time()}
    sd["replacement_claims"] = {"r1": {"replaces_device_id": "D1", "at": time.time()}}
    sd["server_passwords"] = {"kiosk": "fp1", "other": "fp2"}
    hass.casa["qu_data"]["updates"] = {"D1": [{"id": "x"}], "D9": [{"id": "y"}]}
    unregistered = []

    async def fake_unregister(_hass, proxy_token, device_id):
        unregistered.append(proxy_token)

    monkeypatch.setattr(casa, "_unregister_relay_token", fake_unregister)
    return hass, sd, unregistered


def test_forget_user_purges_devices_relay_tokens_sessions_and_claims(monkeypatch):
    hass, sd, unregistered = _hass(monkeypatch)
    asyncio.run(_forget_user(hass, "u1", "kiosk"))
    assert sd["users"]["u1"]["devices"] == {}
    assert sorted(unregistered) == ["p1", "p2"]
    assert {"r1", "r2"} <= set(hass.auth.removed_tokens)
    assert "u1" not in sd["provision_claims"] and "u1" not in sd["provision_opened"]
    assert "r1" not in sd["replacement_claims"]
    assert "kiosk" not in sd["server_passwords"]
    assert "D1" not in hass.casa["qu_data"]["updates"]
    # Other users untouched
    assert "D9" in sd["users"]["u2"]["devices"] and "u2" in sd["provision_claims"]
    assert sd["server_passwords"]["other"] == "fp2" and "D9" in hass.casa["qu_data"]["updates"]


def test_forget_user_keeps_relay_token_and_queue_a_live_record_still_uses(monkeypatch):
    # The phone moved to u2 under the same device_id and proxy token.
    hass, sd, unregistered = _hass(monkeypatch)
    sd["users"]["u2"]["devices"]["D1"] = {"push_token": "p1", "refresh_token_id": "r9"}
    asyncio.run(_forget_user(hass, "u1", "kiosk"))
    assert unregistered == ["p2"]
    assert "D1" in hass.casa["qu_data"]["updates"]


def test_startup_forgets_devices_of_already_deleted_users(monkeypatch):
    hass, sd, unregistered = _hass(monkeypatch)
    sd["users"]["u1"]["deleted"] = True
    asyncio.run(_forget_deleted_users(hass))
    assert sd["users"]["u1"]["devices"] == {} and sd["users"]["u1"]["deleted"] is True
    assert sorted(unregistered) == ["p1", "p2"]
    assert "D9" in sd["users"]["u2"]["devices"]
