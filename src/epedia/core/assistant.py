"""The answering core: plan (rewrite + route) -> retrieve -> stream a grounded answer. Front ends consume events."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..config import Settings
from ..kinds import OFFER_EXCLUDED_PAGE_KINDS, OFFER_MIN_WEIGHT, QUESTION_KINDS
from ..llm import prompts
from ..llm.ollama import Ollama, OllamaError
from ..retrieval.search import Hit, search
from ..util import page_url
from . import memory
from .attribution import attribute

log = logging.getLogger("epedia.assistant")

MIN_OFFER_INLINKS = 8  # a page must be linked from at least this many pages to be offered as a thread to pull on


@dataclass
class Source:
    n: int
    title: str
    section: str
    url: str
    chunk_id: int | None = None
    snippet: str = ""  # opening of the passage, shown when hovering a citation


def _snippet(text: str, limit: int = 260) -> str:
    flat = re.sub(r"\s+", " ", prompts.clean_passage_text(text)).strip()
    flat = re.sub(r"^\((?:Marked|marked)[^)]*\)\s*", "", flat)  # the maintenance note is not the passage's content
    return flat if len(flat) <= limit else flat[:limit].rsplit(" ", 1)[0] + "…"


PARTIAL_NOTE = "I couldn't find every part of that in the chronicle."


def soften_refusal(answer: str) -> str:
    """Replace a 'nothing on that' line that was tacked onto an answer that has real content.

    That line is meant to be the whole reply when no passage is relevant (the program emits it itself in that case).
    A small model sometimes appends it after a good answer, which contradicts everything above it; or it may have
    meant "one detail is missing". Deleting it could make a partial answer look complete, so it becomes an honest
    caveat that is true in both cases."""
    for line in (prompts.NO_PASSAGES_ANSWER, "The wiki doesn't seem to cover that."):
        if line in answer and len(answer.replace(line, "").strip()) >= 80:
            return answer.replace(line, PARTIAL_NOTE)
    return answer


@dataclass
class Retrieval:
    sources: list[Source]
    standalone_q: str
    mode: str
    kind: str = "general"
    related: list[str] = field(default_factory=list)


@dataclass
class Token:
    text: str


@dataclass
class Done:
    answer: str
    cited: list[int]
    sources: list[Source]
    related: list[str] = field(default_factory=list)


Event = Retrieval | Token | Done


@dataclass
class Plan:
    standalone_question: str
    mode: str = "search"
    items_query: dict[str, Any] | None = None
    follow_up: bool = False  # depends on earlier turns: bring the previous answer's source passages along
    kind: str = "general"  # what the question is about; steers retrieval weights and the answer's shape


class Assistant:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, ollama: Ollama):
        self.s = settings
        self.conn = conn
        self.ollama = ollama

    @property
    def name(self) -> str:
        return self.s.persona.name

    async def plan(self, hist: list[dict[str, str]], message: str, session_id: str | None = None) -> Plan:
        catalog = self._property_catalog()
        system = prompts.ROUTER_SYSTEM.replace("{property_catalog}", catalog or "(no item properties indexed yet)")
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in hist) or "(no earlier messages)"
        user = f"Conversation so far:\n{convo}\n"
        last_pages = self._prior_titles(session_id) if hist else []
        if last_pages:
            user += "\nPages used in the last answer:\n" + "\n".join(f"- {t}" for t in last_pages) + "\n"
        user += f"\nLatest user message: {message}"
        try:
            data = await self.ollama.chat_json(
                [{"role": "system", "content": system}, {"role": "user", "content": user}], prompts.ROUTER_SCHEMA
            )
            q = (data.get("standalone_question") or message).strip() or message
            mode = data.get("mode") if data.get("mode") in ("search", "items") else "search"
            iq = data.get("items_query") if mode == "items" else None
            if mode == "items" and not catalog:
                mode, iq = "search", None
            kind = data.get("kind") if data.get("kind") in QUESTION_KINDS else "general"
            if mode == "items":
                kind = "item"
            follow_up = bool(hist) and bool(data.get("follow_up")) and kind != "meta"
            return Plan(q, mode, iq, follow_up=follow_up, kind=kind)
        except (OllamaError, KeyError, ValueError) as exc:
            log.warning("planner failed (%s); falling back to plain search", exc)
            return Plan(message)

    def _property_catalog(self) -> str:
        rows = self.conn.execute(
            "SELECT prop, value_type, coalesce(description, label, '') AS d, sample_values FROM smw_properties "
            "WHERE page_count >= 100 ORDER BY page_count DESC LIMIT 60"
        ).fetchall()
        return "\n".join(f"- {r['prop']}: {r['value_type']}, {r['d'] or r['sample_values'] or ''}"[:160] for r in rows)

    def _prior_sources(self, session_id: str | None) -> list[dict]:
        """Sources of the last assistant turn in this session, in their original order."""
        if not session_id:
            return []
        row = self.conn.execute(
            "SELECT sources FROM messages WHERE session_id=? AND role='assistant' ORDER BY msg_id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if not row or not row["sources"]:
            return []
        try:
            return json.loads(row["sources"])
        except (ValueError, TypeError):
            return []

    def _prior_chunk_ids(self, session_id: str | None) -> list[int]:
        return [s["chunk_id"] for s in self._prior_sources(session_id) if s.get("chunk_id")]

    def _prior_titles(self, session_id: str | None) -> list[str]:
        seen: list[str] = []
        for s in self._prior_sources(session_id):
            if s["title"] not in seen:
                seen.append(s["title"])
        return seen[:6]

    def _prior_passages(self, session_id: str | None, limit: int = 5) -> list[dict]:
        """The previous answer's own passages, so a follow-up is answered from (and cites) the same pages."""
        out: list[dict] = []
        for cid in self._prior_chunk_ids(session_id)[:limit]:
            r = self.conn.execute(
                "SELECT c.chunk_id, c.title, c.section_path, c.anchor, c.text FROM chunks c WHERE c.chunk_id=?",
                (cid,),
            ).fetchone()
            if r:
                out.append({"title": r["title"], "section": r["section_path"], "text": r["text"],
                            "url": page_url(self.s.wiki.base_url, r["title"], r["anchor"]), "chunk_id": r["chunk_id"]})
        return out

    # ------------------------------------------------------------------------------------ retrieval

    async def retrieve(self, plan: Plan, session_id: str | None = None) -> list[dict]:
        """Passages for the answer as dicts (title, section, url, text, chunk_id)."""
        return (await self.retrieve_with_extras(plan, session_id))[0]

    async def retrieve_with_extras(
        self, plan: Plan, session_id: str | None = None
    ) -> tuple[list[dict], list[dict]]:
        """(passages for the answer, runner-up hits that did not make the cut; used for 'related pages')."""
        if plan.kind == "meta":
            return self._meta_passages(), []
        passages: list[dict] = []
        k = self.s.retrieval.k_final
        if plan.mode == "items" and plan.items_query:
            try:
                from .items_tool import items_passage

                p = await items_passage(self.s, self.conn, plan.items_query)
                if p:
                    passages.append(p)
                    k = 4
            except Exception as exc:  # the items tool is best-effort; ordinary search still answers
                log.warning("items tool failed: %s", exc)
        prior: list[dict] = self._prior_passages(session_id) if plan.follow_up else []
        passages.extend(prior)
        have = {p["chunk_id"] for p in prior}
        extra_k = self.s.retrieval.related_k if self.s.retrieval.offer_related else 0
        qvec = (await self.ollama.embed(["search_query: " + plan.standalone_question]))[0]
        hits: list[Hit] = search(
            self.conn, plan.standalone_question, self.s.retrieval, self.s.wiki.base_url, qvec, k_final=k + extra_k,
            prior_chunk_ids=self._prior_chunk_ids(session_id) if plan.follow_up else None, kind=plan.kind,
        )
        extras: list[dict] = []
        taken = 0
        for h in hits:
            if h.chunk_id in have:
                continue
            item = {"title": h.title, "section": h.section_path, "url": h.url, "text": h.text,
                    "chunk_id": h.chunk_id, "page_kind": h.page_kind}
            if taken < k:
                passages.append(item)
                taken += 1
            else:
                extras.append(item)
        return passages, extras

    def _meta_passages(self) -> list[dict]:
        """What the assistant truthfully is, plus the wiki's own pages about itself when they are indexed."""
        pages = self.conn.execute("SELECT count(*) FROM pages WHERE is_redirect=0").fetchone()[0]
        last = (
            self.conn.execute("SELECT value FROM state WHERE key='sync.last_run'").fetchone()
            or self.conn.execute("SELECT max(fetched_at) FROM page_html").fetchone()
        )
        last_sync = (last[0] or "never")[:10] if last else "never"
        blurb = (prompts.META_BLURB.replace("{name}", self.name).replace("{pages}", f"{pages:,}")
                 .replace("{last_sync}", last_sync))
        out = [{"title": "About this assistant", "section": "", "url": self.s.wiki.base_url, "text": blurb,
                "chunk_id": None}]
        for title in ("Elanthipedia:About", "Main Page"):
            r = self.conn.execute(
                "SELECT c.chunk_id, c.title, c.section_path, c.anchor, c.text FROM chunks c "
                "JOIN pages p ON p.page_id=c.page_id WHERE p.title LIKE ? AND c.kind='section' ORDER BY c.ord LIMIT 1",
                (title,),
            ).fetchone()
            if r:
                out.append({"title": r["title"], "section": r["section_path"], "text": r["text"],
                            "url": page_url(self.s.wiki.base_url, r["title"], r["anchor"]),
                            "chunk_id": r["chunk_id"]})
        return out

    def _related_pages(self, kind: str, passages: list[dict], extras: list[dict]) -> list[str]:
        """Up to related_k titles worth offering as 'a thread to pull on' after the answer.

        Candidates: runner-up search hits, then pages linked from the top-ranked passage's page (most linked-to
        first). Offers stay within kinds this question kind favours, and never include logs, obsolete pages,
        disambiguations or pages already used in the answer. The top-ranked page stands in for 'the page the
        answer is about': citations are not known until after generation."""
        cfg = self.s.retrieval
        if not cfg.offer_related or kind == "meta" or not passages:
            return []
        weights = cfg.kind_weights.get(kind, {})
        used = {p["title"] for p in passages}
        out: list[str] = []

        def consider(title: str, page_kind: str | None) -> None:
            if title in used or title in out or page_kind in OFFER_EXCLUDED_PAGE_KINDS:
                return
            if title.startswith("RanikMap"):  # map pages are not a thread anyone wants to pull on
                return
            if page_kind and weights.get(page_kind, 1.0) < OFFER_MIN_WEIGHT:
                return
            out.append(title)

        # Pages the answer's own page links to, most linked-to first: the community's own idea of "related", and
        # popular pages make better offers than obscure ones (hence the minimum in-link count).
        top = passages[0].get("chunk_id")
        row = self.conn.execute("SELECT page_id FROM chunks WHERE chunk_id=?", (top,)).fetchone() if top else None
        if row:
            for r in self.conn.execute(
                "SELECT p.title, p.page_kind FROM page_links l JOIN pages p ON p.page_id=l.to_page_id "
                "WHERE l.from_page_id=? AND p.is_log=0 AND p.is_obsolete=0 "
                "AND (SELECT count(*) FROM page_links x WHERE x.to_page_id=p.page_id) >= ? "
                "ORDER BY (SELECT count(*) FROM page_links x WHERE x.to_page_id=p.page_id) DESC LIMIT 40",
                (row["page_id"], MIN_OFFER_INLINKS),
            ):
                consider(r["title"], r["page_kind"])
                if len(out) >= cfg.related_k:
                    return out
        for e in extras:  # runner-up search hits fill any remaining slots, if they are popular enough too
            n = self.conn.execute(
                "SELECT count(*) FROM page_links l JOIN pages p ON p.page_id=l.to_page_id WHERE p.title=?",
                (e["title"],),
            ).fetchone()[0]
            if n >= MIN_OFFER_INLINKS:
                consider(e["title"], e.get("page_kind"))
            if len(out) >= cfg.related_k:
                break
        return out[: cfg.related_k]

    # ------------------------------------------------------------------------------------ answering

    async def answer_stream(
        self, session_id: str, user_message: str, *, kind_override: str | None = None
    ) -> AsyncIterator[Event]:
        memory.new_session(self.conn, session_id=session_id)
        hist = memory.history(self.conn, session_id, self.s.chat.history_turns)
        plan = await self.plan(hist, user_message, session_id)
        if kind_override in QUESTION_KINDS:
            plan.kind = kind_override
        passages, extras = await self.retrieve_with_extras(plan, session_id)
        text, used = prompts.format_passages(passages, self.s.chat.context_token_budget)
        passages = passages[:used]
        related = self._related_pages(plan.kind, passages, extras)
        sources = [
            Source(i + 1, p["title"], p.get("section", ""), p["url"], p.get("chunk_id"), _snippet(p["text"]))
            for i, p in enumerate(passages)
        ]
        yield Retrieval(sources, plan.standalone_question, plan.mode, plan.kind, related)

        if not passages:
            answer = (prompts.NO_PASSAGES_ANSWER if self.s.persona.enabled
                      else "The wiki doesn't seem to cover that.")
            yield Token(answer)
            self._persist(session_id, user_message, answer, plan, [])
            yield Done(answer, [], [], [])
            return

        system = prompts.build_answer_system(plan.kind, text, self.name, persona=self.s.persona.enabled)
        messages = [{"role": "system", "content": system}, *hist,
                    {"role": "user", "content": plan.standalone_question}]
        chunks: list[str] = []
        async for tok in self.ollama.chat_stream(messages, temperature=self.s.chat.temperature):
            chunks.append(tok)
            yield Token(tok)
        answer = soften_refusal("".join(chunks).strip())
        # Lines the model left uncited get a "[n*]" tag where one passage clearly matches (see core/attribution.py).
        # The streamed text is the raw draft; the final text, with these tags, is what Done carries and what is saved.
        answer = attribute(answer, [p["text"] for p in passages]).text
        offer = prompts.offer_line(related) if self.s.persona.enabled else ""
        if offer and not answer.endswith("?"):
            tail = "\n\n" + offer
            yield Token(tail)
            answer += tail
        cited = sorted({int(n) for n in re.findall(r"\[(\d+)\*?\]", answer) if 1 <= int(n) <= len(sources)})
        self._persist(session_id, user_message, answer, plan, sources)
        yield Done(answer, cited, sources, related)

    def _persist(self, sid: str, user_message: str, answer: str, plan: Plan, sources: list[Source]) -> None:
        memory.add_message(self.conn, sid, "user", user_message, standalone_q=plan.standalone_question,
                           mode=plan.mode, kind=plan.kind)
        memory.add_message(
            self.conn, sid, "assistant", answer, standalone_q=plan.standalone_question, mode=plan.mode,
            kind=plan.kind, sources=[s.__dict__ for s in sources],
        )

    async def ask(self, session_id: str, message: str, *, kind_override: str | None = None) -> Done:
        done: Done | None = None
        async for ev in self.answer_stream(session_id, message, kind_override=kind_override):
            if isinstance(ev, Done):
                done = ev
        assert done is not None
        return done
