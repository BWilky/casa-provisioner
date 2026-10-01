"""Minimal Home Assistant stand-ins for exercising casa's module-level helpers
and HTTP views with plain pytest (no HA test harness)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace


class FakeStore:
    def __init__(self):
        self.saved = None
        self.delayed = 0

    def async_delay_save(self, data_func, delay=0):
        self.delayed += 1
        self.pending = data_func

    async def async_save(self, data):
        self.saved = data
        await asyncio.sleep(0)


class InvalidUser(Exception):
    pass


class FakeProviderData:
    def __init__(self, provider):
        self._provider = provider
        self.passwords = {}
        self.save_delays = []  # per-call extra yields, consumed in order

    @property
    def users(self):
        return [{"username": u, "password": "x"} for u in self.passwords]

    def change_password(self, username, password):
        key = username.strip().casefold()
        if key not in self.passwords:
            raise InvalidUser
        self.passwords[key] = password

    def add_auth(self, username, password):
        key = username.strip().casefold()
        if key in self.passwords:
            raise InvalidUser("username already exists")
        self.passwords[key] = password

    async def async_remove_auth(self, username):
        self.passwords.pop(username.strip().casefold(), None)

    async def async_save(self):
        # Yield so concurrent coroutines can interleave like a real disk write.
        extra = self.save_delays.pop(0) if self.save_delays else 0
        for _ in range(2 + extra):
            await asyncio.sleep(0)


class FakeProvider:
    type = "homeassistant"

    def __init__(self):
        self.data = FakeProviderData(self)

    async def async_get_or_create_credentials(self, data):
        return cred(data["username"])


def cred(username):
    return SimpleNamespace(auth_provider_type="homeassistant", data={"username": username})


class FakeToken:
    def __init__(self, tid):
        self.id = tid
        self.client_name = "Casa"
        self.client_id = "http://x/"
        self.last_used_ip = "192.0.2.1"


def make_user(uid, login=None, name=None, admin=False, tokens=()):
    return SimpleNamespace(
        id=uid,
        name=name if name is not None else (login or uid),
        is_admin=admin,
        is_active=True,
        is_owner=False,
        system_generated=False,
        credentials=[cred(login)] if login else [],
        refresh_tokens={t: FakeToken(t) for t in tokens},
        groups=[],
    )


class FakeAuth:
    def __init__(self, hass):
        self._hass = hass
        self.users = {}
        self.provider = FakeProvider()
        self.auth_providers = [self.provider]
        self.removed_tokens = []
        self.fail_link = False

    def add_user(self, user, password=None):
        self.users[user.id] = user
        for c in user.credentials:
            self.provider.data.passwords[c.data["username"].casefold()] = password or "pw-" + user.id
        return user

    async def async_get_users(self):
        return list(self.users.values())

    async def async_get_user(self, uid):
        return self.users.get(uid)

    async def async_create_user(self, name, group_ids=None, local_only=False):
        uid = f"new{len(self.users)}"
        user = make_user(uid, name=name)
        self.users[uid] = user
        return user

    async def async_link_user(self, user, credentials):
        if self.fail_link:
            raise RuntimeError("link failed")
        user.credentials.append(credentials)

    async def async_remove_user(self, user):
        self.users.pop(user.id, None)

    def async_remove_refresh_token(self, token):
        self.removed_tokens.append(token.id)
        for u in self.users.values():
            u.refresh_tokens.pop(token.id, None)


class FakeHass:
    def __init__(self):
        self.auth = FakeAuth(self)
        self.fired = []
        self.bus = SimpleNamespace(async_fire=lambda name, data: self.fired.append((name, data)))
        self.tasks = []
        self.config = SimpleNamespace(path=lambda *p: "/nonexistent/" + "/".join(p))
        self.data = {
            "casa": {
                "stored_data": {"users": {}, "native_devices": {}},
                "qu_data": {"updates": {}},
                "lz_data": {"config_version": "", "stale_after_minutes": 30, "anchors": []},
                "wg_data": {"profiles": []},
                "pp_data": {"profiles": []},
                "store": FakeStore(),
                "qu_store": FakeStore(),
                "lz_store": FakeStore(),
                "listeners": {},
                "timers": {},
            }
        }

    def async_create_task(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    @property
    def casa(self):
        return self.data["casa"]


class FakeRequest(dict):
    def __init__(self, user, body=None, headers=None, query=None):
        super().__init__(hass_user=user)
        self._body = body or {}
        self.headers = headers or {}
        self.query = query or {}
        self.remote = "192.0.2.9"

    async def json(self):
        return self._body


def bind_view(view):
    """Give a stubbed HomeAssistantView the json helpers the real base has."""
    view.json = lambda data, status_code=200: (status_code, data)
    view.json_message = lambda msg, status_code=200: (status_code, {"message": msg})
    return view


def bearer(refresh_token_id):
    """A fake HA access token whose 'iss' claim is refresh_token_id."""
    import base64
    import json

    body = base64.urlsafe_b64encode(json.dumps({"iss": refresh_token_id}).encode()).decode().rstrip("=")
    return {"Authorization": f"Bearer h.{body}.s"}
