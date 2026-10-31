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


class TurnLimiter:
    """At most `per_hour` questions per visitor in any hour, so one person cannot keep the only GPU busy for everyone.
    0 means no limit. Kept in memory: a restart forgives everyone, which is fine for a courtesy limit."""

    def __init__(self, per_hour: int, window: float = 3600.0):
        self.per_hour, self.window = per_hour, window
        self._asked: dict[str, list[float]] = {}

    def allow(self, who: str) -> bool:
        """Count a question from `who` and say whether it may be answered."""
        if self.per_hour <= 0:
            return True
        now = time.monotonic()
        recent = [t for t in self._asked.get(who, []) if now - t < self.window]
        if len(recent) >= self.per_hour:
            self._asked[who] = recent
            return False
        self._asked[who] = [*recent, now]
        return True

    def wait_minutes(self, who: str) -> int:
        """Minutes until `who` may ask again (at least 1)."""
        recent = self._asked.get(who) or [time.monotonic()]
        return max(1, int((self.window - (time.monotonic() - min(recent))) // 60) + 1)


# What a visitor through the tunnel sees first: Fennick himself. His portrait is under /static/, which needs no code.
LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fennick · come in</title>
<style>
:root{--bg:#e9e2d0;--paper:#fbf5e6;--ink:#3b2f22;--muted:#7a6a52;--edge:#d8c8a4;--accent:#b5651d;--on-accent:#fffaf0;--bad:#b23a2a}
@media (prefers-color-scheme: dark){:root{--bg:#16140f;--paper:#2a241a;--ink:#efe6d2;--muted:#b3a58c;--edge:#4a3f2c;--accent:#d9a066;--on-accent:#1f1a12;--bad:#f0907f}}
*{box-sizing:border-box}
body{margin:0;min-height:100dvh;display:grid;place-items:center;padding:16px;background:var(--bg);color:var(--ink);font:16px/1.55 Georgia,'Iowan Old Style',serif}
main{width:min(100%,460px);padding:28px 28px 24px;background:var(--paper);border:1px solid var(--edge);border-radius:8px;box-shadow:0 10px 30px rgba(40,30,15,.22)}
.portrait{display:block;width:150px;height:150px;margin:0 auto 14px;border-radius:50%;border:3px solid var(--edge);object-fit:cover}
h1{margin:0;text-align:center;font-size:1.55rem;font-weight:700}
.sub{margin:2px 0 16px;text-align:center;color:var(--muted);font-style:italic;font-size:.95rem}
.says{position:relative;margin:0 0 16px;padding:12px 14px;border:1px solid var(--edge);border-radius:10px}
.says::before{content:"";position:absolute;top:-8px;left:50%;width:14px;height:14px;margin-left:-7px;background:var(--paper);border-left:1px solid var(--edge);border-top:1px solid var(--edge);transform:rotate(45deg)}
.says p{margin:0}.says p+p{margin-top:.6em}
form{font-family:system-ui,sans-serif}
.how{margin:0 0 12px;padding-left:1.3em;font-size:.92rem}.how li{margin:.25em 0}
label{display:block;margin-bottom:4px;font-size:.85rem;font-weight:600;color:var(--muted)}
input,button{width:100%;padding:11px 12px;border-radius:8px;font:inherit}
input{border:1px solid var(--edge);background:transparent;color:var(--ink)}
input:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button{margin-top:10px;border:1px solid var(--accent);background:var(--accent);color:var(--on-accent);font-weight:600;cursor:pointer}
button[disabled]{opacity:.65;cursor:wait}
#err{margin-top:12px;font-family:Georgia,'Iowan Old Style',serif;color:var(--bad)}#err:empty{display:none}
</style></head><body>
<main>
<img class="portrait" src="/static/fennick_large.png" alt="Fennick, a Halfling Bard in a green feathered cap, with his lute" width="150" height="150">
<h1>Hello there, come on in!</h1>
<p class="sub">Halfling, Bard, keeper of an alarmingly heavy satchel</p>
<div class="says">
<p>I'm Fennick. I carry a satchel of notes on all of Elanthia, and I'm glad of any excuse to open it. I just need your access code first. I don't make the rules. Well, Sekmeht makes the rules.</p>
</div>
<form id="f">
<ol class="how">
<li>Enter your access code below.</li>
<li>If it isn't accepted, please reach out to Sekmeht for the current valid access code.</li>
</ol>
<label for="c">Access code</label>
<input id="c" type="password" autocomplete="current-password" autofocus required>
<button id="go" type="submit">Come in</button>
<div id="err" role="alert"></div>
</form>
</main>
<script>
const err = document.getElementById('err'), code = document.getElementById('c'), go = document.getElementById('go');
document.getElementById('f').addEventListener('submit', async e => {
  e.preventDefault();
  err.textContent = ''; go.disabled = true;
  try {
    const r = await fetch('/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({code: code.value})});
    if (r.ok) { location.reload(); return; }
    err.textContent = r.status === 429
      ? "That's a lot of tries, friend. Let's both rest a few minutes. If it still won't open after that, please reach out to Sekmeht for the current valid access code."
      : "Hmm, that one doesn't open my satchel. The code may have changed. Please reach out to Sekmeht for the current valid access code.";
    code.select();
  } catch (x) {
    err.textContent = "I can't seem to reach my satchel just now. Give it a moment and try again.";
  }
  go.disabled = false;
});
</script></body></html>
"""
