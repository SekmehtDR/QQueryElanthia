"""Polite MediaWiki API client: rate limiting, maxlag, retries, continuation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from ..config import WikiCfg

log = logging.getLogger("epedia.wiki")

BACKOFF = [2, 4, 8, 16, 32, 60]


class WikiError(Exception):
    def __init__(self, code: str, info: str = ""):
        super().__init__(f"{code}: {info}")
        self.code = code
        self.info = info


class TokenBucket:
    """Simple async rate limiter: at most `rps` acquisitions per second, smoothed."""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps if rps > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next = max(now, self._next) + self.interval


class WikiClient:
    def __init__(
        self,
        cfg: WikiCfg,
        *,
        rps: float | None = None,
        concurrency: int | None = None,
        retries: int | None = None,
    ):
        """retries: how many backoff retries to allow (default: the full ladder, ~2 minutes, for background work;
        interactive callers pass a small number so they can fall back quickly)."""
        self.cfg = cfg
        self.api_url = cfg.api_url
        self.backoff = BACKOFF if retries is None else BACKOFF[: max(retries, 0)]
        self.bucket = TokenBucket(rps if rps is not None else cfg.rps)
        self.sem = asyncio.Semaphore(concurrency if concurrency is not None else cfg.concurrency)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": cfg.user_agent, "Accept-Encoding": "gzip"},
            timeout=httpx.Timeout(60.0, connect=15.0),
            http2=False,
        )
        self.requests = 0

    async def __aenter__(self) -> "WikiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, params: dict[str, Any]) -> dict[str, Any]:
        p: dict[str, Any] = {"format": "json", "formatversion": 2, "maxlag": self.cfg.maxlag}
        p.update({k: v for k, v in params.items() if v is not None})
        last_error = "no response"
        for attempt in range(len(self.backoff) + 1):
            try:
                await self.bucket.acquire()
                async with self.sem:
                    self.requests += 1
                    resp = await self._client.get(self.api_url, params=p)
                if resp.status_code in (429, 503):
                    last_error = f"HTTP {resp.status_code}"
                    if attempt >= len(self.backoff):
                        break
                    delay = _retry_after(resp, default=10.0)
                    log.warning("HTTP %s from wiki; sleeping %.0fs", resp.status_code, delay)
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    code = data["error"].get("code", "unknown")
                    info = data["error"].get("info", "")
                    if code in ("maxlag", "ratelimited"):
                        last_error = code
                        if attempt >= len(self.backoff):
                            break
                        delay = max(_retry_after(resp, default=5.0), 5.0) if code == "maxlag" else 30.0
                        log.warning("%s: %s; sleeping %.0fs", code, info, delay)
                        await asyncio.sleep(delay)
                        continue
                    raise WikiError(code, info)
                if "warnings" in data:
                    log.debug("API warnings: %s", json.dumps(data["warnings"])[:500])
                return data
            except (httpx.TransportError, httpx.HTTPStatusError, json.JSONDecodeError) as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
                if attempt >= len(self.backoff):
                    break
                delay = self.backoff[attempt]
                log.warning("wiki request failed (%s); retry in %ss", exc.__class__.__name__, delay)
                await asyncio.sleep(delay)
        raise WikiError("transport", last_error)

    async def paginate(self, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        p = dict(params)
        while True:
            data = await self.get(p)
            yield data
            cont = data.get("continue")
            if not cont:
                return
            p.update(cont)


def _retry_after(resp: httpx.Response, default: float) -> float:
    value = resp.headers.get("Retry-After")
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default
