"""Exercise the actual HA auth manager and actual aiohttp transport."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from homeassistant.auth import auth_manager_from_config
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.components import frontend
from homeassistant.components.http.const import KEY_HASS_REFRESH_TOKEN_ID, KEY_HASS_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry

from custom_components.better_proxy import SessionView
from custom_components.better_proxy.const import DEFAULTS, proxy_path
from custom_components.better_proxy.proxy import ProxyTransport
from custom_components.better_proxy.security import SessionStore


@pytest.fixture
async def environment(tmp_path, aiohttp_client):
    hass = HomeAssistant(str(tmp_path))
    # Older HA releases initialize the registry inside async_load.
    if setup_registry := getattr(device_registry, "async_setup", None):
        setup_registry(hass)
    await device_registry.async_load(hass)
    if panel_config_key := getattr(frontend, "DATA_PANELS_CONFIG", None):
        hass.data[panel_config_key] = {}
    hass.auth = await auth_manager_from_config(hass, [{"type": "homeassistant"}], [])
    admin = await hass.auth.async_create_user("Admin", group_ids=[GROUP_ID_ADMIN])
    user = await hass.auth.async_create_user("Member", group_ids=[GROUP_ID_USER])
    other = await hass.auth.async_create_user("Other", group_ids=[GROUP_ID_USER])
    refresh = await hass.auth.async_create_refresh_token(admin, client_id="http://test")
    member_refresh = await hass.auth.async_create_refresh_token(
        user, client_id="http://test"
    )
    store = SessionStore(hass)
    transport = ProxyTransport(store)
    transport.bridge = await hass.async_add_executor_job(
        Path("custom_components/better_proxy/frontend/bridge.js").read_text
    )
    upstream_app = web.Application()
    seen = []

    async def upstream(request):
        seen.append(
            {
                "path": request.raw_path,
                "headers": dict(request.headers),
                "body": await request.read(),
            }
        )
        if request.path == "/ws":
            ws = web.WebSocketResponse(protocols=["echo"])
            await ws.prepare(request)
            async for message in ws:
                if isinstance(message.data, str):
                    await ws.send_str(message.data)
                elif isinstance(message.data, bytes):
                    await ws.send_bytes(message.data)
            return ws
        if request.path == "/redirect":
            return web.Response(status=302, headers={"Location": "/target?q=1"})
        if request.path == "/cookies":
            response = web.Response(text="cookies")
            response.headers.add(
                "Set-Cookie", "login=alice; Path=/; HttpOnly; Domain=internal"
            )
            response.headers.add("Set-Cookie", "csrf=abc; Path=/; SameSite=Lax")
            return response
        if request.path == "/page":
            return web.Response(
                text='<html><head></head><body><img src="/logo.png"><script src="/app.js"></script></body></html>',
                content_type="text/html",
            )
        if request.path == "/range":
            return web.Response(
                status=206,
                body=b"1234",
                headers={"Content-Range": "bytes 0-3/10", "Content-Type": "video/mp4"},
            )
        if request.path == "/empty":
            return web.Response(status=204)
        if request.path == "/sse":
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: first\n\n")
            await response.write(b"data: second\n\n")
            return response
        return web.json_response(
            {
                "path": request.raw_path,
                "body": seen[-1]["body"].decode(errors="replace"),
                "headers": dict(request.headers),
            }
        )

    upstream_app.router.add_route("*", "/{path:.*}", upstream)
    upstream_client = await aiohttp_client(upstream_app)
    transport.entries["test"] = DEFAULTS | {
        "name": "Test",
        "url": str(upstream_client.make_url("/")),
    }

    @web.middleware
    async def auth(request, handler):
        if request.path.endswith("/__better_proxy_session"):
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            validated = hass.auth.async_validate_access_token(token)
            if not validated:
                raise web.HTTPUnauthorized()
            request[KEY_HASS_USER] = validated.user
            request[KEY_HASS_REFRESH_TOKEN_ID] = validated.id
        return await handler(request)

    app = web.Application(middlewares=[auth])
    view = SessionView(transport)

    async def issue(request):
        return await view.post(request, request.match_info["entry_id"])

    async def proxy(request):
        return await transport.handle(request, **request.match_info)

    app.router.add_post(
        "/api/better_proxy/proxy/{entry_id}/__better_proxy_session", issue
    )
    app.router.add_route("*", "/api/better_proxy/proxy/{entry_id}/{path:.*}", proxy)
    client = await aiohttp_client(app)
    prefix = proxy_path("test")

    async def login(which=refresh):
        response = await client.post(
            prefix + "__better_proxy_session",
            headers={
                "Authorization": f"Bearer {hass.auth.async_create_access_token(which)}",
                "Origin": str(client.make_url("/")).rstrip("/"),
            },
        )
        return response

    yield SimpleNamespace(
        hass=hass,
        admin=admin,
        user=user,
        other=other,
        refresh=refresh,
        member_refresh=member_refresh,
        transport=transport,
        store=store,
        client=client,
        prefix=prefix,
        seen=seen,
        login=login,
    )
    await transport.close()
    await hass.async_stop(force=True)
