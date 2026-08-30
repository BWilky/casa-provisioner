import json
import time

import pytest

from custom_components.casa.location import (
    ALLOWED_REPORT_KEYS,
    MAX_TOTAL_RINGS,
    RESERVED_LABELS,
    compute_config_version,
    decrypt_report_payload,
    encrypt_report_payload,
    validate_zone_config,
)


def _valid_config():
    return {
        "stale_after_minutes": 30,
        "anchors": [
            {
                "id": "a1",
                "name": "House",
                "latitude": 49.1,
                "longitude": -123.2,
                "rings": [
                    {"label": "home", "radius_m": 150},
                    {"label": "nearby", "radius_m": 1000},
                ],
            }
        ],
    }


def test_valid_config_passes():
    assert validate_zone_config(_valid_config()) == []


def test_empty_anchors_is_valid():
    assert validate_zone_config({"stale_after_minutes": 0, "anchors": []}) == []


def test_radii_must_strictly_ascend():
    cfg = _valid_config()
    cfg["anchors"][0]["rings"][1]["radius_m"] = 150
    assert any("ascending" in e for e in validate_zone_config(cfg))


def test_reserved_labels_rejected():
    for label in RESERVED_LABELS:
        cfg = _valid_config()
        cfg["anchors"][0]["rings"][0]["label"] = label
        assert any("reserved" in e for e in validate_zone_config(cfg))


def test_duplicate_ring_labels_within_anchor_rejected():
    cfg = _valid_config()
    cfg["anchors"][0]["rings"][1]["label"] = "home"
    assert any("unique" in e for e in validate_zone_config(cfg))


def test_duplicate_anchor_names_rejected():
    cfg = _valid_config()
    cfg["anchors"].append(dict(cfg["anchors"][0], id="a2"))
    assert any("unique" in e for e in validate_zone_config(cfg))


def test_ring_budget_enforced():
    cfg = _valid_config()
    cfg["anchors"][0]["rings"] = [
        {"label": f"r{i}", "radius_m": 100 * (i + 1)} for i in range(MAX_TOTAL_RINGS + 1)
    ]
    assert any("18" in e for e in validate_zone_config(cfg))


def test_lat_long_range_checked():
    cfg = _valid_config()
    cfg["anchors"][0]["latitude"] = 91
    assert any("latitude" in e for e in validate_zone_config(cfg))


def test_negative_stale_minutes_rejected():
    cfg = _valid_config()
    cfg["stale_after_minutes"] = -5
    assert any("stale_after_minutes" in e for e in validate_zone_config(cfg))


def test_anchor_name_length_capped():
    cfg = _valid_config()
    cfg["anchors"][0]["name"] = "x" * 49
    assert any("48" in e for e in validate_zone_config(cfg))


def test_ring_label_length_capped():
    cfg = _valid_config()
    cfg["anchors"][0]["rings"][0]["label"] = "x" * 49
    assert any("48" in e for e in validate_zone_config(cfg))


def test_config_version_stable_and_content_sensitive():
    a = _valid_config()["anchors"]
    v1 = compute_config_version(a)
    v2 = compute_config_version(json.loads(json.dumps(a)))
    assert v1 == v2 and len(v1) == 8
    a[0]["rings"][0]["radius_m"] = 151
    assert compute_config_version(a) != v1


def test_report_roundtrip():
    inner = {"state": "House: home", "reason": None, "config_version": "abcd1234", "ts": int(time.time())}
    blob = encrypt_report_payload(json.dumps(inner), "deadbeef" * 8, "DEV-1")
    assert decrypt_report_payload(blob, "deadbeef" * 8, "DEV-1") == inner


def test_report_wrong_device_rejected():
    blob = encrypt_report_payload("{}", "deadbeef" * 8, "DEV-1")
    with pytest.raises(ValueError):
        decrypt_report_payload(blob, "deadbeef" * 8, "DEV-2")


def test_report_not_replayable_from_push_domain():
    # A payload encrypted with the push info string must not decrypt as a report.
    import base64, secrets as pysecrets
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"DEV-1", info=b"casa-update-v1").derive(b"k" * 64)
    nonce = pysecrets.token_bytes(12)
    blob = base64.b64encode(nonce + AESGCM(key).encrypt(nonce, b"{}", None)).decode()
    with pytest.raises(ValueError):
        decrypt_report_payload(blob, "k" * 64, "DEV-1")


def test_report_garbage_rejected():
    with pytest.raises(ValueError):
        decrypt_report_payload("not base64!!!", "k" * 64, "DEV-1")
