"""Every turn is recorded and nothing a person asked is lost: the question is saved first, failures and abandoned
answers leave a trace, the model client survives a dropped connection, and the prompt always fits the context."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from epedia.config import OllamaCfg, Settings
from epedia.core import memory, turns
from epedia.core.assistant import Assistant, Done, Token
from epedia.db import connect
from epedia.frontends.web import access
from epedia.frontends.web.app import create_app
from epedia.llm import prompts
from epedia.llm.ollama import Ollama, OllamaError

PLAN = {"standalone_question": "How big are musk hogs?", "mode": "search", "kind": "creature", "follow_up": False}
PASSAGE = {"title": "Musk hog", "section": "", "url": "u", "text": "Musk hogs are small and smelly.", "chunk_id": None}


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    yield c
    c.close()


class Model:
    """A scripted model: the planner returns PLAN; the answer is `words`, then `fail` is raised if given."""

    def __init__(self, words=("Musk hogs are small [1].",), fail=None, plan_fail=None):
        self.words, self.fail, self.plan_fail, self.planned = list(words), fail, plan_fail, 0

    async def chat_json(self, *a, **k):
        self.planned += 1
        if self.plan_fail:
            raise self.plan_fail
        return dict(PLAN)

    async def embed(self, texts, **k):
        return [[0.0] * 768 for _ in texts]

    async def chat_stream(self, *a, **k):
        for w in self.words:
            yield w
        if self.fail:
            raise self.fail


@pytest.fixture
def one_passage(monkeypatch):
    async def fake_retrieve(self, plan, session_id=None):
        return [dict(PASSAGE)], []

    monkeypatch.setattr(Assistant, "retrieve_with_extras", fake_retrieve)


def run(assistant, sid, text):
    async def go():
        return [e async for e in assistant.answer_stream(sid, text)]

    return asyncio.run(go())


def turn_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM turns ORDER BY turn_id")]


def test_an_answered_turn_is_recorded_with_its_timings_plan_and_messages(conn, one_passage):
    sid = memory.new_session(conn, "web")
    events = run(Assistant(Settings(), conn, Model()), sid, "how big are musk hogs?")
    done = events[-1]
    assert isinstance(done, Done) and done.turn_id is not None
    (t,) = turn_rows(conn)
    assert t["turn_id"] == done.turn_id and t["error"] is None and t["owner"] == "local" and t["frontend"] == "web"
    assert t["total_ms"] is not None and t["planner_ms"] is not None and t["ttft_ms"] is not None
    assert json.loads(t["plan_json"])["standalone_question"] == PLAN["standalone_question"]
    assert "qvec" not in json.loads(t["plan_json"])  # 768 floats are not worth keeping
    rows = memory.messages(conn, sid)
    assert [m["role"] for m in rows] == ["user", "assistant"]  # the question is saved once, not twice
    assert rows[0]["standalone_q"] == PLAN["standalone_question"] and rows[0]["kind"] == "creature"
    msg = conn.execute("SELECT cited, turn_id FROM messages WHERE msg_id=?", (t["assistant_msg_id"],)).fetchone()
    assert json.loads(msg["cited"]) == [1] and msg["turn_id"] == t["turn_id"]


def test_the_question_survives_a_model_that_is_down(conn):
    class Down(Model):
        async def embed(self, texts, **k):
            raise OllamaError("embed failed: ConnectError")

    sid = memory.new_session(conn, "web")
    with pytest.raises(OllamaError):
        run(Assistant(Settings(), conn, Down(plan_fail=OllamaError("connection refused"))), sid, "Who is Hodierna?")
    rows = memory.messages(conn, sid)
    assert [m["role"] for m in rows] == ["user"] and rows[0]["content"] == "Who is Hodierna?"
    assert rows[0]["standalone_q"] == "Who is Hodierna?"  # the planner fell back to the message itself
    (t,) = turn_rows(conn)
    assert "embed failed" in t["error"] and t["assistant_msg_id"] is None and t["total_ms"] is not None


def test_an_answer_that_fails_midway_is_kept_for_the_transcript_but_not_remembered(conn, one_passage):
    sid = memory.new_session(conn, "web")
    model = Model(words=["Musk hogs ", "are"], fail=OllamaError("connection reset"))
    with pytest.raises(OllamaError):
        run(Assistant(Settings(), conn, model), sid, "how big are musk hogs?")
    rows = memory.messages(conn, sid)
    assert [(m["role"], m["mode"]) for m in rows] == [("user", "search"), ("assistant", "error")]
    assert rows[1]["content"] == "Musk hogs are"
    (t,) = turn_rows(conn)
    assert "connection reset" in t["error"] and t["assistant_msg_id"] is not None
    assert [m["role"] for m in memory.history(conn, sid, 4)] == ["user"]  # the broken half-answer is not context


def test_an_answer_the_reader_walks_away_from_is_recorded_as_aborted(conn, one_passage):
    sid = memory.new_session(conn, "web")
    a = Assistant(Settings(), conn, Model(words=["Musk ", "hogs ", "are small."]))

    async def go():
        gen = a.answer_stream(sid, "how big are musk hogs?")
        async for ev in gen:
            if isinstance(ev, Token):
                break  # the page was closed after the first word
        await gen.aclose()

    asyncio.run(go())
    (t,) = turn_rows(conn)
    assert t["error"] == "aborted by the reader"
    assert memory.messages(conn, sid)[-1]["content"] == "Musk"


def test_the_clock_and_crafting_tables_are_answered_without_the_planner(conn, monkeypatch):
    from epedia.core import assistant as asst

    model = Model()
    a = Assistant(Settings(), conn, model)
    events = run(a, memory.new_session(conn, "web"), "What time is it?")
    assert model.planned == 0 and "By my reckoning" in "".join(e.text for e in events if isinstance(e, Token))
    assert turn_rows(conn)[-1]["error"] is None

    table = [{"title": "Category:Crafting materials", "section": "", "url": "u", "text": "t", "chunk_id": None,
              "answer": "Use animite [1]."}]
    built = []
    monkeypatch.setattr(asst.materials, "passage", lambda conn, q, base: built.append(q) or table)
    events = run(a, memory.new_session(conn, "web"), "What is the best metal for a sword?")
    assert model.planned == 0 and events[-1].answer.startswith("Use animite")
    assert len(built) == 1  # the table built while planning is reused, not built again for retrieval
    assert "prebuilt" not in json.loads(turn_rows(conn)[-1]["plan_json"])
    # ...but with a conversation under way the planner still runs: "and for armor?" needs the thread resolved
    sid = memory.new_session(conn, "web")
    memory.add_message(conn, sid, "user", "Tell me about forging")
    memory.add_message(conn, sid, "assistant", "Forging is hot work.")
    run(a, sid, "What is the best metal for a sword?")
    assert model.planned == 1


# --------------------------------------------------------------------------- the prompt fits the context window


def test_fit_messages_drops_the_oldest_turns_first_then_reports_what_is_still_over():
    hist = [{"role": "user", "content": "q" * 400}, {"role": "assistant", "content": "a" * 400},
            {"role": "user", "content": "r" * 400}, {"role": "assistant", "content": "b" * 400}]
    kept, over = prompts.fit_messages("s" * 400, hist, "now?", num_ctx=1000, reserve=500)
    assert kept == hist[2:] and over == 0  # one exchange (200 tokens) had to go
    kept, over = prompts.fit_messages("s" * 4000, hist, "now?", num_ctx=1000, reserve=500)
    assert kept == [] and over == 1000 + 1 + 500 - 1000  # nothing left to drop: the passages must shrink
    assert prompts.fit_messages("s", hist, "q", num_ctx=16384, reserve=1500) == (hist, 0)


# --------------------------------------------------------------------------- the model client


def _client(handler, retry=True):
    ol = Ollama(OllamaCfg(retry_before_first_token=retry))
    ol._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ollama")
    return ol


def test_a_dropped_connection_is_an_ollama_error_the_planner_can_fall_back_on():
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(OllamaError):
        asyncio.run(_client(refuse).chat_json([{"role": "user", "content": "x"}], {}))

    def not_an_object(request):
        return httpx.Response(200, json={"message": {"content": "[1, 2]"}})

    with pytest.raises(OllamaError):
        asyncio.run(_client(not_an_object).chat_json([{"role": "user", "content": "x"}], {}))


def _lines(*words):
    return "\n".join(json.dumps({"message": {"content": w}, "done": False}) for w in words) + "\n" + json.dumps(
        {"done": True})


def _collect(ol):
    async def go():
        return [t async for t in ol.chat_stream([{"role": "user", "content": "x"}])]

    return asyncio.run(go())


_REAL_SLEEP = asyncio.sleep


def _no_wait(monkeypatch):
    monkeypatch.setattr("epedia.llm.ollama.asyncio.sleep", lambda s: _REAL_SLEEP(0))


def test_an_answer_that_fails_before_its_first_word_is_tried_once_more(monkeypatch):
    _no_wait(monkeypatch)
    calls = []

    def flaky(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadError("model still loading")
        return httpx.Response(200, text=_lines("Hello", " there"))

    assert _collect(_client(flaky)) == ["Hello", " there"] and len(calls) == 2
    calls.clear()
    with pytest.raises(OllamaError):
        _collect(_client(flaky, retry=False))  # switched off: one try only
    assert len(calls) == 1

    def always_down(request):
        calls.append(1)
        raise httpx.ConnectError("refused")

    calls.clear()
    with pytest.raises(OllamaError):
        _collect(_client(always_down))
    assert len(calls) == 2  # once more, not forever


def test_an_answer_that_fails_after_words_were_shown_is_never_restarted(monkeypatch):
    _no_wait(monkeypatch)
    calls = []

    def breaks_midway(request):
        calls.append(1)
        body = json.dumps({"message": {"content": "Hello"}, "done": False}) + "\n" + json.dumps({"error": "out of memory"})
        return httpx.Response(200, text=body)

    got = []

    async def go():
        async for t in _client(breaks_midway).chat_stream([{"role": "user", "content": "x"}]):
            got.append(t)

    with pytest.raises(OllamaError, match="out of memory"):
        asyncio.run(go())
    assert got == ["Hello"] and len(calls) == 1  # the reader would otherwise see "HelloHello..."


# --------------------------------------------------------------------------- the web endpoint

LOCAL = {"X-QQE": "1", "Host": "localhost:8765"}
CODE = "correct horse battery"
TUNNEL = {"cf-connecting-ip": "203.0.113.7", "cf-ray": "abc123", "x-forwarded-proto": "https", "X-QQE": "1"}


def _fake_stream(monkeypatch):
    async def fake(self, session_id, message, **kw):
        yield Token("hi")
        yield Done("hi", [], [], [], None, turn_id=42)

    monkeypatch.setattr(Assistant, "answer_stream", fake)


def test_chat_needs_the_pages_header_and_reports_the_turn_id(tmp_path, monkeypatch):
    _fake_stream(monkeypatch)
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    with TestClient(create_app(s)) as client:
        assert client.post("/api/chat", json={"session_id": "x", "message": "hi"}).status_code == 403
        cross = client.post("/api/chat", json={"session_id": "x", "message": "hi"},
                            headers={**LOCAL, "Origin": "https://evil.example"})
        assert cross.status_code == 403
        text = client.post("/api/chat", json={"session_id": "x", "message": "hi"}, headers=LOCAL).text
        assert '"turn_id": 42' in text


def test_a_tunnel_that_passes_its_public_name_in_x_forwarded_host_is_this_page(tmp_path, monkeypatch):
    _fake_stream(monkeypatch)
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    s.web.access_code = CODE
    with TestClient(create_app(s), base_url="https://testserver") as client:
        client.post("/login", json={"code": CODE}, headers=TUNNEL)
        h = {**TUNNEL, "Host": "localhost:8765", "X-Forwarded-Host": "fennick.example.com",
             "Origin": "https://fennick.example.com"}
        assert client.post("/api/chat", json={"session_id": "x", "message": "hi"}, headers=h).status_code == 200


def test_visitors_are_limited_per_hour_and_the_owner_never_is(tmp_path, monkeypatch):
    _fake_stream(monkeypatch)
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    s.web.access_code = CODE
    s.web.visitor_turns_per_hour = 2
    with TestClient(create_app(s), base_url="https://testserver") as client:
        client.post("/login", json={"code": CODE}, headers=TUNNEL)
        for _ in range(2):
            assert client.post("/api/chat", json={"session_id": "x", "message": "hi"}, headers=TUNNEL).status_code == 200
        over = client.post("/api/chat", json={"session_id": "x", "message": "hi"}, headers=TUNNEL)
        assert over.status_code == 429 and "minutes" in over.json()["detail"]
        for _ in range(3):  # the owner at the computer
            assert client.post("/api/chat", json={"session_id": "x", "message": "hi"}, headers=LOCAL).status_code == 200


def test_the_turn_limiter_counts_per_person_and_zero_means_no_limit():
    lim = access.TurnLimiter(2)
    assert lim.allow("a") and lim.allow("a") and not lim.allow("a") and lim.allow("b")
    assert 1 <= lim.wait_minutes("a") <= 61
    unlimited = access.TurnLimiter(0)
    assert all(unlimited.allow("a") for _ in range(100))


def test_turn_records_close_once_and_get_returns_the_row(conn):
    sid = memory.new_session(conn, "web")
    uid = memory.add_message(conn, sid, "user", "q")
    t = turns.Turn.start(conn, sid, uid)
    t.fail(RuntimeError("boom"))
    t.finish(None)  # already closed: ignored
    row = turns.get(conn, t.turn_id)
    assert row["error"] == "RuntimeError: boom" and turns.get(conn, 999) is None
    assert conn.execute("SELECT turn_id FROM messages WHERE msg_id=?", (uid,)).fetchone()[0] == t.turn_id
