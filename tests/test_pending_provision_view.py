"""Unredeemed provisioning links in the admin panel: the summary lists them
(without links), and /api/casa/admin/pending_provision shows one link again,
revokes it, regenerates it, or dismisses an expired row."""
import asyncio
import time

import custom_components.casa as casa
from custom_components.casa import (
    CasaAdminPendingProvisionView,
    CasaAdminSummaryView,
    _pending_provision_rows,
)
from tests.fakes import FakeHass, FakeRequest, bind_view, make_user

ADMIN = make_user("admin", admin=True)
GUEST = make_user("guest")


def _rec(**kw):
    now = time.time()
    rec = {
        "provision_id": "p1", "login_username": "casa-host-christoph", "method": "qr", "single_use": True,
        "scramble_at": now + 86400, "window_ends_at": now + 86400, "listen_until": now + 86430,
        "known_token_ids": ["t0"], "qr_file": None, "qr_expire_mode": "delete", "created_at": now,
        "deep_link": "hascasa://setup?data=AoZ", "universal_link": "https://bonjour.casa/setup?d=AoZ",
        "expires_at": int(now + 86400), "device_alias": "Host - Christoph", "profile_id": "t1",
        "profile_name": "Owners", "expiration_hours": 336, "host_url": "https://h",
        "regen": {"method": "qr", "host_url": "https://h", "username": "casa-host-christoph",
                  "device_alias": "Host - Christoph", "profile": "t1", "timeout_minutes": 1440},
        "replaces_device_id": None,
    }
    rec.update(kw)
    return rec


def _hass(pending=None, expired=None):
    hass = FakeHass()
    hass.auth.add_user(make_user("u1", login="casa-host-christoph", tokens=["t0"]), password="link-pw")
    hass.casa["stored_data"]["users"]["u1"] = {"username": "casa-host-christoph", "name": "Host - Christoph", "devices": {}}
    if pending is not None:
        hass.casa["stored_data"]["pending_provisions"] = {"u1": pending}
    if expired is not None:
        hass.casa["stored_data"]["expired_provisions"] = {"u1": expired}
    return hass


def _expired_row(**kw):
    row = {"provision_id": "p0", "login_username": "casa-host-christoph", "method": "qr",
           "device_alias": "Host - Christoph", "profile_name": "Owners", "created_at": time.time() - 90000,
           "expired_at": time.time() - 3600, "regen": {"method": "qr", "host_url": "https://h", "username": "casa-host-christoph"},
           "host_url": "https://h", "replaces_device_id": None}
    row.update(kw)
    return row


def _view(hass, provision=None):
    calls = []

    async def default_provision(service_data, users=None, *, replaces_device_id=None):
        calls.append((dict(service_data), replaces_device_id))
        return {"method": "qr", "provision_id": "p2", "qr_data_uri": "data:image/png;base64,BB",
                "deep_link": "hascasa://setup?data=new", "universal_link": "https://bonjour.casa/setup?d=new",
                "expires_at": int(time.time() + 86400), "session_expiration_hours": 336}

    view = bind_view(CasaAdminPendingProvisionView(hass, provision or default_provision))
    return view, calls


# ---- rows / summary ----

def test_rows_list_awaiting_and_expired_without_links():
    now = time.time()
    stored = {"users": {"u1": {"username": "a"}, "u2": {"username": "b"}, "u3": {"username": "c", "deleted": True}},
              "pending_provisions": {"u1": _rec(), "u3": _rec()},
              "expired_provisions": {"u2": _expired_row(), "u3": _expired_row()}}
    rows = _pending_provision_rows(stored, now)
    assert [(r["user_id"], r["state"]) for r in rows] == [("u1", "awaiting"), ("u2", "expired")]
    assert rows[0]["name"] == "Host - Christoph" and rows[0]["has_links"] is True
    assert rows[0]["expires_at"] == _rec()["expires_at"] or abs(rows[0]["expires_at"] - (now + 86400)) < 5
    assert rows[1]["expires_at"] is None and rows[1]["has_links"] is False
    for r in rows:
        assert "deep_link" not in r and "universal_link" not in r and "regen" not in r


def test_rows_prune_old_tombstones_and_hide_one_superseded_by_a_new_window():
    now = time.time()
    stored = {"users": {"u1": {}, "u2": {}},
              "pending_provisions": {"u1": _rec()},
              "expired_provisions": {"u1": _expired_row(), "u2": _expired_row(expired_at=now - 8 * 86400)}}
    rows = _pending_provision_rows(stored, now)
    assert [(r["user_id"], r["state"]) for r in rows] == [("u1", "awaiting")]
    assert "u2" not in stored["expired_provisions"]  # pruned


def test_legacy_window_without_links_still_lists_with_scramble_as_expiry():
    rec = _rec()
    for k in ("deep_link", "universal_link", "expires_at", "device_alias", "regen"):
        rec.pop(k)
    rows = _pending_provision_rows({"users": {}, "pending_provisions": {"u1": rec}}, time.time())
    assert rows[0]["has_links"] is False
    assert rows[0]["expires_at"] == int(rec["scramble_at"])


def test_summary_carries_pending_rows_and_stat():
    hass = _hass(pending=_rec())
    view = bind_view(CasaAdminSummaryView(hass))
    status, resp = asyncio.run(view.get(FakeRequest(ADMIN)))
    assert status == 200
    assert resp["pending_provisions"][0]["user_id"] == "u1"
    assert resp["stats"]["awaiting_setup"] == 1
    assert resp["accounts"][0]["device_count"] == 0  # the account itself is unaffected


# ---- GET ----

def test_get_requires_admin():
    view, _ = _view(_hass(pending=_rec()))
    status, _ = asyncio.run(view.get(FakeRequest(GUEST, query={"user_id": "u1"})))
    assert status == 403


def test_get_404_when_no_open_window():
    view, _ = _view(_hass())
    status, resp = asyncio.run(view.get(FakeRequest(ADMIN, query={"user_id": "u1"})))
    assert status == 404


def test_get_returns_links_and_renders_qr_on_demand(monkeypatch):
    monkeypatch.setattr(casa, "_qr_png_data_uri", lambda text: "data:image/png;base64,QR:" + text)
    view, _ = _view(_hass(pending=_rec()))
    status, resp = asyncio.run(view.get(FakeRequest(ADMIN, query={"user_id": "u1"})))
    assert status == 200
    assert resp["deep_link"] == "hascasa://setup?data=AoZ"
    assert resp["universal_link"] == "https://bonjour.casa/setup?d=AoZ"
    assert resp["qr_data_uri"] == "data:image/png;base64,QR:hascasa://setup?data=AoZ"
    assert resp["name"] == "Host - Christoph" and resp["username"] == "casa-host-christoph"
    assert resp["session_expiration_hours"] == 336
    assert "regen" not in resp and "known_token_ids" not in resp


# ---- POST ----

def _post(view, body, user=ADMIN):
    return asyncio.run(view.post(FakeRequest(user, body=body)))


def test_post_rejects_bad_action():
    view, _ = _view(_hass(pending=_rec()))
    status, resp = _post(view, {"user_id": "u1", "action": "explode"})
    assert status == 400


def test_revoke_closes_window_scrambles_and_leaves_no_expired_row():
    hass = _hass(pending=_rec())
    view, _ = _view(hass)
    status, resp = _post(view, {"user_id": "u1", "action": "revoke"})
    assert status == 200 and resp["status"] == "revoked"
    assert hass.casa["stored_data"]["pending_provisions"] == {}
    assert hass.auth.provider.data.passwords["casa-host-christoph"] != "link-pw"
    assert "u1" not in (hass.casa["stored_data"].get("expired_provisions") or {})
    assert "u1" in hass.casa["stored_data"]["users"]  # account untouched


def test_revoke_404_when_nothing_open():
    view, _ = _view(_hass(expired=_expired_row()))
    status, _ = _post(view, {"user_id": "u1", "action": "revoke"})
    assert status == 404


def test_dismiss_drops_expired_row():
    hass = _hass(expired=_expired_row())
    view, _ = _view(hass)
    status, resp = _post(view, {"user_id": "u1", "action": "dismiss"})
    assert status == 200 and resp["status"] == "dismissed"
    assert hass.casa["stored_data"]["expired_provisions"] == {}


def test_regenerate_from_expired_row_reuses_request_without_password():
    hass = _hass(expired=_expired_row(regen={"method": "qr", "host_url": "https://h", "username": "casa-host-christoph",
                                               "password": "old-secret", "profile": "t1"}))
    view, calls = _view(hass)
    status, resp = _post(view, {"user_id": "u1", "action": "regenerate", "host_url": "https://origin"})
    assert status == 200 and resp["method"] == "qr" and resp["deep_link"] == "hascasa://setup?data=new"
    (data, replaces), = calls
    assert data["user_id"] == "u1" and data["username"] == "casa-host-christoph"
    assert data["profile"] == "t1" and data["host_url"] == "https://h"  # stored host wins over origin
    assert data["device_alias"] == "Host - Christoph"
    assert "password" not in data
    assert replaces is None
    assert hass.casa["stored_data"]["expired_provisions"] == {}  # tombstone cleared


def test_regenerate_from_open_window_keeps_replaces_device_id():
    hass = _hass(pending=_rec(replaces_device_id="D-old"))
    view, calls = _view(hass)
    status, resp = _post(view, {"user_id": "u1", "action": "regenerate"})
    assert status == 200
    assert calls[0][1] == "D-old"
    assert calls[0][0]["timeout_minutes"] == 1440  # the admin's link window is kept


def test_regenerate_404_when_nothing_to_regenerate():
    view, calls = _view(_hass())
    status, _ = _post(view, {"user_id": "u1", "action": "regenerate"})
    assert status == 404 and calls == []


def test_regenerate_refuses_deleted_or_admin_accounts():
    hass = _hass(expired=_expired_row())
    hass.casa["stored_data"]["users"]["u1"]["deleted"] = True
    view, calls = _view(hass)
    status, _ = _post(view, {"user_id": "u1", "action": "regenerate"})
    assert status == 400 and calls == []

    hass = _hass(expired=_expired_row())
    hass.auth.users["u1"].is_admin = True
    view, calls = _view(hass)
    status, _ = _post(view, {"user_id": "u1", "action": "regenerate"})
    assert status == 400 and calls == []


def test_regenerate_provision_error_is_400_and_keeps_the_row():
    hass = _hass(expired=_expired_row())

    async def failing(service_data, users=None, *, replaces_device_id=None):
        return {"error": "Encryption failed"}

    view, _ = _view(hass, failing)
    status, resp = _post(view, {"user_id": "u1", "action": "regenerate"})
    assert status == 400 and resp["error"] == "Encryption failed"
    assert "u1" in hass.casa["stored_data"]["expired_provisions"]
