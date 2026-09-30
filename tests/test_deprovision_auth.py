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
