import asyncio
import time

from custom_components.casa import (
    _apply_device_replacement,
    _consume_replacement_claim,
    _record_provision_claims,
)
from tests.fakes import FakeHass, make_user


def _hass():
    hass = FakeHass()
    hass.auth.add_user(make_user("u1", login="kitchen", tokens=["r-old", "r-new"]))
    hass.casa["stored_data"]["users"]["u1"] = {"username": "kitchen", "devices": {
        "OLD": {"alias": "Kitchen iPad", "provisioning_profile_id": "t1",
                "provisioning_profile_name": "Mobile Owners", "refresh_token_id": "r-old"},
        "NEW": {"refresh_token_id": "r-new"},
    }}
    return hass


def _devices(hass):
    return hass.casa["stored_data"]["users"]["u1"]["devices"]


def test_claim_records_replacement_per_token():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"r-new"}, replaces_device_id="OLD")
    claim = hass.casa["stored_data"]["replacement_claims"]["r-new"]
    assert claim["replaces_device_id"] == "OLD"
    assert "r-new" in hass.casa["stored_data"]["provision_claims"]["u1"]


def test_claim_without_replacement_records_none():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"r-new"})
    assert hass.casa["stored_data"].get("replacement_claims", {}) == {}


def test_consume_is_single_use():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"r-new"}, replaces_device_id="OLD")
    assert _consume_replacement_claim(hass, "r-new") == "OLD"
    assert _consume_replacement_claim(hass, "r-new") is None


def test_new_phone_replaces_old_record():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"r-new"}, replaces_device_id="OLD")
    new_info = _devices(hass)["NEW"]
    replaced = asyncio.run(_apply_device_replacement(hass, "NEW", new_info, "r-new"))
    assert replaced == "OLD"
    assert "OLD" not in _devices(hass)
    assert new_info["alias"] == "Kitchen iPad"
    assert new_info["provisioning_profile_id"] == "t1"
    assert new_info["provisioning_profile_name"] == "Mobile Owners"
    assert "r-old" in hass.auth.removed_tokens


def test_new_alias_is_not_overwritten():
    hass = _hass()
    _devices(hass)["NEW"]["alias"] = "Typed on phone"
    _record_provision_claims(hass, "u1", {"r-new"}, replaces_device_id="OLD")
    asyncio.run(_apply_device_replacement(hass, "NEW", _devices(hass)["NEW"], "r-new"))
    assert _devices(hass)["NEW"]["alias"] == "Typed on phone"


def test_same_phone_keeps_record():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"r-new"}, replaces_device_id="OLD")
    replaced = asyncio.run(_apply_device_replacement(hass, "OLD", _devices(hass)["OLD"], "r-new"))
    assert replaced is None
    assert "OLD" in _devices(hass)
    assert hass.casa["stored_data"]["replacement_claims"] == {}


def test_replacement_noop_without_claim():
    hass = _hass()
    replaced = asyncio.run(_apply_device_replacement(hass, "NEW", _devices(hass)["NEW"], "r-new"))
    assert replaced is None
    assert set(_devices(hass)) == {"OLD", "NEW"}


def test_replacement_ignores_expired_claim():
    hass = _hass()
    hass.casa["stored_data"]["replacement_claims"] = {
        "r-new": {"replaces_device_id": "OLD", "at": time.time() - 2 * 86400},
    }
    replaced = asyncio.run(_apply_device_replacement(hass, "NEW", _devices(hass)["NEW"], "r-new"))
    assert replaced is None
    assert "OLD" in _devices(hass)


def test_replacement_noop_when_old_record_gone():
    hass = _hass()
    _devices(hass).pop("OLD")
    _record_provision_claims(hass, "u1", {"r-new"}, replaces_device_id="OLD")
    assert asyncio.run(_apply_device_replacement(hass, "NEW", _devices(hass)["NEW"], "r-new")) is None


import custom_components.casa as casa
from custom_components.casa import _arm_pending_provision


def test_armed_window_forwards_replaces_device_id(monkeypatch):
    hass = _hass()
    now = time.time()
    hass.casa["stored_data"]["pending_provisions"] = {"u1": {
        "provision_id": "p1", "login_username": "kitchen", "method": "qr", "single_use": True,
        "scramble_at": now + 300, "window_ends_at": now + 300, "listen_until": now + 330,
        "known_token_ids": ["r-old"], "qr_file": None, "qr_expire_mode": "delete",
        "created_at": now, "replaces_device_id": "OLD",
    }}
    captured = {}

    def fake_listener(*args, **kwargs):
        captured.update(kwargs)

        async def noop():
            return None
        return noop()

    monkeypatch.setattr(casa, "_login_listener", fake_listener)

    async def scenario():
        _arm_pending_provision(hass, "u1")
        captured["on_tokens"]({"r-new"})

    asyncio.run(scenario())
    assert hass.casa["stored_data"]["replacement_claims"]["r-new"]["replaces_device_id"] == "OLD"
