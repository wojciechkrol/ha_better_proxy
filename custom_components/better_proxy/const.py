"""Constants for Better Proxy."""

DOMAIN = "better_proxy"
ROOT = "/api/better_proxy"
FRONTEND_URL = f"{ROOT}/panel.js"
SESSION_IDLE_SECONDS = 1800
SESSION_MAX_SECONDS = 43200
MAX_SESSIONS = 2048
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_WS_BYTES = 16 * 1024 * 1024
MAX_AUTH_BODY_BYTES = 64 * 1024 * 1024
DEFAULTS = {
    "name": "",
    "url": "",
    "panel_path": "",
    "icon": "mdi:web",
    "show_header": True,
    "access": "admin",
    "users": [],
    "auth_type": "basic",
    "username": "",
    "password": "",
    "bearer_token": "",
    "verify_ssl": True,
    "rewrite": True,
}


def proxy_path(entry_id: str) -> str:
    """Stable path; knowing it never grants access."""
    return f"{ROOT}/proxy/{entry_id}/"


def session_cookie(entry_id: str) -> str:
    return f"bp_session_{entry_id}"
