import asyncio
import json
import time

from custom_components.casa import CasaNotificationActionView
from custom_components.casa.actions import create_pending
from custom_components.casa.location import ACTION_INFO, encrypt_report_payload
from tests.fakes import FakeHass, FakeRequest, bind_view

KEY = "deadbeef" * 8
ACTS = [{"action": "UNLOCK", "title": "Unlock", "service": "lock.unlock", "target": {"entity_id": "lock.a"}, "data": {"code": "1"}},
        {"action": "REPLY", "title": "Reply", "text_input": True},
        {"action": "IGNORE", "title": "Ignore"}]


def _hass():
    hass = FakeHass()
    hass.casa["stored_data"]["device_key"] = KEY
    hass.casa["stored_data"]["users"]["u1"] = {"username": "alice", "devices": {"D1": {}, "D2": {}}}
    nid = create_pending(hass.casa["na_data"], device_id="D1", user_id="u1", username="alice",
                         actions=ACTS, tag="door", action_data={"c": 1})
    return hass, nid


def _press(hass, device_id, inner, info=ACTION_INFO):
    inner = {"ts": int(time.time()), **inner}
    blob = encrypt_report_payload(json.dumps(inner), KEY, device_id, info=info)
    view = bind_view(CasaNotificationActionView(hass))
    return asyncio.run(view.post(FakeRequest(None, {"device_id": device_id, "payload": blob})))


def test_valid_press_fires_event_then_calls_service():
    hass, nid = _hass()
    status, body = _press(hass, "D1", {"notification_id": nid, "action": "UNLOCK"})
    assert status == 200 and body == {"status": "ok"}
    assert hass.fired == [("casa_notification_action", {
        "action": "UNLOCK", "notification_id": nid, "tag": "door", "action_data": {"c": 1},
        "device_id": "D1", "username": "alice", "user_id": "u1"})]
    assert hass.service_calls == [("lock", "unlock", {"code": "1"}, {"entity_id": "lock.a"})]
    assert hass.casa["na_store"].delayed == 1


def test_press_without_service_fires_event_only_and_reply_text_echoed_for_text_input():
    hass, nid = _hass()
    status, _ = _press(hass, "D1", {"notification_id": nid, "action": "REPLY", "reply_text": "on my way"})
    assert status == 200
    assert hass.fired[0][1]["reply_text"] == "on my way"
    assert hass.service_calls == []


def test_reply_text_ignored_for_non_text_action():
    hass, nid = _hass()
    _press(hass, "D1", {"notification_id": nid, "action": "IGNORE", "reply_text": "junk"})
    assert "reply_text" not in hass.fired[0][1]


def test_duplicate_press_is_200_without_second_event():
    hass, nid = _hass()
    _press(hass, "D1", {"notification_id": nid, "action": "IGNORE"})
    status, body = _press(hass, "D1", {"notification_id": nid, "action": "IGNORE"})
    assert status == 200 and body == {"status": "ok", "duplicate": True}
    assert len(hass.fired) == 1


def test_service_error_still_200_and_event_fired():
    hass, nid = _hass()
    hass.service_raises = RuntimeError("boom")
    status, _ = _press(hass, "D1", {"notification_id": nid, "action": "UNLOCK"})
    assert status == 200 and len(hass.fired) == 1


def _rejected(hass, device_id, inner, **kw):
    status, body = _press(hass, device_id, inner, **kw)
    assert status == 403 and body == {"error": "Rejected"}
    assert hass.fired == [] and hass.service_calls == []


def test_wrong_device_rejected():
    hass, nid = _hass()
    _rejected(hass, "D2", {"notification_id": nid, "action": "UNLOCK"})


def test_unknown_notification_rejected():
    hass, _ = _hass()
    _rejected(hass, "D1", {"notification_id": "nope", "action": "UNLOCK"})


def test_unknown_action_rejected():
    hass, nid = _hass()
    _rejected(hass, "D1", {"notification_id": nid, "action": "EXPLODE"})


def test_expired_rejected():
    hass, nid = _hass()
    hass.casa["na_data"]["pending"][nid]["created_at"] -= 24 * 3600 + 1
    _rejected(hass, "D1", {"notification_id": nid, "action": "UNLOCK"})


def test_stale_ts_rejected():
    hass, nid = _hass()
    _rejected(hass, "D1", {"notification_id": nid, "action": "UNLOCK", "ts": int(time.time()) - 301})


def test_report_channel_key_cannot_press():
    hass, nid = _hass()
    _rejected(hass, "D1", {"notification_id": nid, "action": "UNLOCK"}, info=b"casa-report-v1")


def test_oversize_reply_rejected():
    hass, nid = _hass()
    _rejected(hass, "D1", {"notification_id": nid, "action": "REPLY", "reply_text": "x" * 1001})


def test_unknown_device_rejected():
    hass, nid = _hass()
    _rejected(hass, "ZZ", {"notification_id": nid, "action": "UNLOCK"})


def test_bad_body_is_400():
    hass, _ = _hass()
    view = bind_view(CasaNotificationActionView(hass))
    status, _ = asyncio.run(view.post(FakeRequest(None, {"device_id": "D1"})))
    assert status == 400
