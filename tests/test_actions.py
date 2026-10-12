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
