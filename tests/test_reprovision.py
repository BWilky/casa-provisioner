from custom_components.casa import _reprovision_service_data
from tests.fakes import FakeHass

ORIGIN = "http://192.168.1.21:8123"


def _hass(templates=()):
    hass = FakeHass()
    hass.casa["pp_data"]["profiles"] = list(templates)
    return hass


def test_seed_uses_reported_fields_and_live_template():
    hass = _hass([{"id": "t1", "name": "Mobile Owners", "fields": {"host_url": "https://t"}}])
    info = {"alias": "Kitchen iPad", "provisioning_profile_id": "t1",
            "provisioning_fields": {"host_url": "https://h", "default_dashboard": "/d", "bogus": 1}}
    data = _reprovision_service_data(hass, info, ORIGIN)
    assert data["method"] == "qr"
    assert data["deauthenticate_existing"] is False
    assert data["host_url"] == "https://h"
    assert data["default_dashboard"] == "/d"
    assert "bogus" not in data
    assert data["profile"] == "t1"
    assert data["device_alias"] == "Kitchen iPad"


def test_seed_normalizes_legacy_reports():
    data = _reprovision_service_data(_hass(), {"provisioning_fields": {
        "host_url": "https://h", "immersive_level": "2,custom,#ff0000"}}, ORIGIN)
    assert data["immersive_level"] == "2"
    assert data["theme_color_mode"] == "custom"


def test_seed_without_reports_uses_template_host():
    hass = _hass([{"id": "t1", "name": "T", "fields": {"host_url": "https://t"}}])
    data = _reprovision_service_data(hass, {"provisioning_profile_id": "t1"}, ORIGIN)
    assert data["profile"] == "t1"
    assert "host_url" not in data  # the template supplies it via get_field


def test_seed_falls_back_to_defaults_and_origin():
    data = _reprovision_service_data(_hass(), {"provisioning_profile_id": "deleted"}, ORIGIN)
    assert "profile" not in data
    assert data["host_url"] == ORIGIN
    assert "device_alias" not in data


import asyncio

import custom_components.casa as casa
from custom_components.casa import CasaAdminReprovisionDeviceView
from tests.fakes import FakeRequest, bind_view, make_user

QR_RESULT = {"method": "qr", "provision_id": "p1", "qr_data_uri": "data:image/png;base64,AA",
             "deep_link": "hascasa://setup?d=x", "universal_link": "https://bonjour.casa/setup#x",
             "expires_at": 1900000000}


def _site(push=False):
    hass = FakeHass()
    hass.auth.add_user(make_user("u1", login="mobile-bryce", tokens=["r1", "r2"]), password="pw")
    d1 = {"refresh_token_id": "r1", "alias": "Kitchen iPad",
          "provisioning_fields": {"host_url": "https://h"}}
    if push:
        d1["push_token"] = "a" * 64
        hass.casa["stored_data"]["device_key"] = "k" * 44
    hass.casa["stored_data"]["users"]["u1"] = {"username": "mobile-bryce", "devices": {
        "D1": d1, "D2": {"refresh_token_id": "r2"},
    }}
    calls = []

    async def provision(service_data, users=None, *, replaces_device_id=None):
        calls.append((dict(service_data), replaces_device_id))
        return dict(QR_RESULT)

    return hass, provision, calls


def _post(hass, provision, body, admin=True):
    view = bind_view(CasaAdminReprovisionDeviceView(hass, provision))
    return asyncio.run(view.post(FakeRequest(make_user("admin", admin=admin), body)))


def test_qr_path_provisions_and_revokes_only_this_device():
    hass, provision, calls = _site()
    status, resp = _post(hass, provision, {"device_id": "D1", "method": "auto", "host_url": ORIGIN})
    assert status == 200 and resp["method"] == "qr"
    assert resp["qr_data_uri"] == QR_RESULT["qr_data_uri"]
    (service_data, replaces), = calls
    assert replaces == "D1"
    assert service_data["user_id"] == "u1" and service_data["username"] == "mobile-bryce"
    assert service_data["method"] == "qr" and service_data["host_url"] == "https://h"
    assert hass.auth.removed_tokens == ["r1"]
    assert "r2" in hass.auth.users["u1"].refresh_tokens


def test_qr_cancels_pending_reauth():
    hass, provision, _ = _site()
    hass.casa["stored_data"]["users"]["u1"]["devices"]["D1"]["reauth_pending"] = {"update_id": "e1"}
    hass.casa["qu_data"]["updates"]["D1"] = [{"id": "e1", "type": "auth", "action": "reauthenticate",
                                             "payload": {"username": "mobile-bryce", "password": "x"}}]
    status, _ = _post(hass, provision, {"device_id": "D1", "method": "qr", "host_url": ORIGIN})
    assert status == 200
    assert "reauth_pending" not in hass.casa["stored_data"]["users"]["u1"]["devices"]["D1"]
    assert hass.casa["qu_data"]["updates"].get("D1", []) == []


def test_qr_provision_error_revokes_nothing():
    hass, _, _ = _site()

    async def failing(service_data, users=None, *, replaces_device_id=None):
        return {"error": "User not found"}

    status, resp = _post(hass, failing, {"device_id": "D1", "method": "qr", "host_url": ORIGIN})
    assert status == 400 and resp["error"] == "User not found"
    assert hass.auth.removed_tokens == []


def test_push_path_queues_same_account_reauth(monkeypatch):
    hass, provision, calls = _site(push=True)
    pushed = []

    async def fake_push(*args, **kwargs):
        pushed.append(args)
        return True

    monkeypatch.setattr(casa, "_send_encrypted_update_push", fake_push)
    status, resp = _post(hass, provision, {"device_id": "D1", "method": "auto", "host_url": ORIGIN})
    assert status == 200 and resp["method"] == "push" and resp["pushed"] is True
    assert "password" not in resp
    assert calls == []  # no QR provision
    assert hass.auth.removed_tokens == []  # old session revoked only after the new login
    marker = hass.casa["stored_data"]["users"]["u1"]["devices"]["D1"]["reauth_pending"]
    assert marker["target_user_id"] == "u1" and marker["old_refresh_token_id"] == "r1"
    entry, = [e for e in hass.casa["qu_data"]["updates"]["D1"] if e["type"] == "auth"]
    assert entry["payload"]["password"] == hass.auth.provider.data.passwords["mobile-bryce"]
    assert entry["payload"]["password"] != "pw"  # rotated


def test_push_reuses_other_devices_queued_password(monkeypatch):
    hass, provision, _ = _site(push=True)

    async def fake_push(*args, **kwargs):
        return True

    monkeypatch.setattr(casa, "_send_encrypted_update_push", fake_push)
    # D2 already has a queued reauth carrying the server-set password.
    from custom_components.casa import _set_account_password
    current = asyncio.run(_set_account_password(hass, hass.auth.provider, "mobile-bryce"))
    hass.casa["qu_data"]["updates"]["D2"] = [{"id": "e2", "type": "auth", "action": "reauthenticate",
                                             "payload": {"username": "mobile-bryce", "password": current}}]
    hass.casa["stored_data"]["users"]["u1"]["devices"]["D2"]["reauth_pending"] = {"update_id": "e2"}
    status, _ = _post(hass, provision, {"device_id": "D1", "method": "auto", "host_url": ORIGIN})
    assert status == 200
    assert hass.auth.provider.data.passwords["mobile-bryce"] == current
    assert [e["id"] for e in hass.casa["qu_data"]["updates"]["D2"]] == ["e2"]


def test_forced_qr_skips_push_even_when_registered():
    hass, provision, calls = _site(push=True)
    status, resp = _post(hass, provision, {"device_id": "D1", "method": "qr", "host_url": ORIGIN})
    assert status == 200 and resp["method"] == "qr" and len(calls) == 1


def test_rejects_non_admin_unknown_native_and_bad_method():
    hass, provision, _ = _site()
    assert _post(hass, provision, {"device_id": "D1"}, admin=False)[0] == 403
    assert _post(hass, provision, {"device_id": "NOPE"})[0] == 404
    assert _post(hass, provision, {"device_id": "D1", "method": "ble"})[0] == 400
    hass.auth.add_user(make_user("n1", login="native", tokens=["rn"]))
    hass.casa["stored_data"]["native_devices"] = {"n1": {"N1": {"refresh_token_id": "rn"}}}
    status, resp = _post(hass, provision, {"device_id": "N1"})
    assert status == 400 and "Casa-managed" in resp["error"]


from custom_components.casa import CasaAdminSummaryView


def test_summary_accounts_carry_user_id():
    hass = FakeHass()
    hass.casa["stored_data"]["users"]["u1"] = {"username": "mobile-bryce", "name": "Mobile Bryce", "devices": {}}
    view = bind_view(CasaAdminSummaryView(hass))
    status, resp = asyncio.run(view.get(FakeRequest(make_user("admin", admin=True))))
    assert status == 200
    assert resp["accounts"][0]["user_id"] == "u1"
