"""The wizard's device name / template lineage is stashed per HA user at
provision time and applied to the device record that redeems the link. It
must survive a restart and stay valid for as long as the link is (plus a
grace for the login → register lag), not a fixed 30 minutes."""
import asyncio
import time

from custom_components.casa import (
    _apply_pending_provision,
    _pending_profile_valid_until,
    _stash_pending_profile,
)
from tests.fakes import FakeHass


def _hass_with_claim(rtid="r1"):
    hass = FakeHass()
    hass.casa["stored_data"]["provision_claims"] = {"u1": {rtid: time.time()}}
    return hass


def _stash(hass, **kw):
    args = dict(profile_id="t1", profile_name="Owners", device_alias="Host - Christoph",
                expiration_hours=336, valid_until=time.time() + 10 * 3600)
    args.update(kw)
    _stash_pending_profile(hass, "u1", **args)


def test_valid_until_follows_the_window_plus_grace():
    now = 1_000_000.0
    # scramble ends the window → that + 30 min grace
    assert _pending_profile_valid_until(now, scramble_at=now + 86400, window_ends_at=None, listen_until=now + 86430) == now + 86400 + 1800
    # no scramble: the listener deadline bounds it
    assert _pending_profile_valid_until(now, scramble_at=None, window_ends_at=None, listen_until=now + 300) == now + 300 + 1800
    # nothing ends the window: legacy fixed 30 min
    assert _pending_profile_valid_until(now, scramble_at=None, window_ends_at=None, listen_until=None) == now + 1800


def test_profile_applies_hours_later_while_link_is_valid():
    hass = _hass_with_claim()
    _stash(hass, valid_until=time.time() + 10 * 3600)
    # simulate "3 hours later" by back-dating set_at; valid_until is what gates
    hass.casa["stored_data"]["pending_profiles"]["u1"]["set_at"] = time.time() - 3 * 3600
    info = {}
    assert asyncio.run(_apply_pending_provision(hass, "u1", info, "r1")) is True
    assert info["alias"] == "Host - Christoph"
    assert info["provisioning_profile_id"] == "t1"
    assert info["provisioning_profile_name"] == "Owners"
    assert info["provisioning_expiration_hours"] == 336
    assert "u1" not in hass.casa["stored_data"]["pending_profiles"]  # consumed


def test_profile_past_valid_until_is_dropped_not_applied():
    hass = _hass_with_claim()
    _stash(hass, valid_until=time.time() - 1)
    info = {}
    assert asyncio.run(_apply_pending_provision(hass, "u1", info, "r1")) is False
    assert "alias" not in info
    assert "u1" not in hass.casa["stored_data"]["pending_profiles"]


def test_profile_needs_a_fresh_claim_from_the_redeeming_session():
    hass = _hass_with_claim("r1")
    _stash(hass)
    info = {}
    assert asyncio.run(_apply_pending_provision(hass, "u1", info, "other-token")) is False
    assert "u1" in hass.casa["stored_data"]["pending_profiles"]  # left for the right session


def test_stash_lives_in_stored_data_so_it_survives_a_restart():
    hass = _hass_with_claim()
    _stash(hass)
    stored = hass.casa["stored_data"]
    # "restart": brand-new hass.data pointing at the same persisted dict
    hass2 = FakeHass()
    hass2.casa["stored_data"] = stored
    info = {}
    assert asyncio.run(_apply_pending_provision(hass2, "u1", info, "r1")) is True
    assert info["alias"] == "Host - Christoph"
