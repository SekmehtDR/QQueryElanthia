"""Local web chat: FastAPI app streaming answers over Server-Sent Events."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ...buildinfo import build_info, git_commit
from ...config import Settings
from ...core import memory
from ...core.assistant import Assistant, Done, Retrieval, Token
from ...db import connect
from ...index.reset import clear_index
from ...jobs import (
    JobBusy,
    JobManager,
    find_reindex_pids,
    index_changes,
    index_info,
    kill_pid_tree,
    mark_reindex_interrupted,
    reindex_status,
)
from ...llm.ollama import Ollama
from ...progress import snapshot
from . import access

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


class LoginRequest(BaseModel):
    code: str


def _host_name(header: str) -> str:
    """The host part of a Host header: 'localhost:8765' -> 'localhost', '[::1]:8765' -> '::1'."""
    if header.startswith("["):
        return header[1:header.find("]")] if "]" in header else header[1:]
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


class ClearRequest(BaseModel):
    confirm: str  # must be the word CLEAR: this empties the search index


class StartRequest(BaseModel):
    mode: str  # "delta" (only what changed) or "full" (re-walk and compare every page)


def create_app(settings: Settings) -> FastAPI:
    state: dict = {"locks": {}, "active": set()}  # session id -> lock; client ids with an answer in progress
    limiter = access.LoginLimiter()
    turns = asyncio.Semaphore(max(1, settings.web.max_concurrent))  # answers written at once; the rest queue

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

    @app.middleware("http")
    async def sharing(request: Request, call_next):
        """Visitors through a tunnel need the access code and get their own browser id; the owner at this computer
        needs neither (see access.py for what makes a request local)."""
        local = access.is_local(request)
        code = settings.web.access_code
        path = request.url.path
        public = path.startswith("/static/") or path == "/login"
        if not local and not code:
            # Fail closed: a request that came through a tunnel while no access code is set would otherwise get in freely.
            return JSONResponse({"detail": "sharing is switched off: the owner has not set web.access_code"}, status_code=403)
        if not local and code and not public and not access.has_valid_cookie(request, code):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "login required"}, status_code=401)
            return HTMLResponse(access.LOGIN_PAGE, status_code=401)
        if not local and not request.cookies.get(access.CLIENT_COOKIE):
            request.state.client_id = access.new_client_id()
        response = await call_next(request)
        new_id = getattr(request.state, "client_id", None)
        if new_id:
            response.set_cookie(access.CLIENT_COOKIE, new_id, max_age=365 * 86400, httponly=True, samesite="lax",
                                secure=access.is_https(request))
        return response

    def local_only(request: Request) -> None:
        """The Index panel, jobs and status are for the owner at this computer, never for visitors through a tunnel."""
        if not access.is_local(request):
            raise HTTPException(403, "only available from the computer running the server")

    @app.post("/login")
    async def login(request: Request, body: LoginRequest) -> JSONResponse:
        code = settings.web.access_code
        ip = access.client_ip(request)
        if not code:
            return JSONResponse({"ok": True})
        if limiter.blocked(ip):
            return JSONResponse({"detail": "too many attempts"}, status_code=429)
        if not access.code_matches(body.code, code):
            limiter.fail(ip)
            return JSONResponse({"detail": "wrong code"}, status_code=401)
        limiter.clear(ip)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(access.AUTH_COOKIE, access.auth_token(code), max_age=30 * 86400, httponly=True,
                        samesite="lax", secure=access.is_https(request))
        return resp

    def csrf(request: Request) -> None:
        """State-changing calls must come from this page: a custom header cannot be sent cross-site without a CORS
        preflight (which this app never grants), and a mismatched Origin is refused too."""
        if request.headers.get("x-qqe") != "1":
            raise HTTPException(403, "missing X-QQE header")
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            raise HTTPException(403, "cross-site request refused")

    def guard(request: Request) -> None:
        """Index controls start real work against the wiki, so they are for the owner at this computer only, and CSRF-safe.

        On top of csrf(): only requests that really come from this computer (no tunnel headers), and only under the
        names this server is reached by (DNS rebinding: a hostile page can make its own name resolve to 127.0.0.1,
        which would pass an Origin == Host check). If the server is bound beyond loopback, only loopback clients."""
        local_only(request)
        csrf(request)
        if _host_name(request.headers.get("host", "")) not in {"localhost", "127.0.0.1", "::1", settings.web.host}:
            raise HTTPException(403, "unexpected Host header")
        local_bind = settings.web.host in ("127.0.0.1", "localhost", "::1")
        peer = request.client.host if request.client else ""
        if not local_bind and peer not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "index controls are only available from this computer")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")  # Fennick's portraits

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    async def status(request: Request) -> dict:
        local_only(request)
        return snapshot(state["conn"])

    @app.get("/api/index/info")
    async def index_state(request: Request) -> dict:
        local_only(request)
        return {**index_info(state["conn"], settings), "job": jobs.snapshot()}

    @app.get("/api/index/changes/{sync_id}")
    async def index_run_changes(request: Request, sync_id: int) -> dict:
        local_only(request)
        found = index_changes(state["conn"], settings, sync_id)
        if found is None:
            raise HTTPException(404, "no such run")
        return found

    @app.post("/api/index/start")
    async def index_start(request: Request, body: StartRequest) -> dict:
        guard(request)
        running = reindex_status(state["conn"])
        if running and running["state"] == "running" and not jobs.running:
            # one started from the command line: two writers re-chunking the same index would only fight
            raise HTTPException(409, "a re-index is already running (started outside this page)")
        try:
            return await jobs.start(body.mode)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except JobBusy as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/index/clear")
    async def index_clear(request: Request, body: ClearRequest) -> dict:
        guard(request)
        if body.confirm != "CLEAR":
            raise HTTPException(400, "type CLEAR to confirm")
        running = reindex_status(state["conn"])
        if jobs.running or (running and running["state"] == "running"):
            raise HTTPException(409, "a job or re-index is running; stop it first")

        def clear() -> dict:  # its own connection: a long transaction on the shared one would swallow chat's queries
            conn = connect(settings)
            try:
                return clear_index(conn)
            finally:
                conn.close()

        return await asyncio.to_thread(clear)

    @app.post("/api/index/stop")
    async def index_stop(request: Request) -> dict:
        guard(request)
        snap = await jobs.stop()
        running = reindex_status(state["conn"])
        if not snap["running"] and not snap["stopped"] and running and running["state"] == "running":
            # a re-index started outside this page (a terminal, a scheduled task): the page can stop it too
            pids = await asyncio.to_thread(find_reindex_pids)
            for pid in pids:
                await asyncio.to_thread(kill_pid_tree, pid)
            mark_reindex_interrupted(state["conn"])
            return {**snap, "external_stopped": len(pids)}
        return snap

    async def build() -> dict:
        # `git rev-parse` is a subprocess (up to 3 s on a slow machine): run it off the event loop so a page load can
        # never stall a streaming answer. It is cached, so build_info's own database counts are then instant.
        await asyncio.to_thread(git_commit)
        return build_info(settings, state["conn"])

    def public_build(info: dict) -> dict:
        return {"version": info.get("version"), "commit": info.get("commit")}

    @app.get("/api/version")
    async def version(request: Request) -> dict:
        info = await build()
        return info if access.is_local(request) else public_build(info)

    @app.get("/api/persona")
    async def persona(request: Request) -> dict:
        p = settings.persona
        local = access.is_local(request)
        info = await build()
        info = info if local else public_build(info)
        if not p.enabled:
            return {"enabled": False, "name": "QQueryElanthia", "tagline": "answers from the wiki",
                    "greeting": "Ask a question about DragonRealms.", "chips": CHIPS, "build": info, "local": local}
        return {
            "enabled": True,
            "local": local,  # the page hides the Index panel from visitors
            "build": info,
            "name": p.name,
            "tagline": "a traveler's notes on Elanthia, from Elanthipedia",
            "greeting": (f"Hey, I'm {p.name}. I carry a satchel of notes copied from Elanthipedia, the wiki the players "
                         "write about Elanthia. Ask me about crafting, places, people, creatures, "
                         "the gods and wars, or what something's worth."),
            "chips": CHIPS,
        }

    @app.post("/api/sessions")
    async def create_session(request: Request) -> dict:
        return {"session_id": memory.new_session(state["conn"], "web", owner=access.owner_of(request))}

    @app.get("/api/sessions")
    async def list_sessions(request: Request) -> list[dict]:
        return memory.list_sessions(state["conn"], "web", owner=access.owner_of(request))

    @app.get("/api/sessions/{session_id}/messages")
    async def get_messages(request: Request, session_id: str) -> dict:
        if not memory.owned_by(state["conn"], session_id, access.owner_of(request)):
            raise HTTPException(404, "no such conversation")
        return {"messages": memory.messages(state["conn"], session_id),
                "topic": memory.get_topic(state["conn"], session_id)}

    @app.post("/api/sessions/{session_id}/forget-context")
    async def forget_context(request: Request, session_id: str) -> dict:
        csrf(request)
        if not memory.owned_by(state["conn"], session_id, access.owner_of(request)):
            raise HTTPException(404, "no such conversation")
        memory.clear_context(state["conn"], session_id)
        return {"topic": None}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(request: Request, session_id: str) -> dict:
        csrf(request)
        if not memory.owned_by(state["conn"], session_id, access.owner_of(request)):
            raise HTTPException(404, "no such conversation")
        memory.delete_session(state["conn"], session_id)
        return {"deleted": True}

    @app.post("/api/chat")
    async def chat(request: Request, req: ChatRequest) -> StreamingResponse:
        if not req.message.strip():
            raise HTTPException(400, "empty message")
        if len(req.message) > settings.web.max_message_chars:
            raise HTTPException(413, f"message too long (the limit is {settings.web.max_message_chars:,} characters)")
        owner = access.owner_of(request)
        if owner in state["active"]:
            raise HTTPException(429, "still answering your last question")

        async def events():
            def sse(name: str, data: dict) -> str:
                return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

            sid = req.session_id
            if not memory.owned_by(state["conn"], sid, owner):
                # an unknown id (a deleted chat, a restored tab) or someone else's: start a fresh conversation
                sid = memory.new_session(state["conn"], "web", owner=owner)
                yield sse("session", {"session_id": sid})
            lock = state["locks"].setdefault(sid, asyncio.Lock())  # one answer at a time per conversation
            state["active"].add(owner)
            try:
                async with turns, lock:
                    async for ev in state["assistant"].answer_stream(sid, req.message.strip()):
                        if isinstance(ev, Retrieval):
                            yield sse("sources", {"sources": [s.__dict__ for s in ev.sources],
                                                  "standalone_q": ev.standalone_q, "mode": ev.mode, "kind": ev.kind})
                        elif isinstance(ev, Token):
                            yield sse("token", {"text": ev.text})
                        elif isinstance(ev, Done):
                            # `answer` is the final text, including tags added for lines the model left uncited
                            yield sse("done", {"cited": ev.cited, "related": ev.related, "answer": ev.answer,
                                               "topic": ev.topic})
            except Exception as exc:  # surface model/db problems in the UI instead of a dropped stream
                yield sse("error", {"message": str(exc)})
            finally:
                state["active"].discard(owner)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
