"""casa.update_wireguard goes through the durable update queue instead of the
legacy wireguard_update push, which the app drops in the background
(security Phase 4, G3)."""
import asyncio

import custom_components.casa as casa
from custom_components.casa import _queue_wireguard_change, _wireguard_key_fingerprint
from tests.fakes import FakeHass

CONF = "[Interface]\nPrivateKey = AAAA=\nAddress = 10.0.0.2/32\n\n[Peer]\nPublicKey = SRV=\n"


def _hass(monkeypatch, push_token="a" * 64):
    hass = FakeHass()
    sd = hass.casa["stored_data"]
    sd["device_key"] = "s" * 64
    dev = {"push_token": push_token} if push_token else {}
    sd["users"]["u1"] = {"username": "alice", "devices": {"D1": dev}}
    sent, nudged = [], []

    async def fake_relay(_hass, _session, payload):
        sent.append(payload)
        return True

    async def fake_nudge(_hass, _session, _sd, dinfo, command="request_heartbeat"):
        nudged.append(dinfo)

    monkeypatch.setattr(casa, "_send_push_to_relay", fake_relay)
    monkeypatch.setattr(casa, "_nudge_device_checkin", fake_nudge)
    monkeypatch.setattr(casa, "async_get_clientsession", lambda hass: None)
    hass.sent, hass.nudged = sent, nudged
    return hass


def _dev(hass):
    return hass.casa["stored_data"]["users"]["u1"]["devices"]["D1"]


def _run(hass, action, config=""):
    targets = [("D1", _dev(hass), "u1")]
    return asyncio.run(_queue_wireguard_change(hass, targets, action, config, "Home", "svc"))


def _queue(hass):
    return hass.casa["qu_data"]["updates"].get("D1", [])


def test_update_wireguard_enqueues_wireguard_update(monkeypatch):
    hass = _hass(monkeypatch)
    res = _run(hass, "update", CONF)
    (entry,) = _queue(hass)
    assert (entry["type"], entry["action"]) == ("wireguard", "update")
    assert entry["payload"] == {"config": CONF, "excluded_wifi": "Home"}
    assert _dev(hass)["wireguard_key_fp"] == _wireguard_key_fingerprint(CONF)
    assert res == {"success": True, "queued_count": 1, "sent_count": 1, "skipped_count": 0, "failed_count": 0}
    assert hass.sent[0]["data"]["command"] == "casa_update"


def test_update_wireguard_queues_without_push_token(monkeypatch):
    hass = _hass(monkeypatch, push_token=None)
    res = _run(hass, "update", CONF)
    assert len(_queue(hass)) == 1
    assert res["queued_count"] == 1 and res["skipped_count"] == 1 and res["sent_count"] == 0
    assert hass.sent == []


def test_failed_push_nudges_and_stays_queued(monkeypatch):
    hass = _hass(monkeypatch)

    async def down(*a):
        return False

    monkeypatch.setattr(casa, "_send_push_to_relay", down)
    res = _run(hass, "update", CONF)
    assert len(_queue(hass)) == 1 and res["sent_count"] == 0 and res["success"] is True
    assert hass.nudged == [_dev(hass)]


def test_revoke_enqueues_and_frees(monkeypatch):
    hass = _hass(monkeypatch)
    _run(hass, "update", CONF)
    _run(hass, "revoke")
    revoke = _queue(hass)[-1]
    assert (revoke["type"], revoke["action"], revoke["payload"]) == ("wireguard", "revoke", {})
    assert "wireguard_key_fp" not in _dev(hass)


def test_no_legacy_wireguard_push_sent(monkeypatch):
    hass = _hass(monkeypatch)
    _run(hass, "update", CONF)
    _run(hass, "revoke")
    assert all(p["data"]["command"] not in ("wireguard_update", "wireguard_revoke") for p in hass.sent)


def test_only_a_visible_notice_request_is_flagged_as_ignored():
    from custom_components.casa import _asks_for_wireguard_notice
    assert not _asks_for_wireguard_notice({})
    assert not _asks_for_wireguard_notice({"silent": True, "title": "", "message": ""})
    assert _asks_for_wireguard_notice({"silent": False})
    assert _asks_for_wireguard_notice({"title": "VPN updated"})
    assert _asks_for_wireguard_notice({"message": "x"})
