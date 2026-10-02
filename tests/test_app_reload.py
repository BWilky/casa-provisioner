import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import custom_components.casa as casa
from custom_components.casa import _drop_expired_app_reloads, _queue_app_reload
from tests.fakes import FakeHass


def _hass(push_token="a" * 64):
    hass = FakeHass()
    d1 = {"refresh_token_id": "r1"}
    if push_token:
        d1["push_token"] = push_token
    hass.casa["stored_data"]["users"]["u1"] = {"username": "kiosk", "devices": {"D1": d1}}
    hass.casa["stored_data"]["device_key"] = "k" * 44
    return hass


def _patch(monkeypatch, pushed=True):
    calls = {"push": 0, "nudge": 0}

    async def fake_push(*a, **kw):
        calls["push"] += 1
        return pushed

    async def fake_nudge(*a, **kw):
        calls["nudge"] += 1
        return True

    monkeypatch.setattr(casa, "_send_encrypted_update_push", fake_push)
    monkeypatch.setattr(casa, "_nudge_device_checkin", fake_nudge)
    return calls


def _reloads(hass):
    return [e for e in hass.casa["qu_data"]["updates"].get("D1", []) if e["type"] == "app"]


def test_queues_entry_and_pushes(monkeypatch):
    hass = _hass()
    calls = _patch(monkeypatch)
    result = asyncio.run(_queue_app_reload(hass, "D1", created_by="test"))
    (entry,) = _reloads(hass)
    assert entry["action"] == "clear_cache_reload" and entry["payload"] == {}
    assert result == {"status": "queued", "update_id": entry["id"], "pushed": True}
    assert calls == {"push": 1, "nudge": 0}


def test_second_reload_replaces_pending_one(monkeypatch):
    hass = _hass()
    _patch(monkeypatch)
    first = asyncio.run(_queue_app_reload(hass, "D1", created_by="test"))
    hass.casa["qu_data"]["updates"]["D1"].append({"id": "p1", "type": "profile", "action": "update", "payload": {}})
    second = asyncio.run(_queue_app_reload(hass, "D1", created_by="test"))
    reloads = _reloads(hass)
    assert [e["id"] for e in reloads] == [second["update_id"]] != [first["update_id"]]
    assert any(e["id"] == "p1" for e in hass.casa["qu_data"]["updates"]["D1"])


def test_no_push_token_still_queues_and_nudges(monkeypatch):
    hass = _hass(push_token=None)
    calls = _patch(monkeypatch, pushed=False)
    result = asyncio.run(_queue_app_reload(hass, "D1", created_by="test"))
    assert result["pushed"] is False and len(_reloads(hass)) == 1
    assert calls == {"push": 1, "nudge": 1}


def test_unknown_device_raises(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(Exception, match="not found"):
        asyncio.run(_queue_app_reload(_hass(), "NOPE", created_by="test"))


def test_expired_reloads_are_dropped_on_pull():
    now = datetime.now(timezone.utc)
    qu_data = {"updates": {"D1": [
        {"id": "old", "type": "app", "action": "clear_cache_reload", "payload": {}, "created_at": (now - timedelta(hours=25)).isoformat()},
        {"id": "new", "type": "app", "action": "clear_cache_reload", "payload": {}, "created_at": (now - timedelta(hours=1)).isoformat()},
        {"id": "prof", "type": "profile", "action": "update", "payload": {}, "created_at": (now - timedelta(days=3)).isoformat()},
    ]}}
    assert _drop_expired_app_reloads(qu_data, "D1") is True
    assert [e["id"] for e in qu_data["updates"]["D1"]] == ["new", "prof"]
    assert _drop_expired_app_reloads(qu_data, "D1") is False
