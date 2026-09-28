"""Sectioned forms preserve stored settings, access rules and error feedback."""

import json
from pathlib import Path
from types import SimpleNamespace

from homeassistant import config_entries

from custom_components.better_proxy.config_flow import (
    SECTIONS,
    BetterProxyConfigFlow,
    BetterProxyOptionsFlow,
)
from custom_components.better_proxy.const import DEFAULTS, DOMAIN


def grouped(values):
    """Represent the nested input submitted by HA's native sectioned form."""
    return {
        section: {field: values[field] for field in fields}
        for section, fields in SECTIONS.items()
    }


async def test_sectioned_entry_keeps_selected_users_and_secrets(environment):
    flow = BetterProxyConfigFlow()
    flow.hass = environment.hass
    settings = DEFAULTS | {
        "name": "  Test service  ",
        "url": "https://internal/base",
        "access": "users",
        "users": [environment.user.id],
        "auth_type": "bearer",
        "bearer_token": "synthetic-service-token",
        "show_header": False,
        "verify_ssl": False,
        "rewrite": False,
    }
    form = await flow.async_step_user()
    assert list(form["data_schema"].schema) == list(SECTIONS)
    assert form["data_schema"]({}) == grouped(DEFAULTS)
    result = await flow.async_step_user(form["data_schema"](grouped(settings)))
    assert result["type"] == "create_entry"
    assert result["data"] == settings | {"name": "Test service"}
    assert not any(isinstance(v, dict) for v in result["data"].values())


async def test_section_errors_preserve_input_and_expand_authentication(environment):
    flow = BetterProxyConfigFlow()
    flow.hass = environment.hass
    settings = DEFAULTS | {
        "name": "Retained name",
        "url": "http://internal",
        "access": "users",
        "auth_type": "bearer",
    }
    form = await flow.async_step_user(grouped(settings))
    assert form["type"] == "form"
    assert form["errors"] == {
        "access": "select_users",
        "authentication": "token_required",
    }
    schema = form["data_schema"].schema
    assert not schema["authentication"].options["collapsed"]
    assert schema["advanced"].options["collapsed"]
    assert (
        form["data_schema"](
            {"service": {}, "access": {}, "authentication": {}, "advanced": {}}
        )["service"]["name"]
        == "Retained name"
    )


async def test_options_keep_legacy_flat_values(environment):
    settings = DEFAULTS | {
        "name": "Legacy",
        "url": "http://internal",
        "access": "users",
        "users": [environment.user.id],
        "auth_type": "digest",
        "username": "synthetic-user",
        "password": "synthetic-password",
    }
    entry = config_entries.ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Legacy",
        data=settings,
        options={"show_header": False},
        source="user",
        unique_id=None,
        discovery_keys={},
        subentries_data=[],
    )
    environment.hass.config_entries = SimpleNamespace(
        async_get_known_entry=lambda entry_id: entry,
        async_entries=lambda domain: [entry],
    )
    flow = BetterProxyOptionsFlow()
    flow.hass = environment.hass
    flow.handler = entry.entry_id
    form = await flow.async_step_init()
    submitted = form["data_schema"](grouped(settings | entry.options))
    result = await flow.async_step_init(submitted)
    assert result["type"] == "create_entry"
    assert result["data"] == settings | {"show_header": False}


def leaf_paths(data, prefix=()):
    """Translation files must have identical keys and no blank values."""
    if isinstance(data, dict):
        return set().union(
            *(leaf_paths(value, (*prefix, key)) for key, value in data.items())
        )
    assert isinstance(data, str) and data.strip()
    return {prefix}


def test_translation_coverage_and_sections():
    root = Path("custom_components/better_proxy")
    source = json.loads((root / "strings.json").read_text())
    expected = leaf_paths(source)
    translations = list((root / "translations").glob("*.json"))
    assert len(translations) == 36
    for path in translations:
        localized = json.loads(path.read_text())
        assert leaf_paths(localized) == expected, path.name
        assert (
            localized["config"]["step"]["user"] == localized["options"]["step"]["init"]
        )
        assert localized["config"]["error"] == localized["options"]["error"]
        sections = localized["config"]["step"]["user"]["sections"]
        for name, fields in SECTIONS.items():
            assert set(sections[name]["data"]) == set(fields)
    assert json.loads((root / "translations/en.json").read_text()) == source
