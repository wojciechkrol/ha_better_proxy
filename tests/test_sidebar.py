"""Panel visibility must match HTTP access without changing other HA panels."""

from types import SimpleNamespace

import pytest
from homeassistant.components import frontend, panel_custom, websocket_api

from custom_components.better_proxy.sidebar import install_panel_filter


async def register_panels(env, path="better-proxy-test"):
    hass = env.hass
    websocket_api.async_register_command(hass, frontend.websocket_get_panels)
    for key, admin in [("map", False), ("config", True)]:
        frontend.async_register_built_in_panel(
            hass, key, sidebar_title=key, require_admin=admin
        )
    await panel_custom.async_register_panel(
        hass,
        path,
        "better-proxy-panel",
        module_url="/panel.js",
        sidebar_title="Test",
        config={"entry_id": "test"},
    )


def get_panels(env, user):
    messages = []
    connection = SimpleNamespace(user=user, hass=env.hass, send_message=messages.append)
    handler, _ = env.hass.data[websocket_api.DOMAIN]["get_panels"]
    handler(env.hass, connection, {"id": 1, "type": "get_panels"})
    assert len(messages) == 1
    assert messages[0]["success"] is True
    return messages[0]["result"]


@pytest.mark.parametrize(
    "mode,selected,visible",
    [
        ("users", ["user"], {"user"}),
        ("users", ["admin", "other"], {"admin", "other"}),
        ("users", [], set()),
        ("admin", [], {"admin"}),
        ("authenticated", [], {"admin", "user", "other"}),
    ],
)
@pytest.mark.parametrize("path", ["better-proxy-test", "my-service"])
async def test_visibility_matches_acl(environment, mode, selected, visible, path):
    env = environment
    await register_panels(env, path)
    settings = env.transport.entries["test"]
    settings.update(access=mode, users=[getattr(env, name).id for name in selected])
    before = dict(env.hass.data[frontend.DATA_PANELS])
    install_panel_filter(env.hass, env.transport.entries)
    for name in ("admin", "user", "other"):
        panels = get_panels(env, getattr(env, name))
        assert (path in panels) == (name in visible)
        assert "map" in panels
        assert ("config" in panels) == (name == "admin")
        assert panels["map"] == before["map"].to_response()
    assert env.hass.data[frontend.DATA_PANELS] == before


@pytest.mark.parametrize("path", ["better-proxy-test", "my-service"])
async def test_options_reload_and_entry_removal(environment, path):
    env = environment
    await register_panels(env, path)
    env.transport.entries["test"].update(access="users", users=[env.user.id])
    install_panel_filter(env.hass, env.transport.entries)
    assert path in get_panels(env, env.user)
    env.transport.entries["test"] = {"access": "users", "users": [env.other.id]}
    assert path not in get_panels(env, env.user)
    assert path in get_panels(env, env.other)
    env.transport.entries.pop("test")
    assert path not in get_panels(env, env.other)


async def test_inactive_users_are_hidden(environment):
    env = environment
    await register_panels(env)
    env.transport.entries["test"].update(access="authenticated")
    install_panel_filter(env.hass, env.transport.entries)
    env.user.is_active = False
    assert "better-proxy-test" not in get_panels(env, env.user)


async def test_preserves_registered_handler_and_cleanup(environment):
    env = environment
    await register_panels(env)
    handlers = env.hass.data[websocket_api.DOMAIN]
    original = handlers["get_panels"]
    uninstall = install_panel_filter(env.hass, env.transport.entries)
    assert handlers["get_panels"][1] is original[1]
    uninstall()
    assert handlers["get_panels"] == original
    uninstall = install_panel_filter(env.hass, env.transport.entries)
    # Do not overwrite another integration's later registration when stopping.
    replacement = (lambda *args: None, original[1])
    handlers["get_panels"] = replacement
    uninstall()
    assert handlers["get_panels"] == replacement
