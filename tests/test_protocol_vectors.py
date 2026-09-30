import base64
import json
import zlib
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from custom_components.casa import build_links

VEC = json.loads((Path(__file__).parent / "vectors" / "provisioning-v2.json").read_text())


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_envelope_shape():
    env = VEC["envelope_b64url"]
    assert "=" not in env and "+" not in env and "/" not in env
    raw = _b64url_decode(env)
    assert raw[0] == 0x02
    assert len(raw) > 1 + 256 + 12 + 16


def test_envelope_round_trips_with_test_key():
    raw = _b64url_decode(VEC["envelope_b64url"])
    priv = serialization.load_pem_private_key(VEC["test_private_key_pem"].encode(), password=None)
    aes_key = priv.decrypt(
        raw[1:257],
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    nonce, ct = raw[257:269], raw[269:]
    compressed = AESGCM(aes_key).decrypt(nonce, ct, None)
    plaintext = zlib.decompress(compressed, -15).decode()
    assert json.loads(plaintext) == VEC["plaintext"]


def test_links_match_vectors():
    deep, universal = build_links(VEC["envelope_b64url"], 2)
    assert deep == VEC["deep_link"]
    assert universal == VEC["universal_link"]


SERVER_ORDER = [
    "v", "server_version", "server_url", "username", "password", "site_id", "pin",
    "default_dashboard", "welcome_url", "immersive_level", "theme_color_mode", "custom_color",
    "session_expiration", "expiration", "cache_control_hours", "allowed_pages", "allowed_wifi",
    "require_alias", "push_notifications", "wireguard", "connect_wifi",
]


def test_plaintext_key_order_matches_server():
    assert list(VEC["plaintext"].keys()) == SERVER_ORDER
