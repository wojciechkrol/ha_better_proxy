"""Path confinement, cookie rewriting, and configuration validation."""

from types import SimpleNamespace

import pytest
from aiohttp import web
from yarl import URL

from custom_components.better_proxy.config_flow import (
    BetterProxyConfigFlow,
    validate_settings,
)
from custom_components.better_proxy.const import DEFAULTS
from custom_components.better_proxy.proxy import target_url
from custom_components.better_proxy.rewrite import (
    map_url,
    rewrite_cookie,
    rewrite_location,
    rewrite_text,
    upstream_cookie_header,
)


@pytest.mark.parametrize(
    "value",
    [
        "ftp://host",
        "http://user:secret@host",
        "http://host/?secret=1",
        "http://host/#fragment",
        "missing",
        "http://host:99999",
    ],
)
def test_invalid_urls(value):
    assert (
        validate_settings(DEFAULTS | {"name": "Service", "url": value}, set())["url"]
        == "invalid_url"
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://esphome:6052",
        "http://frigate:5000",
        "https://host/base",
        "http://[::1]:8080",
    ],
)
def test_valid_urls(value):
    assert not validate_settings(DEFAULTS | {"name": "Service", "url": value}, set())


def test_selected_user_validation():
    data = DEFAULTS | {"name": "Service", "url": "http://host", "access": "users"}
    assert validate_settings(data, {"known"})["users"] == "select_users"
    assert (
        validate_settings(data | {"users": ["missing"]}, {"known"})["users"]
        == "select_users"
    )
    assert not validate_settings(data | {"users": ["known"]}, {"known"})


@pytest.mark.parametrize(
    "path",
    ["%2e%2e/secret", "%252e%252e/secret", "a/../secret", "%5csecret", "%00secret"],
)
def test_path_traversal_denied(path):
    with pytest.raises(web.HTTPBadRequest):
        target_url(
            SimpleNamespace(raw_path="/proxy/" + path),
            URL("http://internal/base"),
            "/proxy/",
        )


def test_origin_never_controlled_by_path():
    result = target_url(
        SimpleNamespace(raw_path="/proxy//evil.test/a?x=1&x=2"),
        URL("http://internal/base"),
        "/proxy/",
    )
    assert result.host == "internal"
    assert result.raw_path == "/base//evil.test/a"
    assert result.raw_query_string == "x=1&x=2"


def test_rewrite_leaves_external_urls():
    assert (
        map_url("https://external.test/logo", URL("http://internal"), "/proxy/")
        == "https://external.test/logo"
    )
    assert (
        map_url("http://internal/base/logo", URL("http://internal/base"), "/proxy/")
        == "/proxy/logo"
    )
    assert map_url("/proxy/logo", URL("http://internal"), "/proxy/") == "/proxy/logo"


@pytest.mark.parametrize(
    "value",
    [
        "/proxy/login?next=dashboard#form",
        "http://internal/proxy/login?next=dashboard#form",
        "//internal/proxy/login?next=dashboard#form",
    ],
)
def test_native_ingress_urls_keep_existing_prefix(value):
    upstream = URL("http://internal")
    expected = "/proxy/login?next=dashboard#form"
    assert map_url(value, upstream, "/proxy/") == expected
    assert rewrite_location(value, upstream, upstream, "/proxy/") == expected


def test_esphome_ingress_base_is_not_prefixed_twice():
    page = '<html><head><base href="/proxy/"></head><body><esphome-app></esphome-app><script src="app.hash.js"></script></body></html>'
    result = rewrite_text(page, "text/html", URL("http://esphome:6052"), "/proxy/")
    assert '<base href="/proxy/">' in result
    assert '<script src="app.hash.js"></script>' in result
    assert "/proxy/proxy/" not in result


def test_cookie_deletion_security_and_filter():
    rewritten = rewrite_cookie(
        "__Host-auth=; Path=/; Domain=internal; Secure; HttpOnly; Max-Age=0",
        "/proxy/",
        "unique_",
        True,
    )[0]
    assert rewritten.startswith("unique___Host-auth=")
    assert "Domain" not in rewritten
    assert "Path=/proxy/" in rewritten
    assert (
        "HttpOnly" in rewritten and "Secure" in rewritten and "Max-Age=0" in rewritten
    )
    assert (
        upstream_cookie_header("HA=secret; another_token=x; unique_app=ok", "unique_")
        == "app=ok"
    )


async def test_native_config_flow(environment):
    flow = BetterProxyConfigFlow()
    flow.hass = environment.hass
    form = await flow.async_step_user()
    assert form["type"] == "form"
    assert "icon" in form["data_schema"].schema["service"].schema.schema
    result = await flow.async_step_user(
        DEFAULTS | {"name": "Frigate", "url": "http://frigate:5000"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Frigate"
    assert result["data"]["access"] == "admin"


def test_cookie_path_with_upstream_base():
    cookie = rewrite_cookie(
        "auth=yes; Path=/base/login",
        "/proxy/",
        "user_",
        False,
        URL("http://internal/base"),
    )[0]
    assert "Path=/proxy/login" in cookie
