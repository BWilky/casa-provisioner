from custom_components.casa import build_links


def test_v2_links_are_raw_base64url():
    payload = "Aj-gk_Zj"  # base64url chars incl. '-' and '_'
    deep, universal = build_links(payload, 2)
    assert deep == "hascasa://setup?data=Aj-gk_Zj"
    assert universal == "https://bonjour.casa/setup?d=Aj-gk_Zj"


def test_v1_links_keep_legacy_quoting():
    payload = "ab+/cd=="  # standard base64 alphabet
    deep, universal = build_links(payload, 1)
    assert deep == "hascasa://setup?data=ab%2B/cd%3D%3D"
    assert universal == "https://bonjour.casa/setup?d=ab%2B%2Fcd%3D%3D"


def test_unknown_version_rejected():
    import pytest
    with pytest.raises(ValueError):
        build_links("x", 3)
