"""Bounded compatibility rewriting for applications served below a path prefix."""

import re
from html import escape
from html.parser import HTMLParser
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urljoin, urlsplit

from yarl import URL


def map_url(value: str, upstream: URL, prefix: str) -> str:
    """Map only this upstream origin; preserve external links and special schemes."""
    if (
        value.startswith(prefix)
        or not value
        or value.startswith(("#", "data:", "blob:", "javascript:", "mailto:"))
    ):
        return value
    if value.startswith("/") and not value.startswith("//"):
        path = value
    elif value.startswith(("http://", "https://", "ws://", "wss://", "//")):
        parsed = urlsplit(
            value if not value.startswith("//") else f"{upstream.scheme}:{value}"
        )
        candidate = URL(
            value if not value.startswith("//") else f"{upstream.scheme}:{value}"
        )
        if candidate.host != upstream.host or candidate.port != upstream.port:
            return value
        path = (
            parsed.path
            + (f"?{parsed.query}" if parsed.query else "")
            + (f"#{parsed.fragment}" if parsed.fragment else "")
        )
    else:
        return value
    # Native ingress support can already include the proxy prefix, including
    # absolute URLs resolved from an application's <base> or redirect.
    if path.startswith(prefix):
        return path
    base = upstream.path.rstrip("/")
    if base and (path == base or path.startswith(base + "/")):
        path = path[len(base) :]
    return prefix + path.lstrip("/")


def rewrite_location(value, upstream, target, prefix):
    return map_url(urljoin(str(target), value), upstream, prefix)


def rewrite_srcset(value, upstream, prefix):
    """Map candidate URLs while preserving descriptors and data-URL commas."""
    output = []
    remaining = value
    while remaining:
        match = re.match(r"([\s,]*)([^\s]+)", remaining)
        if not match:
            output.append(remaining)
            break
        leading, candidate = match.groups()
        remaining = remaining[match.end() :]
        suffix = candidate[len(candidate.rstrip(",")) :]
        candidate = candidate.rstrip(",")
        output.append(leading + map_url(candidate, upstream, prefix) + suffix)
        if not suffix:
            descriptor, separator, remaining = remaining.partition(",")
            output.append(descriptor + separator)
    return "".join(output)


def rewrite_refresh(value, upstream, target, prefix):
    match = re.match(r"^(\s*[\d.]+\s*;\s*url\s*=\s*)(.*)$", value, re.I)
    if not match:
        return value
    destination = match[2].strip().strip("\"'")
    return match[1] + rewrite_location(destination, upstream, target, prefix)


def rewrite_literals(text, upstream, prefix):
    def replace(match):
        return match[1] + map_url(match[2], upstream, prefix) + match[1]

    text = re.sub(r"""(["'])(/[^\s"'<>]*|https?://[^\s"'<>]+)\1""", replace, text)
    return re.sub(
        r"url\(\s*(/[^\s)'\"]+)\s*\)",
        lambda m: f"url({map_url(m[1], upstream, prefix)})",
        text,
    )


class ProxyHTML(HTMLParser):
    """Rewrite URL-bearing attributes without treating arbitrary HTML as JavaScript."""

    def __init__(self, upstream, target, prefix, inject_bridge=True):
        super().__init__(convert_charrefs=False)
        self.upstream, self.target, self.prefix = upstream, target, prefix
        self.output = []
        self.raw_text = None
        self.injected = False
        self.inject_bridge = inject_bridge

    def handle_starttag(self, tag, attrs):
        self.start(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self.start(tag, attrs, True)

    def start(self, tag, attrs, closed):
        values = dict(attrs)
        if tag == "meta" and values.get("http-equiv", "").lower() in {
            "content-security-policy",
            "content-security-policy-report-only",
        }:
            return
        rewritten = []
        internal_resource = False
        for key, value in attrs:
            if value is not None:
                old = value
                if key in {
                    "src",
                    "href",
                    "action",
                    "formaction",
                    "poster",
                    "data",
                    "background",
                }:
                    value = map_url(value, self.upstream, self.prefix)
                    if tag == "base" and key == "href":
                        value = rewrite_location(
                            old, self.upstream, self.target, self.prefix
                        )
                    if key in {"src", "href"} and rewrite_location(
                        old, self.upstream, self.target, self.prefix
                    ).startswith(self.prefix):
                        internal_resource = True
                elif key in {"srcset", "imagesrcset"}:
                    value = rewrite_srcset(value, self.upstream, self.prefix)
                elif key == "style":
                    value = rewrite_literals(value, self.upstream, self.prefix)
                elif (
                    tag == "meta"
                    and key == "content"
                    and values.get("http-equiv", "").lower() == "refresh"
                ):
                    value = rewrite_refresh(
                        value, self.upstream, self.target, self.prefix
                    )
            rewritten.append((key, value))
        # Only CSS is transformed. Preserve integrity on unchanged JavaScript,
        # including modulepreload links, and on external resources.
        if (
            internal_resource
            and tag == "link"
            and (
                "stylesheet" in values.get("rel", "").lower().split()
                or values.get("as", "").lower() == "style"
            )
        ):
            rewritten = [(k, v) for k, v in rewritten if k != "integrity"]
        attributes = "".join(
            " " + k if v is None else f' {k}="{escape(v, quote=True)}"'
            for k, v in rewritten
        )
        self.output.append(f"<{tag}{attributes}{' /' if closed else ''}>")
        if tag == "head" and not self.injected:
            self.output.append(self.bridge)
            self.injected = True
        if tag in {"script", "style"} and not closed:
            self.raw_text = tag

    @property
    def bridge(self):
        if not self.inject_bridge:
            return ""
        return f'<script src="{self.prefix}__better_proxy_bridge.js"></script>'

    def handle_endtag(self, tag):
        self.output.append(f"</{tag}>")
        if tag == self.raw_text:
            self.raw_text = None

    def handle_data(self, data):
        self.output.append(
            rewrite_literals(data, self.upstream, self.prefix)
            if self.raw_text == "style"
            else data
        )

    def handle_entityref(self, name):
        self.output.append(f"&{name};")

    def handle_charref(self, name):
        self.output.append(f"&#{name};")

    def handle_comment(self, data):
        self.output.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.output.append(f"<!{decl}>")


HLS_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
}


def rewrite_text(
    text, content_type, upstream, prefix, target=None, *, inject_bridge=True
):
    """Rewrite document/CSS/media URLs without changing JavaScript semantics."""
    # URL-looking JS strings can be separators, regular expressions, router
    # patterns or data. Map requests in the browser bridge instead of source code.
    if content_type in {
        "application/javascript",
        "text/javascript",
        "application/x-javascript",
    }:
        return text
    target = target or upstream
    if content_type == "text/html":
        parser = ProxyHTML(upstream, target, prefix, inject_bridge)
        parser.feed(text)
        parser.close()
        return ("" if parser.injected else parser.bridge) + "".join(parser.output)
    if content_type in HLS_TYPES:
        lines = []
        for line in text.splitlines(keepends=True):
            if line.strip() and not line.startswith("#"):
                line = line.replace(
                    line.strip(),
                    rewrite_location(line.strip(), upstream, target, prefix),
                    1,
                )
            elif line.startswith("#"):
                line = re.sub(
                    r'URI="([^"\n]+)"',
                    lambda m: (
                        'URI="' + rewrite_location(m[1], upstream, target, prefix) + '"'
                    ),
                    line,
                )
            lines.append(line)
        return "".join(lines)
    return rewrite_literals(text, upstream, prefix)


def upstream_cookie_header(header: str, cookie_prefix: str) -> str:
    """Never forward HA cookies or another resource/user's cookies."""
    result = []
    for part in header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name.startswith(cookie_prefix):
            result.append(f"{name[len(cookie_prefix) :]}={value}")
    return "; ".join(result)


def rewrite_cookie(
    value: str,
    prefix: str,
    cookie_prefix: str,
    secure: bool,
    upstream: URL | None = None,
    target: URL | None = None,
) -> list[str]:
    """Namespace cookies and constrain them to this resource's path."""
    parsed = SimpleCookie()
    try:
        parsed.load(value)
    except CookieError:
        return []
    result = []
    for morsel in parsed.values():
        cookie = SimpleCookie()
        name = cookie_prefix + morsel.key
        cookie[name] = morsel.value
        for attribute in ("expires", "max-age", "httponly", "samesite"):
            if morsel[attribute]:
                cookie[name][attribute] = morsel[attribute]
        # Respect narrower upstream paths while keeping all cookies under the proxy.
        default_path = target.path.rpartition("/")[0] or "/" if target else "/"
        path = morsel["path"] if morsel["path"].startswith("/") else default_path
        cookie[name]["path"] = map_url(
            path, upstream or URL("http://placeholder"), prefix
        )
        cookie[name]["secure"] = secure or bool(morsel["secure"])
        result.append(cookie[name].OutputString())
    return result
