"""No secret in log calls; device IPs come from the socket peer (HA's own
forwarded-header handling has already applied trusted_proxies there)."""
import re
from pathlib import Path

from custom_components.casa import _client_ip
from tests.fakes import FakeRequest, make_user

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "casa"


def test_no_log_call_formats_a_site_key_or_full_push_token():
    offenders = []
    for path in sorted(PKG.rglob("*.py")):
        text = path.read_text()
        for m in re.finditer(r"_LOGGER\.\w+\(", text):
            # take the call up to its balancing parenthesis
            depth, i = 1, m.end()
            while depth and i < len(text):
                depth += {"(": 1, ")": -1}.get(text[i], 0)
                i += 1
            call = text[m.start():i]
            if re.search(r"site_key\b(?!\s*persisted)|SiteKey|get\(\s*[\"']site_key", call) and "persisted" not in call:
                offenders.append(f"{path.name}: {call[:80]}")
            if re.search(r"Target=%s", call):
                offenders.append(f"{path.name}: {call[:80]}")
    assert offenders == []


def test_client_ip_is_the_socket_peer_not_a_header():
    req = FakeRequest(make_user("u"), headers={"X-Forwarded-For": "6.6.6.6", "X-Real-IP": "7.7.7.7"})
    assert _client_ip(req) == "192.0.2.9"
