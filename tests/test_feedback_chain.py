"""The chain to an answer: recorded while it is written, and worked through from real votes."""

import asyncio
import json

import pytest
from typer.testing import CliRunner

from epedia import __version__, cli
from epedia.config import Settings
from epedia.core import feedback, memory, turns
from epedia.core.assistant import Assistant
from epedia.db import connect

DIM = 768


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    return s


@pytest.fixture
def conn(settings):
    c = connect(settings)
    pid = 1
    text = "Binu is a Necromancer of the Moon."
    c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, fetched_revid, indexed_revid, updated_at, page_kind, is_redirect) "
              "VALUES (?,0,'Binu','binu',1,1,1,'2026-01-01','pc',0)", (pid,))
    c.execute("INSERT INTO titles_fts(title_norm, page_id, is_redirect) VALUES ('binu',?,0)", (pid,))
    c.execute("INSERT INTO chunks(chunk_id, page_id, revid, ord, kind, title, section_path, text, token_est, content_hash) "
              "VALUES (1,?,1,0,'section','Binu','',?,1,'h')", (pid, text))
    c.execute("INSERT INTO chunks_fts(rowid, title, section_path, text) VALUES (1,'Binu','',?)", (text,))
    c.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (1,?)", (json.dumps([1.0] + [0.0] * (DIM - 1)),))
    yield c
    c.close()


class Model:
    def __init__(self, *replies):
        self.replies = list(replies)

    async def chat_json(self, messages, *a, **k):
        return {"standalone_question": "Who is Binu?", "mode": "search", "kind": "character", "follow_up": False}

    async def embed(self, texts, **k):
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

    async def chat_stream(self, messages, **k):
        yield self.replies.pop(0)


def ask(c, sid, settings, *replies):
    async def go():
        return [e async for e in Assistant(settings, c, Model(*replies)).answer_stream(sid, "Who is Binu?")]

    return asyncio.run(go())[-1]


def test_an_answer_records_how_it_was_reached_with_no_vote_needed(conn, settings):
    sid = memory.new_session(conn, "web")
    done = ask(conn, sid, settings, "Binu is a Necromancer [1].")
    row = conn.execute("SELECT chain, version, plan_json FROM turns WHERE turn_id=?", (done.turn_id,)).fetchone()
    chain = turns.unpack_chain(row["chain"])
    assert row["version"] == __version__
    assert chain["passages"][0]["title"] == "Binu" and "Necromancer of the Moon" in chain["passages"][0]["text"]
    assert chain["sent_to_model"][0]["role"] == "system" and "Necromancer of the Moon" in chain["sent_to_model"][0]["content"]
    assert chain["sent_to_model"][-1]["content"] == "Who is Binu?"
    assert chain["drafts"] == [{"temperature": Settings().chat.temperature, "text": "Binu is a Necromancer [1].", "looped": False}]
    assert "Latest user message: Who is Binu?" in json.loads(row["plan_json"])["planner_input"]


def test_a_withdrawn_draft_and_what_the_model_was_told_stay_in_the_chain(conn, settings):
    sid = memory.new_session(conn, "web")
    done = ask(conn, sid, settings, "Binu serves Zarnak the Unbound of Moonwraith Keep [1].", "Binu is a Necromancer [1].")
    assert done.revised
    chain = turns.unpack_chain(conn.execute("SELECT chain FROM turns WHERE turn_id=?", (done.turn_id,)).fetchone()[0])
    assert "Zarnak" in chain["drafts"][0]["text"] and "Zarnak" in chain["told_on_rewrite"] and len(chain["drafts"]) == 2
    assert "Zarnak" not in done.answer


def test_a_failed_answer_keeps_what_it_had_got_to(conn, settings):
    sid = memory.new_session(conn, "web")

    class Broken(Model):
        async def chat_stream(self, messages, **k):
            raise RuntimeError("the model went away")
            yield ""

    async def go():
        async for _ in Assistant(settings, conn, Broken()).answer_stream(sid, "Who is Binu?"):
            pass

    with pytest.raises(RuntimeError):
        asyncio.run(go())
    row = conn.execute("SELECT chain, error FROM turns ORDER BY turn_id DESC LIMIT 1").fetchone()
    assert "went away" in row["error"] and turns.unpack_chain(row["chain"])["passages"][0]["title"] == "Binu"


def test_the_queue_is_real_votes_with_thumbs_down_first_and_each_with_its_chain(conn, settings):
    sid = memory.new_session(conn, "web")
    unvoted = ask(conn, sid, settings, "Binu is a Necromancer [1].").turn_id
    liked = ask(conn, sid, settings, "Binu is a Necromancer [1].").turn_id
    disliked = ask(conn, sid, settings, "Binu is a Necromancer [1].").turn_id
    newest_liked = ask(conn, sid, settings, "Binu is a Necromancer [1].").turn_id
    for t in (liked, newest_liked):
        feedback.record(conn, t, "up")
    feedback.record(conn, disliked, "down", ["wrong"], "He is not")
    got = feedback.queue(conn)
    assert [r["turn"]["turn_id"] for r in got] == [disliked, newest_liked, liked] and unvoted not in [r["turn"]["turn_id"] for r in got]
    top = got[0]
    assert top["turn"]["reasons"] == ["wrong"] and top["turn"]["note"] == "He is not"
    assert top["turn"]["chain"]["passages"][0]["title"] == "Binu" and top["turn"]["verify"] is not None
    assert [m["role"] for m in top["messages"]].count("user") == 3  # the conversation up to that answer, and no further


def test_a_vote_worked_through_leaves_the_queue_until_it_changes(conn, settings):
    sid = memory.new_session(conn, "web")
    t = ask(conn, sid, settings, "Binu is a Necromancer [1].").turn_id
    assert not feedback.mark_processed(conn, t, "nothing voted on yet")
    feedback.record(conn, t, "down")
    assert feedback.mark_processed(conn, t, "Fixed the planner; eval/questions.yaml has a case")
    assert feedback.queue(conn) == []
    (kept,) = feedback.queue(conn, include_processed=True)
    assert kept["turn"]["processed_note"].startswith("Fixed the planner")
    feedback.record(conn, t, "up")
    assert [r["turn"]["turn_id"] for r in feedback.queue(conn)] == [t]


def test_an_owners_review_vote_is_queued_beside_the_visitors(conn, settings):
    sid = memory.new_session(conn, "web", owner="visitor")
    t = ask(conn, sid, settings, "Binu is a Necromancer [1].").turn_id
    feedback.record(conn, t, "down", ["missed"], reviewing=True)
    (got,) = feedback.queue(conn)
    assert got["turn"]["owner_feedback"] == "down" and got["turn"]["owner_reasons"] == ["missed"] and got["turn"]["feedback"] is None


def test_deleting_a_chat_deletes_its_turns_but_a_vote_stays_in_the_queue_with_its_own_copy(conn, settings):
    sid = memory.new_session(conn, "web")
    t = ask(conn, sid, settings, "Binu is a Necromancer [1].").turn_id
    feedback.record(conn, t, "down")
    memory.delete_session(conn, sid)
    assert conn.execute("SELECT count(*) FROM turns WHERE turn_id=?", (t,)).fetchone()[0] == 0
    assert [r["turn"]["turn_id"] for r in feedback.queue(conn)] == [t]


def test_the_commands_write_the_queue_and_mark_it_done(conn, settings, tmp_path, monkeypatch):
    sid = memory.new_session(conn, "web")
    t = ask(conn, sid, settings, "Binu is a Necromancer [1].").turn_id
    feedback.record(conn, t, "down", ["wrong"], "He is not")
    monkeypatch.setattr(cli, "settings", lambda: settings)
    target = tmp_path / "out" / "queue.json"
    run = CliRunner()
    out = run.invoke(cli.app, ["feedback-queue", "--out", str(target)])
    assert out.exit_code == 0 and f"#{t}" in out.output and "chain" in out.output and "1 to work through" in out.output
    (saved,) = json.loads(target.read_text(encoding="utf-8"))
    assert saved["turn"]["chain"]["sent_to_model"][0]["role"] == "system"
    assert "done" in run.invoke(cli.app, ["feedback-done", str(t), "--note", "Fixed"]).output
    assert "0 to work through" in run.invoke(cli.app, ["feedback-queue", "--out", str(target)]).output
    assert "no vote" in run.invoke(cli.app, ["feedback-done", "999", "--note", "x"]).output


def test_the_export_of_one_answer_carries_its_chain(conn, settings, monkeypatch):
    sid = memory.new_session(conn, "web")
    t = ask(conn, sid, settings, "Binu is a Necromancer [1].").turn_id
    monkeypatch.setattr(cli, "settings", lambda: settings)
    got = json.loads(CliRunner().invoke(cli.app, ["chats", "export", str(t)]).output)
    assert got["turn"]["chain"]["drafts"][0]["text"].startswith("Binu is")
