"""Who is asking: this computer, or someone reaching it through a tunnel.

A tunnel (Cloudflare, Tailscale, ngrok) connects to the server from this computer, so the peer address is always
loopback; what gives a tunnelled request away is the forwarding headers the tunnel adds. A request is "local" only if
it comes from loopback AND carries none of them. Local requests are the owner at the keyboard: no login, the Index
controls, their own saved chats. Everything else needs the access code, gets its own private chats, and cannot touch
the index.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import Request

PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded",
                 "x-real-ip", "cf-visitor", "tailscale-user-login")
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}  # "testclient" is what Starlette's test client reports
AUTH_COOKIE = "qqe_auth"
CLIENT_COOKIE = "qqe_client"
LOCAL_OWNER = "local"


def is_local(request: Request) -> bool:
    peer = request.client.host if request.client else ""
    return peer in LOOPBACK and not any(h in request.headers for h in PROXY_HEADERS)


def client_ip(request: Request) -> str:
    """The visitor's address as the tunnel reports it (only meaningful for a non-local request)."""
    fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-real-ip")
    if not fwd and request.headers.get("x-forwarded-for"):
        fwd = request.headers["x-forwarded-for"].split(",")[0].strip()
    return fwd or (request.client.host if request.client else "?")


def is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", "").lower() == "https" or '"scheme":"https"' in request.headers.get(
        "cf-visitor", "")


def auth_token(code: str) -> str:
    """What the login cookie holds: a keyed hash of the access code, so changing the code logs everyone out."""
    return hmac.new(code.encode("utf-8"), b"qqe-session-v1", hashlib.sha256).hexdigest()


def code_matches(supplied: str, code: str) -> bool:
    return hmac.compare_digest(supplied.encode("utf-8"), code.encode("utf-8"))


def has_valid_cookie(request: Request, code: str) -> bool:
    token = request.cookies.get(AUTH_COOKIE, "")
    return bool(token) and hmac.compare_digest(token, auth_token(code))


def owner_of(request: Request) -> str:
    """Who owns the chats started in this request: 'local' for the owner at this computer, else a per-browser id."""
    if is_local(request):
        return LOCAL_OWNER
    cid = request.cookies.get(CLIENT_COOKIE) or getattr(request.state, "client_id", None)
    return cid or "unknown"


def new_client_id() -> str:
    return secrets.token_hex(12)


class LoginLimiter:
    """At most `attempts` wrong access codes per address in `window` seconds, so a code cannot be guessed."""

    def __init__(self, attempts: int = 5, window: float = 600.0):
        self.attempts, self.window = attempts, window
        self._fails: dict[str, list[float]] = {}

    def blocked(self, ip: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._fails.get(ip, []) if now - t < self.window]
        self._fails[ip] = recent
        return len(recent) >= self.attempts

    def fail(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.monotonic())

    def clear(self, ip: str) -> None:
        self._fails.pop(ip, None)


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fennick · sign in</title>
<style>
:root{--bg:#fbf8f1;--ink:#2a2118;--muted:#7a6c5a;--line:#e3d9c6;--accent:#b5651d}
@media (prefers-color-scheme: dark){:root{--bg:#171410;--ink:#f1e9da;--muted:#a39580;--line:#3a3329;--accent:#e0a35a}}
body{margin:0;min-height:100dvh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif}
form{width:min(92vw,360px);padding:24px;border:1px solid var(--line);border-radius:14px}
h1{font-size:1.2rem;margin:0 0 4px}p{color:var(--muted);margin:0 0 16px;font-size:.92rem}
input,button{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;font:inherit;border:1px solid var(--line);background:transparent;color:var(--ink)}
button{margin-top:10px;background:var(--accent);border-color:var(--accent);color:var(--bg);cursor:pointer}
#err{color:#d24a3a;min-height:1.4em;margin-top:8px;font-size:.9rem}
</style></head><body>
<form id="f"><h1>Fennick</h1><p>Enter the access code you were given.</p>
<input id="c" type="password" autocomplete="current-password" autofocus aria-label="Access code">
<button type="submit">Come in</button><div id="err" role="alert"></div></form>
<script>
document.getElementById('f').addEventListener('submit', async e => {
  e.preventDefault();
  const r = await fetch('/login', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({code: document.getElementById('c').value})});
  if (r.ok) location.reload();
  else document.getElementById('err').textContent = r.status === 429 ? 'Too many tries. Wait a few minutes.' : 'That code is not right.';
});
</script></body></html>
"""
