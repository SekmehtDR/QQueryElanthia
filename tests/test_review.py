"""What the owner reads: answers worth a look, a chat as it went, and a bad answer turned into an evaluation question."""

import json

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from epedia import cli
from epedia.config import Settings
from epedia.core import feedback, memory, turns
from epedia.db import connect
from epedia.frontends.web.app import STATIC, create_app

LOCAL = {"Host": "localhost:8765", "X-QQE": "1"}
TUNNEL = {**LOCAL, "cf-connecting-ip": "203.0.113.7", "cf-ray": "abc123", "x-forwarded-proto": "https"}


def answered(c, sid, question, answer, *, verify=None, sources=("Binu",), cited=(1,), kind="character"):
    user = memory.add_message(c, sid, "user", question, standalone_q=question, mode="search", kind=kind)
    t = turns.Turn.start(c, sid, user)
    msg = memory.add_message(c, sid, "assistant", answer, standalone_q=question, mode="search", kind=kind,
                             sources=[{"n": n, "title": s} for n, s in enumerate(sources, 1)], cited=list(cited), turn_id=t.turn_id)
    t.finish(msg, verify=verify)
    return t.turn_id


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    yield c
    c.close()


@pytest.fixture
def chats(conn):
    mine = memory.new_session(conn, "web", owner="local")
    theirs = memory.new_session(conn, "web", owner="visitor-1")
    typed = memory.new_session(conn, "cli")
    ids = {
        "fine": answered(conn, mine, "Who is fine?", "Fine [1]."),
        "names": answered(conn, mine, "Who is Zarn?", "Zarn [1].", verify={"unknown_entities": ["Zarn"]}),
        "voted": answered(conn, theirs, "Who is Binu?", "Wrong [1]."),
        "cli": answered(conn, typed, "Who is typed?", "x [1].", verify={"unknown_entities": ["Q"]}),
    }
    feedback.record(conn, ids["voted"], "down", ["wrong"], "He is a Necromancer")
    return conn, ids, mine, theirs


def test_the_list_is_what_is_worth_a_look_with_thumbs_down_first(chats):
    conn, ids, *_ = chats
    rows = feedback.review(conn)
    assert [r["turn_id"] for r in rows] == [ids["voted"], ids["names"]]  # the web page's, not the command line's; nothing fine
    assert rows[0]["flags"] == ["thumbs down"] and rows[0]["note"] == "He is a Necromancer"
    assert rows[1]["flags"] == ["named things no page has"]


def test_the_list_filters_by_who_when_and_where(chats):
    conn, ids, *_ = chats
    assert [r["turn_id"] for r in feedback.review(conn, owner="visitors")] == [ids["voted"]]
    assert [r["turn_id"] for r in feedback.review(conn, owner="local")] == [ids["names"]]
    assert ids["cli"] in [r["turn_id"] for r in feedback.review(conn, frontend=None)]
    assert feedback.review(conn, since="2999-01-01") == []
    assert ids["fine"] in [r["turn_id"] for r in feedback.review(conn, flagged=False)]


@pytest.mark.parametrize("row, expected", [
    ({"error": "boom"}, ["failed"]),
    ({"checks": {"numbers_bad": 1}}, ["a figure its page does not have"]),
    ({"checks": {"unsupported": ["x"]}}, ["lines its pages do not back"]),
    ({"regenerated": 1}, ["rewritten"]),
    ({"feedback": "up"}, []),
])
def test_why_an_answer_is_worth_a_look(row, expected):
    assert feedback.why_flagged(row) == expected


def test_a_marked_line_alone_does_not_put_an_answer_on_the_list(conn):
    sid = memory.new_session(conn, "web", owner="local")
    answered(conn, sid, "One?", "x [1].", verify={"unsupported": ["x"]})
    both = answered(conn, sid, "Two?", "y [1].", verify={"unsupported": ["y"], "numbers_bad": 1})
    (row,) = feedback.review(conn)
    assert row["turn_id"] == both and row["flags"] == ["a figure its page does not have", "lines its pages do not back"]


def test_a_failed_turn_with_no_answer_is_listed_too(conn):
    sid = memory.new_session(conn, "web", owner="local")
    user = memory.add_message(conn, sid, "user", "Who?", standalone_q="Who?")
    turns.Turn.start(conn, sid, user).fail("the model went away")
    (row,) = feedback.review(conn)
    assert row["flags"] == ["failed"] and row["question"] == "Who?"


def test_an_answer_becomes_an_eval_question_that_parses_and_expects_only_what_it_cited(conn):
    sid = memory.new_session(conn, "web")
    answered(conn, sid, "Who is Binu?", "A mage [1].")
    tid = answered(conn, sid, "What guild is he in?", "Necromancer [2].", sources=("Innu", "Necromancer"), cited=(2,), kind="mechanics")
    text = feedback.eval_item(conn, tid)
    assert text.startswith("# Check before adding")
    (item,) = yaml.safe_load(text)
    assert item == {"q": "What guild is he in?", "kind": "mechanics", "expect_pages": ["Necromancer"], "history": ["Who is Binu?"]}
    assert feedback.eval_item(conn, 999) is None


@pytest.fixture
def web(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "w.db")
    with TestClient(create_app(s)) as client:
        c = connect(s)
        sid = memory.new_session(c, "web", owner="visitor-1")
        tid = answered(c, sid, "Who is Binu?", "Wrong [1].")
        feedback.record(c, tid, "down", ["wrong"], "")
        c.commit()
        c.close()
        yield client, tid, sid


def test_the_review_list_and_export_are_for_this_computer_only(web):
    client, tid, sid = web
    rows = client.get("/api/review/turns", headers=LOCAL).json()
    assert [r["turn_id"] for r in rows] == [tid] and rows[0]["session_id"] == sid and rows[0]["owner"] == "visitor-1"
    assert "expect_pages" in client.get(f"/api/review/turns/{tid}/eval", headers=LOCAL).json()["yaml"]
    assert client.get("/api/review/turns", headers=TUNNEL).status_code == 403
    assert client.get(f"/api/review/turns/{tid}/eval", headers=TUNNEL).status_code == 403
    assert client.get("/api/review/turns/999/eval", headers=LOCAL).status_code == 404


def test_the_page_lists_them_and_offers_clarifying_buttons(web):
    client, *_ = web
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="reviewlist"' in html and "loadReview" in html and "/api/review/turns" in html
    assert "showChoices" in html and "pin: pin ?? null" in html
    assert client.get("/", headers=LOCAL).status_code == 200


@pytest.fixture
def run_cli(chats, tmp_path, monkeypatch):
    conn, ids, mine, theirs = chats
    conn.commit()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    s = Settings()
    s.db.path = path
    monkeypatch.setattr(cli, "settings", lambda: s)
    runner = CliRunner()

    def go(*args):
        return runner.invoke(cli.app, ["chats", *args])

    return go, ids, mine, theirs


def test_chats_list_flagged_and_who(run_cli):
    go, ids, mine, _ = run_cli
    flagged = go("list", "--flagged")
    assert flagged.exit_code == 0 and f"#{ids['voted']}" in flagged.output and "thumbs down" in flagged.output
    assert f"#{ids['fine']}" not in flagged.output
    everything = go("list")
    assert mine in everything.output and "Who is fine?" in everything.output
    assert mine not in go("list", "--owner", "visitors").output


def test_chats_show_reads_a_chat_with_its_turn_ids(run_cli):
    go, ids, mine, _ = run_cli
    out = go("show", mine)
    assert out.exit_code == 0 and "Who is Zarn?" in out.output and f"answer #{ids['names']}" in out.output
    assert go("show", "nosuchchat").exit_code == 1


def test_chats_export_as_json_and_as_an_eval_question(run_cli):
    go, ids, *_ = run_cli
    full = json.loads(go("export", str(ids["voted"])).output)
    assert full["turn"]["reasons"] == ["wrong"] and full["messages"][0]["content"] == "Who is Binu?"
    (item,) = yaml.safe_load(go("export", str(ids["voted"]), "--eval").output)
    assert item["q"] == "Who is Binu?" and item["expect_pages"] == ["Binu"]
    assert go("export", "999").exit_code == 1
