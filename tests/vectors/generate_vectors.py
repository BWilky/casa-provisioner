"""Regenerate provisioning-v2.json. Run once; commit the output.

Uses a throwaway RSA-2048 pair generated here, NEVER the production
casa_public.pem, so the private half can be committed for the iOS tests.
"""
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import tests.conftest  # noqa: F401,E402  (installs homeassistant/aiohttp/qrcode stubs)
from custom_components.casa import _encrypt_payload_hybrid, build_links  # noqa: E402

OUT = Path(__file__).with_name("provisioning-v2.json")

PLAINTEXT = {
    "v": 2,
    "server_version": "26.09.30",
    "server_url": "http://192.0.2.10:8123",
    "username": "vector-user",
    "password": "vector-pass",
    "site_id": "0123456789abcdef0123456789abcdef",
    "pin": "1234",
    "default_dashboard": "/lovelace/0",
    "welcome_url": "",
    "immersive_level": "1",
    "theme_color_mode": "inherit",
    "custom_color": "#000000",
    "session_expiration": 0,
    "expiration": 0,
    "cache_control_hours": "48",
    "allowed_pages": "/*",
    "allowed_wifi": "",
    "require_alias": False,
    "push_notifications": "false",
    "wireguard": {"allowed": False, "config": "", "excluded_wifi": ""},
    "connect_wifi": {"ssid": "", "password": ""},
}


def main():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    plaintext = json.dumps(PLAINTEXT, separators=(",", ":"))
    envelope = _encrypt_payload_hybrid(plaintext, pub_pem.encode())
    deep, universal = build_links(envelope, 2)
    OUT.write_text(json.dumps({
        "note": "Throwaway test key pair. Regenerate with generate_vectors.py.",
        "plaintext": PLAINTEXT,
        "test_public_key_pem": pub_pem,
        "test_private_key_pem": priv_pem,
        "envelope_b64url": envelope,
        "deep_link": deep,
        "universal_link": universal,
    }, indent=2) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
