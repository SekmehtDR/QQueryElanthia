"""Thumbs up or down on an answer: kept on its turn (so with its conversation), only by whoever had that conversation,
shown again when the chat is reopened, and listed for review with why and what the checks recorded."""

import json

import pytest
from fastapi.testclient import TestClient

from epedia.config import Settings
from epedia.core import feedback, memory, turns
from epedia.db import connect
from epedia.frontends.web.app import create_app

LOCAL = {"X-QQE": "1", "Host": "localhost:8765"}
TUNNEL = {"cf-connecting-ip": "203.0.113.7", "cf-ray": "abc123", "x-forwarded-proto": "https"}


def answered(conn, sid, question, answer, verify=None):
    """A finished turn as the assistant records it."""
    u = memory.add_message(conn, sid, "user", question)
    memory.note_plan(conn, u, question, "search", "character")
    t = turns.Turn.start(conn, sid, u)
    a = memory.add_message(conn, sid, "assistant", answer, standalone_q=question, mode="search", kind="character",
                           turn_id=t.turn_id)
    t.finish(a, verify=verify)
    return t.turn_id


@pytest.fixture
def setup(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    sid = memory.new_session(c, "web", owner="local")
    first = answered(c, sid, "Who is Binu?", "Binu is a Human Necromancer [1].")
    second = answered(c, sid, "What guild was he before?", "He hunts Moonwraiths [1?].",
                      verify={"unknown_entities": ["Moonwraiths"], "unsupported": ["He hunts Moonwraiths"]})
    yield s, c, sid, first, second
    c.close()


def test_a_vote_is_kept_on_the_turn_and_can_be_changed_or_taken_back(setup):
    s, c, sid, first, second = setup
    assert feedback.record(c, first, "up") == {"vote": "up", "reasons": [], "note": ""}
    got = feedback.record(c, second, "down", ["wrong", "bogus", "wrong", "context"], "  He was never a hunter.  ")
    assert got == {"vote": "down", "reasons": ["wrong", "context"], "note": "He was never a hunter."}
    row = turns.get(c, second)
    assert row["feedback"] == "down" and row["feedback_at"]
    assert json.loads(row["feedback_reason"]) == {"reasons": ["wrong", "context"], "note": "He was never a hunter."}
    feedback.record(c, second, "up", ["wrong"], "ignored")  # reasons belong to a thumbs-down only
    assert turns.get(c, second)["feedback_reason"] is None
    feedback.record(c, second, None)
    row = turns.get(c, second)
    assert row["feedback"] is None and row["feedback_at"] is None
    with pytest.raises(ValueError):
        feedback.record(c, first, "meh")
    long_note = feedback.record(c, first, "down", [], "x" * 2000)["note"]
    assert len(long_note) == feedback.MAX_NOTE


def test_the_review_lists_thumbs_down_with_why_and_what_the_checks_found(setup):
    s, c, sid, first, second = setup
    feedback.record(c, first, "up")
    feedback.record(c, second, "down", ["wrong"], "Not a hunter")
    rows = feedback.votes(c)
    assert [r["turn_id"] for r in rows] == [second]
    r = rows[0]
    assert r["question"] == "What guild was he before?" and r["answer"].startswith("He hunts")
    assert r["reasons"] == ["wrong"] and r["note"] == "Not a hunter" and r["owner"] == "local"
    assert r["checks"]["unknown_entities"] == ["Moonwraiths"]
    assert {x["turn_id"] for x in feedback.votes(c, vote=None)} == {first, second}
    assert feedback.votes(c, since="2999-01-01") == []
    convo = feedback.conversation(c, second)
    assert [m["role"] for m in convo["messages"]] == ["user", "assistant", "user", "assistant"]
    assert convo["messages"][1]["feedback"] == "up" and convo["messages"][3]["feedback"] == "down"
    assert convo["turn"]["verify"]["unknown_entities"] == ["Moonwraiths"] and convo["turn"]["note"] == "Not a hunter"
    assert feedback.conversation(c, 9999) is None


def test_deleting_a_conversation_takes_its_votes_with_it(setup):
    s, c, sid, first, second = setup
    feedback.record(c, second, "down")
    memory.delete_session(c, sid)
    assert feedback.votes(c, vote=None) == []


def test_a_visitor_votes_on_their_own_answers_the_owner_on_any_and_votes_show_again(setup):
    s, c, sid, first, second = setup
    visitor_sid = memory.new_session(c, "web", owner="someone-else")
    theirs = answered(c, visitor_sid, "Where is the Crossing?", "In Zoluren [1].")
    s.web.access_code = "correct horse battery"
    with TestClient(create_app(s), base_url="https://testserver") as cl:
        url = f"/api/turns/{second}/feedback"
        assert cl.post(url, json={"vote": "down"}, headers={"Host": "localhost:8765"}).status_code == 403  # no X-QQE
        assert cl.post(url, json={"vote": "sideways"}, headers=LOCAL).status_code == 422
        assert cl.post("/api/turns/9999/feedback", json={"vote": "up"}, headers=LOCAL).status_code == 404
        # the owner reviews visitors' answers too
        assert cl.post(f"/api/turns/{theirs}/feedback", json={"vote": "up"}, headers=LOCAL).json()["vote"] == "up"
        ok = cl.post(url, json={"vote": "down", "reasons": ["missed"], "note": "wrong guild"}, headers=LOCAL)
        assert ok.json() == {"vote": "down", "reasons": ["missed"], "note": "wrong guild"}
        msgs = cl.get(f"/api/sessions/{sid}/messages").json()["messages"]
        assert [(m["turn_id"], m["feedback"]) for m in msgs if m["role"] == "assistant"] == [(first, None), (second, "down")]
        assert cl.get("/api/persona").json()["feedback_reasons"] == feedback.REASONS
        # a visitor through the tunnel votes on their own answers, never on the owner's
        cl.post("/login", json={"code": "correct horse battery"}, headers=TUNNEL)
        assert cl.post(url, json={"vote": "up"}, headers={**TUNNEL, "X-QQE": "1"}).status_code == 404
    assert turns.get(c, second)["feedback"] == "down"


def test_the_owner_reviewing_a_visitors_answer_votes_beside_them_never_over_them(setup):
    settings, c, *_ = setup
    theirs = memory.new_session(c, "web", owner="someone-else")
    tid = answered(c, theirs, "Who is Binu?", "A mage [1].")
    feedback.record(c, tid, "down", ["wrong"], "He is a Necromancer")
    with TestClient(create_app(settings)) as client:
        assert client.post(f"/api/turns/{tid}/feedback", headers=LOCAL, json={"vote": "up"}).status_code == 200
        row = c.execute("SELECT feedback, feedback_reason, owner_feedback FROM turns WHERE turn_id=?", (tid,)).fetchone()
        assert row["feedback"] == "down" and "He is a Necromancer" in row["feedback_reason"] and row["owner_feedback"] == "up"
        (m,) = [x for x in client.get(f"/api/sessions/{theirs}/messages", headers=LOCAL).json()["messages"] if x["role"] == "assistant"]
        assert (m["feedback"], m["reasons"], m["note"], m["owner_feedback"]) == ("down", ["wrong"], "He is a Necromancer", "up")
        assert {(v["by"], v["feedback"]) for v in feedback.votes(c, None) if v["turn_id"] == tid} == {("visitor", "down"), ("you, reviewing", "up")}
        assert client.post(f"/api/turns/{tid}/feedback", headers=LOCAL, json={"vote": None}).status_code == 200  # takes back mine
        assert tuple(c.execute("SELECT feedback, owner_feedback FROM turns WHERE turn_id=?", (tid,)).fetchone()) == ("down", None)  # not theirs


def test_the_owners_vote_on_their_own_chat_is_an_ordinary_vote(setup):
    settings, c, _, first, _ = setup
    with TestClient(create_app(settings)) as client:
        assert client.post(f"/api/turns/{first}/feedback", headers=LOCAL, json={"vote": "down"}).status_code == 200
    assert tuple(c.execute("SELECT feedback, owner_feedback FROM turns WHERE turn_id=?", (first,)).fetchone()) == ("down", None)


def test_your_thumbs_down_on_a_visitors_answer_puts_it_on_the_list_and_says_whose(setup):
    _, c, *_ = setup
    theirs = memory.new_session(c, "web", owner="someone-else")
    tid = answered(c, theirs, "Who is Binu?", "A mage [1].")
    feedback.record(c, tid, "down", ["missed"], "mine", reviewing=True)
    row = next(r for r in feedback.review(c) if r["turn_id"] == tid)
    assert row["flags"] == ["your thumbs down"] and row["owner_reasons"] == ["missed"] and row["feedback"] is None


def test_what_people_liked_is_listed_with_who_liked_it(setup):
    settings, c, _, first, _ = setup
    assert feedback.review(c, liked=True) == []
    feedback.record(c, first, "up")
    (row,) = feedback.review(c, liked=True)
    assert row["turn_id"] == first and row["flags"] == ["thumbs up"]
    assert first not in [r["turn_id"] for r in feedback.review(c)]  # being liked is not a problem
    theirs = memory.new_session(c, "web", owner="someone-else")
    tid = answered(c, theirs, "Who?", "x [1].")
    feedback.record(c, tid, "up", reviewing=True)
    assert feedback.review(c, liked=True)[0]["flags"] == ["your thumbs up"]
    with TestClient(create_app(settings)) as client:
        assert {r["turn_id"] for r in client.get("/api/review/turns?liked=true", headers=LOCAL).json()} == {first, tid}
        assert client.get("/api/review/turns?liked=true", headers={**LOCAL, **TUNNEL}).status_code == 403


def test_archiving_an_answer_only_changes_the_owners_lists_never_the_queue(setup):
    _, c, _, first, _ = setup
    feedback.record(c, first, "down", note="wrong")
    assert first in [r["turn_id"] for r in feedback.review(c)]
    assert feedback.set_archived(c, first) and not feedback.set_archived(c, 99999)
    assert first not in [r["turn_id"] for r in feedback.review(c)]
    assert [r["turn_id"] for r in feedback.review(c, archived=True)] == [first]
    assert first in [q["turn"]["turn_id"] for q in feedback.queue(c)]  # still to be worked through
    feedback.set_archived(c, first, on=False)
    assert first in [r["turn_id"] for r in feedback.review(c)] and feedback.review(c, archived=True) == []


def test_a_deleted_chats_thumbs_down_can_be_archived_and_stays_in_the_queue(setup):
    _, c, sid, first, _ = setup
    feedback.record(c, first, "down")
    memory.delete_session(c, sid)
    (row,) = [r for r in feedback.review(c) if r.get("chat_deleted")]
    assert feedback.set_archived(c, row["turn_id"])
    assert not [r for r in feedback.review(c) if r.get("chat_deleted")]
    assert [r["turn_id"] for r in feedback.review(c, archived=True) if r.get("chat_deleted")] == [first]
    assert first in [q["turn"]["turn_id"] for q in feedback.queue(c)]


def test_a_new_vote_brings_back_what_was_archived(setup):
    _, c, sid, first, _ = setup
    feedback.record(c, first, "up")
    feedback.set_archived(c, first)
    memory.set_archived(c, sid)
    assert feedback.review(c, liked=True) == []
    feedback.record(c, first, "down")
    assert first in [r["turn_id"] for r in feedback.review(c)]
    assert sid in [r["session_id"] for r in memory.list_sessions(c)]


def test_an_archived_chat_leaves_the_lists_but_stays_readable_and_comes_back_if_it_carries_on(setup):
    _, c, sid, first, second = setup
    feedback.record(c, first, "down")
    assert memory.set_archived(c, sid) and not memory.set_archived(c, "nope")
    assert memory.list_sessions(c) == [] and [r["session_id"] for r in memory.list_sessions(c, archived=True)] == [sid]
    assert first not in [r["turn_id"] for r in feedback.review(c)]
    assert memory.messages(c, sid) and first in [q["turn"]["turn_id"] for q in feedback.queue(c)]
    c.execute("UPDATE sessions SET archived_at='2000-01-01T00:00:00Z' WHERE session_id=?", (sid,))  # older than its last message
    assert [r["session_id"] for r in memory.list_sessions(c)] == [sid] and memory.list_sessions(c, archived=True) == []


def test_only_the_owner_at_this_computer_can_archive(setup):
    settings, c, sid, first, _ = setup
    with TestClient(create_app(settings)) as client:
        for url, known in ((f"/api/sessions/{sid}/archive", "/api/sessions/nope/archive"), (f"/api/review/turns/{first}/archive", "/api/review/turns/99999/archive")):
            assert client.post(url, json={"on": True}, headers={**LOCAL, **TUNNEL}).status_code == 403
            assert client.post(url, json={"on": True}, headers={"Host": "localhost:8765"}).status_code == 403  # no X-QQE
            assert client.post(known, json={"on": True}, headers=LOCAL).status_code == 404
            assert client.post(url, json={"on": True}, headers=LOCAL).json() == {"archived": True}
        assert client.get("/api/sessions?archived=true", headers=LOCAL).json()[0]["session_id"] == sid
        assert client.get("/api/sessions?archived=true", headers={**LOCAL, **TUNNEL}).status_code == 403
        assert client.post(f"/api/sessions/{sid}/archive", json={"on": False}, headers=LOCAL).json() == {"archived": False}
        assert client.get("/api/sessions", headers=LOCAL).json()[0]["session_id"] == sid


def test_the_page_shows_what_a_visitor_said_and_what_people_liked():
    from epedia.frontends.web.app import STATIC

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "showTheirVote" in html and "viewingVisitor ? m.owner_feedback : m.feedback" in html
    assert 'id="likedlist"' in html and "liked=true" in html
    assert "d.textContent = 'The visitor gave this" in html  # their words are text, never markup


def test_the_page_offers_thumbs_with_reasons_under_each_answer():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "src/epedia/frontends/web/static/index.html").read_text(encoding="utf-8")
    assert "function addVotes(" in html and "'/api/turns/' + turnId + '/feedback'" in html
    assert "addActions(a, full, { id: data.turn_id," in html  # a fresh answer
    assert "feedback: viewingVisitor ? m.owner_feedback : m.feedback" in html  # a reopened chat shows the reader's own vote
    assert "persona.feedback_reasons" in html and "What went wrong? (optional)" in html
    assert "state === vote ? null : vote" in html  # a second click takes the vote back


def test_the_review_commands_work_on_a_database_older_than_the_code(setup, monkeypatch):
    from typer.testing import CliRunner

    from epedia import cli

    settings, c, sid, first, _ = setup
    feedback.record(c, first, "down", ["wrong"])
    for column in ("owner_feedback", "owner_feedback_reason", "owner_feedback_at"):
        c.execute(f"ALTER TABLE turns DROP COLUMN {column}")
    monkeypatch.setattr(cli, "settings", lambda: settings)
    run = CliRunner()
    for args in (["feedback"], ["feedback", str(first)], ["chats", "list", "--flagged"], ["chats", "show", sid], ["chats", "export", str(first)]):
        out = run.invoke(cli.app, args)
        assert out.exit_code == 0, (args, out.output)
