"""Security and transport regression checks over real HTTP connections."""

import asyncio

import aiohttp
import pytest

from custom_components.better_proxy.const import session_cookie


async def test_no_anonymous_resources(environment):
    env = environment
    for path in ("", "page", "logo.png", "__better_proxy_bridge.js", "ws"):
        response = await env.client.get(env.prefix + path)
        assert response.status == 401
    assert env.seen == []


async def test_session_requires_ha_bearer(environment):
    env = environment
    response = await env.client.post(env.prefix + "__better_proxy_session")
    assert response.status == 401
    assert (await env.login()).status == 200
    response = await env.client.post(env.prefix + "__better_proxy_session")
    assert response.status == 401  # Proxy cookie alone is insufficient.


async def test_admin_only_and_exact_allowlist(environment):
    env = environment
    assert (await env.login(env.member_refresh)).status == 403
    env.transport.entries["test"].update(access="users", users=[env.user.id])
    assert (await env.login()).status == 403
    assert (await env.login(env.member_refresh)).status == 200
    assert (await env.client.get(env.prefix)).status == 200
    env.transport.entries["test"]["users"] = [env.other.id]
    assert (await env.client.get(env.prefix)).status == 401


async def test_auth_all_still_requires_login(environment):
    env = environment
    env.transport.entries["test"]["access"] = "authenticated"
    assert (await env.client.get(env.prefix)).status == 401
    assert (await env.login(env.member_refresh)).status == 200


async def test_refresh_revocation_and_disabled_user(environment):
    env = environment
    await env.login()
    env.admin.is_active = False
    assert (await env.client.get(env.prefix)).status == 401
    env.admin.is_active = True
    await env.login()
    env.hass.auth.async_remove_refresh_token(env.refresh)
    assert (await env.client.get(env.prefix)).status == 401


async def test_cookie_scope_and_upstream_credentials(environment):
    env = environment
    response = await env.login()
    cookie = response.cookies[session_cookie("test")]
    assert cookie["httponly"] and cookie["samesite"] == "Strict"
    assert cookie["path"] == env.prefix
    env.transport.entries["test"].update(username="service", password="secret")
    await env.client.get(env.prefix + "cookies")
    response = await env.client.get(
        env.prefix + "echo",
        headers={
            "Authorization": "Bearer do-not-forward",
            "X-Forwarded-For": "spoofed",
            "X-HA-Access": "secret",
        },
    )
    headers = (await response.json())["headers"]
    assert headers["Authorization"] == aiohttp.BasicAuth("service", "secret").encode()
    assert set(headers["Cookie"].split("; ")) == {"login=alice", "csrf=abc"}
    assert headers["X-Ingress-Path"] == env.prefix.rstrip("/")
    assert headers["X-Forwarded-For"] != "spoofed"
    assert "X-HA-Access" not in headers


async def test_cookies_are_not_shared_between_users(environment):
    env = environment
    env.transport.entries["test"]["access"] = "authenticated"
    await env.login()
    await env.client.get(env.prefix + "cookies")
    await env.login(env.member_refresh)
    response = await env.client.get(env.prefix)
    assert "Cookie" not in (await response.json())["headers"]


async def test_csrf_denied_and_post_streamed(environment):
    env = environment
    await env.login()
    for headers in (
        {},
        {"Origin": "https://evil.test"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        response = await env.client.post(
            env.prefix + "echo", data=b"payload", headers=headers
        )
        assert response.status == 403
    response = await env.client.post(
        env.prefix + "echo",
        data=b"payload",
        headers={"Origin": str(env.client.make_url("/")).rstrip("/")},
    )
    assert response.status == 200
    assert (await response.json())["body"] == "payload"


async def test_redirect_html_and_raw_query(environment):
    env = environment
    await env.login()
    response = await env.client.get(env.prefix + "redirect", allow_redirects=False)
    assert response.headers["Location"] == env.prefix + "target?q=1"
    response = await env.client.get(env.prefix + "page")
    text = await response.text()
    assert f'src="{env.prefix}logo.png"' in text
    assert text.index("__better_proxy_bridge.js") < text.index("app.js")
    assert "no-store" in response.headers["Cache-Control"]
    response = await env.client.get(env.prefix + "echo?a=x%2By&a=x%20y")
    assert (await response.json())["path"] == "/echo?a=x%2By&a=x%20y"


async def test_range_head_empty_sse(environment):
    env = environment
    await env.login()
    response = await env.client.get(
        env.prefix + "range", headers={"Range": "bytes=0-3"}
    )
    assert response.status == 206
    assert response.headers["Content-Range"] == "bytes 0-3/10"
    assert await response.read() == b"1234"
    assert (await env.client.head(env.prefix + "range")).status == 206
    assert (await env.client.get(env.prefix + "empty")).status == 204
    response = await env.client.get(env.prefix + "sse")
    assert "data: second" in await response.text()


async def test_websocket_text_binary_and_revocation(environment):
    env = environment
    await env.login()
    origin = str(env.client.make_url("/")).rstrip("/")
    with pytest.raises(aiohttp.WSServerHandshakeError) as error:
        await env.client.ws_connect(
            env.prefix + "ws", headers={"Origin": "https://evil.test"}
        )
    assert error.value.status == 403
    async with env.client.ws_connect(
        env.prefix + "ws", protocols=["echo"], headers={"Origin": origin}
    ) as ws:
        assert ws.protocol == "echo"
        await ws.send_str("hello")
        assert (await ws.receive()).data == "hello"
        await ws.send_bytes(b"binary")
        assert (await ws.receive()).data == b"binary"
        env.hass.auth.async_remove_refresh_token(env.refresh)
        message = await asyncio.wait_for(ws.receive(), 7)
        assert message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE}


async def test_expiry_and_resource_binding(environment):
    env = environment
    await env.login()
    session = next(iter(env.store.sessions.values()))
    session.entry_id = "another-resource"
    assert (await env.client.get(env.prefix)).status == 401
    await env.login()
    next(iter(env.store.sessions.values())).touched -= 1801
    assert (await env.client.get(env.prefix)).status == 401


async def test_reload_revokes_and_service_worker_denied(environment):
    env = environment
    await env.login()
    response = await env.client.get(
        env.prefix + "worker.js", headers={"Service-Worker": "script"}
    )
    assert response.status == 403
    await env.transport.close_entry("test")
    assert (await env.client.get(env.prefix)).status == 404
    assert not env.store.sessions


async def test_session_renewal_preserves_cookie_namespace(environment):
    env = environment
    await env.login()
    original = next(iter(env.store.sessions.values()))
    await env.login()
    assert list(env.store.sessions.values()) == [original]


async def test_absolute_expiry_cannot_be_extended_by_activity(environment):
    env = environment
    await env.login()
    next(iter(env.store.sessions.values())).created -= 43201
    assert (await env.client.get(env.prefix)).status == 401


async def test_browser_basic_auth_and_ha_cookie_not_forwarded(environment):
    env = environment
    await env.login()
    basic = aiohttp.BasicAuth("upstream", "browser-password").encode()
    env.client.session.cookie_jar.update_cookies({"ha_private": "never-forward"})
    response = await env.client.get(env.prefix, headers={"Authorization": basic})
    headers = (await response.json())["headers"]
    assert headers["Authorization"] == basic
    assert "Cookie" not in headers


async def test_compatibility_text_limit(environment, monkeypatch):
    from custom_components.better_proxy import proxy

    env = environment
    await env.login()
    monkeypatch.setattr(proxy, "MAX_TEXT_BYTES", 16)
    response = await env.client.get(env.prefix + "page")
    assert response.status == 502
    env.transport.entries["test"]["rewrite"] = False
    response = await env.client.get(env.prefix + "page")
    assert response.status == 200
    assert "__better_proxy_bridge.js" not in await response.text()


async def test_register_routes_with_home_assistant_cors(environment):
    from aiohttp import web
    from homeassistant.components.http.cors import setup_cors

    from custom_components.better_proxy import ProxyView, SessionView

    app = web.Application()
    setup_cors(app, ["https://example.com"])
    SessionView(environment.transport).register(environment.hass, app, app.router)
    ProxyView(environment.transport).register(environment.hass, app, app.router)
    proxy_routes = [
        route for route in app.router.routes() if "{path}" in route.resource.canonical
    ]
    assert {route.method for route in proxy_routes} == {
        "GET",
        "HEAD",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
    }


async def test_websocket_graceful_client_close(environment):
    env = environment
    await env.login()
    origin = str(env.client.make_url("/")).rstrip("/")
    ws = await env.client.ws_connect(env.prefix + "ws", headers={"Origin": origin})
    await ws.send_str("check")
    assert (await ws.receive()).data == "check"
    await ws.close(code=1000)
    assert ws.close_code == 1000
