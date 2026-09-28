"""Panel addresses and ownership, independent of proxy resource paths."""

import re

from homeassistant.components import frontend

from .const import DOMAIN

# Reserve HA routes and built-in panels even when their integrations are unloaded.
RESERVED_PATHS = {
    "api",
    "auth",
    "local",
    "static",
    "frontend_latest",
    "frontend_es5",
    "hacsfiles",
    "media",
    "onboarding",
    "config",
    "profile",
    "lovelace",
    "developer-tools",
    "energy",
    "history",
    "logbook",
    "map",
    "calendar",
    "todo",
    "shopping-list",
    "states",
    "home",
    "media-browser",
}


def normalize_panel_path(value: str) -> str:
    """Remove the display prefix to persist the HA panel key."""
    return value.strip().removeprefix("/")


def panel_url_path(entry_id: str, settings: dict) -> str:
    """Keep existing entry links when no custom path is configured."""
    return settings.get("panel_path") or f"better-proxy-{entry_id}"


def panel_entry_id(panel: dict) -> str | None:
    """Identify our panels by their component, regardless of their URL."""
    config = panel.get("config") or {}
    if (config.get("_panel_custom") or {}).get("name") == "better-proxy-panel":
        return config.get("entry_id")
    return None


def panel_path_error(
    hass,
    value: str,
    entry_id: str | None = None,
    *,
    require_leading_slash: bool = False,
) -> str | None:
    """Reject unsafe paths and collisions, including disabled config entries."""
    address = value.strip()
    if (
        require_leading_slash
        and address
        and (not address.startswith("/") or address == "/")
    ):
        return "invalid_panel_path"
    path = normalize_panel_path(address)
    if path and (
        not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", path)
        or path in RESERVED_PATHS
        or path.startswith("better-proxy-")
    ):
        return "invalid_panel_path"
    path = path or (panel_url_path(entry_id, {}) if entry_id else "")
    if not path:
        return None
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (
            entry.entry_id != entry_id
            and panel_url_path(entry.entry_id, entry.data | entry.options) == path
        ):
            return "panel_path_in_use"
    if panel := hass.data.get(frontend.DATA_PANELS, {}).get(path):
        if entry_id is None or panel_entry_id(panel.to_response()) != entry_id:
            return "panel_path_in_use"
    return None
