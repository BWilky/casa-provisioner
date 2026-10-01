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


def test_explicit_entry_wins_without_hass_data():
    entry = SimpleNamespace(options={"relay_base_url": "http://relay.local:9000"})
    hass = SimpleNamespace(data={})
    assert relay_url(hass, "/remove_site", entry) == "http://relay.local:9000/remove_site"


def test_explicit_entry_wins_over_stored_entry():
    entry = SimpleNamespace(options={"relay_base_url": "http://relay.local:9000"})
    assert relay_url(_hass("http://10.0.0.1"), "/send", entry=entry) == "http://relay.local:9000/send"


from custom_components.casa import payload_relay_url


def test_payload_relay_url_only_for_non_default_relay():
    assert payload_relay_url(_hass()) is None
    assert payload_relay_url(_hass("https://push.bonjour.casa/")) is None
    assert payload_relay_url(_hass("http://relay.local:9000/")) == "http://relay.local:9000"
