"""Test-collection shim.

`tests/test_location.py` imports `custom_components.casa.location`, which
means Python must first import the `custom_components.casa` package (i.e.
run `custom_components/casa/__init__.py`) before it can reach the `location`
submodule. That `__init__.py` is the full Home Assistant integration and
imports real `homeassistant`/`aiohttp`/`qrcode` packages at module level —
none of which are needed to exercise `location.py`'s pure functions, and
none of which are installed in a plain-pytest environment.

If a real `homeassistant` install is present (e.g. under the HA test
harness), this shim does nothing and the genuine modules are used. Otherwise
it registers minimal no-op stand-ins in `sys.modules` just so that
`custom_components/casa/__init__.py` can be imported without error; nothing
here is exercised by the location.py tests, which never touch Home
Assistant objects.
"""
from __future__ import annotations

import sys
import types


def _ensure_module(dotted: str, **attrs) -> types.ModuleType:
    mod = sys.modules.get(dotted)
    if mod is None:
        mod = types.ModuleType(dotted)
        sys.modules[dotted] = mod
        if "." in dotted:
            parent_name, child_name = dotted.rsplit(".", 1)
            parent = _ensure_module(parent_name)
            setattr(parent, child_name, mod)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _install_homeassistant_stubs() -> None:
    try:
        import homeassistant  # noqa: F401
        return  # real Home Assistant is installed; use it, stub nothing
    except ImportError:
        pass

    class _Stub:
        def __init__(self, *args, **kwargs):
            pass

    class _SupportsResponse:
        NONE = "none"
        OPTIONAL = "optional"
        ONLY = "only"

    _ensure_module("homeassistant")
    _ensure_module(
        "homeassistant.core",
        HomeAssistant=_Stub,
        ServiceCall=_Stub,
        SupportsResponse=_SupportsResponse,
    )
    _ensure_module("homeassistant.config_entries", ConfigEntry=_Stub)
    _ensure_module("homeassistant.helpers")
    _ensure_module("homeassistant.helpers.typing", ConfigType=dict)
    _ensure_module("homeassistant.helpers.storage", Store=_Stub)
    _ensure_module(
        "homeassistant.helpers.event",
        async_track_time_interval=lambda *a, **kw: None,
    )
    _ensure_module(
        "homeassistant.helpers.aiohttp_client",
        async_get_clientsession=lambda *a, **kw: None,
    )
    _ensure_module("homeassistant.util")
    _ensure_module("homeassistant.util.dt")
    _ensure_module("homeassistant.exceptions", HomeAssistantError=Exception)
    _ensure_module("homeassistant.components")
    _ensure_module(
        "homeassistant.components.http",
        HomeAssistantView=_Stub,
        StaticPathConfig=_Stub,
    )
    _ensure_module("homeassistant.components.frontend")

    try:
        import aiohttp  # noqa: F401
    except ImportError:
        _ensure_module("aiohttp", ClientTimeout=_Stub)

    try:
        import qrcode  # noqa: F401
    except ImportError:
        _ensure_module("qrcode")


_install_homeassistant_stubs()
