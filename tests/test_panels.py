"""Custom addresses preserve panel ownership, sessions and entry lifecycle."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components import frontend
from homeassistant.exceptions import HomeAssistantError

from custom_components.better_proxy import async_setup_entry, async_unload_entry
from custom_components.better_proxy.config_flow import (
    SECTIONS,
    BetterProxyConfigFlow,
    BetterProxyOptionsFlow,
)
from custom_components.better_proxy.const import DEFAULTS, DOMAIN
from custom_components.better_proxy.panels import panel_path_error


@pytest.fixture(autouse=True)
async def config_entries_manager(environment):
    hass = environment.hass
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()


def make_entry(path="", options=None):
    return config_entries.ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Service",
        data=DEFAULTS
        | {"name": "Service", "url": "http://internal", "panel_path": path},
        options=options or {},
        source="user",
        unique_id=None,
        discovery_keys={},
        subentries_data=[],
    )


@pytest.mark.parametrize(
    "value", ["/esphome", " /esphome ", "/camera_2", "/my-service"]
)
async def test_new_entry_accepts_custom_path(environment, value):
    flow = BetterProxyConfigFlow()
    flow.hass = environment.hass
    values = DEFAULTS | {
        "name": "Service",
        "url": "http://internal",
        "panel_path": value,
    }
    result = await flow.async_step_user(values)
    assert result["type"] == "create_entry"
    assert result["data"]["panel_path"] == value.strip().removeprefix("/")


@pytest.mark.parametrize(
    "value",
    [
        "esphome",
        "camera_2",
        "my-service",
        "/",
        "api",
        "/auth",
        "config",
        "home",
        "media-browser",
        "static",
        "local",
        "better-proxy-other",
        "https://service",
        "//service",
        "a/b",
        "../service",
        "Service",
        "service?x=1",
        "service#x",
        "%2fservice",
        "service\npath",
        "serwis łazienka",
    ],
)
async def test_invalid_path_is_reported_in_service_section(environment, value):
    flow = BetterProxyConfigFlow()
    flow.hass = environment.hass
    values = DEFAULTS | {
        "name": "Service",
        "url": "http://internal",
        "panel_path": value,
    }
    result = await flow.async_step_user(values)
    assert result["type"] == "form"
    assert result["errors"] == {"service": "invalid_panel_path"}


async def test_collision_with_other_panel_and_unloaded_entry(environment):
    hass = environment.hass
    frontend.async_register_built_in_panel(hass, "custom", frontend_url_path="existing")
    assert panel_path_error(hass, "existing") == "panel_path_in_use"
    entry = make_entry(options={"panel_path": "reserved-service"})
    hass.config_entries._entries[entry.entry_id] = entry
    assert panel_path_error(hass, "reserved-service") == "panel_path_in_use"
    flow = BetterProxyConfigFlow()
    flow.hass = hass
    result = await flow.async_step_user(
        entry.data | entry.options | {"panel_path": "/reserved-service"}
    )
    assert result["errors"] == {"service": "panel_path_in_use"}


async def test_change_and_clear_path_remove_old_panel_and_revoke_session(environment):
    env = environment
    entry = make_entry("my-service")
    env.hass.data[DOMAIN] = env.transport
    env.hass.config_entries._entries[entry.entry_id] = entry
    assert await async_setup_entry(env.hass, entry)
    panels = env.hass.data[frontend.DATA_PANELS]
    assert panels["my-service"].config["entry_id"] == entry.entry_id
    flow = BetterProxyOptionsFlow()
    flow.hass = env.hass
    flow.handler = entry.entry_id
    # Retaining the entry's own registered path is not a collision.
    form = await flow.async_step_init()
    assert form["data_schema"]({})["service"]["panel_path"] == "/my-service"
    unchanged = await flow.async_step_init(entry.data | {"panel_path": "/my-service"})
    assert unchanged["type"] == "create_entry"
    for requested, expected in [
        ("/renamed", "renamed"),
        ("", f"better-proxy-{entry.entry_id}"),
    ]:
        old_path = next(
            path
            for path, panel in panels.items()
            if panel.config["entry_id"] == entry.entry_id
        )
        token, _ = env.store.issue(entry.entry_id, env.refresh.id, env.admin.id)
        result = await flow.async_step_init(entry.data | {"panel_path": requested})
        assert result["type"] == "create_entry"
        # HA updates options before calling the reload listener.
        with patch.object(
            env.hass.config_entries, "async_reload", AsyncMock()
        ) as reload:
            env.hass.config_entries.async_update_entry(entry, options=result["data"])
            await env.hass.async_block_till_done()
            reload.assert_called_with(entry.entry_id)
        assert await async_unload_entry(env.hass, entry)
        assert old_path not in panels
        assert token not in env.store.sessions
        assert await async_setup_entry(env.hass, entry)
        assert expected in panels


async def test_startup_collision_does_not_expose_entry_or_overwrite_panel(environment):
    env = environment
    entry = make_entry("existing")
    env.hass.data[DOMAIN] = env.transport
    frontend.async_register_built_in_panel(
        env.hass, "custom", frontend_url_path="existing"
    )
    original = env.hass.data[frontend.DATA_PANELS]["existing"]
    with pytest.raises(HomeAssistantError, match="panel_path_in_use"):
        await async_setup_entry(env.hass, entry)
    assert entry.entry_id not in env.transport.entries
    assert env.hass.data[frontend.DATA_PANELS]["existing"] is original


async def test_custom_path_keeps_server_access_checks(environment):
    env = environment
    env.transport.entries["test"].update(
        panel_path="my-service",
        access="users",
        users=[env.user.id],
    )
    assert (await env.client.get(env.prefix + "page")).status == 401
    assert (await env.login()).status == 403  # Admin is not selected.
    assert (await env.login(env.member_refresh)).status == 200
    assert (await env.client.get(env.prefix + "page")).status == 200
    env.transport.entries["test"]["users"] = []
    assert (await env.client.get(env.prefix + "page")).status == 401


async def test_cleared_optional_path_is_not_restored_from_old_schema(environment):
    hass = environment.hass
    entry = make_entry("my-service")
    hass.config_entries._entries[entry.entry_id] = entry
    flow = BetterProxyOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    form = await flow.async_step_init()
    # HA omits an optional text selector when its value is cleared.
    submitted = {
        section: {field: entry.data[field] for field in fields if field != "panel_path"}
        for section, fields in SECTIONS.items()
    }
    submitted = form["data_schema"](submitted)
    result = await flow.async_step_init(submitted)
    assert result["type"] == "create_entry"
    assert result["data"]["panel_path"] == ""
