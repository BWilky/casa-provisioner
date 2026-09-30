"""The three version strings must move together (see memory: casa version handshake)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "casa"


def _const_version():
    text = (ROOT / "const.py").read_text()
    return re.search(r'^CASA_VERSION\s*=\s*"([^"]+)"', text, re.M).group(1)


def _panel_version():
    text = (ROOT / "panel" / "version.js").read_text()
    return re.search(r'PANEL_VERSION\s*=\s*"([^"]+)"', text).group(1)


def _manifest_version():
    return json.loads((ROOT / "manifest.json").read_text())["version"]


def test_versions_agree():
    assert _const_version() == _panel_version() == _manifest_version()


def test_version_is_dated_release_format():
    assert re.fullmatch(r"\d{2}\.\d{2}\.\d{2}", _const_version())
