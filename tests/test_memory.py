import asyncio

import pytest
from fastapi.testclient import TestClient

from epedia.config import Settings
from epedia.core import memory
from epedia.core.assistant import Assistant, Done, Token
from epedia.db import connect
from epedia.frontends.web.app import create_app


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    yield c
    c.close()


def say(conn, sid, question, answer, *, standalone=None, sources=None, related=None):
    memory.add_message(conn, sid, "user", question, standalone_q=standalone or question)
    memory.add_message(conn, sid, "assistant", answer, standalone_q=standalone or question, sources=sources, related=related)


def test_a_conversation_is_titled_by_its_first_question_and_listed_newest_first(conn):
    a = memory.new_session(conn, "web")
    say(conn, a, "How do I start forging?", "Visit the hall.")
    say(conn, a, "And what tools?", "Tongs.")
    b = memory.new_session(conn, "web")
    say(conn, b, "Who is Hodierna?", "A goddess.")
    memory.new_session(conn, "web")  # opened but never used: not listed
    cli = memory.new_session(conn, "cli")
    say(conn, cli, "cli question", "cli answer")
    rows = memory.list_sessions(conn, "web")
    assert [r["title"] for r in rows] == ["Who is Hodierna?", "How do I start forging?"]  # the CLI chat is not here
    assert rows[1]["questions"] == 2


def test_setting_a_topic_aside_keeps_the_transcript_but_not_the_memory(conn):
    sid = memory.new_session(conn, "web")
    say(conn, sid, "Tell me about forging", "Forging is...", standalone="Tell me about forging")
    memory.set_topic(conn, sid, "Forging")
    assert len(memory.history(conn, sid, 4)) == 2 and memory.get_topic(conn, sid) == "Forging"
    memory.clear_context(conn, sid)
    assert memory.history(conn, sid, 4) == [] and memory.recent_questions(conn, sid) == []
    assert memory.get_topic(conn, sid) is None
    assert len(memory.messages(conn, sid)) == 2  # still visible
    say(conn, sid, "Who is Hodierna?", "A goddess.")
    assert [m["content"] for m in memory.history(conn, sid, 4)] == ["Who is Hodierna?", "A goddess."]


def test_recent_questions_are_the_standalone_forms_oldest_first(conn):
    sid = memory.new_session(conn, "web")
    for i in range(10):
        say(conn, sid, f"it {i}?", "ok", standalone=f"What about thing {i}?")
    got = memory.recent_questions(conn, sid, 3)
    assert got == ["What about thing 7?", "What about thing 8?", "What about thing 9?"]


def test_pages_already_cited_or_offered_are_remembered_as_seen(conn):
    sid = memory.new_session(conn, "web")
    say(conn, sid, "q", "a", sources=[{"n": 1, "title": "Forging guide"}], related=["Blacksmithing"])
    assert memory.seen_titles(conn, sid) == {"Forging guide", "Blacksmithing"}
    assert memory.seen_titles(conn, memory.new_session(conn, "web")) == set()


def test_deleting_a_conversation_removes_its_messages(conn):
    sid = memory.new_session(conn, "web")
    say(conn, sid, "q", "a")
    assert memory.delete_session(conn, sid) is True and memory.delete_session(conn, sid) is False
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_history_is_trimmed_and_citation_markers_removed(conn):
    sid = memory.new_session(conn, "web")
    say(conn, sid, "q", "Hogs are small [2][3*]. " + "x" * 3000)
    out = memory.history(conn, sid, 4)[1]["content"]
    assert "[2]" not in out and "[3*]" not in out and len(out) <= memory.MAX_HISTORY_CHARS + 4


# --------------------------------------------------------------------------- the assistant's use of it


class FakeOllama:
    """Scripted planner answers; embeds to a constant; the answer model is never reached (no passages exist)."""

    def __init__(self, plans):
        self.plans, self.seen_prompts = list(plans), []

    async def chat_json(self, messages, schema, **kw):
        self.seen_prompts.append(messages[-1]["content"])
        return self.plans.pop(0)

    async def embed(self, texts, **kw):
        return [[0.0] * 768 for _ in texts]

    async def chat_stream(self, messages, **kw):
        yield "never"

    async def aclose(self):
        pass


def run_turn(assistant, sid, text):
    async def go():
        out = []
        async for ev in assistant.answer_stream(sid, text):
            out.append(ev)
        return out

    return asyncio.run(go())


def make_assistant(conn, plans):
    return Assistant(Settings(), conn, FakeOllama(plans))


def test_a_subjectless_first_message_is_answered_with_a_question_once(conn):
    ask = {"standalone_question": "How much does it cost?", "mode": "search", "kind": "general",
           "follow_up": False, "clarify": "What would you like the price of?"}
    a = make_assistant(conn, [ask, ask])
    sid = memory.new_session(conn, "web")
    first = run_turn(a, sid, "how much does it cost?")
    assert any(isinstance(e, Token) and "price of" in e.text for e in first)
    assert isinstance(first[-1], Done) and first[-1].sources == []
    assert memory.last_was_clarification(conn, sid)
    second = run_turn(a, sid, "how much does it cost?")  # never twice in a row: it now does its best
    assert not any(isinstance(e, Token) and "price of" in e.text for e in second)


def test_clarify_is_ignored_when_the_conversation_already_gives_a_subject(conn):
    sid = memory.new_session(conn, "web")
    say(conn, sid, "Tell me about the broadsword", "It is a heavy edged weapon.")
    plan = {"standalone_question": "How much does the broadsword cost?", "mode": "search", "kind": "item",
            "follow_up": True, "clarify": "Which item?"}
    a = make_assistant(conn, [plan])
    events = run_turn(a, sid, "how much is it?")
    assert not any(isinstance(e, Token) and "Which item" in e.text for e in events)


def test_the_planner_sees_earlier_questions_and_the_recent_turns(conn):
    sid = memory.new_session(conn, "web")
    for i in range(4):
        say(conn, sid, f"q{i}", f"a{i}", standalone=f"standalone {i}")
    plan = {"standalone_question": "x", "mode": "search", "kind": "general", "follow_up": False}
    a = make_assistant(conn, [plan])
    run_turn(a, sid, "next")
    prompt = a.ollama.seen_prompts[0]
    assert "standalone 0" in prompt and "standalone 1" in prompt  # older questions, listed
    assert "user: q3" in prompt and "assistant: a3" in prompt  # the last turns, in full


def test_offers_skip_pages_already_cited_or_offered(conn):
    a = make_assistant(conn, [])
    assert a._related_pages("lore", [], [], {"Anything"}) == []  # no passages: nothing to offer, and no crash


# --------------------------------------------------------------------------- the web API


@pytest.fixture
def client(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    with TestClient(create_app(s)) as c:
        yield c


LOCAL = {"X-QQE": "1", "Host": "localhost:8765"}


def test_past_chats_api_lists_and_returns_a_conversation_with_its_topic(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    sid = memory.new_session(c, "web")
    say(c, sid, "How do I start forging?", "Visit the hall [1].", sources=[{"n": 1, "title": "Forging guide"}])
    memory.set_topic(c, sid, "Forging")
    c.close()
    with TestClient(create_app(s)) as client:
        rows = client.get("/api/sessions").json()
        assert rows[0]["title"] == "How do I start forging?" and rows[0]["topic"] == "Forging"
        got = client.get(f"/api/sessions/{sid}/messages").json()
        assert got["topic"] == "Forging" and [m["role"] for m in got["messages"]] == ["user", "assistant"]
        assert got["messages"][1]["sources"][0]["title"] == "Forging guide"


def test_an_unknown_session_id_starts_a_fresh_conversation_and_says_so(tmp_path, monkeypatch):
    async def fake(self, session_id, message, **kw):
        yield Token("hi")
        yield Done("hi", [], [], [], None)

    monkeypatch.setattr(Assistant, "answer_stream", fake)
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    with TestClient(create_app(s)) as client:
        text = client.post("/api/chat", json={"session_id": "stale-id", "message": "hello"}).text
        assert text.startswith("event: session")
        new_id = text.split('"session_id": "')[1].split('"')[0]
        assert new_id != "stale-id" and client.get(f"/api/sessions/{new_id}/messages").status_code == 200


def test_unknown_conversation_is_a_404_and_guarded_calls_need_the_header(client):
    assert client.get("/api/sessions/nope/messages").status_code == 404
    sid = client.post("/api/sessions").json()["session_id"]
    assert client.post(f"/api/sessions/{sid}/forget-context", headers={"Host": "localhost:8765"}).status_code == 403
    assert client.delete(f"/api/sessions/{sid}", headers={"Host": "localhost:8765"}).status_code == 403
    assert client.post(f"/api/sessions/{sid}/forget-context", headers=LOCAL).json() == {"topic": None}
    assert client.delete(f"/api/sessions/{sid}", headers=LOCAL).json() == {"deleted": True}
    assert client.delete(f"/api/sessions/{sid}", headers=LOCAL).status_code == 404


def test_a_question_the_planner_had_to_rewrite_counts_as_a_follow_up_even_if_it_forgot_the_flag(conn):
    sid = memory.new_session(conn, "web")
    say(conn, sid, "Tell me about the broadsword", "It is a heavy edged weapon.")
    hist = memory.history(conn, sid, 4)
    rewritten = {"standalone_question": "How much does the broadsword cost?", "mode": "search", "kind": "item",
                 "follow_up": False}
    same = {"standalone_question": "Who is Hodierna?", "mode": "search", "kind": "lore", "follow_up": False}

    async def plan(data, text):
        return await make_assistant(conn, [data]).plan(hist, text, sid)

    assert asyncio.run(plan(rewritten, "how much does it cost?")).follow_up is True
    assert asyncio.run(plan(same, "Who is Hodierna?")).follow_up is False  # untouched: a change of subject


def test_context_words_make_a_message_a_follow_up_but_named_subjects_do_not():
    from epedia.core.assistant import ANAPHORA

    for text in ("What about them?", "and its price?", "Tell me more", "how about the second one", "Where is that?"):
        assert ANAPHORA.search(text), text
    for text in ("Who is Hodierna?", "How do I start forging?", "What shops are in Riverhaven?"):
        assert not ANAPHORA.search(text), text


def test_offers_are_ranked_by_closeness_to_the_question_not_by_popularity(conn):
    import struct

    def vec(x, y=0.0):
        return struct.pack("768f", *([x, y] + [0.0] * 766))

    def add_page(pid, title, x, y=0.0):
        conn.execute("INSERT INTO pages(page_id, ns, title, title_norm, updated_at) VALUES (?, 0, ?, ?, ?)", (pid, title, title.lower(), "2026-01-01"))
        conn.execute("INSERT INTO chunks(chunk_id, page_id, revid, ord, kind, title, section_path, text, token_est, content_hash) "
                     "VALUES (?, ?, 1, 0, 'section', ?, '', 'x', 1, ?)", (pid, pid, title, str(pid)))
        conn.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)", (pid, vec(x, y)))

    q = [1.0] + [0.0] * 767
    add_page(1, "Black Spire", 1.0)  # about the question: same direction
    add_page(2, "Crossing", 0.0, 1.0)  # a hub page about something else: orthogonal
    a = make_assistant(conn, [])
    assert a._closest_to_question(["Crossing", "Black Spire"], q) == ["Black Spire"]  # the unrelated hub is dropped
    assert a._closest_to_question(["Crossing"], q) == []  # nothing close enough: no offer at all
