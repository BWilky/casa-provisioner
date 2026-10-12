"""Pure logic for actionable notifications: validation, device-safe copy,
pending-record lifecycle."""
import pytest

from custom_components.casa.actions import (
    CATEGORY,
    MAX_ACTIONS,
    device_actions,
    validate_actions,
)


def _ok(**extra):
    return {"actions": [{"action": "UNLOCK", "title": "Unlock", **extra}]}


def test_category_constant():
    assert CATEGORY == "CASA_DYNAMIC"


def test_minimal_action_passes_and_is_normalised():
    out = validate_actions(_ok())
    assert out == [{"action": "UNLOCK", "title": "Unlock"}]


def test_full_action_passes():
    out = validate_actions(_ok(service="lock.unlock", target={"entity_id": "lock.a"}, data={"x": 1},
                                nav_path="/lovelace/0", text_input=True, destructive=True, auth_required=True))
    assert out[0]["service"] == "lock.unlock" and out[0]["auth_required"] is True


@pytest.mark.parametrize("bad", [
    {"actions": []},
    {"actions": "nope"},
    {"actions": [{"title": "x"}]},                       # missing action
    {"actions": [{"action": "A"}]},                      # missing title
    {"actions": [{"action": "", "title": "x"}]},
    {"actions": [{"action": "a" * 65, "title": "x"}]},
    {"actions": [{"action": "A", "title": "t" * 65}]},
    {"actions": [{"action": "A", "title": "x"}, {"action": "A", "title": "y"}]},   # duplicate
    {"actions": [{"action": "A", "title": "x", "service": "lock"}]},                # no dot
    {"actions": [{"action": "A", "title": "x", "service": "Lock.Unlock"}]},         # uppercase
    {"actions": [{"action": "A", "title": "x", "target": "lock.a"}]},               # not a dict
    {"actions": [{"action": "A", "title": "x", "data": []}]},
    {"actions": [{"action": "A", "title": "x", "nav_path": "/" * 513}]},
    {"actions": [{"action": "A", "title": "x", "text_input": "yes"}]},
    {"actions": [{"action": "A", "title": "x", "bogus": 1}]},                       # unknown key
    {"actions": [{"action": str(i), "title": "x"} for i in range(MAX_ACTIONS + 1)]},
    {"actions": [{"action": "A", "title": "x"}], "tag": "t" * 129},
    {"actions": [{"action": "A", "title": "x"}], "tag": 5},
    {"actions": [{"action": "A", "title": "x"}], "action_data": "str"},
])
def test_invalid_actions_rejected(bad):
    with pytest.raises(ValueError):
        validate_actions(bad)


def test_ten_actions_allowed():
    out = validate_actions({"actions": [{"action": str(i), "title": "x"} for i in range(MAX_ACTIONS)]})
    assert len(out) == MAX_ACTIONS


def test_device_actions_strips_server_only_keys_without_mutating():
    actions = validate_actions(_ok(service="lock.unlock", target={"entity_id": "lock.a"}, data={"x": 1}, nav_path="/p"))
    safe = device_actions(actions)
    assert safe == [{"action": "UNLOCK", "title": "Unlock", "nav_path": "/p"}]
    assert "service" in actions[0]  # original untouched


from custom_components.casa.actions import (
    CONSUMED_TTL_SECONDS, PENDING_TTL_SECONDS,
    consume_pending, create_pending, prune_pending, validate_press,
)

ACTS = [{"action": "UNLOCK", "title": "Unlock", "service": "lock.unlock"}, {"action": "IGNORE", "title": "Ignore"}]


def _na(now=1_000_000.0):
    na = {"pending": {}}
    nid = create_pending(na, device_id="D1", user_id="u1", username="alice", actions=ACTS, tag="t", action_data={"k": 1}, now=now)
    return na, nid


def test_create_pending_records_everything_and_unique_ids():
    na, nid = _na()
    rec = na["pending"][nid]
    assert rec["device_id"] == "D1" and rec["user_id"] == "u1" and rec["username"] == "alice"
    assert rec["actions"] == ACTS and rec["tag"] == "t" and rec["action_data"] == {"k": 1}
    assert rec["created_at"] == 1_000_000.0 and "consumed_at" not in rec
    nid2 = create_pending(na, device_id="D2", user_id="u1", username="alice", actions=ACTS, now=1_000_000.0)
    assert nid2 != nid and len(nid) >= 32


def test_consume_returns_entry_then_duplicate():
    na, nid = _na()
    rec, entry = consume_pending(na, notification_id=nid, device_id="D1", action="UNLOCK", now=1_000_010.0)
    assert entry["service"] == "lock.unlock" and rec["consumed_at"] == 1_000_010.0
    rec2, entry2 = consume_pending(na, notification_id=nid, device_id="D1", action="UNLOCK", now=1_000_020.0)
    assert rec2 is rec and entry2 is None


@pytest.mark.parametrize("kw", [
    dict(notification_id="nope", device_id="D1", action="UNLOCK"),
    dict(device_id="D2", action="UNLOCK"),            # wrong device
    dict(device_id="D1", action="EXPLODE"),           # unknown action
])
def test_consume_rejects(kw):
    na, nid = _na()
    kw.setdefault("notification_id", nid)
    with pytest.raises(LookupError):
        consume_pending(na, now=1_000_010.0, **kw)


def test_consume_rejects_expired():
    na, nid = _na()
    with pytest.raises(LookupError):
        consume_pending(na, notification_id=nid, device_id="D1", action="UNLOCK", now=1_000_000.0 + PENDING_TTL_SECONDS + 1)


def test_prune_drops_expired_and_old_consumed_only():
    na, old = _na(now=0.0)
    fresh = create_pending(na, device_id="D1", user_id="u1", username="a", actions=ACTS, now=1_000_000.0)
    recent_consumed = create_pending(na, device_id="D1", user_id="u1", username="a", actions=ACTS, now=1_000_000.0)
    consume_pending(na, notification_id=recent_consumed, device_id="D1", action="IGNORE", now=1_000_000.0)
    old_consumed = create_pending(na, device_id="D1", user_id="u1", username="a", actions=ACTS, now=990_000.0)
    consume_pending(na, notification_id=old_consumed, device_id="D1", action="IGNORE", now=990_000.0)
    removed = prune_pending(na, now=1_000_000.0 + 600)
    assert removed == 2
    assert set(na["pending"]) == {fresh, recent_consumed}
    assert prune_pending(na, now=1_000_000.0 + CONSUMED_TTL_SECONDS + 1) == 1
    assert set(na["pending"]) == {fresh}


def test_validate_press_ok_and_optional_reply():
    inner = validate_press({"notification_id": "n", "action": "A", "ts": 1_000_000}, now=1_000_100.0)
    assert inner == {"notification_id": "n", "action": "A", "ts": 1_000_000}
    inner = validate_press({"notification_id": "n", "action": "A", "reply_text": "hi", "ts": 1_000_000}, now=1_000_000.0)
    assert inner["reply_text"] == "hi"


@pytest.mark.parametrize("inner", [
    {"notification_id": "n", "action": "A"},                                  # no ts
    {"notification_id": "n", "action": "A", "ts": 1_000_000 - 301},
    {"notification_id": "n", "action": "A", "ts": "1000000"},
    {"notification_id": "n", "action": "A", "ts": 1_000_000, "extra": 1},
    {"notification_id": "", "action": "A", "ts": 1_000_000},
    {"notification_id": "n", "action": 5, "ts": 1_000_000},
    {"notification_id": "n", "action": "A", "ts": 1_000_000, "reply_text": "x" * 1001},
    {"notification_id": "n", "action": "A", "ts": 1_000_000, "reply_text": 7},
])
def test_validate_press_rejects(inner):
    with pytest.raises(ValueError):
        validate_press(inner, now=1_000_000.0)


def test_validate_press_rejects_non_finite_ts():
    for bad_ts in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            validate_press({"notification_id": "n", "action": "A", "ts": bad_ts}, now=1_000_000.0)


def test_consumed_record_still_enforces_device_and_action():
    na, nid = _na()
    rec, entry = consume_pending(na, notification_id=nid, device_id="D1", action="UNLOCK", now=1_000_010.0)
    assert entry is not None
    with pytest.raises(LookupError):
        consume_pending(na, notification_id=nid, device_id="D2", action="UNLOCK", now=1_000_020.0)
    with pytest.raises(LookupError):
        consume_pending(na, notification_id=nid, device_id="D1", action="EXPLODE", now=1_000_020.0)
    rec2, entry2 = consume_pending(na, notification_id=nid, device_id="D1", action="UNLOCK", now=1_000_030.0)
    assert rec2 is rec and entry2 is None


def test_actions_at_every_cap_are_too_large_to_push():
    acts = [{"action": f"{i}" + "A" * 63, "title": "T" * 64, "nav_path": "/" + "p" * 511,
             "destructive": True, "auth_required": True, "text_input": True} for i in range(10)]
    with pytest.raises(ValueError, match="too large to push"):
        validate_actions({"actions": acts})


def test_realistic_ten_actions_fit():
    acts = [{"action": f"ACTION_{i:02d}", "title": f"Do thing number {i}", "nav_path": f"/lovelace/room-{i}",
             "service": "script.turn_on", "target": {"entity_id": "script." + "x" * 200},
             "data": {"variables": {"blob": "y" * 500}}} for i in range(10)]
    out = validate_actions({"actions": acts})
    assert len(out) == 10
