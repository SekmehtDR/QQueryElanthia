"""Bugs found by the code review of 1.3.0 to 1.6.0. Each test failed before its fix."""

import asyncio
import json
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from epedia import cli
from epedia.config import Settings
from epedia.core import feedback, layers, memory, offers, turns
from epedia.core.assistant import Assistant, Plan
from epedia.db import connect
from epedia.frontends.web.app import STATIC
from epedia.llm import prompts

DIM = 768
BASE = "https://elanthipedia.play.net"


def vec(*xs: float) -> bytes:
    v = list(xs) + [0.0] * (DIM - len(xs))
    norm = sum(x * x for x in v) ** 0.5 or 1.0
    return struct.pack(f"{DIM}f", *[x / norm for x in v])


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    yield c
    c.close()


def page(c, pid, title, kind, text="x", direction=(1, 0), ns=0, is_log=0, norm=None):
    c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, fetched_revid, indexed_revid, updated_at, page_kind, "
              "is_log, is_redirect) VALUES (?,?,?,?,1,1,1,'2026-01-01',?,?,0)", (pid, ns, title, title.lower(), kind, is_log))
    c.execute("INSERT INTO titles_fts(title_norm, page_id, is_redirect) VALUES (?,?,0)", ((norm or title).lower(), pid))
    c.execute("INSERT INTO chunks(chunk_id, page_id, revid, ord, kind, title, section_path, text, token_est, content_hash) "
              "VALUES (?,?,1,0,'section',?,'',?,1,?)", (pid, pid, title, text, str(pid)))
    c.execute("INSERT INTO chunks_fts(rowid, title, section_path, text) VALUES (?,?,'',?)", (pid, title, text))
    c.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?,?)", (pid, vec(*direction)))


class Model:
    def __init__(self, plan=None, reply="An answer [1]."):
        self.plan, self.reply = plan or {}, reply
        self.planner_calls, self.system = 0, ""

    async def chat_json(self, messages, *a, **k):
        self.planner_calls += 1
        return {"mode": "search", "kind": "general", "follow_up": False, **self.plan}

    async def embed(self, texts, **k):
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

    async def chat_stream(self, messages, **k):
        self.system = messages[0]["content"]
        yield self.reply


def ask(c, sid, message, model, settings=None, **kw):
    async def go():
        return [e async for e in Assistant(settings or Settings(), c, model).answer_stream(sid, message, **kw)]

    return asyncio.run(go())


# ------------------------------------------------------------------------------------------- the page

@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_pages_sending_function_really_runs_as_far_as_the_send():
    script = Path(__file__).parent / "js" / "run_send.js"
    out = subprocess.run(["node", str(script), str(STATIC / "index.html")], capture_output=True, text=True, timeout=30)
    assert "REACHED SEND" in out.stdout and '"pin":7' in out.stdout, out.stdout + out.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_start_screen_puts_a_fire_on_hot_questions_only():
    script = Path(__file__).parent / "js" / "run_recent.js"
    out = subprocess.run(["node", str(script), str(STATIC / "index.html")], capture_output=True, encoding="utf-8", timeout=30)
    assert json.loads(out.stdout) == [  # each further asking is 1px larger, up to 10; the count keeps going, and the fire stays in front
        {"q": "Who are the dragon priests?", "fire": "\U0001F525", "times": "x4", "more": 3, "first": "hot"},
        {"q": "What metals are yellow?", "fire": None, "times": "x25", "more": 10, "first": "times"},
        {"q": "Where is Shard?", "fire": None, "times": "x1", "more": 0, "first": "times"}], out.stdout + out.stderr


# ------------------------------------------------------------------------------------------- hunting ranks

def plan_for(question, asked=""):
    return Plan(question, asked=asked or question)


def test_a_number_given_now_is_not_overridden_by_an_older_unrelated_one(conn):
    sid = memory.new_session(conn, "web")
    memory.add_message(conn, sid, "user", "is 50 ranks of forging enough?")
    q = "I have 300 offense and 280 defense, what should I hunt?"
    memory.add_message(conn, sid, "user", q)
    assert Assistant(Settings(), conn, Model())._hunt_ranks(plan_for(q), q, sid) == [300, 280]


def test_ranks_of_a_craft_said_earlier_do_not_answer_what_should_i_hunt(conn):
    sid = memory.new_session(conn, "web")
    memory.add_message(conn, sid, "user", "is 50 ranks of forging enough?")
    q = "what should I hunt?"
    memory.add_message(conn, sid, "user", q)
    assert Assistant(Settings(), conn, Model())._hunt_ranks(plan_for(q), q, sid) == []


def test_the_latest_combat_ranks_said_earlier_are_used_when_none_are_given_now(conn):
    sid = memory.new_session(conn, "web")
    memory.add_message(conn, sid, "user", "I had 100 offense and 90 defense")
    memory.add_message(conn, sid, "user", "now it is 200 offense and 180 defense")
    q = "what should I hunt?"
    assert Assistant(Settings(), conn, Model())._hunt_ranks(plan_for(q), q, sid) == [200, 180]


def test_a_bare_rank_replying_to_the_question_about_ranks_counts_even_if_the_planner_drops_it(conn):
    sid = memory.new_session(conn, "web")
    plan = Plan("What should I hunt?", asked="about 150 ranks")
    assert Assistant(Settings(), conn, Model())._hunt_ranks(plan, plan.standalone_question, sid) == [150]


# ------------------------------------------------------------------------------------------- the subject

def anchored(conn, title="Svidaw sis"):
    sid = memory.new_session(conn, "web")
    memory.add_message(conn, sid, "user", f"what is {title}?")
    memory.add_message(conn, sid, "assistant", "A thing [1].")
    memory.set_anchor(conn, sid, title)
    return sid, memory.history(conn, sid, 4)


def test_a_question_naming_its_own_subject_does_not_get_the_old_one_added(conn):
    page(conn, 1, "Hodierna", "lore")
    sid, hist = anchored(conn)
    q = "Who is Hodierna and what is her domain?"
    plan = asyncio.run(Assistant(Settings(), conn, Model({"standalone_question": q})).plan(hist, q, sid))
    assert plan.standalone_question == q and not plan.follow_up


def test_a_word_that_merely_has_a_command_page_does_not_count_as_naming_a_subject(conn):
    page(conn, 1, "Weigh", "command")
    sid, hist = anchored(conn)
    q = "how much does it weigh?"
    plan = asyncio.run(Assistant(Settings(), conn, Model({"standalone_question": q})).plan(hist, q, sid))
    assert plan.standalone_question == "how much does it weigh (about Svidaw sis)?" and plan.follow_up


# ------------------------------------------------------------------------------------------- plain answers, offers

def test_with_the_persona_off_the_earlier_questions_still_reach_the_model():
    assert "Earlier: Q" in prompts.build_answer_system("general", "[1] x", "Fennick", persona=False, earlier="Earlier: Q")


def test_turning_offers_off_turns_off_the_offer_of_the_rest(conn):
    page(conn, 1, "Svidaw sis", "item", "The svidaw sis is a weapon.")
    page(conn, 2, "Moot", "event", "A svidaw sis was a prize.", (1, 0.1))
    s = Settings()
    plan = Plan("How much does a svidaw sis weigh?")
    assert Assistant(s, conn, Model())._rest_of_it(plan, "Svidaw sis") == "the story behind Svidaw sis"
    s.retrieval.offer_related = False
    assert Assistant(s, conn, Model())._rest_of_it(plan, "Svidaw sis") is None


def test_the_label_on_a_log_is_not_a_citation_of_it(conn, monkeypatch):
    page(conn, 1, "Svidaw sis", "item", "The svidaw sis is a heavy weapon.", (1, 0))
    page(conn, 2, "Ayrell/Logs/Night", "log", "Bralinson strikes with a svidaw sis.", (1, 0.3), is_log=1)
    seen = {}

    def spy(conn_, cfg, kind, anchor, used, qvec):
        seen["anchor"] = anchor
        return []

    monkeypatch.setattr(offers, "related", spy)
    found = layers.gather(conn, layers.open_question(conn, "Tell me about svidaw sis"), 8, BASE)
    n = next(i for i, p in enumerate(found, 1) if p.get("log"))
    sid = memory.new_session(conn, "web")
    # nothing here is in the log, so its one citation is marked [n?]: only the code's own label would count for it
    ask(conn, sid, "Tell me about svidaw sis", Model({"standalone_question": "Tell me about svidaw sis"},
                                                    f"Dragons devoured every moon above the western ocean tonight [{n}]."))
    assert seen["anchor"] == "Svidaw sis"


# ------------------------------------------------------------------------------------------- layers

def test_a_big_subject_keeps_its_closest_linked_pages_not_the_first_two_hundred(conn):
    page(conn, 1, "Lanival", "npc", "Lanival led the war.", (1, 0))
    for n in range(2, 262):
        page(conn, n, f"Far place {n}", "place", "A place.", (1, 1.1))
        conn.execute("INSERT INTO page_links VALUES (1, ?)", (n,))
    page(conn, 300, "Dragon Hold", "place", "The hold of Lanival.", (1, 0.05))
    conn.execute("INSERT INTO page_links VALUES (1, 300)")
    found = layers.gather(conn, layers.open_question(conn, "Who is Lanival?"), 8, BASE)
    assert "Dragon Hold" in [p["title"] for p in found if p["layer"] == "ties"]


@pytest.fixture
def two_mos(conn):
    page(conn, 1, "Mo", "npc", "Mo is a tall Barbarian.", (1, 0))
    page(conn, 2, "Marshal Order", "spell", "A spell.", (0, 1), norm="mo")
    page(conn, 3, "Bards' news", "news", "Mo led the Barbarians to war.", (1, 0.1))
    return conn


def test_a_clarifying_button_gets_the_answer_the_same_question_would_have(two_mos):
    sid = memory.new_session(two_mos, "web")
    done = ask(two_mos, sid, "Tell me about Mo", Model({"standalone_question": "Tell me about Mo", "kind": "character"}))[-1]
    choice = next(c for c in done.choices if c["page_id"] == 1)
    model = Model(reply="Mo is a Barbarian [1].")
    events = ask(two_mos, sid, "I meant " + choice["text"], model, pin=1)
    assert model.planner_calls == 0  # the question is already known
    assert "Answer in these parts" in model.system and "**What they've done**" in model.system
    assert memory.recent_questions(two_mos, sid)[-1] == "Tell me about Mo (the character Mo)?"
    assert events[-1].sources[0].title == "Mo"


def test_a_button_on_a_narrow_question_answers_it_from_that_page_without_parts(two_mos):
    sid = memory.new_session(two_mos, "web")
    done = ask(two_mos, sid, "How tall is Mo?", Model({"standalone_question": "How tall is Mo?", "kind": "character"}))[-1]
    assert done.choices
    model = Model(reply="Mo is a Barbarian [1].")
    events = ask(two_mos, sid, "I meant the character Mo", model, pin=1)
    assert "Answer in these parts" not in model.system and events[-1].sources[0].title == "Mo"


# ------------------------------------------------------------------------------------------- what the owner reads

def answered(c, sid, question, *, verify=None):
    user = memory.add_message(c, sid, "user", question, standalone_q=question, mode="search", kind="general")
    t = turns.Turn.start(c, sid, user)
    t.finish(memory.add_message(c, sid, "assistant", "x [1].", standalone_q=question, mode="search", sources=[{"n": 1, "title": "B"}],
                                cited=[1], turn_id=t.turn_id), verify=verify)
    return t.turn_id


def test_the_list_is_thumbs_down_then_newest_with_a_thumbs_up_getting_no_special_place(conn):
    sid = memory.new_session(conn, "web", owner="local")
    bad = {"unknown_entities": ["Z"]}
    upvoted, down, newer = answered(conn, sid, "old", verify=bad), answered(conn, sid, "disliked"), answered(conn, sid, "new", verify=bad)
    feedback.record(conn, upvoted, "up")
    feedback.record(conn, down, "down")
    assert [r["turn_id"] for r in feedback.review(conn)] == [down, newer, upvoted]


def test_old_problems_are_still_found_behind_many_fine_answers(conn):
    sid = memory.new_session(conn, "web", owner="local")
    old = answered(conn, sid, "old problem", verify={"unknown_entities": ["Z"]})
    for i in range(40):
        answered(conn, sid, f"fine {i}")
    assert [r["turn_id"] for r in feedback.review(conn, limit=3)] == [old]


@pytest.fixture
def many_chats(conn):
    for i in range(3):
        memory.add_message(conn, memory.new_session(conn, "web", owner="local", session_id=f"mine{i}"), "user", f"mine {i}")
    for i in range(3):
        memory.add_message(conn, memory.new_session(conn, "web", owner="visitor-1", session_id=f"their{i}"), "user", f"theirs {i}")
    memory.add_message(conn, memory.new_session(conn, "cli", session_id="typed"), "user", "typed")
    return conn


def test_sessions_can_be_listed_by_who_and_across_front_ends(many_chats):
    ids = lambda rows: [r["session_id"] for r in rows]  # noqa: E731
    assert ids(memory.list_sessions(many_chats, "web", 2, owner="local")) == ["mine2", "mine1"]
    assert set(ids(memory.list_sessions(many_chats, "web", 9, owner="visitors"))) == {"their0", "their1", "their2"}
    everything = memory.list_sessions(many_chats, None, 2)
    assert ids(everything)[0] == "typed" and len(everything) == 2  # newest first, and the limit holds


def test_chats_list_finds_older_chats_of_one_owner_and_keeps_the_limit(many_chats, monkeypatch):
    many_chats.commit()
    s = Settings()
    s.db.path = many_chats.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(cli, "settings", lambda: s)
    run = CliRunner()
    out = run.invoke(cli.app, ["chats", "list", "--owner", "local", "--limit", "1"]).output
    assert "mine2" in out and "1 shown" in out
    out = run.invoke(cli.app, ["chats", "list", "--frontend", "all", "--limit", "2"]).output
    assert "typed" in out and "2 shown" in out


def test_a_suggestion_waits_in_the_box_and_only_replies_send_at_once():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert sorted(re.findall(r"onclick = \(\) => (?:\{[^}]*?)?ask\(([^)]*)", html)) == ["'I meant ' + c.text, c.page_id", "question"]  # "I meant..." and try again
    assert len(re.findall(r"onclick = \(\) => prefill\(", html)) == 6
