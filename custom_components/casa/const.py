DOMAIN = "casa"
CONF_ADMIN_SYSTEM_ONLY = "admin_system_only"
CONF_CREATE_DEVICES = "create_devices"

RELAY_BASE_URL = "https://push.bonjour.casa"
RELAY_REGISTER_SITE_URL = f"{RELAY_BASE_URL}/register_site"
RELAY_UNREGISTER_URL = f"{RELAY_BASE_URL}/unregister"
RELAY_RECONCILE_URL = f"{RELAY_BASE_URL}/reconcile"

RELAY_URLS = [
    f"{RELAY_BASE_URL}/send",
]