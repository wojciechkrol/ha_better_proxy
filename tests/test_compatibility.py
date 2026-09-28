"""LAN interface authentication and browser privacy-policy regressions."""

import hashlib
import re

import aiohttp
import pytest
from aiohttp import web
from yarl import URL

from custom_components.better_proxy.config_flow import validate_settings
from custom_components.better_proxy.const import DEFAULTS
from custom_components.better_proxy.rewrite import rewrite_cookie, rewrite_text


@pytest.mark.parametrize(
    "metadata,status",
    [
        ("same-origin", 200),
        ("same-site", 403),
        ("cross-site", 403),
        ("none", 403),
        ("", 403),
    ],
)
async def test_opaque_form_origin(environment, metadata, status):
    env = environment
    await env.login()
    result = await env.client.post(
        env.prefix + "submit",
        data={"message": "Form works"},
        headers={"Origin": "null", "Sec-Fetch-Site": metadata},
    )
    assert result.status == status
    if status == 200:
        assert (await result.json())["body"] == "message=Form+works"
        assert env.seen[-1]["headers"]["Origin"] == str(
            URL(env.transport.entries["test"]["url"]).origin()
        )
    else:
        assert not env.seen


async def test_same_origin_metadata_cannot_override_foreign_origin(environment):
    env = environment
    await env.login()
    result = await env.client.post(
        env.prefix,
        headers={"Origin": "https://evil.test", "Sec-Fetch-Site": "same-origin"},
    )
    assert result.status == 403
    env.client.session.cookie_jar.clear()
    result = await env.client.post(
        env.prefix, headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"}
    )
    assert result.status == 401
    assert not env.seen


async def test_bearer_sources_and_priority(environment):
    env = environment
    await env.login()
    for settings, headers, expected in [
        ({"auth_type": "none"}, {"Authorization": "Bearer HA-must-stay-local"}, None),
        (
            {"auth_type": "none"},
            {"X-Better-Proxy-Authorization": "Bearer app-token"},
            "Bearer app-token",
        ),
        (
            {"auth_type": "bearer", "bearer_token": "configured-token"},
            {"X-Better-Proxy-Authorization": "Bearer app-token"},
            "Bearer configured-token",
        ),
    ]:
        env.transport.entries["test"].update(settings)
        response = await env.client.get(env.prefix, headers=headers)
        actual = (await response.json())["headers"]
        assert actual.get("Authorization") == expected
        assert "X-Better-Proxy-Authorization" not in actual


@pytest.mark.parametrize(
    "algorithm,qop", [("MD5", "auth"), ("SHA-256", "auth-int"), ("MD5-sess", "auth")]
)
async def test_digest_upload_signs_exact_target(
    environment, aiohttp_client, algorithm, qop
):
    env = environment
    attempts = []
    raw_target = "/upload/a%2Fb?x=a%2Bb&x=a%20b"
    payload = b"\x00firmware\xff" * 32768
    hashfn = hashlib.sha256 if algorithm == "SHA-256" else hashlib.md5

    def digest(value):
        return hashfn(value).hexdigest()

    async def protected(request):
        body = await request.read()
        attempts.append(body)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Digest "):
            return web.Response(
                status=401,
                headers={
                    "WWW-Authenticate": f'Digest realm="camera", nonce="abc123", algorithm={algorithm}, qop="{qop}"'
                },
            )
        fields = dict(
            (k, quoted or plain)
            for k, quoted, plain in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', auth)
        )
        assert fields["uri"] == request.raw_path == raw_target
        ha1 = digest(b"operator:camera:password")
        if algorithm.endswith("-sess"):
            ha1 = digest(f"{ha1}:abc123:{fields['cnonce']}".encode())
        a2 = f"{request.method}:{request.raw_path}"
        if qop == "auth-int":
            a2 += ":" + digest(body)
        expected = digest(
            f"{ha1}:abc123:{fields['nc']}:{fields['cnonce']}:{qop}:{digest(a2.encode())}".encode()
        )
        assert fields["response"] == expected
        assert body == payload
        return web.Response(body=body)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", protected)
    upstream = await aiohttp_client(app)
    env.transport.entries["test"].update(
        url=str(upstream.make_url("/")),
        auth_type="digest",
        username="operator",
        password="password",
    )
    await env.login()
    response = await env.client.post(
        URL(env.prefix + raw_target[1:], encoded=True),
        data=payload,
        headers={"Origin": str(env.client.make_url("/").origin())},
    )
    assert response.status == 200
    assert await response.read() == payload
    assert attempts == [payload, payload]


async def test_digest_websocket(environment, aiohttp_client):
    env = environment
    attempts = []

    async def protected(request):
        attempts.append(request.headers.get("Authorization", ""))
        if not attempts[-1].startswith("Digest "):
            return web.Response(
                status=401,
                headers={
                    "WWW-Authenticate": 'Digest realm="camera", nonce="wsnonce", qop="auth"'
                },
            )
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str("authenticated")
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/ws", protected)
    upstream = await aiohttp_client(app)
    env.transport.entries["test"].update(
        url=str(upstream.make_url("/")),
        auth_type="digest",
        username="operator",
        password="password",
    )
    await env.login()
    async with env.client.ws_connect(
        env.prefix + "ws", headers={"Origin": str(env.client.make_url("/").origin())}
    ) as ws:
        message = await ws.receive()
        assert message.type == aiohttp.WSMsgType.TEXT
        assert message.data == "authenticated"
    assert len(attempts) == 2


def test_html_attributes_and_integrity():
    html = """<html><head><base href="/ui/"><meta http-equiv="refresh" content="5;url=/login"><script src="/app.js" integrity="sha256-old"></script><script src="https://cdn.test/lib.js" integrity="sha256-external"></script></head><body><img srcset="/small.png 1x, /large.png 2x"><button formaction=/save>Save</button><style>.x{background:url(/img.png)}</style></body></html>"""
    result = rewrite_text(html, "text/html", URL("http://router"), "/proxy/")
    assert 'href="/proxy/ui/"' in result
    assert 'content="5;url=/proxy/login"' in result
    assert 'srcset="/proxy/small.png 1x, /proxy/large.png 2x"' in result
    assert 'formaction="/proxy/save"' in result
    assert "sha256-old" in result and "sha256-external" in result
    assert "url(/proxy/img.png)" in result
    assert result.index("__better_proxy_bridge.js") < result.index("/proxy/app.js")


def test_hls_nested_playlist_keys_segments():
    playlist = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="/key?id=1"\n../segments/1.ts\nhttp://camera/live/2.ts\nhttps://cdn.test/3.ts\n'
    result = rewrite_text(
        playlist,
        "application/vnd.apple.mpegurl",
        URL("http://camera"),
        "/proxy/",
        URL("http://camera/live/main.m3u8"),
    )
    assert 'URI="/proxy/key?id=1"' in result
    assert "\n/proxy/segments/1.ts\n/proxy/live/2.ts\nhttps://cdn.test/3.ts\n" in result


def test_default_cookie_directory():
    result = rewrite_cookie(
        "session=abc; HttpOnly",
        "/proxy/",
        "ns_",
        False,
        URL("http://router/base"),
        URL("http://router/base/login/index"),
    )
    assert "Path=/proxy/login" in result[0]


def test_auth_configuration():
    base = DEFAULTS | {"name": "Camera", "url": "http://camera"}
    assert (
        validate_settings(base | {"auth_type": "digest"}, set())["username"]
        == "username_required"
    )
    assert (
        validate_settings(base | {"auth_type": "bearer"}, set())["bearer_token"]
        == "token_required"
    )
    assert (
        validate_settings(base | {"bearer_token": "a\r\nb"}, set())["bearer_token"]
        == "invalid_credentials"
    )
    assert not validate_settings(
        base | {"auth_type": "digest", "username": "user", "password": "pass"}, set()
    )


async def test_digest_upload_limit(environment, monkeypatch):
    from custom_components.better_proxy import upstream_auth

    monkeypatch.setattr(upstream_auth, "MAX_AUTH_BODY_BYTES", 8)
    env = environment
    env.transport.entries["test"].update(auth_type="digest", username="user")
    await env.login()
    for body in (b"123456789", None):

        async def chunks():
            yield b"1234"
            yield b"56789"

        response = await env.client.post(
            env.prefix,
            data=body if body else chunks(),
            headers={"Origin": str(env.client.make_url("/").origin())},
        )
        assert response.status == 413
    assert not env.seen


async def test_mjpeg_and_refresh_headers(environment, aiohttp_client):
    env = environment
    multipart = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8frame\xff\xd9\r\n--frame--\r\n"

    async def stream(request):
        if request.path == "/refresh":
            return web.Response(headers={"Refresh": "0; url=/login"})
        response = web.StreamResponse(
            headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"}
        )
        await response.prepare(request)
        await response.write(multipart[:15])
        await response.write(multipart[15:])
        return response

    app = web.Application()
    app.router.add_get("/{path:.*}", stream)
    upstream = await aiohttp_client(app)
    env.transport.entries["test"]["url"] = str(upstream.make_url("/"))
    await env.login()
    response = await env.client.get(env.prefix + "stream")
    assert (
        response.headers["Content-Type"] == "multipart/x-mixed-replace; boundary=frame"
    )
    assert await response.read() == multipart
    response = await env.client.get(env.prefix + "refresh")
    assert response.headers["Refresh"] == "0; url=" + env.prefix + "login"


def test_relative_integrity_and_meta_csp():
    page = '<head><meta http-equiv="Content-Security-Policy" content="script-src none"><script src="app.js" integrity="sha256-before"></script></head>'
    result = rewrite_text(page, "text/html", URL("http://router"), "/proxy/")
    assert "sha256-before" in result
    assert "script-src none" not in result


@pytest.mark.parametrize(
    "content_type",
    ["application/javascript", "text/javascript", "application/x-javascript"],
)
def test_javascript_url_like_literals_are_not_rewritten(content_type):
    # Reduced examples from Zigbee2MQTT's core-js bundle and WebSocket URL builder.
    source = r"""const escapes = {"\\/":"/"};
const quote = /"/g;
const re = String(/a/i) === "/a/i";
const pathname = window.location.pathname;
const endpoint = `${window.location.host}${pathname}${pathname.endsWith("/") ? "" : "/"}api`;
fetch("/api/check");
"""
    assert (
        rewrite_text(source, content_type, URL("http://zigbee:8080"), "/proxy/")
        == source
    )
    html = (
        "<head><script>"
        + source
        + '</script><script type="application/json">{"delimiter":"/"}</script></head>'
    )
    result = rewrite_text(html, "text/html", URL("http://zigbee:8080"), "/proxy/")
    assert "<script>" + source + "</script>" in result
    assert '{"delimiter":"/"}' in result
    assert "__better_proxy_bridge.js" in result


def test_integrity_changes_only_for_rewritten_stylesheets():
    html = '<head><script src="/app.js" integrity="js-original"></script><link rel="modulepreload" href="/module.js" integrity="module-original"><link rel="stylesheet" href="/app.css" integrity="css-original"><link rel="stylesheet" href="https://cdn.test/lib.css" integrity="external-original"></head>'
    result = rewrite_text(html, "text/html", URL("http://zigbee:8080"), "/proxy/")
    assert "js-original" in result and "module-original" in result
    assert "css-original" not in result and "external-original" in result


@pytest.mark.parametrize(
    "headers,expect_bridge",
    [
        ({"Sec-Fetch-Dest": "iframe"}, True),
        ({"Sec-Fetch-Dest": "document"}, True),
        # HA's service worker fetches navigation requests with an empty destination.
        ({"Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "navigate"}, True),
        (
            {
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "navigate",
                "X-Requested-With": "XMLHttpRequest",
            },
            True,
        ),
        ({"Sec-Fetch-Dest": "empty"}, False),
        ({"Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors"}, False),
        ({"X-Requested-With": "XMLHttpRequest"}, False),
        ({}, True),
    ],
)
async def test_navigation_gets_bridge_without_changing_ajax_fragments(
    environment, headers, expect_bridge
):
    env = environment
    await env.login()
    response = await env.client.get(env.prefix + "page", headers=headers)
    assert response.status == 200
    html = await response.text()
    assert ("__better_proxy_bridge.js" in html) == expect_bridge
    assert f'src="{env.prefix}logo.png"' in html


def test_router_fragment_retains_root_and_template_bindings():
    fragment = '<div view="{main}"><div widget="paragraph" text="{LOGIN.TITLE}"></div><img src="/logo.png"></div>'
    result = rewrite_text(
        fragment, "text/html", URL("http://router"), "/proxy/", inject_bridge=False
    )
    assert result == fragment.replace("/logo.png", "/proxy/logo.png")


async def test_router_form_retains_content_length(environment):
    env = environment
    await env.login()
    payload = b"operation=read&value=%C5%BC"
    response = await env.client.post(
        env.prefix,
        data=payload,
        headers={
            "Origin": str(env.client.make_url("/").origin()),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert response.status == 200
    assert env.seen[-1]["body"] == payload
    assert env.seen[-1]["headers"]["Content-Length"] == str(len(payload))
    assert "Transfer-Encoding" not in env.seen[-1]["headers"]


async def test_multipart_upload_retains_length_and_boundary(environment):
    env = environment
    await env.login()
    data = aiohttp.FormData()
    data.add_field("file", b"firmware-data", filename="firmware.bin")
    data.add_field("operation", "upload")
    response = await env.client.post(
        env.prefix,
        data=data,
        headers={"Origin": str(env.client.make_url("/").origin())},
    )
    assert response.status == 200
    seen = env.seen[-1]
    assert seen["headers"]["Content-Length"] == str(len(seen["body"]))
    assert "Transfer-Encoding" not in seen["headers"]
    boundary = seen["headers"]["Content-Type"].split("boundary=")[1]
    assert seen["body"].startswith(b"--" + boundary.encode())
    assert b"firmware-data" in seen["body"]


async def test_compressed_form_does_not_forward_stale_length(environment):
    import gzip

    env = environment
    await env.login()
    payload = b"operation=read"
    response = await env.client.post(
        env.prefix,
        data=gzip.compress(payload),
        headers={
            "Origin": str(env.client.make_url("/").origin()),
            "Content-Encoding": "gzip",
        },
    )
    assert response.status == 200
    assert env.seen[-1]["body"] == payload
    assert "Content-Encoding" not in env.seen[-1]["headers"]
    assert "Content-Length" not in env.seen[-1]["headers"]
