from custom_components.casa import (
    _migrate_legacy_site_credentials,
    _relay_site_credentials,
    _set_relay_site_credentials,
    _delete_relay_site_credentials,
    _validate_relay_base_url,
)

PROD = "https://push.bonjour.casa"
LAB = "http://192.168.1.50:8000"


def test_migrates_legacy_keys_under_default_base():
    data = {"site_id": "S1", "site_key": "K1"}
    _migrate_legacy_site_credentials(data, PROD)
    assert data["relay_sites"] == {PROD: {"site_id": "S1", "site_key": "K1"}}
    assert _relay_site_credentials(data, PROD) == {"site_id": "S1", "site_key": "K1"}


def test_migration_is_noop_when_relay_sites_present():
    data = {"site_id": "S1", "site_key": "K1", "relay_sites": {LAB: {"site_id": "L", "site_key": "LK"}}}
    _migrate_legacy_site_credentials(data, PROD)
    assert data["relay_sites"] == {LAB: {"site_id": "L", "site_key": "LK"}}


def test_migration_without_legacy_keys_creates_empty_map():
    data = {}
    _migrate_legacy_site_credentials(data, PROD)
    assert data["relay_sites"] == {}


def test_per_base_isolation():
    data = {}
    _set_relay_site_credentials(data, PROD, "S1", "K1")
    _set_relay_site_credentials(data, LAB, "S2", "K2")
    assert _relay_site_credentials(data, PROD) == {"site_id": "S1", "site_key": "K1"}
    assert _relay_site_credentials(data, LAB) == {"site_id": "S2", "site_key": "K2"}
    assert _relay_site_credentials(data, "https://other") is None


def test_deleting_one_base_leaves_the_other():
    data = {}
    _set_relay_site_credentials(data, PROD, "S1", "K1")
    _set_relay_site_credentials(data, LAB, "S2", "K2")
    _delete_relay_site_credentials(data, LAB)
    assert _relay_site_credentials(data, LAB) is None
    assert _relay_site_credentials(data, PROD) == {"site_id": "S1", "site_key": "K1"}


def test_validator_accepts_https():
    assert _validate_relay_base_url("https://relay.example.com") is None
    assert _validate_relay_base_url("https://push.bonjour.casa/") is None


def test_validator_accepts_http_on_lan():
    for url in (
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://10.0.0.5",
        "http://172.16.0.1",
        "http://172.31.255.1",
        "http://192.168.1.50:8000",
        "http://relay.local",
    ):
        assert _validate_relay_base_url(url) is None, url


def test_validator_rejects_http_public():
    for url in ("http://relay.example.com", "http://172.32.0.1", "http://8.8.8.8"):
        assert _validate_relay_base_url(url) == "invalid_relay_url", url


def test_validator_rejects_garbage():
    for url in ("not a url", "ftp://relay.local", "https://", "relay.example.com"):
        assert _validate_relay_base_url(url) == "invalid_relay_url", url


def test_validator_blank_means_default():
    assert _validate_relay_base_url("") is None
    assert _validate_relay_base_url("   ") is None
    assert _validate_relay_base_url(None) is None


def test_verify_403_on_lab_relay_keeps_production_credentials(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import custom_components.casa as casa

    calls = []

    class _Resp:
        def __init__(self, status, body=None):
            self.status, self._body = status, body or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self, **kw):
            return self._body

        async def text(self):
            return ""

    class _Session:
        def post(self, url, json=None, timeout=None):
            calls.append(url)
            if url.endswith("/verify_site"):
                return _Resp(403)
            return _Resp(201, {"site_key": "NEWLAB"})

    class _Store:
        async def async_save(self, data):
            pass

    monkeypatch.setattr(casa, "async_get_clientsession", lambda hass: _Session())
    entry = SimpleNamespace(options={"relay_base_url": LAB})
    hass = SimpleNamespace(data={"casa": {"config_entry": entry}})
    data = {"site_id": "S1", "site_key": "K1"}  # legacy prod credentials
    casa._migrate_legacy_site_credentials(data, PROD)
    _set_relay_site_credentials(data, LAB, "L1", "LK1")
    casa._activate_relay_site(data, casa.relay_base(hass))
    assert (data["site_id"], data["site_key"]) == ("L1", "LK1")

    asyncio.run(casa._ensure_site_registration(hass, data, _Store()))

    assert calls == [LAB + "/verify_site", LAB + "/register_site"]
    assert _relay_site_credentials(data, PROD) == {"site_id": "S1", "site_key": "K1"}
    assert _relay_site_credentials(data, LAB) == {"site_id": "L1", "site_key": "NEWLAB"}
    assert data["site_key"] == "NEWLAB"


def test_activate_base_without_entry_clears_active_keys_only():
    data = {"site_id": "S1", "site_key": "K1"}
    _migrate_legacy_site_credentials(data, PROD)
    import custom_components.casa as casa
    casa._activate_relay_site(data, LAB)
    assert "site_id" not in data and "site_key" not in data
    assert _relay_site_credentials(data, PROD) == {"site_id": "S1", "site_key": "K1"}
    casa._activate_relay_site(data, PROD)
    assert (data["site_id"], data["site_key"]) == ("S1", "K1")
