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


def test_the_page_offers_thumbs_with_reasons_under_each_answer():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "src/epedia/frontends/web/static/index.html").read_text(encoding="utf-8")
    assert "function addVotes(" in html and "'/api/turns/' + turnId + '/feedback'" in html
    assert "addActions(a, full, { id: data.turn_id })" in html  # a fresh answer
    assert "addActions(a, m.content, { id: m.turn_id, feedback: m.feedback })" in html  # a reopened chat shows its votes
    assert "persona.feedback_reasons" in html and "What went wrong? (optional)" in html
    assert "state === vote ? null : vote" in html  # a second click takes the vote back
