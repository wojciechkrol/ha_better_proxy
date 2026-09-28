"""Upstream authentication, kept separate from Home Assistant credentials."""

import aiohttp
from aiohttp import web
from packaging.version import Version
from yarl import URL

from .const import MAX_AUTH_BODY_BYTES


class RawPathDigestAuth(aiohttp.DigestAuthMiddleware):
    """Sign the exact request target, including percent-encoded path and query.

    aiohttp versions before 3.14 decode the signing target. Apply the encoding
    workaround only there; newer encoders already sign the raw request target.
    """

    async def _encode(self, method, url, body):
        if Version(aiohttp.__version__) >= Version("3.14.0"):
            return await super()._encode(method, url, body)
        signing_url = URL.build(
            scheme=url.scheme,
            authority=url.raw_authority,
            path=url.raw_path,
            query_string=url.raw_query_string,
        )
        return await super()._encode(method, signing_url, body)


async def digest_body(request):
    """Replay identical upload bytes after a Digest challenge, with bounded memory."""
    if request.content_length and request.content_length > MAX_AUTH_BODY_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_AUTH_BODY_BYTES, actual_size=request.content_length
        )
    body = bytearray()
    async for chunk in request.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body) > MAX_AUTH_BODY_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_AUTH_BODY_BYTES, actual_size=len(body)
            )
    return bytes(body)
