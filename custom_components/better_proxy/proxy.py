"""Streaming HTTP and WebSocket transport; all routes enforce the session ACL."""

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from urllib.parse import unquote

import aiohttp
from aiohttp import web
from aiohttp.helpers import must_be_empty_body
from multidict import CIMultiDict
from yarl import URL

from .const import MAX_TEXT_BYTES, MAX_WS_BYTES, proxy_path, session_cookie
from .rewrite import (
    HLS_TYPES,
    rewrite_cookie,
    rewrite_location,
    rewrite_refresh,
    rewrite_text,
    upstream_cookie_header,
)
from .security import check_origin
from .upstream_auth import RawPathDigestAuth, digest_body

HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
TEXT_TYPES = {
    "text/html",
    "text/css",
} | HLS_TYPES
BRIDGE = Path(__file__).parent / "frontend" / "bridge.js"


def hop_headers(headers):
    return HOP_HEADERS | {
        part.strip().lower() for part in headers.get("Connection", "").split(",")
    }


def target_url(request, upstream, prefix):
    """Preserve escaped path/query without allowing origin or base-path traversal."""
    raw = request.raw_path.partition("?")
    if not raw[0].startswith(prefix):
        raise web.HTTPBadRequest(text="Invalid proxy path")
    path = raw[0][len(prefix) :]
    decoded = path
    for _ in range(4):
        decoded_next = unquote(decoded)
        if decoded_next == decoded:
            break
        decoded = decoded_next
    if (
        "\\" in decoded
        or any(p in {".", ".."} for p in decoded.split("/"))
        or any(ord(c) < 32 for c in decoded)
    ):
        raise web.HTTPBadRequest(text="Invalid upstream path")
    # Concatenation after an origin/base path, never urljoin on attacker input.
    return URL(
        str(upstream).rstrip("/") + "/" + path + ("?" + raw[2] if raw[1] else ""),
        encoded=True,
    )


def request_headers(request, upstream, prefix, session, settings):
    blocked = hop_headers(request.headers) | {
        "host",
        "cookie",
        "authorization",
        "content-length",
        "content-encoding",
        "accept-encoding",
        "forwarded",
        "origin",
        "referer",
    }
    headers = CIMultiDict(
        (k, v)
        for k, v in request.headers.items()
        if k.lower() not in blocked
        and not k.lower().startswith(
            ("x-forwarded-", "x-ingress-", "x-ha-", "sec-websocket-", "x-better-proxy-")
        )
    )
    headers["Host"] = upstream.host_port_subcomponent
    headers["Accept-Encoding"] = "identity"
    headers["X-Forwarded-Host"] = request.host
    headers["X-Forwarded-Proto"] = request.scheme
    headers["X-Forwarded-Prefix"] = prefix.rstrip("/")
    headers["X-Ingress-Path"] = prefix.rstrip("/")
    if request.remote:
        headers["X-Forwarded-For"] = request.remote
    if request.headers.get("Origin"):
        headers["Origin"] = str(upstream.origin())
    if referer := request.headers.get("Referer"):
        original = URL(referer)
        if original.path.startswith(prefix):
            headers["Referer"] = (
                str(upstream).rstrip("/") + "/" + original.path[len(prefix) :]
            )
    if cookies := upstream_cookie_header(
        request.headers.get("Cookie", ""), session.cookie_prefix
    ):
        headers["Cookie"] = cookies
    auth_type = settings.get("auth_type", "basic")
    if auth_type == "basic" and settings.get("username"):
        headers["Authorization"] = aiohttp.BasicAuth(
            settings["username"], settings.get("password", "")
        ).encode()
    elif auth_type == "bearer":
        headers["Authorization"] = "Bearer " + settings["bearer_token"].strip()
    elif auth_type != "digest":
        # The bridge tunnels application Authorization explicitly; raw HA Bearer
        # headers are never copied. Browser Basic challenges work without a bridge.
        application_auth = request.headers.get("X-Better-Proxy-Authorization", "")
        if application_auth.lower().startswith(("basic ", "bearer ")):
            headers["Authorization"] = application_auth
        elif request.headers.get("Authorization", "").lower().startswith("basic "):
            headers["Authorization"] = request.headers["Authorization"]
    return headers


def response_headers(result, request, upstream, target, prefix, session, rewrite):
    blocked = hop_headers(result.headers) | {
        "set-cookie",
        "content-length",
        "content-encoding",
        "clear-site-data",
        "service-worker-allowed",
        "access-control-allow-origin",
        "access-control-allow-credentials",
    }
    if rewrite:
        blocked |= {
            "content-security-policy",
            "content-security-policy-report-only",
            "etag",
            "content-md5",
            "digest",
        }
    headers = CIMultiDict(
        (k, v) for k, v in result.headers.items() if k.lower() not in blocked
    )
    # Protected resources must not remain available from an intermediary/browser cache.
    headers["Cache-Control"] = "no-store, private"
    headers["Referrer-Policy"] = "same-origin"
    headers["X-Frame-Options"] = "SAMEORIGIN"
    if rewrite:
        headers["Content-Security-Policy"] = (
            "frame-ancestors 'self'; worker-src 'none'; object-src 'none'"
        )
    for key in ("Location", "Content-Location"):
        if key in headers:
            headers[key] = rewrite_location(headers[key], upstream, target, prefix)
    if "Refresh" in headers:
        headers["Refresh"] = rewrite_refresh(
            headers["Refresh"], upstream, target, prefix
        )
    for raw in result.headers.getall("Set-Cookie", []):
        for cookie in rewrite_cookie(
            raw,
            prefix,
            session.cookie_prefix,
            request.scheme == "https",
            upstream,
            target,
        ):
            headers.add("Set-Cookie", cookie)
    return headers


class ProxyTransport:
    def __init__(self, store):
        self.store = store
        self.entries = {}
        self.client = aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=15),
            auto_decompress=True,
            trust_env=False,
        )
        self.bridge = ""
        self.tasks = {}

    async def close_entry(self, entry_id):
        self.entries.pop(entry_id, None)
        self.store.revoke_entry(entry_id)
        pending = list(self.tasks.get(entry_id, set()))
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def close(self):
        for entry_id in list(self.entries):
            await self.close_entry(entry_id)
        await self.client.close()

    async def handle(self, request, entry_id, path=""):
        settings = self.entries.get(entry_id)
        if settings is None:
            raise web.HTTPNotFound()
        websocket = request.headers.get("Upgrade", "").lower() == "websocket"
        check_origin(
            request,
            required=websocket or request.method not in {"GET", "HEAD", "OPTIONS"},
        )
        token = request.cookies.get(session_cookie(entry_id), "")
        session = self.store.authenticate(token, entry_id, settings, request)
        if request.headers.get("Service-Worker", "").lower() == "script":
            raise web.HTTPForbidden(
                text="Service workers are not supported inside Home Assistant"
            )
        prefix = proxy_path(entry_id)
        upstream = URL(settings["url"])
        if path == "__better_proxy_bridge.js":
            config = json.dumps(
                {
                    "prefix": prefix,
                    "upstream": str(upstream),
                    "cookiePrefix": session.cookie_prefix,
                }
            )
            return web.Response(
                text=f"(() => {{ const config = {config};\n{self.bridge}\n}})();",
                content_type="application/javascript",
                headers={"Cache-Control": "no-store, private"},
            )
        target = target_url(request, upstream, prefix)
        headers = request_headers(request, upstream, prefix, session, settings)
        work = asyncio.create_task(
            self.dispatch(
                request, target, headers, upstream, prefix, session, settings, websocket
            )
        )
        self.tasks.setdefault(entry_id, set()).add(work)
        watch = asyncio.create_task(
            self.watch_session(token, session, entry_id, settings, request, work)
        )
        try:
            return await work
        except (aiohttp.ClientError, TimeoutError):
            raise web.HTTPBadGateway(
                text="The configured upstream service is unavailable"
            ) from None
        finally:
            work.cancel()
            watch.cancel()
            await asyncio.gather(work, watch, return_exceptions=True)
            self.tasks[entry_id].discard(work)

    async def watch_session(self, token, session, entry_id, settings, request, work):
        """Revoke already open streams, not only subsequent HTTP requests."""
        while True:
            await asyncio.sleep(5)
            if self.store.sessions.get(token) is not session or not self.store.valid(
                session, entry_id, settings, request
            ):
                work.cancel()
                if request.transport:
                    request.transport.close()
                return

    async def dispatch(
        self, request, target, headers, upstream, prefix, session, settings, websocket
    ):
        async def send(client):
            if websocket:
                return await self.websocket(request, target, headers, settings, client)
            return await self.http(
                request, target, headers, upstream, prefix, session, settings, client
            )

        if settings.get("auth_type") != "digest":
            return await send(self.client)
        # Each request gets its own challenge/nonce state, including WS handshakes.
        # Share only the connection pool; cookies and credentials never cross entries.
        async with aiohttp.ClientSession(
            connector=self.client.connector,
            connector_owner=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=self.client.timeout,
            middlewares=(
                RawPathDigestAuth(settings["username"], settings.get("password", "")),
            ),
            trust_env=False,
        ) as client:
            return await send(client)

    async def http(
        self, request, target, headers, upstream, prefix, session, settings, client
    ):
        data = request.content if request.can_read_body else None
        # Preserve framing for embedded HTTP servers that cannot parse chunked
        # forms. aiohttp streams the body without buffering when length is known.
        # Incoming compressed bodies are already decoded by aiohttp, so their
        # original length and Content-Encoding must not describe the new stream.
        if (
            request.content_length is not None
            and request.headers.get("Content-Encoding", "identity").lower()
            == "identity"
        ):
            headers["Content-Length"] = str(request.content_length)
        if settings.get("auth_type") == "digest" and request.can_read_body:
            data = await digest_body(request)
        async with client.request(
            request.method,
            target,
            headers=headers,
            data=data,
            allow_redirects=False,
            ssl=settings["verify_ssl"],
            skip_auto_headers={"Content-Type"},
        ) as result:
            content_type = result.content_type
            rewrite = (
                settings["rewrite"]
                and content_type in TEXT_TYPES
                and result.status != 206
            )
            outgoing = response_headers(
                result, request, upstream, target, prefix, session, rewrite
            )
            if must_be_empty_body(request.method, result.status):
                return web.Response(status=result.status, headers=outgoing)
            if rewrite:
                body = bytearray()
                async for chunk in result.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_TEXT_BYTES:
                        raise web.HTTPBadGateway(
                            text="Text resource exceeds the compatibility rewrite limit; disable URL rewriting for this resource"
                        )
                try:
                    text = body.decode(result.charset or "utf-8")
                    text = rewrite_text(
                        text,
                        content_type,
                        upstream,
                        prefix,
                        target,
                        inject_bridge=(
                            # A service worker's fetch retains navigation mode
                            # while changing the destination to "empty".
                            request.headers.get("Sec-Fetch-Mode") == "navigate"
                            or (
                                request.headers.get("Sec-Fetch-Dest") != "empty"
                                and request.headers.get("X-Requested-With", "").lower()
                                != "xmlhttprequest"
                            )
                        ),
                    )
                    body = text.encode(result.charset or "utf-8")
                except (UnicodeError, LookupError):
                    raise web.HTTPBadGateway(
                        text="Unsupported upstream text encoding"
                    ) from None
                return web.Response(status=result.status, headers=outgoing, body=body)
            response = web.StreamResponse(status=result.status, headers=outgoing)
            await response.prepare(request)
            async for chunk in result.content.iter_chunked(65536):
                await response.write(chunk)
            await response.write_eof()
            return response

    async def websocket(self, request, target, headers, settings, client):
        protocols = [
            p.strip()
            for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
            if p.strip()
        ]
        # Connect before sending HTTP 101 so upstream failures produce an HTTP error.
        async with client.ws_connect(
            target,
            headers=headers,
            protocols=protocols,
            ssl=settings["verify_ssl"],
            max_msg_size=MAX_WS_BYTES,
            autoclose=False,
            autoping=False,
        ) as upstream:
            response = web.WebSocketResponse(
                protocols=[upstream.protocol] if upstream.protocol else [],
                max_msg_size=MAX_WS_BYTES,
                autoclose=False,
                autoping=False,
            )
            await response.prepare(request)
            tasks = [
                asyncio.create_task(forward_websocket(response, upstream)),
                asyncio.create_task(forward_websocket(upstream, response)),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                with suppress(ConnectionError):
                    code = response.close_code or upstream.close_code or 1000
                    await response.close(
                        code=code if code not in {1005, 1006, 1015} else 1011
                    )
            return response


async def forward_websocket(source, destination):
    while True:
        message = await source.receive()
        if message.type == aiohttp.WSMsgType.TEXT:
            await destination.send_str(message.data)
        elif message.type == aiohttp.WSMsgType.BINARY:
            await destination.send_bytes(message.data)
        elif message.type == aiohttp.WSMsgType.PING:
            await destination.ping(message.data)
        elif message.type == aiohttp.WSMsgType.PONG:
            await destination.pong(message.data)
        elif message.type == aiohttp.WSMsgType.CLOSE:
            await destination.close(code=message.data, message=message.extra.encode())
            return
        elif message.type in {
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.ERROR,
        }:
            return
