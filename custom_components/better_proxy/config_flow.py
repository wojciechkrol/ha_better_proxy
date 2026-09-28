"""Native Home Assistant configuration and options forms."""

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from yarl import URL

from .const import DEFAULTS, DOMAIN
from .panels import normalize_panel_path, panel_path_error

SECTIONS = {
    "service": ("name", "url", "panel_path", "icon", "show_header"),
    "access": ("access", "users"),
    "authentication": ("auth_type", "username", "password", "bearer_token"),
    "advanced": ("verify_ssl", "rewrite"),
}


def flatten_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Keep persisted settings flat, including entries created before sections."""
    result = {
        key: value
        for key, value in data.items()
        if key not in SECTIONS or not isinstance(value, dict)
    }
    for name, fields in SECTIONS.items():
        values = data.get(name, {})
        if isinstance(values, dict):
            result.update({key: values[key] for key in fields if key in values})
    return result


def section_errors(schema: vol.Schema, errors: dict[str, str]) -> dict[str, str]:
    """Show errors next to their section and reveal fields needing correction."""
    result = {}
    for key, value in schema.schema.items():
        for field in SECTIONS[key.schema]:
            if field in errors:
                result.setdefault(key.schema, errors[field])
                value.options["collapsed"] = False
    return result


def validate_settings(data: dict[str, Any], valid_users: set[str]) -> dict[str, str]:
    """Validate without contacting a potentially unavailable internal service."""
    errors = {}
    try:
        url = URL(data["url"])
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.user is not None
            or url.query_string
            or url.fragment
            or url.port is None
        ):
            raise ValueError
    except (ValueError, KeyError):
        errors["url"] = "invalid_url"
    if not data.get("name", "").strip():
        errors["name"] = "name_required"
    if data.get("access") == "users" and (
        not data.get("users") or not set(data["users"]) <= valid_users
    ):
        errors["users"] = "select_users"
    auth_type = data.get("auth_type", "basic")
    if auth_type not in {"none", "basic", "digest", "bearer"}:
        errors["auth_type"] = "invalid_auth_type"
    if (
        auth_type in {"basic", "digest"}
        and (data.get("password") or auth_type == "digest")
        and not data.get("username")
    ):
        errors["username"] = "username_required"
    if auth_type == "basic" and ":" in data.get("username", ""):
        errors["username"] = "invalid_username"
    if auth_type == "bearer" and not data.get("bearer_token", "").strip():
        errors["bearer_token"] = "token_required"
    for field in ("username", "password", "bearer_token"):
        if any(c in data.get(field, "") for c in ("\r", "\n")):
            errors[field] = "invalid_credentials"
    return errors


async def settings_schema(hass, values):
    """Use HA's icon picker, user labels and secret input."""
    users = [
        u
        for u in await hass.auth.async_get_users()
        if u.is_active and not u.system_generated
    ]
    values = DEFAULTS | values
    fields = {
        vol.Required("name", default=values["name"]): selector.TextSelector(),
        vol.Required("url", default=values["url"]): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
        ),
        # The section supplies the current value; an omitted field clears it.
        vol.Optional("panel_path", default=""): selector.TextSelector(),
        vol.Required("icon", default=values["icon"]): selector.IconSelector(),
        vol.Required(
            "show_header", default=values["show_header"]
        ): selector.BooleanSelector(),
        vol.Required("access", default=values["access"]): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["admin", "authenticated", "users"],
                translation_key="access",
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional("users", default=values["users"]): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[{"value": u.id, "label": u.name or u.id} for u in users],
                multiple=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required("auth_type", default=values["auth_type"]): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["none", "basic", "digest", "bearer"],
                translation_key="auth_type",
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional("username", default=values["username"]): selector.TextSelector(),
        vol.Optional("password", default=values["password"]): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
        vol.Optional(
            "bearer_token", default=values["bearer_token"]
        ): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
        vol.Required(
            "verify_ssl", default=values["verify_ssl"]
        ): selector.BooleanSelector(),
        vol.Required("rewrite", default=values["rewrite"]): selector.BooleanSelector(),
    }
    schema = vol.Schema(
        {
            vol.Required(
                name, default={field: values[field] for field in names}
            ): section(
                vol.Schema(
                    {key: value for key, value in fields.items() if key.schema in names}
                ),
                {"collapsed": name in {"authentication", "advanced"}},
            )
            for name, names in SECTIONS.items()
        }
    )
    return schema, {u.id for u in users}


class BetterProxyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """One independently authorized resource per config entry."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            user_input = flatten_settings(user_input)
        schema, users = await settings_schema(self.hass, user_input or {})
        errors = {}
        if user_input is not None:
            errors = validate_settings(user_input, users)
            if error := panel_path_error(
                self.hass, user_input.get("panel_path", ""), require_leading_slash=True
            ):
                errors["panel_path"] = error
            if not errors:
                data = DEFAULTS | user_input
                data["name"] = data["name"].strip()
                data["panel_path"] = normalize_panel_path(data["panel_path"])
                return self.async_create_entry(title=data["name"], data=data)
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=section_errors(schema, errors)
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BetterProxyOptionsFlow()


class BetterProxyOptionsFlow(config_entries.OptionsFlow):
    """Editing an entry invalidates all of its proxy sessions."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            user_input = flatten_settings(user_input)
        values = self.config_entry.data | self.config_entry.options
        if values.get("panel_path"):
            values["panel_path"] = f"/{normalize_panel_path(values['panel_path'])}"
        schema, users = await settings_schema(self.hass, values | (user_input or {}))
        errors = {}
        if user_input is not None:
            errors = validate_settings(user_input, users)
            if error := panel_path_error(
                self.hass,
                user_input.get("panel_path", ""),
                self.config_entry.entry_id,
                require_leading_slash=True,
            ):
                errors["panel_path"] = error
            if not errors:
                data = DEFAULTS | user_input
                data["name"] = data["name"].strip()
                data["panel_path"] = normalize_panel_path(data["panel_path"])
                return self.async_create_entry(title="", data=data)
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=section_errors(schema, errors)
        )
