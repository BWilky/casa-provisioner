import asyncio
import logging
from types import SimpleNamespace

from custom_components.casa import _login_listener

_real_sleep = asyncio.sleep  # monkeypatching casa.asyncio.sleep patches the global asyncio.sleep


class _Token:
    def __init__(self, tid):
        self.id = tid
        self.client_name = "Casa"
        self.client_id = "http://x/"
        self.last_used_ip = "192.0.2.1"


class _FakeHass:
    def __init__(self, user):
        self._user = user
        self.fired = []
        self.auth = SimpleNamespace(async_get_users=self._users)
        self.bus = SimpleNamespace(async_fire=lambda name, data: self.fired.append((name, data)))
        self.data = {"casa": {"listeners": {}, "timers": {}}}

    async def _users(self):
        return [self._user]


def _run(listener_coro):
    return asyncio.run(listener_coro)


def test_on_redeemed_called_once_after_event(monkeypatch):
    import custom_components.casa as casa
    monkeypatch.setattr(casa.asyncio, "sleep", _fast_sleep)
    user = SimpleNamespace(id="u1", refresh_tokens={"t1": _Token("t1")})
    hass = _FakeHass(user)
    calls = []

    async def on_redeemed():
        calls.append(len(hass.fired))  # how many events had fired when called

    async def scenario():
        task = asyncio.ensure_future(
            _login_listener(hass, "alice", "u1", {"t1"}, 10, "deep_link", on_redeemed=on_redeemed)
        )
        await _real_sleep(0)  # let it start
        user.refresh_tokens["t2"] = _Token("t2")  # the phone logged in
        await task

    _run(scenario())
    assert [n for n, _ in hass.fired] == ["casa_code_redeemed"]
    assert calls == [1]  # called exactly once, and after the event fired


def test_without_callback_only_fires_event(monkeypatch):
    import custom_components.casa as casa
    monkeypatch.setattr(casa.asyncio, "sleep", _fast_sleep)
    user = SimpleNamespace(id="u1", refresh_tokens={"t1": _Token("t1")})
    hass = _FakeHass(user)

    async def scenario():
        task = asyncio.ensure_future(_login_listener(hass, "alice", "u1", {"t1"}, 4, "qr"))
        await _real_sleep(0)
        user.refresh_tokens["t2"] = _Token("t2")
        await task

    _run(scenario())
    assert len(hass.fired) == 1


def test_on_redeemed_error_is_logged_and_listener_returns(monkeypatch, caplog):
    import custom_components.casa as casa
    monkeypatch.setattr(casa.asyncio, "sleep", _fast_sleep)
    user = SimpleNamespace(id="u1", refresh_tokens={"t1": _Token("t1")})
    hass = _FakeHass(user)

    async def on_redeemed():
        raise RuntimeError("boom")

    async def scenario():
        task = asyncio.ensure_future(
            _login_listener(hass, "alice", "u1", {"t1"}, 10, "deep_link", on_redeemed=on_redeemed)
        )
        await _real_sleep(0)
        user.refresh_tokens["t2"] = _Token("t2")
        await task

    with caplog.at_level(logging.ERROR):
        _run(scenario())
    assert [n for n, _ in hass.fired] == ["casa_code_redeemed"]
    assert "on_redeemed for 'alice' failed" in caplog.text


async def _fast_sleep(_seconds):
    await _real_sleep(0)


def test_poll_interval_rises_after_1800s(monkeypatch):
    import custom_components.casa as casa
    delays = []

    async def recording_sleep(seconds):
        delays.append(seconds)
        await _real_sleep(0)

    monkeypatch.setattr(casa.asyncio, "sleep", recording_sleep)
    user = SimpleNamespace(id="u1", refresh_tokens={"t1": _Token("t1")})
    hass = _FakeHass(user)
    _run(_login_listener(hass, "alice", "u1", {"t1"}, 2000, "deep_link", on_redeemed=None))
    assert delays[:900] == [2] * 900  # 2 s polls for the first 30 minutes
    assert set(delays[900:]) == {10}  # then 10 s
    assert sum(delays) >= 2000
