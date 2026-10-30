"""Thin async client for the Ollama HTTP API (embed, chat streaming, JSON-schema chat, pull)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Callable

import httpx

from ..config import OllamaCfg

log = logging.getLogger("epedia.ollama")


class OllamaError(Exception):
    pass


class Ollama:
    def __init__(self, cfg: OllamaCfg):
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            timeout=httpx.Timeout(600.0, connect=10.0),
        )

    async def __aenter__(self) -> "Ollama":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def alive(self) -> bool:
        try:
            r = await self._client.get("/api/version")
            return r.status_code == 200
        except httpx.TransportError:
            return False

    async def version(self) -> str:
        r = await self._client.get("/api/version")
        r.raise_for_status()
        return r.json().get("version", "?")

    async def list_models(self) -> list[str]:
        r = await self._client.get("/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    async def has_model(self, name: str) -> bool:
        want = name if ":" in name else f"{name}:latest"
        return want in await self.list_models()

    async def pull(self, name: str, progress: Callable[[str, int, int], None] | None = None) -> None:
        async with self._client.stream("POST", "/api/pull", json={"model": name, "stream": True}) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                if "error" in msg:
                    raise OllamaError(msg["error"])
                if progress:
                    progress(msg.get("status", ""), msg.get("completed", 0) or 0, msg.get("total", 0) or 0)

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        payload = {
            "model": model or self.cfg.embed_model,
            "input": texts,
            "truncate": True,
            "keep_alive": self.cfg.keep_alive,
        }
        for attempt in range(4):
            try:
                r = await self._client.post("/api/embed", json=payload)
                r.raise_for_status()
                data = r.json()
                vectors = data.get("embeddings")
                if not isinstance(vectors, list) or len(vectors) != len(texts):
                    raise OllamaError(f"embed returned {len(vectors) if vectors else 0} vectors for {len(texts)} inputs")
                return vectors
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == 3:
                    raise OllamaError(f"embed failed: {exc}") from exc
                delay = 2 ** (attempt + 1)
                log.warning("embed failed (%s); retry in %ss", exc.__class__.__name__, delay)
                await asyncio.sleep(delay)
        raise OllamaError("unreachable")

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        num_ctx: int | None = None,
        think: bool = False,
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": model or self.cfg.chat_model,
            "messages": messages,
            "stream": True,
            "think": think,
            "keep_alive": self.cfg.keep_alive,
            "options": {
                "num_ctx": num_ctx or self.cfg.num_ctx,
                **({"temperature": temperature} if temperature is not None else {}),
            },
        }
        attempts = 2 if self.cfg.retry_before_first_token else 1
        for attempt in range(attempts):
            started = False
            try:
                async for content in self._stream_once(payload):
                    started = True
                    yield content
                return
            except (OllamaError, httpx.TransportError) as exc:
                if started or attempt == attempts - 1:
                    raise exc if isinstance(exc, OllamaError) else OllamaError(f"chat failed: {exc!r}") from exc
                log.warning("chat failed before the first token (%s); trying once more", exc.__class__.__name__)
                await asyncio.sleep(1.0)

    async def _stream_once(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        async with self._client.stream("POST", "/api/chat", json=payload) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode("utf-8", "replace")
                raise OllamaError(f"chat HTTP {r.status_code}: {body[:300]}")
            async for line in r.aiter_lines():
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise OllamaError(f"chat returned a line that is not JSON: {line[:200]}") from exc
                if "error" in msg:
                    raise OllamaError(msg["error"])
                content = msg.get("message", {}).get("content", "")
                if content:
                    yield content
                if msg.get("done"):
                    break

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        model: str | None = None,
        num_ctx: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or self.cfg.chat_model,
            "messages": messages,
            "stream": False,
            "think": False,
            "format": schema,
            "keep_alive": self.cfg.keep_alive,
            "options": {"num_ctx": num_ctx or min(self.cfg.num_ctx, 8192), "temperature": 0},
        }
        try:
            r = await self._client.post("/api/chat", json=payload)
        except httpx.TransportError as exc:  # Ollama not running, or it dropped the connection: the caller falls back
            raise OllamaError(f"chat failed: {exc!r}") from exc
        if r.status_code != 200:
            raise OllamaError(f"chat HTTP {r.status_code}: {r.text[:300]}")
        try:
            content = r.json().get("message", {}).get("content", "")
            data = json.loads(content)
        except (json.JSONDecodeError, AttributeError) as exc:
            raise OllamaError(f"model returned non-JSON: {r.text[:200]}") from exc
        if not isinstance(data, dict):
            raise OllamaError(f"model returned JSON that is not an object: {content[:200]}")
        return data
