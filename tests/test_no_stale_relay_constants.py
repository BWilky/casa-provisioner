import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "casa"
_STALE = re.compile(r"\bRELAY_URLS\b|\bRELAY_[A-Z_]*_URL\b")


def test_no_stale_relay_constants_anywhere():
    offenders = []
    for path in sorted(PKG.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for match in _STALE.finditer(line):
                if match.group(0) != "RELAY_BASE_URL":
                    offenders.append(f"{path.name}:{lineno}: {match.group(0)}")
    assert offenders == []


def test_button_module_imports():
    import custom_components.casa.button  # noqa: F401


def test_reload_button_press_goes_through_send_push_to_relay(monkeypatch):
    import custom_components.casa as casa
    from custom_components.casa.button import CasaDeviceReloadButton

    sent = []

    async def fake_send(hass, session, payload):
        sent.append(payload)
        return True

    monkeypatch.setattr(casa, "_send_push_to_relay", fake_send)
    stored = {
        "site_id": "S", "site_key": "K",
        "users": {"u1": {"devices": {"d1": {"push_token": "tok1234567890"}}}},
    }
    hass = SimpleNamespace(data={"casa": {"stored_data": stored}})
    button = CasaDeviceReloadButton(hass, "d1", "alice", False)
    asyncio.run(button.async_press())
    assert len(sent) == 1
    assert sent[0]["target"] == "tok1234567890"
    assert sent[0]["data"] == {"command": "clear_cache_and_reload"}
