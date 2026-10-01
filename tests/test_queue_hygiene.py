import asyncio
from datetime import datetime, timedelta, timezone

from custom_components.casa import _prune_stale_queued_updates
from tests.fakes import FakeHass, make_user


def _ts(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _entry(eid, kind="profile", days_ago=0, username="kiosk"):
    e = {"id": eid, "type": kind, "action": "update", "payload": {}, "created_at": _ts(days_ago)}
    if kind == "auth":
        e["action"] = "reauthenticate"
        e["payload"] = {"username": username, "password": "p"}
    return e


def _hass():
    hass = FakeHass()
    hass.auth.add_user(make_user("u1", login="kiosk"))
    hass.casa["stored_data"]["users"]["u1"] = {"username": "kiosk", "devices": {"D1": {}, "D2": {}}}
    return hass


def test_old_non_auth_entries_are_pruned_auth_kept():
    hass = _hass()
    hass.casa["qu_data"]["updates"]["D1"] = [
        _entry("old", days_ago=40), _entry("new", days_ago=1), _entry("a", "auth", days_ago=90),
    ]
    removed = asyncio.run(_prune_stale_queued_updates(hass))
    assert removed == 1
    assert [e["id"] for e in hass.casa["qu_data"]["updates"]["D1"]] == ["new", "a"]


def test_queue_is_capped_dropping_oldest_non_auth():
    hass = _hass()
    entries = [_entry("a", "auth")] + [_entry(f"p{i}") for i in range(60)]
    hass.casa["qu_data"]["updates"]["D2"] = entries
    removed = asyncio.run(_prune_stale_queued_updates(hass))
    kept = hass.casa["qu_data"]["updates"]["D2"]
    assert removed == 11 and len(kept) == 50
    assert kept[0]["id"] == "a" and kept[1]["id"] == "p11" and kept[-1]["id"] == "p59"
