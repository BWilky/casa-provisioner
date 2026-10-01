import asyncio
import time
from types import SimpleNamespace

import pytest

import custom_components.casa as casa
from custom_components.casa import (
    _arm_pending_provision,
    _end_provision_window,
    _login_listener,
    _rearm_pending_provisions,
    _safe_qr_filename,
)
from tests.fakes import FakeHass, FakeToken, make_user


@pytest.mark.parametrize("raw,expected", [
    ("front_door", "front_door.png"),
    ("front_door.png", "front_door.png"),
    ("Front Door!", "Front_Door_.png"),
    ("casa_qr", "casa_qr.png"),
    ("../secrets", None),
    ("a/b", None),
    ("a\\b", None),
    ("", None),
    (".hidden", None),
    ("..png", None),
])
def test_safe_qr_filename(raw, expected):
    assert _safe_qr_filename(raw) == expected


def _hass(tmp_path, record=None, user=True):
    hass = FakeHass()
    hass.config = SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p)))
    if user:
        hass.auth.add_user(make_user("u1", login="kitchen", tokens=["t0"]), password="link-pw")
    if record is not None:
        hass.casa["stored_data"]["pending_provisions"] = {"u1": record}
    return hass


def _record(**kw):
    now = time.time()
    rec = {
        "provision_id": "p1", "login_username": "kitchen", "method": "qr", "single_use": True,
        "scramble_at": now + 300, "window_ends_at": now + 300, "listen_until": now + 330,
        "known_token_ids": ["t0"], "qr_file": None, "qr_expire_mode": "delete", "created_at": now,
    }
    rec.update(kw)
    return rec


def _qr(tmp_path, name):
    (tmp_path / "www").mkdir(exist_ok=True)
    f = tmp_path / "www" / name
    f.write_bytes(b"png")
    return f


def test_expired_window_is_closed_immediately_on_rearm(tmp_path):
    past = time.time() - 10
    qr = _qr(tmp_path, "door.png")
    hass = _hass(tmp_path, _record(scramble_at=past, window_ends_at=past, listen_until=past, qr_file="door.png"))

    async def scenario():
        _rearm_pending_provisions(hass)
        await asyncio.gather(*hass.tasks)

    asyncio.run(scenario())
    assert hass.auth.provider.data.passwords["kitchen"] != "link-pw"  # scrambled
    assert not qr.exists()
    assert hass.casa["stored_data"]["pending_provisions"] == {}
    assert hass.casa["timers"] == {} and hass.casa["listeners"] == {}


def test_live_window_rearms_tasks_keyed_by_user_id(tmp_path):
    hass = _hass(tmp_path, _record())

    async def scenario():
        _rearm_pending_provisions(hass)
        keys = (set(hass.casa["timers"]), set(hass.casa["listeners"]))
        for t in hass.tasks:
            t.cancel()
        await asyncio.gather(*hass.tasks, return_exceptions=True)
        return keys

    assert asyncio.run(scenario()) == ({"u1"}, {"u1"})
    assert hass.auth.provider.data.passwords["kitchen"] == "link-pw"  # untouched
    assert "u1" in hass.casa["stored_data"]["pending_provisions"]


def test_redemption_closes_window_and_retires_file(tmp_path, monkeypatch):
    class _Img:
        def __init__(self, text):
            self.text = text

        def save(self, path):
            open(path, "wb").write(self.text.encode())

    monkeypatch.setattr(casa.qrcode, "make", _Img, raising=False)
    qr = _qr(tmp_path, "door.png")
    hass = _hass(tmp_path, _record(qr_file="door.png", qr_expire_mode="expire"))

    async def scenario():
        _arm_pending_provision(hass, "u1")
        await _end_provision_window(hass, "u1", "p1", "redeemed")
        await asyncio.gather(*hass.tasks, return_exceptions=True)

    asyncio.run(scenario())
    assert hass.auth.provider.data.passwords["kitchen"] != "link-pw"
    assert qr.read_bytes().startswith(b"EXPIRED")  # own file overwritten, not deleted
    assert hass.casa["stored_data"]["pending_provisions"] == {}
    assert all(t.done() for t in hass.tasks)


def test_window_for_deleted_user_closes_cleanly(tmp_path):
    past = time.time() - 1
    qr = _qr(tmp_path, "door.png")
    hass = _hass(tmp_path, _record(scramble_at=past, listen_until=past, qr_file="door.png"), user=False)

    async def scenario():
        _rearm_pending_provisions(hass)
        await asyncio.gather(*hass.tasks)

    asyncio.run(scenario())
    assert not qr.exists()
    assert hass.casa["stored_data"]["pending_provisions"] == {}


def test_window_whose_login_was_removed_does_not_raise(tmp_path):
    past = time.time() - 1
    hass = _hass(tmp_path, _record(scramble_at=past, listen_until=past))
    hass.auth.provider.data.passwords.clear()  # change_password -> InvalidUser

    async def scenario():
        _rearm_pending_provisions(hass)
        await asyncio.gather(*hass.tasks)

    asyncio.run(scenario())
    assert hass.casa["stored_data"]["pending_provisions"] == {}


def test_redeemed_event_carries_user_and_provision_id(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast_sleep(_s):
        await real_sleep(0)

    monkeypatch.setattr(casa.asyncio, "sleep", fast_sleep)
    hass = FakeHass()
    user = hass.auth.add_user(make_user("u1", login="kitchen", tokens=["t0"]))

    async def scenario():
        task = asyncio.ensure_future(_login_listener(hass, "kitchen", "u1", {"t0"}, 6, "qr", provision_id="p9"))
        await real_sleep(0)
        user.refresh_tokens["t1"] = FakeToken("t1")
        await task

    asyncio.run(scenario())
    name, event = hass.fired[0]
    assert name == "casa_code_redeemed"
    assert event["user_id"] == "u1" and event["provision_id"] == "p9" and event["username"] == "kitchen"
