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


FALLBACK_ERRORS = ("transport", "maxlag", "ratelimited")  # the source is unreachable or overloaded
MISSING_ERRORS = ("missingtitle", "nosuchpageid")  # a lagging copy may simply not have the page yet
BREAKER_FAILURES = 3
BREAKER_SECONDS = 300.0


class FailoverClient:
    """A preferred wiki source with a second one behind it, so a crawl keeps going when the first has trouble.

    The preferred source (for example a fast mirror) gets one quick retry only; after a few failures in a row it is
    skipped for a few minutes so a dead mirror costs seconds, not a two-minute backoff per page. A page the
    preferred source says is missing is asked of the second source before it is treated as deleted, because a
    mirror that lags would otherwise make every brand-new page look removed. The second source keeps its own rate
    limit and full retry ladder."""

    def __init__(self, cfg: WikiCfg, *, rps: float | None = None, concurrency: int | None = None,
                 retries: int | None = None):
        if not cfg.fallback_source or cfg.fallback_source == cfg.source:
            raise ValueError("FailoverClient needs a fallback source different from the active one")
        self.cfg = cfg
        self.primary = WikiClient(cfg, rps=rps, concurrency=concurrency, retries=1 if retries is None else retries)
        self.secondary = WikiClient(cfg.model_copy(update={"source": cfg.fallback_source, "fallback_source": None}),
                                    retries=retries)
        self.api_url = self.primary.api_url
        self._failures = 0
        self._skip_until = 0.0
        self.fallbacks = 0

    @property
    def requests(self) -> int:
        return self.primary.requests + self.secondary.requests

    async def __aenter__(self) -> "FailoverClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.primary.aclose()
        await self.secondary.aclose()

    async def get(self, params: dict[str, Any]) -> dict[str, Any]:
        if time.monotonic() >= self._skip_until:
            try:
                data = await self.primary.get(params)
                self._failures = 0
                return data
            except WikiError as exc:
                if exc.code in FALLBACK_ERRORS:
                    self._failures += 1
                    if self._failures >= BREAKER_FAILURES:
                        self._skip_until = time.monotonic() + BREAKER_SECONDS
                        log.warning("preferred source failed %d times in a row; using the fallback for %.0fs",
                                    self._failures, BREAKER_SECONDS)
                elif exc.code not in MISSING_ERRORS:
                    raise
        self.fallbacks += 1
        return await self.secondary.get(params)

    async def paginate(self, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        p = dict(params)
        while True:
            data = await self.get(p)
            yield data
            cont = data.get("continue")
            if not cont:
                return
            p.update(cont)


def open_client(cfg: WikiCfg, **kw: Any) -> "WikiClient | FailoverClient":
    """The client for the configured source: with a fallback when the settings name one."""
    if cfg.fallback_source and cfg.fallback_source != cfg.source:
        return FailoverClient(cfg, **kw)
    return WikiClient(cfg, **kw)
