"""Per-device keys (security Phase 4, G1): creation, which key encrypts a
push, which keys decrypt a report, rotation."""
import json

import pytest

from custom_components.casa.device_keys import (
    decrypt_from_device,
    encryption_key,
    ensure_device_key,
    key_id,
    public_key_state,
    rotate_device_key,
)
from custom_components.casa.location import ACTION_INFO, encrypt_report_payload

SITE = "s" * 64


def _payload(key, did="D1", obj=None, info=None):
    text = json.dumps(obj or {"ts": 1})
    return encrypt_report_payload(text, key, did) if info is None else encrypt_report_payload(text, key, did, info=info)


def test_ensure_creates_once():
    info = {}
    first = ensure_device_key(info)
    assert len(first) == 64 and int(first, 16) >= 0
    assert ensure_device_key(info) == first


def test_encryption_key_is_site_key_until_delivered():
    sd = {"device_key": SITE}
    info = {}
    own = ensure_device_key(info)
    assert encryption_key(sd, info) == SITE
    info["device_key_delivered_at"] = "2026-11-13T00:00:00"
    assert encryption_key(sd, info) == own


def test_decrypt_prefers_device_key_and_confirms():
    sd = {"device_key": SITE}
    info = {}
    own = ensure_device_key(info)
    assert decrypt_from_device(sd, info, "D1", _payload(own)) == {"ts": 1}
    assert info.get("device_key_confirmed_at")
    assert not info.get("legacy_key_uses")


def test_action_channel_is_kept():
    sd = {"device_key": SITE}
    info = {}
    own = ensure_device_key(info)
    assert decrypt_from_device(sd, info, "D1", _payload(own, info=ACTION_INFO), info=ACTION_INFO) == {"ts": 1}
    with pytest.raises(ValueError):
        decrypt_from_device(sd, info, "D1", _payload(own, info=ACTION_INFO))


def test_site_key_accepted_and_counted_before_confirmation():
    sd = {"device_key": SITE}
    info = {}
    ensure_device_key(info)
    assert decrypt_from_device(sd, info, "D1", _payload(SITE)) == {"ts": 1}
    assert info["legacy_key_uses"] == 1
    assert "device_key_confirmed_at" not in info


def test_site_key_works_for_a_record_without_its_own_key():
    sd = {"device_key": SITE}
    info = {}
    assert decrypt_from_device(sd, info, "D1", _payload(SITE)) == {"ts": 1}
    assert info["legacy_key_uses"] == 1


def test_site_key_refused_after_confirmation():
    sd = {"device_key": SITE}
    info = {}
    own = ensure_device_key(info)
    decrypt_from_device(sd, info, "D1", _payload(own))
    with pytest.raises(ValueError):
        decrypt_from_device(sd, info, "D1", _payload(SITE))


def test_nothing_decrypts_raises():
    with pytest.raises(ValueError):
        decrypt_from_device({}, {}, "D1", _payload(SITE))


def test_rotate_keeps_delivered_and_confirmed():
    info = {"device_key_delivered_at": "t", "device_key_confirmed_at": "t"}
    old = key_id(ensure_device_key(info))
    new = rotate_device_key(info)
    assert new != old and new == key_id(info["device_key"])
    assert info["device_key_delivered_at"] == "t"
    assert info["device_key_confirmed_at"] == "t"


def test_site_key_stays_refused_after_rotation():
    sd = {"device_key": SITE}
    info = {}
    own = ensure_device_key(info)
    decrypt_from_device(sd, info, "D1", _payload(own))
    rotate_device_key(info)
    with pytest.raises(ValueError):
        decrypt_from_device(sd, info, "D1", _payload(SITE))
    assert decrypt_from_device(sd, info, "D1", _payload(info["device_key"])) == {"ts": 1}


def test_public_state_never_contains_key():
    info = {"legacy_key_uses": 2}
    own = ensure_device_key(info)
    state = public_key_state(info)
    assert own not in json.dumps(state)
    assert state == {"device_key_id": key_id(own), "delivered": False, "legacy_key_uses": 2}
