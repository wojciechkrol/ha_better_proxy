"""Authenticated reverse proxy panels for Home Assistant."""

from hashlib import sha256
from pathlib import Path

from aiohttp import web
from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.components.http.const import KEY_HASS_REFRESH_TOKEN_ID, KEY_HASS_USER
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.http import request_handler_factory

from .const import (
    DEFAULTS,
    DOMAIN,
    FRONTEND_URL,
    ROOT,
    SESSION_IDLE_SECONDS,
    proxy_path,
    session_cookie,
)
from .panels import normalize_panel_path, panel_path_error, panel_url_path
from .proxy import BRIDGE, ProxyTransport
from .security import SessionStore, check_origin, has_access
from .sidebar import install_panel_filter

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass, config):
    store = SessionStore(hass)
    transport = ProxyTransport(store)
    transport.bridge = await hass.async_add_executor_job(BRIDGE.read_text)
    hass.data[DOMAIN] = transport
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                FRONTEND_URL,
                str(Path(__file__).parent / "frontend" / "panel.js"),
                False,
            ),
            StaticPathConfig(
                f"{ROOT}/translations.js",
                str(Path(__file__).parent / "frontend" / "translations.js"),
                False,
            ),
        ]
    )
    hass.http.register_view(SessionView(transport))
    hass.http.register_view(ProxyView(transport))
    uninstall_panel_filter = install_panel_filter(hass, transport.entries)

    async def stop(event):
        uninstall_panel_filter()
        await transport.close()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    return True


async def async_setup_entry(hass, entry):
    transport = hass.data[DOMAIN]
    settings = DEFAULTS | dict(entry.data) | dict(entry.options)
    settings["panel_path"] = normalize_panel_path(settings["panel_path"])
    panel_path = Path(__file__).parent / "frontend" / "panel.js"
    panel_source = await hass.async_add_executor_job(panel_path.read_bytes)
    panel_revision = sha256(panel_source).hexdigest()[:12]
    if error := panel_path_error(hass, settings["panel_path"], entry.entry_id):
        raise HomeAssistantError(f"Cannot register Better Proxy panel: {error}")
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=panel_url_path(entry.entry_id, settings),
        webcomponent_name="better-proxy-panel",
        sidebar_title=settings["name"],
        sidebar_icon=settings["icon"],
        module_url=f"{FRONTEND_URL}?v={panel_revision}",
        config={
            "entry_id": entry.entry_id,
            "title": settings["name"],
            "show_header": settings["show_header"],
        },
        require_admin=settings["access"] == "admin",
    )
    transport.entries[entry.entry_id] = settings
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_reload_entry(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    transport = hass.data[DOMAIN]
    if settings := transport.entries.get(entry.entry_id):
        frontend.async_remove_panel(hass, panel_url_path(entry.entry_id, settings))
    await transport.close_entry(entry.entry_id)
    return True


class SessionView(HomeAssistantView):
    """Only HA Bearer auth can issue the HttpOnly resource cookie."""

    url = f"{ROOT}/proxy/{{entry_id}}/__better_proxy_session"
    name = "api:better_proxy:session"
    requires_auth = True

    def __init__(self, transport):
        self.transport = transport

    async def post(self, request, entry_id):
        check_origin(request, required=True)
        settings = self.transport.entries.get(entry_id)
        if settings is None:
            raise web.HTTPNotFound()
        user = request[KEY_HASS_USER]
        refresh_id = request.get(KEY_HASS_REFRESH_TOKEN_ID)
        # Signed URLs, trusted networks and proxy cookies cannot mint a session.
        if not refresh_id or not request.headers.get("Authorization", "").startswith(
            "Bearer "
        ):
            raise web.HTTPUnauthorized()
        if not has_access(user, settings):
            raise web.HTTPForbidden(text="You do not have access to this resource")
        store = self.transport.store
        token = request.cookies.get(session_cookie(entry_id), "")
        session = store.sessions.get(token)
        if (
            session is None
            or session.refresh_token_id != refresh_id
            or not store.valid(session, entry_id, settings, request)
        ):
            store.sessions.pop(token, None)
            token, session = store.issue(entry_id, refresh_id, user.id)
        else:
            store.authenticate(token, entry_id, settings, request)
        response = web.json_response(
            {"url": proxy_path(entry_id)}, headers={"Cache-Control": "no-store"}
        )
        response.set_cookie(
            session_cookie(entry_id),
            token,
            path=proxy_path(entry_id),
            httponly=True,
            secure=request.scheme == "https",
            samesite="Strict",
            max_age=SESSION_IDLE_SECONDS,
        )
        return response


class ProxyView(HomeAssistantView):
    """HA Bearer middleware is replaced here by the scoped session check."""

    url = f"{ROOT}/proxy/{{entry_id}}/{{path:.*}}"
    name = "api:better_proxy:resource"
    requires_auth = False  # Mandatory authentication lives in ProxyTransport.handle.

    def __init__(self, transport):
        self.transport = transport

    def register(self, hass, app, router):
        """Own OPTIONS and reject cross-origin requests instead of HA CORS preflight."""
        handler = request_handler_factory(hass, self, self.handle)
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            router.add_route(method, self.url, handler)

    async def handle(self, request, entry_id, path):
        return await self.transport.handle(request, entry_id, path)
