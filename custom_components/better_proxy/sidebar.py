"""Apply resource permissions to HA's per-connection panel listing."""

from homeassistant.components import websocket_api
from homeassistant.core import callback

from .panels import panel_entry_id
from .security import has_access


class PanelConnection:
    """Filter one response without changing the shared panel registry."""

    def __init__(self, connection, message_id, entries):
        self._connection = connection
        self._message_id = message_id
        self._entries = entries

    def __getattr__(self, name):
        return getattr(self._connection, name)

    @callback
    def send_message(self, message):
        if (
            isinstance(message, dict)
            and message.get("id") == self._message_id
            and message.get("type") == "result"
            and message.get("success") is True
            and isinstance(message.get("result"), dict)
        ):
            panels = {}
            for key, panel in message["result"].items():
                config = panel.get("config") or {}
                if (config.get("_panel_custom") or {}).get(
                    "name"
                ) == "better-proxy-panel":
                    settings = self._entries.get(panel_entry_id(panel))
                    if settings is None or not has_access(self.user, settings):
                        continue
                panels[key] = panel
            message = {**message, "result": panels}
        self._connection.send_message(message)


@callback
def install_panel_filter(hass, entries):
    """Wrap the registered HA command, retaining its schema and native filters.

    HA has no public per-user panel visibility callback. Keep this compatibility
    adapter scoped to get_panels and restore it only if we still own the handler.
    """
    handlers = hass.data[websocket_api.DOMAIN]
    original, schema = handlers["get_panels"]

    @callback
    def get_panels(hass, connection, message):
        return original(
            hass, PanelConnection(connection, message["id"], entries), message
        )

    websocket_api.async_register_command(hass, "get_panels", get_panels, schema)

    @callback
    def uninstall():
        if handlers.get("get_panels", (None,))[0] is get_panels:
            websocket_api.async_register_command(hass, "get_panels", original, schema)

    return uninstall
