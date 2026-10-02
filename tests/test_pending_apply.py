import asyncio
import time

from custom_components.casa import _apply_pending_provision, _record_provision_claims
from tests.fakes import FakeHass, make_user


def _hass():
    hass = FakeHass()
    hass.auth.add_user(make_user("u1", login="casa-bryce-mobile", tokens=["t-new", "t-other"]))
    hass.casa["pending_profile_by_user"] = {"u1": {
        "profile_id": "t1", "profile_name": "Mobile Owners", "device_alias": "Bryce Mobile",
        "expiration_hours": 0, "set_at": time.time(),
    }}
    return hass


def test_redeeming_session_applies_name_lineage_expiration():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"t-new"})
    info = {}
    assert asyncio.run(_apply_pending_provision(hass, "u1", info, "t-new")) is True
    assert info["alias"] == "Bryce Mobile"
    assert info["provisioning_profile_id"] == "t1"
    assert info["provisioning_profile_name"] == "Mobile Owners"
    assert info["provisioning_expiration_hours"] == 0
    assert "u1" not in hass.casa["pending_profile_by_user"]


def test_second_contact_is_a_noop():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"t-new"})
    info = {}
    asyncio.run(_apply_pending_provision(hass, "u1", info, "t-new"))
    info["alias"] = "Kept"
    assert asyncio.run(_apply_pending_provision(hass, "u1", info, "t-new")) is False
    assert info["alias"] == "Kept"


def test_other_device_on_shared_account_does_not_consume():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"t-new"})
    other = {}
    # t-other is an old session: not a recorded claim and created before the window opened.
    hass.casa["stored_data"]["provision_opened"] = {"u1": time.time() + 60}
    assert asyncio.run(_apply_pending_provision(hass, "u1", other, "t-other")) is False
    assert other == {}
    assert "u1" in hass.casa["pending_profile_by_user"]


def test_existing_alias_is_not_overwritten():
    hass = _hass()
    _record_provision_claims(hass, "u1", {"t-new"})
    info = {"alias": "Admin set"}
    asyncio.run(_apply_pending_provision(hass, "u1", info, "t-new"))
    assert info["alias"] == "Admin set"


def test_stale_pending_is_dropped_without_applying():
    hass = _hass()
    hass.casa["pending_profile_by_user"]["u1"]["set_at"] = time.time() - 3600
    _record_provision_claims(hass, "u1", {"t-new"})
    info = {}
    assert asyncio.run(_apply_pending_provision(hass, "u1", info, "t-new")) is False
    assert info == {}


def test_stamps_entity_rename_window_only_with_alias():
    from custom_components.casa import ENTITY_RENAME_WINDOW_SECONDS
    hass = _hass()
    _record_provision_claims(hass, "u1", {"t-new"})
    info = {}
    asyncio.run(_apply_pending_provision(hass, "u1", info, "t-new"))
    assert abs(info["entity_rename_until"] - (time.time() + ENTITY_RENAME_WINDOW_SECONDS)) < 5

    hass = _hass()
    hass.casa["pending_profile_by_user"]["u1"].pop("device_alias")
    _record_provision_claims(hass, "u1", {"t-new"})
    info = {}
    assert asyncio.run(_apply_pending_provision(hass, "u1", info, "t-new")) is True
    assert "entity_rename_until" not in info
