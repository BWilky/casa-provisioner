import asyncio

from custom_components.casa import _create_casa_user
from tests.fakes import FakeHass, make_user


def _create(hass, name, username):
    return asyncio.run(_create_casa_user(hass, name, username, None, created_by="t"))


def test_login_username_in_use_under_other_display_name_is_rejected_before_user_creation():
    hass = FakeHass()
    hass.auth.add_user(make_user("u1", login="kitchen", name="Kitchen Tablet"))
    result, err = _create(hass, "Guest", "Kitchen")
    assert result is None and "already exists" in err
    assert set(hass.auth.users) == {"u1"}


def test_orphaned_provider_login_is_rejected():
    hass = FakeHass()
    hass.auth.provider.data.passwords["ghost"] = "x"  # login with no HA user
    result, err = _create(hass, "Ghost", "ghost")
    assert result is None and err
    assert hass.auth.users == {}


def test_credential_failure_rolls_back_ha_user_and_login():
    hass = FakeHass()
    hass.auth.fail_link = True
    result, err = _create(hass, "Guest", "guest")
    assert result is None and "credentials" in err
    assert hass.auth.users == {}
    assert "guest" not in hass.auth.provider.data.passwords
    assert hass.casa["stored_data"]["users"] == {}


def test_happy_path_creates_and_tracks_user():
    hass = FakeHass()
    result, err = _create(hass, "Guest", "Guest")
    assert err is None and result["username"] == "guest"
    assert result["user_id"] in hass.casa["stored_data"]["users"]
    assert "guest" in hass.auth.provider.data.passwords
