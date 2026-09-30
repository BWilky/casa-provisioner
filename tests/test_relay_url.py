from types import SimpleNamespace

from custom_components.casa import relay_url


def _hass(option=None):
    entry = SimpleNamespace(options={} if option is None else {"relay_base_url": option})
    return SimpleNamespace(data={"casa": {"config_entry": entry}})


def test_default_when_option_absent():
    assert relay_url(_hass(), "/send") == "https://push.bonjour.casa/send"


def test_option_overrides():
    assert relay_url(_hass("http://192.168.1.50:8000"), "/register_site") == "http://192.168.1.50:8000/register_site"


def test_trailing_slash_and_blank_are_normalised():
    assert relay_url(_hass("http://relay.local/"), "/send") == "http://relay.local/send"
    assert relay_url(_hass("   "), "/send") == "https://push.bonjour.casa/send"
