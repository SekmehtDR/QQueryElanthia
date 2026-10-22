"""Local web chat: FastAPI app streaming answers over Server-Sent Events."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ...config import Settings
from ...core import memory
from ...core.assistant import Assistant, Done, Retrieval, Token
from ...db import connect
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


def create_app(settings: Settings) -> FastAPI:
    state: dict = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        state["conn"] = connect(settings)
        state["ollama"] = Ollama(settings.ollama)
        state["assistant"] = Assistant(settings, state["conn"], state["ollama"])
        yield
        await state["ollama"].aclose()
        state["conn"].close()

    app = FastAPI(title="epedia", lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    async def status() -> dict:
        return snapshot(state["conn"])

    @app.get("/api/persona")
    async def persona() -> dict:
        p = settings.persona
        if not p.enabled:
            return {"enabled": False, "name": "QQueryElanthia", "tagline": "answers from the wiki",
                    "greeting": "Ask a question about DragonRealms.", "chips": CHIPS}
        return {
            "enabled": True,
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
                        yield sse("done", {"cited": ev.cited, "related": ev.related})
            except Exception as exc:  # surface model/db problems in the UI instead of a dropped stream
                yield sse("error", {"message": str(exc)})

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
