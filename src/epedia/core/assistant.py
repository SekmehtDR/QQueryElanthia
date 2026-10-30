"""The answering core: plan (rewrite + route) -> retrieve -> stream a grounded answer. Front ends consume events."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import struct
from dataclasses import dataclass, field
from datetime import date
from typing import Any, AsyncIterator

from ..config import Settings
from ..index import topics
from ..kinds import OFFER_EXCLUDED_PAGE_KINDS, OFFER_MIN_WEIGHT, QUESTION_KINDS
from ..llm import prompts
from ..llm.ollama import Ollama, OllamaError
from ..retrieval import resolve
from ..retrieval.search import WHO_KNOWS, Hit, discussion_search, search
from ..retrieval.titles import strip_game_name
from ..util import now_text, page_url
from ..wiki import history as page_history
from ..wiki import timeline
from . import capabilities, disciplines, grounding, hunting, materials, memory, turns, verify
from .attribution import attribute
from .dates import UPCOMING, keep_ahead_lines, over_answer, split_by_time
from .userinput import split_message

log = logging.getLogger("epedia.assistant")

OFFER_CHUNKS_PER_PAGE = 6  # only a page's opening chunks are compared: enough to tell what it is about, and fast
OFFER_CLOSE_MARGIN = 0.06  # an offer must be about as close to the question as the closest candidate...
OFFER_MAX_DISTANCE = 0.45  # ...and close in absolute terms (cosine distance); otherwise nothing is offered
MIN_OFFER_INLINKS = 8  # a page must be linked from at least this many pages to be offered as a thread to pull on
RECHECK_PRIOR = 8  # "are you sure?": passages of the last answer re-read (its cited ones first), before fresh results
WHO_KNOWS_PASSAGES = 3  # "which player knows the most about X?": discussion and player pages added to the evidence


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


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# A message really is small talk or about the assistant only if it greets, thanks, or mentions you / the wiki.
META_CUES = re.compile(
    r"\b(hi|hello|hey|howdy|greetings|thanks?|thank you|bye|you|your|yourself|elanthipedia|wiki|assistant|bot|fennick)\b",
    re.I,
)
PARTIAL_NOTE = "I couldn't find every part of that."


SHORT_MESSAGE_WORDS = 9  # a long question that happens to contain "that" or "it" is usually self-contained

# "What time is it?", "what's today's date?": answered from the computer's clock, not from the wiki.
CLOCK_QUESTION = re.compile(
    r"^\W*(?:(?:hey|hi|hello|fennick)\W+)*(?:can you (?:please )?tell me )?(?:what(?:'s| is| are)?|whats)\s+(?:the\s+|today'?s\s+)?"
    r"(?:current\s+|todays?\s+)?(?:date|time|day(?: of the week)?)(?:\s+(?:and|&)\s+(?:date|time))?(?:\s+is\s+it)?"
    r"(?:\s+(?:right\s+)?(?:now|today|currently))?\W*$"
    r"|^\W*what\s+(?:day|time|date)\s+is\s+(?:it|today)(?:\s+(?:right\s+)?now)?\W*$"
    r"|^\W*(?:do you know\s+)?(?:the\s+)?(?:current\s+)?(?:date\s+and\s+time|time\s+and\s+date)\W*$",
    re.I,
)


def clock_answer(persona: bool) -> str:
    when = now_text()
    if not persona:
        return f"It is {when}."
    return (f"By my reckoning it's {when}, on the clock of the computer I'm running on. "
            "The game keeps its own time, which I can't see: type TIME in DragonRealms for that.")


# Words that make "this year" part of the question: only then is the current year worth searching for.
TIME_WORDS = re.compile(
    r"\b(upcoming|next|current|currently|latest|recent|recently|newest|now|today|tonight|soon|coming|schedule[sd]?|"
    r"this (?:year|month|week|season)|right now|these days|new)\b", re.I,
)


def drop_unasked_year(question: str, message: str, year: int | None = None) -> str:
    """The planner is told to add the current year for questions about what is upcoming or current; it sometimes adds it
    to others ("... least in 2026?"), which only pulls in unrelated pages. Remove a year the user did not ask about."""
    year = year or date.today().year
    ys = str(year)
    if ys not in question or ys in message or TIME_WORDS.search(message):
        return question
    cleaned = re.sub(rf"\s*(?:\b(?:in|for|during|of|on|as of|from|since)\s+)?(?:the year\s+)?\b{ys}\b", "", question)
    cleaned = re.sub(r"\s+([?.!,])", r"\1", cleaned).strip()
    return cleaned or question


# Words that only make sense given what was just said ("what about them?", "and its price?", "tell me more").
ANAPHORA = re.compile(
    r"\b(it|its|they|them|their|those|these|that|this|he|she|him|her|his|hers|the other|the same|the first|"
    r"the second|the last|more|another|also)\b|^(and|but|so|what about|how about)\b", re.I,
)

# A follow-up that points back at text pasted earlier ("what I sent you", "that message", "the post above").
REFERS_TO_PASTE = re.compile(
    r"\b(?:i|we) (?:sent|pasted|shared|posted|gave|showed|copied)\b|\b(?:that|this|the|my|above|previous) "
    r"(?:message|post|text|paste|announcement|quote)\b", re.I,
)

# The model is told not to end with an offer (the program adds one), but sometimes does, which makes two.
_MODEL_OFFER = re.compile(
    r"^(If you(?:'re| are|'d| would)? (?:interested|curious|like|want)|Let me know|Would you like|Want me to|"
    r"I can also (?:tell|explain|go|help)|Feel free to ask)", re.I,
)
_OFFERS_SOMETHING = re.compile(r"\b(I can|I could|I'd be|let me|ask me|just ask|tell you|explore|dig into|go into|more about)\b", re.I)


def strip_closing_offer(answer: str) -> str:
    """Drop a closing 'if you're interested, I can also...' paragraph the model wrote on its own."""
    parts = re.split(r"\n\s*\n", answer.rstrip())
    while len(parts) > 1:  # there can be more than one
        last = parts[-1].strip()
        if not (_MODEL_OFFER.match(last) and _OFFERS_SOMETHING.search(last) and not re.search(r"\[\d+\*?\]", last)
                and len(last) < 400):
            break
        parts.pop()
    return "\n\n".join(parts) if len(parts) < len(re.split(r"\n\s*\n", answer.rstrip())) else answer


def soften_refusal(answer: str) -> str:
    """Replace a 'nothing on that' line that was tacked onto an answer that has real content.

    That line is meant to be the whole reply when no passage is relevant (the program emits it itself in that case).
    A small model sometimes appends it after a good answer, which contradicts everything above it; or it may have
    meant "one detail is missing". Deleting it could make a partial answer look complete, so it becomes an honest
    caveat that is true in both cases."""
    for line in (prompts.NO_PASSAGES_ANSWER, prompts.OLD_NO_PASSAGES_ANSWER, "The wiki doesn't seem to cover that."):
        if line in answer and len(answer.replace(line, "").strip()) >= 80:
            return answer.replace(line, PARTIAL_NOTE)
    return answer


def _signals(hits: list[Hit], question: str) -> dict:
    """How well the search matched, for the turn's record: the closest chunk's cosine distance, whether a title matched,
    and the share of the question's words found in the top three passages. Recorded, not used to decide (see
    core/grounding.py for why distance cannot tell answerable from unanswerable)."""
    from .attribution import _bag

    dists = [h.vec_dist for h in hits[:5] if h.vec_dist is not None]
    words = _bag(question)
    top = set().union(*(_bag(f"{h.title} {h.text}") for h in hits[:3])) if hits else set()
    return {"best_cos": round(min(dists), 4) if dists else None,
            "title_match": next((h.title_match for h in hits[:3] if h.title_match), ""),
            "lexical": round(len(words & top) / len(words), 2) if words else None}


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
class Revise:
    """The draft streamed so far is withdrawn and a second one follows (it named things no passage has, or pretended to
    look something up). Front ends clear what they showed."""
    reason: str  # "names" or "lookup"


@dataclass
class Done:
    answer: str
    cited: list[int]
    sources: list[Source]
    related: list[str] = field(default_factory=list)
    topic: str | None = None
    turn_id: int | None = None  # the turn's record (core/turns.py), for feedback on this answer
    by_code: bool = False  # written by code (clock, a table, a clarifying question, a decline), not by the model
    verification: dict | None = None  # core/verify.py on the model's answer, against the passages it was given
    revised: bool = False  # the first draft was withdrawn and rewritten


Event = Retrieval | Token | Revise | Done

# A citation in a final answer: by the model [2], matched by code [2*], or not borne out by its passage [2?].
ANY_CITE = re.compile(r"\[(\d+)[*?]?\]")
LIST_LINE = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)])\s.*\n(?:.*\n)*?[ \t]*(?:[-*•]|\d+[.)])\s", re.M)  # two list items

# A link to an Elanthipedia page pasted into a message ("check the correct article: https://.../Elanthian_time"): the
# page is read directly instead of searching for the words of its address.
WIKI_LINK = re.compile(r"https?://(?:www\.)?elanthipedia\.play\.net/(?:index\.php\?title=|wiki/)?([^\s?#&<>\"')\]]+)"
                       r"[^\s<>\"')\]]*", re.I)
PINNED_CHUNKS = 4  # a linked page's opening passages that lead the evidence


@dataclass
class Plan:
    standalone_question: str
    mode: str = "search"
    items_query: dict[str, Any] | None = None
    follow_up: bool = False  # depends on earlier turns: bring the previous answer's source passages along
    kind: str = "general"  # what the question is about; steers retrieval weights and the answer's shape
    topic: str = ""  # what the conversation is about, for the "Talking about" chip
    clarify: str = ""  # a question to ask instead of answering, when the message has no subject to work with
    material: str = ""  # text the person pasted with the question (a Discord post, an in-game message)
    qvec: list[float] | None = None  # the question's embedding, filled in by retrieval
    prebuilt: list[dict] | None = None  # passages code already wrote while planning (a crafting table): reused, not rebuilt
    signals: dict = field(default_factory=dict)  # how well retrieval matched (recorded on the turn, see core/grounding.py)
    names: list[str] = field(default_factory=list)  # for a declined story: the wiki pages the request named
    avoid_titles: list[str] = field(default_factory=list)  # "try again": pages already shown, left out of the search
    pinned: list[int] = field(default_factory=list)  # pages the person linked to: their passages lead


class Assistant:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, ollama: Ollama):
        self.s = settings
        self.conn = conn
        self.ollama = ollama

    @property
    def name(self) -> str:
        return self.s.persona.name

    async def plan(self, hist: list[dict[str, str]], message: str, session_id: str | None = None) -> Plan:
        message, material = split_message(message)  # what is being asked, apart from any text pasted with it
        if not material and session_id and REFERS_TO_PASTE.search(message):
            material = memory.last_pasted(self.conn, session_id)  # "the lore of what I sent you": it means the paste
        catalog = self._property_catalog()
        system = prompts.ROUTER_SYSTEM.replace("{property_catalog}", catalog or "(no item properties indexed yet)")
        earlier = memory.recent_questions(self.conn, session_id, 8)[:-2] if session_id and hist else []
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in hist[-4:]) or "(no earlier messages)"
        user = ""
        if earlier:  # questions older than the last two turns: enough to keep the thread without the answers
            user += "Earlier questions in this conversation, oldest first:\n" + "\n".join(f"- {t}" for t in earlier) + "\n\n"
        user += f"Most recent turns:\n{convo}\n"
        last_pages = self._prior_titles(session_id) if hist else []
        if last_pages:
            user += "\nPages used in the last answer:\n" + "\n".join(f"- {t}" for t in last_pages) + "\n"
        user += f"\nCurrent year: {date.today().year}\nLatest user message: {message}"
        if material:
            user += ("\n\nText the user pasted with that message (it is not from the wiki; use it to understand what "
                     f"they mean and to name the specific people, places and events in the question):\n---\n{material[:1500]}\n---")
        try:
            data = await self.ollama.chat_json(
                [{"role": "system", "content": system}, {"role": "user", "content": user}], prompts.ROUTER_SCHEMA
            )
            q = strip_game_name((data.get("standalone_question") or message).strip() or message)
            q = drop_unasked_year(q, message)
            mode = data.get("mode") if data.get("mode") in ("search", "items") else "search"
            iq = data.get("items_query") if mode == "items" else None
            if mode == "items" and not catalog:
                mode, iq = "search", None
            kind = data.get("kind") if data.get("kind") in QUESTION_KINDS else "general"
            if mode == "items":
                kind = "item"
            if kind == "meta" and (not META_CUES.search(message) or (hist and not capabilities.ABOUT_ME.search(message))):
                # The 8B planner files "Who is <name>?" as small talk, and in a conversation anything that says "you"
                # ("are you going to try again?", "are you sure?"): only greetings, thanks and questions plainly about
                # Fennick are small talk once a conversation is under way; the rest is about its subject.
                kind = "general"
            # The 8B planner resolves "it" correctly but often forgets to say follow_up: a question it had to rewrite
            # using the conversation is a follow-up whatever the flag says.
            rewritten = _norm(q) != _norm(message)
            leans_on_context = len(message.split()) <= SHORT_MESSAGE_WORDS and bool(ANAPHORA.search(message))
            follow_up = bool(hist) and (bool(data.get("follow_up")) or rewritten or leans_on_context) and kind != "meta"
            topic = re.sub(r"\s+", " ", str(data.get("topic") or "")).strip()[:40]
            clarify = str(data.get("clarify") or "").strip()
            if hist or kind == "meta" or len(clarify) > 200:
                clarify = ""  # only trusted when there is no conversation to draw a subject from
            return Plan(q, mode, iq, follow_up=follow_up, kind=kind, topic=topic, clarify=clarify, material=material)
        except (OllamaError, KeyError, ValueError) as exc:
            log.warning("planner failed (%s); falling back to plain search", exc)
            return Plan(message, material=material)

    def _not_meant(self, question: str, res=None) -> set[int]:
        """Pages the question's own words rule out: a same-named page of another kind (the creature when it asked how
        to cast), or, for an acronym like SIS, pages whose names merely contain the letters."""
        res = res or resolve.resolve(self.conn, question)
        if res is None:
            return set()
        chosen = {c.page_id for c in res.chosen}
        out = {c.page_id for c in res.candidates} - chosen if chosen else set()
        return out | resolve.set_aside(self.conn, res)

    def _history_passages(self, passages: list[dict], question: str = "", pages: int = 3) -> list[dict]:
        """Recent edits and a reading of them for the pages the question names, then the top pages, so a page that
        was rewritten or reverted is not taken at face value. Off when the 'edits of history' setting is 0, or no
        history is stored yet."""
        n = page_history.edits_setting(self.conn, self.s.retrieval.history_edits)
        if n <= 0:
            return []
        out: list[dict] = []
        seen: set[str] = set()
        q = f" {re.sub(r'[^a-z0-9]+', ' ', question.casefold())} "
        named = [p for p in passages if p.get("chunk_id") and f" {re.sub(r'[^a-z0-9]+', ' ', p['title'].casefold()).strip()} " in q]
        for p in [*named, *passages]:
            title = p["title"]
            if not p.get("chunk_id") or title in seen:
                continue
            seen.add(title)
            h = page_history.passage(self.conn, title, n, self.s.wiki.base_url)
            if h:
                out.append(h)
            if len(seen) >= pages:
                break
        return out

    def _edit_notes(self, passages: list[dict], question: str, answer: str, pages: int = 3) -> list[str]:
        """In-voice notes, written by code, on what the edits say about facts the answer states. A doubtful fact is
        cautioned for any of the top pages; what a trusted fact used to be is volunteered only for the page(s) the
        question names, so a passing mention of another character does not pick up a note."""
        if page_history.edits_setting(self.conn, self.s.retrieval.history_edits) <= 0:
            return []
        q = f" {re.sub(r'[^a-z0-9]+', ' ', question.casefold())} "
        top = list(dict.fromkeys(p["title"] for p in passages if p.get("chunk_id")))[:pages]
        cited = {int(n) for n in ANY_CITE.findall(answer)}
        relied = [passages[i - 1]["title"].removesuffix(": recent edits") for i in sorted(cited) if 1 <= i <= len(passages)]
        out: list[str] = []
        for title in dict.fromkeys([*top, *relied]):
            named = f" {re.sub(r'[^a-z0-9]+', ' ', title.casefold()).strip()} " in q
            leans_on = named or title in relied  # the answer is about this page, or rests on it
            notes = timeline.caveats(self.conn, title, answer, earlier_too=leans_on)
            if not notes and leans_on and title in relied[:2]:
                notes = [n for n in [page_history.revised_note(self.conn, title)] if n]
            out += notes
        return out

    def _is_hunt_fit(self, plan: Plan, session_id: str | None) -> bool:
        """A 'what should I hunt?' question, or the reply to Fennick asking for the ranks to answer one."""
        if hunting.HUNT_FIT.search(plan.standalone_question):
            return True
        row = session_id and self.conn.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='assistant' AND coalesce(mode, '') != 'error' "
            "ORDER BY msg_id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return bool(row) and row["content"] == hunting.NEED_RANKS

    def _hunt_ranks(self, plan: Plan, user_message: str) -> list[int]:
        """Combat ranks the person gave, in this message or in what they said earlier in the conversation."""
        return hunting.parse_ranks(plan.standalone_question, user_message)

    def _property_catalog(self) -> str:
        rows = self.conn.execute(
            "SELECT prop, value_type, coalesce(description, label, '') AS d, sample_values FROM smw_properties "
            "WHERE page_count >= 100 ORDER BY page_count DESC LIMIT 60"
        ).fetchall()
        return "\n".join(f"- {r['prop']}: {r['value_type']}, {r['d'] or r['sample_values'] or ''}"[:160] for r in rows)

    def _prior_sources(self, session_id: str | None, cited_first: bool = False) -> list[dict]:
        """Sources of the last assistant turn in this session, in their original order, or with the ones that answer
        cited first (what "are you sure?" is about)."""
        if not session_id:
            return []
        row = self.conn.execute(
            "SELECT sources, cited FROM messages WHERE session_id=? AND role='assistant' AND coalesce(mode, '') != 'error' "
            "ORDER BY msg_id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if not row or not row["sources"]:
            return []
        try:
            sources = json.loads(row["sources"])
            cited = set(json.loads(row["cited"] or "[]")) if cited_first else set()
        except (ValueError, TypeError):
            return []
        return sorted(sources, key=lambda s: s.get("n") not in cited) if cited else sources

    def _prior_chunk_ids(self, session_id: str | None, cited_first: bool = False) -> list[int]:
        return [s["chunk_id"] for s in self._prior_sources(session_id, cited_first) if s.get("chunk_id")]

    def _prior_titles(self, session_id: str | None) -> list[str]:
        seen: list[str] = []
        for s in self._prior_sources(session_id):
            if s["title"] not in seen:
                seen.append(s["title"])
        return seen[:6]

    def _prior_passages(self, session_id: str | None, limit: int = 5, cited_first: bool = False) -> list[dict]:
        """The previous answer's own passages, so a follow-up is answered from (and cites) the same pages."""
        out: list[dict] = []
        for cid in self._prior_chunk_ids(session_id, cited_first)[:limit]:
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
        if self._is_hunt_fit(plan, session_id):
            ranks = self._hunt_ranks(plan, plan.standalone_question)
            p = hunting.passage(self.conn, ranks, self.s.wiki.base_url, plan.standalone_question) if ranks else None
            if p:
                passages.append(p)
                k = 0  # the table is the whole evidence; other pages only tempt the model to improvise
        mats = plan.prebuilt or materials.passage(self.conn, plan.standalone_question, self.s.wiki.base_url)
        if mats:
            passages = list(mats)  # replaces an items-tool lookup too: the ranking is the whole evidence
            k = 0
        elif (disc := disciplines.passage(self.conn, plan.standalone_question, self.s.wiki.base_url)):
            passages = [disc]  # the Crafting page's own list is the whole evidence
            k = 0
        # A re-check reads every page the last answer cited, first; an ordinary follow-up brings its opening passages.
        recheck = plan.mode == "recheck"
        prior: list[dict] = (self._prior_passages(session_id, RECHECK_PRIOR if recheck else 5, cited_first=recheck)
                             if plan.follow_up and k else [])
        passages.extend(prior)
        if plan.pinned and k:  # a page the person linked to: its opening passages come first
            pinned = [p for p in self._pinned_passages(plan.pinned) if p["chunk_id"] not in {q["chunk_id"] for q in prior}]
            passages[:0] = pinned
            prior = [*pinned, *prior]
        have = {p["chunk_id"] for p in prior}
        extra_k = self.s.retrieval.related_k if self.s.retrieval.offer_related else 0
        qvec = (await self.ollama.embed(["search_query: " + plan.standalone_question]))[0]
        plan.qvec = qvec  # reused to pick related pages that are really about this question
        named = resolve.resolve(self.conn, plan.standalone_question)  # once: the avoid set and the topic family agree
        avoid = self._not_meant(plan.standalone_question, named)
        if plan.avoid_titles:  # "try again": the pages already shown missed the point
            marks = ",".join("?" * len(plan.avoid_titles))
            avoid |= {r[0] for r in self.conn.execute(f"SELECT page_id FROM pages WHERE title IN ({marks})",
                                                       plan.avoid_titles)}
        hits: list[Hit] = search(
            self.conn, plan.standalone_question, self.s.retrieval, self.s.wiki.base_url, qvec, k_final=k + extra_k,
            prior_chunk_ids=self._prior_chunk_ids(session_id) if plan.follow_up else None, kind=plan.kind,
            avoid_pages=avoid, topic_pages=topics.family(self.conn, plan.standalone_question, named) or None,
        )
        plan.signals.update(_signals(hits, plan.standalone_question))
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
        if k and WHO_KNOWS.search(plan.standalone_question):
            # "Which player knows the most about X?": the wiki's answer is in signed talk-page posts and players' own
            # pages, which rank below articles; a few of the best go in near the top so they survive the budget.
            taken_ids = {p["chunk_id"] for p in passages}
            people = [{"title": h.title, "section": h.section_path, "url": h.url, "text": h.text,
                       "chunk_id": h.chunk_id, "page_kind": h.page_kind}
                      for h in discussion_search(self.conn, plan.standalone_question, self.s.retrieval,
                                                 self.s.wiki.base_url, qvec, k=WHO_KNOWS_PASSAGES, avoid_pages=avoid)
                      if h.chunk_id not in taken_ids]
            at = min(2, len(passages))
            passages[at:at] = people
        if not (passages and passages[0].get("answer")):
            passages.extend(self._history_passages(passages, plan.standalone_question))
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
        out = [{"title": "About this assistant", "section": "", "url": self.s.wiki.base_url,
                "text": blurb + "\n\n" + capabilities.capabilities_text(self.name), "chunk_id": None}]
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

    def _related_pages(self, kind: str, passages: list[dict], extras: list[dict],
                       seen: set[str] | frozenset[str] = frozenset(), qvec: list[float] | None = None) -> list[str]:
        """Up to related_k titles worth offering as 'a thread to pull on' after the answer.

        Candidates: runner-up search hits, then pages linked from the top-ranked passage's page (most linked-to
        first). Offers stay within kinds this question kind favours, and never include logs, obsolete pages,
        disambiguations or pages already used in the answer. The top-ranked page stands in for 'the page the
        answer is about': citations are not known until after generation."""
        cfg = self.s.retrieval
        if not cfg.offer_related or kind == "meta" or not passages:
            return []
        weights = cfg.kind_weights.get(kind, {})
        used = {p["title"] for p in passages} | set(seen)  # pages cited or offered earlier are not offered again
        out: list[str] = []

        def consider(title: str, page_kind: str | None) -> None:
            if title in used or title in out or page_kind in OFFER_EXCLUDED_PAGE_KINDS:
                return
            if title.startswith("RanikMap"):  # map pages are not a thread anyone wants to pull on
                return
            if page_kind and weights.get(page_kind, 1.0) < OFFER_MIN_WEIGHT:
                return
            out.append(title)

        # Candidates: pages the answer's own page links to (the community's idea of "related") and the runner-up
        # search hits. Popular pages are not better offers: "Crossing" is linked from everywhere and is rarely what
        # the conversation is about, so candidates are ranked by how close they are to the question itself.
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
        for e in extras:
            n = self.conn.execute(
                "SELECT count(*) FROM page_links l JOIN pages p ON p.page_id=l.to_page_id WHERE p.title=?",
                (e["title"],),
            ).fetchone()[0]
            if n >= MIN_OFFER_INLINKS:
                consider(e["title"], e.get("page_kind"))
        if qvec is not None and out:
            out = self._closest_to_question(out, qvec)
        return out[: cfg.related_k]

    def _closest_to_question(self, titles: list[str], qvec: list[float]) -> list[str]:
        """Order candidate pages by how close their best chunk is to the question; drop the clearly unrelated.

        A page is kept if it is within OFFER_CLOSE_MARGIN of the closest candidate and under OFFER_MAX_DISTANCE, so
        when nothing fits the question well, nothing is offered."""
        blob = struct.pack(f"{len(qvec)}f", *qvec)
        marks = ",".join("?" * len(titles))
        rows = self.conn.execute(
            "SELECT p.title, min(vec_distance_cosine(v.embedding, ?)) AS d FROM pages p "
            "JOIN chunks c ON c.page_id=p.page_id JOIN chunk_vec v ON v.chunk_id=c.chunk_id "
            f"WHERE p.title IN ({marks}) AND c.ord < {OFFER_CHUNKS_PER_PAGE} GROUP BY p.title",
            (blob, *titles),
        ).fetchall()
        dist = {r["title"]: r["d"] for r in rows if r["d"] is not None}
        if not dist:
            return titles
        best = min(dist.values())
        keep = [t for t in sorted(dist, key=dist.get)
                if dist[t] <= best + OFFER_CLOSE_MARGIN and dist[t] <= OFFER_MAX_DISTANCE]
        return keep

    # ------------------------------------------------------------------------------------ answering

    async def answer_stream(
        self, session_id: str, user_message: str, *, kind_override: str | None = None
    ) -> AsyncIterator[Event]:
        """Answer one message. The question is saved before anything can fail, and the turn is recorded however it
        ends (answered, failed, or abandoned by the reader), so nothing a person asked disappears."""
        memory.new_session(self.conn, session_id=session_id)
        hist = memory.history(self.conn, session_id, self.s.chat.history_turns)  # read before this message is saved
        user_id = memory.add_message(self.conn, session_id, "user", user_message)
        turn = turns.Turn.start(self.conn, session_id, user_id)
        said: list[str] = []
        try:
            async for ev in self._answer(turn, hist, user_message, kind_override):
                if isinstance(ev, Retrieval):
                    turn.mark("retrieve")
                elif isinstance(ev, Token):
                    turn.mark("ttft")
                    said.append(ev.text)
                elif isinstance(ev, Revise):
                    said.clear()  # the withdrawn draft is not what a failed turn should keep
                elif isinstance(ev, Done):
                    ev.turn_id = turn.turn_id
                yield ev
        except BaseException as exc:  # GeneratorExit too: the reader closed the page mid-answer
            if not turn.closed:
                partial = "".join(said).strip()
                msg_id = memory.add_message(  # shown in the transcript, never remembered (memory.history skips it)
                    self.conn, session_id, "assistant", partial, mode="error", turn_id=turn.turn_id) if partial else None
                turn.fail("aborted by the reader" if isinstance(exc, (GeneratorExit, asyncio.CancelledError)) else exc,
                          assistant_msg_id=msg_id)
            raise

    async def _answer(self, turn: turns.Turn, hist: list[dict[str, str]], user_message: str,
                      kind_override: str | None) -> AsyncIterator[Event]:
        session_id = turn.session_id
        # Links to wiki pages become the pages' titles for everything that reads the words (the saved question keeps
        # the link as typed); the pages themselves lead the evidence.
        user_message, linked = self._linked_pages(user_message)
        asked, pasted = split_message(user_message)
        early = self._early_plan(asked, pasted, hist)
        if early is not None and early.mode == "clock":
            # What day or time it is needs no wiki: answer from the clock, in code, so it is always exactly right.
            plan = turn.plan = early
            answer = clock_answer(self.s.persona.enabled)
            yield Retrieval([], plan.standalone_question, "clock", "meta", [])
            yield Token(answer)
            self._persist(turn, answer, plan, [], [])
            yield Done(answer, [], [], [], memory.get_topic(self.conn, session_id), by_code=True)
            return
        # A story request and "are you sure?" are recognised by rule: the planner turned the one into a story and filed
        # the other as a question about Fennick himself.
        plan = (early or self._fiction_plan(asked) or self._recheck_plan(asked, hist, session_id)
                or await self.plan(hist, user_message, session_id))
        turn.mark("planner")
        turn.plan = plan
        plan.pinned = linked
        memory.note_plan(self.conn, turn.user_msg_id, plan.standalone_question, plan.mode, plan.kind)
        if kind_override in QUESTION_KINDS:
            plan.kind = kind_override
        if plan.mode == "fiction" and not plan.names:
            # Nothing in it the wiki knows: the decline is the whole answer, and no model is asked.
            answer = capabilities.decline_fiction([], self.s.persona.enabled)
            yield Retrieval([], plan.standalone_question, "fiction", plan.kind, [])
            yield Token(answer)
            self._persist(turn, answer, plan, [], [])
            yield Done(answer, [], [], [], memory.get_topic(self.conn, session_id), by_code=True)
            return
        gate = grounding.Gate()
        if plan.kind != "meta" and plan.mode == "search" and not plan.prebuilt and not plan.pinned:
            # Something the wiki never mentions cannot be answered from it: say so in code, without the model, so it
            # cannot fill the gap with an invented creature or spell. Judged on the person's own words.
            gate = grounding.assess(self.conn, asked)
            plan.signals.update(gate.as_dict())
            if gate.level == "none":
                plan.mode = "declined"
                answer = grounding.decline(gate, self.s.persona.enabled)
                yield Retrieval([], plan.standalone_question, "declined", plan.kind, [])
                yield Token(answer)
                self._persist(turn, answer, plan, [], [], grounding=gate.as_dict())
                yield Done(answer, [], [], [], memory.get_topic(self.conn, session_id), by_code=True)
                return
        asks = plan.mode in ("search", "items")  # a re-check or a declined story has nothing to clarify
        if (asks and not plan.clarify and plan.kind != "meta" and hunting.HUNT_FIT.search(plan.standalone_question)
                and not self._hunt_ranks(plan, user_message)):
            plan.clarify = hunting.NEED_RANKS  # matching needs combat ranks; a circle alone does not give them
        if asks and not plan.clarify and plan.kind != "meta":
            named = resolve.resolve(self.conn, plan.standalone_question)
            if named is not None and named.ambiguous:
                plan.clarify = resolve.clarify(named)  # one name, different kinds of page, and nothing to choose between
        if plan.clarify and not memory.last_was_clarification(self.conn, session_id):
            # Ask rather than guess, but never twice in a row: the second time it is answered as best it can be.
            yield Retrieval([], plan.standalone_question, "clarify", plan.kind, [])
            yield Token(plan.clarify)
            plan.mode = "clarify"
            self._persist(turn, plan.clarify, plan, [], [])
            yield Done(plan.clarify, [], [], [], memory.get_topic(self.conn, session_id), by_code=True)
            return
        passages, extras = await self.retrieve_with_extras(plan, session_id)
        over_only = False
        if plan.kind == "event_news" and (UPCOMING.search(user_message) or UPCOMING.search(plan.standalone_question)):
            # "What is coming up next?" can only be answered by something still ahead. The small model cannot be trusted
            # to see that a September date is over in October, so the filtering and the "nothing is scheduled" reply are code.
            ahead, over, undated = split_by_time(passages, date.today())
            if ahead:
                passages = [{**p, "text": keep_ahead_lines(p["text"], date.today())} for p in ahead] + undated
            elif over:
                over_only, passages = True, over_answer(over, date.today(), self.s.persona.enabled)[1]
        budget = self.s.chat.context_token_budget
        text, used = prompts.format_passages(passages, budget, date.today())
        system = self._answer_system(plan, text, hist)
        # The whole prompt must fit the context window, or the model silently loses its rules: older turns go first,
        # then passages from the bottom of the list.
        hist, over = prompts.fit_messages(system, hist, plan.standalone_question, self.s.ollama.num_ctx,
                                          self.s.ollama.answer_reserve_tokens)
        if over and used > 1:
            text, used = prompts.format_passages(passages, max(1000, budget - over), date.today())
            system = self._answer_system(plan, text, hist)
        passages = passages[:used]
        related = self._related_pages(plan.kind, passages, extras, memory.seen_titles(self.conn, session_id), plan.qvec)
        sources = [
            Source(i + 1, p["title"], p.get("section", ""), p["url"], p.get("chunk_id"), _snippet(p["text"]))
            for i, p in enumerate(passages)
        ]
        yield Retrieval(sources, plan.standalone_question, plan.mode, plan.kind, [] if over_only else related)

        if over_only:
            answer = over_answer(passages, date.today(), self.s.persona.enabled)[0]
            yield Token(answer)
            self._persist(turn, answer, plan, sources, [], cited=[1])
            yield Done(answer, [1], sources, [], memory.get_topic(self.conn, session_id), by_code=True)
            return

        if not passages:
            answer = (capabilities.decline_fiction([], self.s.persona.enabled) if plan.mode == "fiction"
                      else prompts.NO_PASSAGES_ANSWER if self.s.persona.enabled
                      else "The wiki doesn't seem to cover that.")
            yield Token(answer)
            self._persist(turn, answer, plan, [], [])
            yield Done(answer, [], [], [], memory.get_topic(self.conn, session_id), by_code=True)
            return

        if passages[0].get("answer"):
            # The ladder match is data, not prose: the creatures and numbers are written by code so none can be invented.
            answer = passages[0]["answer"]
            note = page_history.caution(self.conn, [p["title"] for p in passages], self.s.retrieval.history_edits)
            if note:
                answer += "\n\n" + note
            yield Token(answer)
            cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer) if 1 <= int(n) <= len(sources)}) or [1]
            self._persist(turn, answer, plan, sources, [], cited=cited)
            yield Done(answer, cited, sources, [], memory.get_topic(self.conn, session_id), by_code=True)
            return

        # Earlier turns always come along, as they would in a conversation with a person: what was just said shapes
        # how the next question is understood, whether or not the planner flagged it as a follow-up.
        user_turn = plan.standalone_question
        if plan.mode in ("recheck", "retry"):
            user_turn = f"{asked.strip()} (This is about my last question: {plan.standalone_question})"
        messages = [{"role": "system", "content": system}, *hist, {"role": "user", "content": user_turn}]
        lead = ""
        if plan.mode == "fiction":  # the decline leads, in code; the model answers what the wiki says about the names
            lead = capabilities.decline_fiction(plan.names, self.s.persona.enabled) + "\n\n"
            yield Token(lead)
        # What the user said earlier and today's date are known: names and numbers from them are not inventions.
        said = " ".join(m["content"] for m in hist if m["role"] == "user")
        context = f"{said} {now_text()}"
        # Small talk is checked too: the log had "I'll have to look through the pages" and names from earlier answers in
        # a reply filed as a question about Fennick.
        policy = self.s.chat.regenerate == "fabrication"
        recheck = plan.mode == "recheck"
        revised = False
        temperature = self.s.chat.temperature
        while True:
            chunks: list[str] = []
            async for tok in self.ollama.chat_stream(messages, temperature=temperature):
                chunks.append(tok)
                yield Token(tok)
            answer = self._clean_draft("".join(chunks), passages)
            # Checked against the very passages it was written from (core/verify.py), and recorded on the turn.
            checked = verify.run(answer, passages, plan.standalone_question, user_message, context)
            if recheck:  # "I checked the passages again" is true of a re-check: not a pretended look-up there
                checked.fake_actions = []
            if revised or not policy or not checked.fabricated:
                break
            # It named things no passage has, or pretended to look something up: withdraw the draft and write it once
            # more, at temperature 0, told exactly what it got wrong. Nothing else is worth a second draft.
            revised = True
            yield Revise("names" if checked.unknown_entities else "lookup")
            if lead:
                yield Token(lead)
            strict = prompts.strict_addendum(checked.unknown_entities, bool(checked.fake_actions))
            messages = [{"role": "system", "content": f"{system}\n\n{strict}"}, *messages[1:]]
            temperature = 0.0
        # The final text: unsupported citations marked [n?], pretended look-ups removed, and one note on what is left.
        answer = lead + (checked.text if recheck else verify.strip_fake_actions(checked.text) or checked.text)
        note = verify.honesty_note(checked, self.s.persona.enabled, uncited=plan.kind != "meta")
        if note:
            tail = "\n\n" + note
            yield Token(tail)
            answer += tail
        if (plan.kind != "meta" and plan.mode != "fiction" and LIST_LINE.search(answer)
                and (capabilities.wants_all(user_message) or capabilities.wants_all(plan.standalone_question))):
            pages = len({p["title"] for p in passages if not p["title"].endswith(": recent edits")})
            tail = "\n\n" + capabilities.partial_list(pages, self.s.persona.enabled)
            yield Token(tail)
            answer += tail
        notes = self._edit_notes(passages, plan.standalone_question, answer)
        if notes:
            tail = "\n\n" + " ".join(notes)
            yield Token(tail)
            answer += tail
        offer = prompts.offer_line(related) if self.s.persona.enabled else ""
        if offer and not answer.endswith("?"):
            tail = "\n\n" + offer
            yield Token(tail)
            answer += tail
        cited = sorted({int(n) for n in ANY_CITE.findall(answer) if 1 <= int(n) <= len(sources)})
        # The record is of the final draft as the model wrote it (code then removed any pretended look-up from the text).
        record = {**checked.as_dict(), "revised": revised}
        self._persist(turn, answer, plan, sources, related, cited=cited, verification=record, regenerated=revised)
        real = [s for s in sources if not s.title.endswith(": recent edits")]
        top = (real[cited[0] - 1] if cited and cited[0] <= len(real) else real[0]).title if real else ""
        topic = plan.topic or re.sub(r"^[A-Za-z]+:(?=\S)", "", top)  # a planner label, else the main page cited
        if topic and plan.kind != "meta":
            memory.set_topic(self.conn, session_id, topic)
        yield Done(answer, cited, sources, related, memory.get_topic(self.conn, session_id),
                   verification=record, revised=revised)

    @staticmethod
    def _clean_draft(raw: str, passages: list[dict]) -> str:
        """The model's draft tidied: "[n]" / "[n4]" copied from the prompt, a closing offer of its own, a stray
        'nothing on that' line; then lines it left uncited get a "[n*]" tag where one passage clearly matches
        (core/attribution.py). The streamed text is the raw draft; this is what Done carries and what is saved."""
        raw = re.sub(r"\[n(\d+)\]", r"[\1]", re.sub(r"\s*\[n\]", "", raw))
        answer = soften_refusal(strip_closing_offer(raw.strip()))
        return attribute(answer, [p["text"] for p in passages]).text

    def _linked_pages(self, text: str) -> tuple[str, list[int]]:
        """(the text with each Elanthipedia link replaced by its page's title in quotes, the linked pages' ids). A link
        to a redirect reaches its target; a page not in the index still becomes its title, which searches well."""
        from urllib.parse import unquote

        from ..retrieval.titles import normalize

        ids: list[int] = []

        def swap(m: re.Match) -> str:
            raw = unquote(m.group(1)).replace("_", " ").strip()
            row = self.conn.execute("SELECT page_id, title FROM pages WHERE title=? COLLATE NOCASE AND is_redirect=0",
                                    (raw,)).fetchone() or self.conn.execute(
                "SELECT p.page_id, p.title FROM redirects r JOIN pages p ON p.page_id=r.to_page_id WHERE r.from_norm=? "
                "LIMIT 1", (normalize(raw),)).fetchone()
            if row is None:
                return f'"{raw}"'
            if row[0] not in ids:
                ids.append(row[0])
            return f'"{row[1]}"'

        return WIKI_LINK.sub(swap, text), ids

    def _pinned_passages(self, page_ids: list[int]) -> list[dict]:
        out: list[dict] = []
        for pid in page_ids:
            for r in self.conn.execute(
                "SELECT chunk_id, title, section_path, anchor, text FROM chunks WHERE page_id=? ORDER BY ord LIMIT ?",
                (pid, PINNED_CHUNKS),
            ):
                out.append({"title": r["title"], "section": r["section_path"], "text": r["text"],
                            "url": page_url(self.s.wiki.base_url, r["title"], r["anchor"]), "chunk_id": r["chunk_id"]})
        return out

    def _fiction_plan(self, asked: str) -> Plan | None:
        """A request for a story, a poem or roleplay: declined, and answered with what the wiki says about the names in
        it that it has pages for."""
        if not capabilities.FICTION.search(asked):
            return None
        titles: list[str] = []
        groups: set[str] = set()
        for name in grounding.names_in(asked):
            res = resolve.resolve(self.conn, name)
            pick = (res.chosen or res.candidates) if res else []
            if pick and pick[0].title not in titles:
                titles.append(pick[0].title)
                groups.add(pick[0].group)
        shown = [re.sub(r"^[A-Za-z]+:(?=\S)", "", t) for t in titles[:3]]
        question = f"Tell me about {capabilities.names_list(shown)}." if shown else asked
        kind = "character" if groups == {"character"} else "lore"
        return Plan(question, mode="fiction", kind=kind, names=shown)

    def _recheck_plan(self, asked: str, hist: list[dict[str, str]], session_id: str) -> Plan | None:
        """After an answer, two replies are about that answer rather than a new subject:

        - "try again" / "none of those pages had anything to do with it": the same question searched again without the
          pages already shown (RETRY in llm/prompts.py), and an honest "the wiki doesn't seem to cover it" if nothing
          new fits;
        - "are you sure?": the same question read against that answer's own pages plus fresh ones (RECHECK).

        Not after an answer code wrote without searching."""
        if not hist:
            return None
        rejected = capabilities.is_rejection(asked)
        if not rejected and not capabilities.is_challenge(asked):
            return None
        last = memory.last_question(self.conn, session_id)
        # Only an answer written from searched passages can be re-read. An items table is rebuilt by the planner (its
        # passage is not stored), and a story decline has nothing to check.
        if last is None or last["answer_mode"] not in ("search", "recheck", "retry"):
            return None
        kind = last["kind"] or "general"
        if rejected:
            return Plan(last["standalone_q"], mode="retry", kind=kind,
                        avoid_titles=memory.recent_source_titles(self.conn, session_id))
        return Plan(last["standalone_q"], mode="recheck", kind=kind, follow_up=True)

    def _answer_system(self, plan: Plan, passages_text: str, hist: list[dict[str, str]]) -> str:
        return prompts.build_answer_system(plan.kind, passages_text, self.name, persona=self.s.persona.enabled,
                                           continuing=bool(hist), pasted=plan.material, today=now_text(),
                                           recheck=plan.mode == "recheck", retry=plan.mode == "retry")

    def _early_plan(self, asked: str, pasted: str, hist: list[dict[str, str]]) -> Plan | None:
        """A plan made by code, without the planner model, for messages code answers on its own: what time it is,
        and (with no conversation to resolve and nothing pasted) the crafting tables. Saves the planner call."""
        if CLOCK_QUESTION.search(asked):
            return Plan(asked, mode="clock", kind="meta")
        if hist or pasted:
            return None
        question = strip_game_name(asked.strip()) or asked
        base = self.s.wiki.base_url
        ready = materials.passage(self.conn, question, base)
        if not ready and (disc := disciplines.passage(self.conn, question, base)):
            ready = [disc]
        return Plan(question, kind="crafting", prebuilt=ready) if ready else None

    def _persist(self, turn: turns.Turn, answer: str, plan: Plan, sources: list[Source], related: list[str],
                 cited: list[int] | None = None, grounding: dict | None = None,
                 verification: dict | None = None, regenerated: bool = False) -> int:
        """Save the answer (the question was saved when the turn began) and close the turn's record."""
        memory.note_plan(self.conn, turn.user_msg_id, plan.standalone_question, plan.mode, plan.kind)
        msg_id = memory.add_message(
            self.conn, turn.session_id, "assistant", answer, standalone_q=plan.standalone_question, mode=plan.mode,
            kind=plan.kind, sources=[s.__dict__ for s in sources], related=related, cited=cited or [],
            turn_id=turn.turn_id,
        )
        turn.finish(msg_id, plan, grounding=grounding or plan.signals or None, verify=verification,
                    regenerated=regenerated)
        return msg_id

    async def ask(self, session_id: str, message: str, *, kind_override: str | None = None) -> Done:
        done: Done | None = None
        async for ev in self.answer_stream(session_id, message, kind_override=kind_override):
            if isinstance(ev, Done):
                done = ev
        assert done is not None
        return done
