from custom_components.casa import _device_owned_by


def _data():
    return {
        "users": {
            "u1": {"username": "alice", "devices": {"d1": {}}},
            "u2": {"username": "bob", "devices": {"d2": {}}},
            "u3": {"username": "gone", "deleted": True, "devices": {"d3": {}}},
        },
        "native_devices": {"n1": {"d4": {}}},
    }


def test_managed_user_owns_own_device():
    assert _device_owned_by(_data(), "u1", "d1") is True


def test_other_users_device_is_not_owned():
    assert _device_owned_by(_data(), "u1", "d2") is False


def test_deleted_user_cannot_own():
    assert _device_owned_by(_data(), "u3", "d3") is False


def test_native_user_owns_native_device():
    assert _device_owned_by(_data(), "n1", "d4") is True


def test_unknown_ids_are_false():
    assert _device_owned_by(_data(), "u1", "nope") is False
    assert _device_owned_by(_data(), "nobody", "d1") is False
    assert _device_owned_by({}, "u1", "d1") is False


from custom_components.casa import _pop_device_record


def _dup():
    return {
        "users": {
            "A": {"username": "old", "deleted": True, "devices": {"PHONE": {"refresh_token_id": "rA"}}},
            "B": {"username": "live", "devices": {"PHONE": {"refresh_token_id": "rB"}}},
        },
        "native_devices": {},
    }


def test_pop_with_owner_removes_only_owners_record():
    data = _dup()
    owner, info, username = _pop_device_record(data, "PHONE", "B")
    assert owner == "B" and info == {"refresh_token_id": "rB"} and username == "live"
    assert "PHONE" not in data["users"]["B"]["devices"]
    assert "PHONE" in data["users"]["A"]["devices"]


def test_pop_without_owner_keeps_legacy_first_match():
    data = _dup()
    owner, info, _ = _pop_device_record(data, "PHONE")
    assert owner == "A" and info == {"refresh_token_id": "rA"}
    assert "PHONE" in data["users"]["B"]["devices"]


def test_pop_with_owner_lacking_device_returns_none():
    data = _dup()
    data["users"]["B"]["devices"] = {}
    owner, info, _ = _pop_device_record(data, "PHONE", "B")
    assert owner is None and info is None
    assert "PHONE" in data["users"]["A"]["devices"]


def test_pop_with_owner_finds_native_record():
    data = {"users": {}, "native_devices": {"N": {"PAD": {"push_token": "p"}}, "M": {"PAD": {}}}}
    owner, info, username = _pop_device_record(data, "PAD", "M")
    assert owner == "M" and info == {} and username == "M"
    assert "PAD" in data["native_devices"]["N"]
