DOMAIN = "casa"

# Integration version, served in /api/casa/admin/summary and compared by the
# panel against PANEL_VERSION in panel/version.js — keep the two in sync.
# Any skew (including a stale Python process after an update) makes the panel
# show a "restart Home Assistant" banner.
CASA_VERSION = "26.07.21"

CONF_ADMIN_SYSTEM_ONLY = "admin_system_only"
CONF_CREATE_DEVICES = "create_devices"
CONF_SHOW_PANEL = "show_panel"

# A device is considered stale if not seen for this many days.
STALE_DAYS = 14

# Maximum length of a device alias (admin- or user-provided).
DEVICE_ALIAS_MAX_LEN = 60

# How often (seconds) devices should heartbeat by default; admin-adjustable
# per site via /api/casa/admin/settings, bounded by the min/max below.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 300
MIN_HEARTBEAT_INTERVAL_SECONDS = 60
MAX_HEARTBEAT_INTERVAL_SECONDS = 3600

# How often (seconds) devices should self-report their provisioning/profile
# state by default; admin-adjustable per site via /api/casa/admin/settings,
# bounded by the min/max below.
DEFAULT_PROFILE_REPORT_INTERVAL_SECONDS = 3600
MIN_PROFILE_REPORT_INTERVAL_SECONDS = 300
MAX_PROFILE_REPORT_INTERVAL_SECONDS = 86400

# --- Provisioning field scopes ------------------------------------------------
# Single source of truth for which provisioning fields are durable device/
# template settings vs one-time provisioning-process inputs. Mirrored on the
# panel side by FIELD_SCOPES in panel/views/profile-fields.js — keep in sync.
#
# LIVE: genuine ongoing device state — savable on provision templates,
# editable per-device via "Force Device Changes", and round-tripped by the
# device's own profile self-report.
LIVE_PROVISIONING_FIELDS = {
    "host_url": "",
    "default_dashboard": "",
    "welcome_url": "",
    "immersive_level": "1",
    "theme_color_mode": "inherit",
    "custom_color": "#000000",
    "allow_all_pages": False,
    "allowed_pages": "",
    "allowed_wifi": "",
    "require_alias": False,
    "push_notifications": "false",
    "allow_wireguard": False,
    "wireguard_config": "",
    "wireguard_excluded_wifi": "",
    "wireguard_profile_id": "",
    "cache_control_hours": "",
}

# PROFILE-ONLY: reusable template policy resolved into concrete device state
# at provision time (expiration_hours -> session_expiration); post-hoc changes
# go through expires_at_override, so it is not device-editable.
PROFILE_ONLY_PROVISIONING_FIELDS = {
    "expiration_hours": 336,
}

# PROCESS: one-time provisioning-ceremony inputs and server-side-only actions
# (deauthenticate_existing revokes sessions; password_scramble* schedules a
# rotation task; timeout_minutes sizes the QR/BLE window). Accepted only as
# casa.provision service_data — never persisted on templates or devices.
PROCESS_PROVISIONING_FIELDS = {
    "username": "",
    "password": "",
    "pin": "",
    "deauthenticate_existing": False,
    "timeout_minutes": 5,
    "password_scramble": True,
    "password_scramble_in": 0,
    "connect_wifi_ssid": "",
    "connect_wifi_password": "",
}

# What a saved provision template may contain. Templates are sparse: only
# explicitly-set fields are stored — an absent key means "unset", surfaced to
# the admin as a fill-in-the-blank at provision time.
PROFILE_PROVISIONING_FIELDS = {
    **LIVE_PROVISIONING_FIELDS,
    **PROFILE_ONLY_PROVISIONING_FIELDS,
}

RELAY_BASE_URL = "https://push.bonjour.casa"
RELAY_REGISTER_SITE_URL = f"{RELAY_BASE_URL}/register_site"
RELAY_VERIFY_SITE_URL = f"{RELAY_BASE_URL}/verify_site"
RELAY_UNREGISTER_URL = f"{RELAY_BASE_URL}/unregister"
RELAY_RECONCILE_URL = f"{RELAY_BASE_URL}/reconcile"
RELAY_REMOVE_SITE_URL = f"{RELAY_BASE_URL}/remove_site"

RELAY_URLS = [
    f"{RELAY_BASE_URL}/send",
]

# Universal (https) links open the app via its applinks:bonjour.casa entitlement.
# Requires bonjour.casa to serve /.well-known/apple-app-site-association covering /setup.
UNIVERSAL_LINK_BASE_URL = "https://bonjour.casa"
UNIVERSAL_LINK_SETUP_URL = f"{UNIVERSAL_LINK_BASE_URL}/setup"