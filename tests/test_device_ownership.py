import asyncio
import time
from datetime import datetime, timedelta, timezone

from custom_components.casa import (
    CasaDeviceProfileReportView,
    _claim_device_for_caller,
    _collapse_duplicate_device_records,
)
from tests.fakes import FakeHass, FakeRequest, bearer, bind_view, make_user


def _hass():
    hass = FakeHass()
    hass.auth.add_user(make_user("A", login="alice", tokens=["rA"]))
    hass.auth.add_user(make_user("B", login="bob", tokens=["rB"]))
    sd = hass.casa["stored_data"]
    sd["users"]["A"] = {"username": "alice", "devices": {
        "PHONE": {"refresh_token_id": "rA", "alias": "Hall", "reauth_pending": {"update_id": "x"}},
    }}
    sd["users"]["B"] = {"username": "bob", "devices": {}}
    hass.casa["qu_data"]["updates"] = {"PHONE": [{"id": "x", "type": "auth", "payload": {"username": "alice", "password": "p"}}]}
    return hass, sd


def _claim(hass, sd, rtid="rB"):
    return asyncio.run(_claim_device_for_caller(hass, "PHONE", "B", sd["users"]["B"]["devices"], rtid))


def _moved(hass, sd):
    return "PHONE" in sd["users"]["B"]["devices"] and "PHONE" not in sd["users"]["A"]["devices"]


def test_knowing_a_device_id_is_not_enough():
    hass, sd = _hass()
    assert _claim(hass, sd) is False
    assert "PHONE" in sd["users"]["A"]["devices"] and sd["users"]["B"]["devices"] == {}
    assert hass.casa["qu_data"]["updates"]["PHONE"]


def test_old_session_gone_alone_is_not_enough():
    hass, sd = _hass()
    hass.auth.users["A"].refresh_tokens.clear()
    assert _claim(hass, sd) is False


def test_pinned_token_moves():
    hass, sd = _hass()
    sd["users"]["A"]["devices"]["PHONE"]["refresh_token_id"] = "rB"  # misfiled record
    assert _claim(hass, sd) and _moved(hass, sd)


def test_redeemed_provisioning_claim_moves_purges_and_revokes_old_session():
    hass, sd = _hass()
    sd["provision_claims"] = {"B": {"rB": time.time()}}
    assert _claim(hass, sd) and _moved(hass, sd)
    moved = sd["users"]["B"]["devices"]["PHONE"]
    assert moved["alias"] == "Hall" and "reauth_pending" not in moved
    assert "PHONE" not in hass.casa["qu_data"]["updates"]
    assert "rA" in hass.auth.removed_tokens  # old owner's pinned session revoked
    assert "rB" not in sd["provision_claims"]["B"]  # claim consumed


def test_young_session_after_recent_window_moves():
    hass, sd = _hass()
    sd["provision_opened"] = {"B": time.time() - 600}
    assert _claim(hass, sd) and _moved(hass, sd)


def test_old_session_or_old_window_does_not_move():
    hass, sd = _hass()
    sd["provision_opened"] = {"B": time.time() - 600}
    hass.auth.users["B"].refresh_tokens["rB"].created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    assert _claim(hass, sd) is False
    hass, sd = _hass()
    sd["provision_opened"] = {"B": time.time() - 2 * 86400}
    assert _claim(hass, sd) is False
    hass, sd = _hass()
    sd["provision_opened"] = {"B": time.time() + 60}  # token predates the window
    assert _claim(hass, sd) is False


def test_reauth_target_moves():
    hass, sd = _hass()
    sd["users"]["A"]["devices"]["PHONE"]["reauth_pending"] = {"update_id": "x", "target_user_id": "B"}
    assert _claim(hass, sd) and _moved(hass, sd)


def test_unknown_device_is_created_by_caller():
    hass, sd = _hass()
    assert asyncio.run(_claim_device_for_caller(hass, "NEW", "B", sd["users"]["B"]["devices"], "rB"))


def test_collapse_keeps_copy_with_live_token():
    hass, sd = _hass()
    sd["users"]["B"]["devices"]["PHONE"] = {"refresh_token_id": "dead", "last_seen_at": "2026-12-01T00:00:00+00:00"}
    sd["native_devices"]["N"] = {"PHONE": {"refresh_token_id": "zz", "last_seen_at": "2026-01-01"}}
    users = asyncio.run(hass.auth.async_get_users())
    assert _collapse_duplicate_device_records(sd, users) == 2
    assert "PHONE" in sd["users"]["A"]["devices"]
    assert "PHONE" not in sd["users"]["B"]["devices"]
    assert "N" not in sd["native_devices"]


def test_collapse_falls_back_to_most_recent_and_moves_marker():
    hass, sd = _hass()
    sd["users"]["A"]["devices"]["PHONE"]["refresh_token_id"] = "gone"
    sd["users"]["A"]["devices"]["PHONE"]["last_seen_at"] = "2026-01-01T00:00:00+00:00"
    sd["users"]["B"]["devices"]["PHONE"] = {"refresh_token_id": "gone2", "last_seen_at": "2026-06-01T00:00:00+00:00"}
    users = asyncio.run(hass.auth.async_get_users())
    assert _collapse_duplicate_device_records(sd, users) == 1
    keeper = sd["users"]["B"]["devices"]["PHONE"]
    assert keeper["reauth_pending"] == {"update_id": "x"}


def _report(hass, user, headers=None):
    view = bind_view(CasaDeviceProfileReportView(hass))
    req = FakeRequest(user, {"device_id": "PHONE", "fields": {"default_dashboard": "/x"}}, headers=headers or {})
    return asyncio.run(view.post(req))


def test_profile_report_from_non_owner_is_404():
    hass, sd = _hass()
    status, _ = _report(hass, hass.auth.users["B"], bearer("rB"))
    assert status == 404
    assert "provisioning_fields" not in sd["users"]["A"]["devices"]["PHONE"]


def test_profile_report_from_owner_or_pinned_token_applies():
    hass, sd = _hass()
    assert _report(hass, hass.auth.users["A"])[0] == 200
    sd["users"]["A"]["devices"]["PHONE"].pop("provisioning_fields")
    sd["users"]["A"]["devices"]["PHONE"]["refresh_token_id"] = "rB"
    assert _report(hass, hass.auth.users["B"], bearer("rB"))[0] == 200


def test_device_is_marked_purging_during_relay_await(monkeypatch):
    import custom_components.casa as casa

    hass, sd = _hass()
    seen = []

    async def fake_unregister(h, token, did):
        seen.append(casa._device_being_purged(h, did))

    monkeypatch.setattr(casa, "_unregister_relay_token", fake_unregister)
    result = asyncio.run(casa._purge_device(hass, "PHONE"))
    assert result["found"] and seen == [True]
    assert not casa._device_being_purged(hass, "PHONE")
    assert "PHONE" not in sd["users"]["A"]["devices"]
