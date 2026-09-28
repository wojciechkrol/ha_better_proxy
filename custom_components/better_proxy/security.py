"""Server-side access checks and revocable, resource-scoped browser sessions."""

import secrets
import time
from dataclasses import dataclass, field

from aiohttp import web
from homeassistant.auth import InvalidAuthError
from homeassistant.components.http.auth import async_user_not_allowed_do_auth
from yarl import URL

from .const import MAX_SESSIONS, SESSION_IDLE_SECONDS, SESSION_MAX_SECONDS


def has_access(user, settings: dict) -> bool:
    """An explicit user list is exact: administrators are not implicitly included."""
    if not user.is_active or user.system_generated:
        return False
    mode = settings.get("access", "admin")
    return (
        (mode == "admin" and user.is_admin)
        or mode == "authenticated"
        or (mode == "users" and user.id in settings.get("users", []))
    )


def check_origin(request: web.Request, *, required=False) -> None:
    """Prevent cross-site form submissions and WebSocket hijacking."""
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        raise web.HTTPForbidden(text="Cross-site proxy access is not allowed")
    supplied = request.headers.get("Origin") or request.headers.get("Referer")
    # HA's no-referrer policy can produce Origin: null on native POST forms.
    # Only browser-controlled same-origin metadata can replace that opaque origin.
    if supplied == "null" and request.headers.get("Sec-Fetch-Site") == "same-origin":
        return
    if supplied:
        try:
            matches = URL(supplied).origin() == request.url.origin()
        except ValueError:
            matches = False
        if not matches:
            raise web.HTTPForbidden(text="Invalid request origin")
    elif required:
        raise web.HTTPForbidden(text="A same-origin request is required")


@dataclass
class ProxySession:
    entry_id: str
    refresh_token_id: str
    user_id: str
    created: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    # Upstream cookies are unique even when another HA account uses the same browser.
    cookie_prefix: str = field(
        default_factory=lambda: f"bp_up_{secrets.token_hex(12)}_"
    )


class SessionStore:
    def __init__(self, hass):
        self.hass = hass
        self.sessions: dict[str, ProxySession] = {}

    def valid(self, session, entry_id, settings, request) -> bool:
        now = time.monotonic()
        if (
            session.entry_id != entry_id
            or now - session.created >= SESSION_MAX_SECONDS
            or now - session.touched >= SESSION_IDLE_SECONDS
        ):
            return False
        refresh = self.hass.auth.async_get_refresh_token(session.refresh_token_id)
        if refresh is None or refresh.user.id != session.user_id:
            return False
        if not has_access(refresh.user, settings):
            return False
        if async_user_not_allowed_do_auth(self.hass, refresh.user, request):
            return False
        try:
            self.hass.auth.async_validate_refresh_token(refresh, request.remote)
        except InvalidAuthError:
            return False
        return True

    def authenticate(self, token, entry_id, settings, request):
        session = self.sessions.get(token)
        if session is None or not self.valid(session, entry_id, settings, request):
            self.sessions.pop(token, None)
            raise web.HTTPUnauthorized(text="Open this resource from Home Assistant")
        session.touched = time.monotonic()
        return session

    def issue(self, entry_id, refresh_token_id, user_id):
        now = time.monotonic()
        for token, session in list(self.sessions.items()):
            if (
                now - session.created >= SESSION_MAX_SECONDS
                or now - session.touched >= SESSION_IDLE_SECONDS
            ):
                self.sessions.pop(token, None)
        if len(self.sessions) >= MAX_SESSIONS:
            raise web.HTTPServiceUnavailable(text="Too many proxy sessions")
        token = secrets.token_urlsafe(32)
        self.sessions[token] = ProxySession(entry_id, refresh_token_id, user_id)
        return token, self.sessions[token]

    def revoke_entry(self, entry_id):
        for token, session in list(self.sessions.items()):
            if session.entry_id == entry_id:
                self.sessions.pop(token, None)
