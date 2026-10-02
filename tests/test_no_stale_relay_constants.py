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


def test_reload_button_press_queues_app_reload(monkeypatch):
    import custom_components.casa as casa
    from custom_components.casa.button import CasaDeviceReloadButton

    queued = []

    async def fake_queue(hass, device_id, created_by):
        queued.append((device_id, created_by))
        return {"status": "queued"}

    monkeypatch.setattr(casa, "_queue_app_reload", fake_queue)
    stored = {
        "site_id": "S", "site_key": "K",
        "users": {"u1": {"devices": {"d1": {"push_token": "tok1234567890"}}}},
    }
    hass = SimpleNamespace(data={"casa": {"stored_data": stored}})
    button = CasaDeviceReloadButton(hass, "d1", "alice", False)
    asyncio.run(button.async_press())
    assert queued == [("d1", "HA button")]
