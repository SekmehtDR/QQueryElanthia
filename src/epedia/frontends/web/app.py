"""Local web chat: FastAPI app streaming answers over Server-Sent Events."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ...buildinfo import build_info, git_commit
from ...config import Settings
from ...core import memory
from ...core.assistant import Assistant, Done, Retrieval, Token
from ...db import connect
from ...jobs import JobBusy, JobManager, index_changes, index_info
from ...llm.ollama import Ollama
from ...progress import snapshot

STATIC = Path(__file__).parent / "static"

# Starter questions shown under the greeting, one per kind of question the assistant is good at.
CHIPS = [
    "How do I start forging?",
    "What shops are in Riverhaven?",
    "Who is Bobba Bandicoot?",
    "Who is Hodierna?",
    "Where do musk hogs live and how tough are they?",
    "What is the heaviest heavy edged weapon?",
]


class ChatRequest(BaseModel):
    session_id: str
    message: str


def _host_name(header: str) -> str:
    """The host part of a Host header: 'localhost:8765' -> 'localhost', '[::1]:8765' -> '::1'."""
    if header.startswith("["):
        return header[1:header.find("]")] if "]" in header else header[1:]
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


class StartRequest(BaseModel):
    mode: str  # "delta" (only what changed) or "full" (re-walk and compare every page)


def create_app(settings: Settings) -> FastAPI:
    state: dict = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        state["conn"] = connect(settings)
        state["ollama"] = Ollama(settings.ollama)
        state["assistant"] = Assistant(settings, state["conn"], state["ollama"])
        yield
        await jobs.stop()  # never leave an index job running with nobody watching it
        await state["ollama"].aclose()
        state["conn"].close()

    jobs = JobManager(settings)
    app = FastAPI(title="QQueryElanthia", lifespan=lifespan)
    app.state.jobs = jobs

    def guard(request: Request) -> None:
        """Index controls start real work against the wiki, so they are local-only and CSRF-safe.

        A custom header cannot be sent cross-site without a CORS preflight (which this app never grants), so a
        malicious web page cannot trigger a job through the visitor's browser; a mismatched Origin is refused too.
        If the server is bound beyond loopback, only loopback clients may use these endpoints."""
        if request.headers.get("x-qqe") != "1":
            raise HTTPException(403, "missing X-QQE header")
        host_header = request.headers.get("host", "")
        # DNS rebinding: a hostile page can make its own name resolve to 127.0.0.1, which would pass an
        # Origin == Host check. Only the names this server is really reached by are accepted.
        if _host_name(host_header) not in {"localhost", "127.0.0.1", "::1", settings.web.host}:
            raise HTTPException(403, "unexpected Host header")
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != host_header:
            raise HTTPException(403, "cross-site request refused")
        local_bind = settings.web.host in ("127.0.0.1", "localhost", "::1")
        peer = request.client.host if request.client else ""
        if not local_bind and peer not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "index controls are only available from this computer")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")  # Fennick's portraits

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    async def status() -> dict:
        return snapshot(state["conn"])

    @app.get("/api/index/info")
    async def index_state() -> dict:
        return {**index_info(state["conn"], settings), "job": jobs.snapshot()}

    @app.get("/api/index/changes/{sync_id}")
    async def index_run_changes(sync_id: int) -> dict:
        found = index_changes(state["conn"], settings, sync_id)
        if found is None:
            raise HTTPException(404, "no such run")
        return found

    @app.post("/api/index/start")
    async def index_start(request: Request, body: StartRequest) -> dict:
        guard(request)
        try:
            return await jobs.start(body.mode)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except JobBusy as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/index/stop")
    async def index_stop(request: Request) -> dict:
        guard(request)
        return await jobs.stop()

    async def build() -> dict:
        # `git rev-parse` is a subprocess (up to 3 s on a slow machine): run it off the event loop so a page load can
        # never stall a streaming answer. It is cached, so build_info's own database counts are then instant.
        await asyncio.to_thread(git_commit)
        return build_info(settings, state["conn"])

    @app.get("/api/version")
    async def version() -> dict:
        return await build()

    @app.get("/api/persona")
    async def persona() -> dict:
        p = settings.persona
        info = await build()
        if not p.enabled:
            return {"enabled": False, "name": "QQueryElanthia", "tagline": "answers from the wiki",
                    "greeting": "Ask a question about DragonRealms.", "chips": CHIPS, "build": info}
        return {
            "enabled": True,
            "build": info,
            "name": p.name,
            "tagline": "a traveler's notes on Elanthia, from Elanthipedia",
            "greeting": (f"Well met. I'm {p.name}. I carry a satchel of notes copied from Elanthipedia, the chronicle "
                         "the players keep of Elanthia. Ask me about crafting, places, people, creatures, "
                         "the gods and wars, or what a thing is worth."),
            "chips": CHIPS,
        }

    @app.post("/api/sessions")
    async def create_session() -> dict:
        return {"session_id": memory.new_session(state["conn"], "web")}

    @app.get("/api/sessions/{session_id}/messages")
    async def get_messages(session_id: str) -> list[dict]:
        return memory.messages(state["conn"], session_id)

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> StreamingResponse:
        if not req.message.strip():
            raise HTTPException(400, "empty message")

        async def events():
            def sse(name: str, data: dict) -> str:
                return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

            try:
                async for ev in state["assistant"].answer_stream(req.session_id, req.message.strip()):
                    if isinstance(ev, Retrieval):
                        yield sse("sources", {"sources": [s.__dict__ for s in ev.sources],
                                              "standalone_q": ev.standalone_q, "mode": ev.mode, "kind": ev.kind})
                    elif isinstance(ev, Token):
                        yield sse("token", {"text": ev.text})
                    elif isinstance(ev, Done):
                        # `answer` is the final text, including tags added for lines the model left uncited
                        yield sse("done", {"cited": ev.cited, "related": ev.related, "answer": ev.answer})
            except Exception as exc:  # surface model/db problems in the UI instead of a dropped stream
                yield sse("error", {"message": str(exc)})

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
