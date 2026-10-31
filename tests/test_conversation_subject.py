"""A long conversation: what the answer step is given of it, the page it is about, setting it aside, and clarifying with buttons."""

import asyncio
import json

import pytest

from epedia.config import Settings
from epedia.core import memory
from epedia.core.assistant import Assistant, Done, Token
from epedia.db import connect
from epedia.llm import prompts

DIM = 768


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    yield c
    c.close()


def page(c, pid, title, kind, text="x", ns=0):
    c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, fetched_revid, indexed_revid, updated_at, page_kind, "
              "is_redirect) VALUES (?,?,?,?,1,1,1,'2026-01-01',?,0)", (pid, ns, title, title.lower(), kind))
    c.execute("INSERT INTO titles_fts(title_norm, page_id, is_redirect) VALUES (?,?,0)", (title.split(":", 1)[-1].lower(), pid))
    c.execute("INSERT INTO chunks(chunk_id, page_id, revid, ord, kind, title, section_path, text, token_est, content_hash) "
              "VALUES (?,?,1,0,'section',?,'',?,1,?)", (pid, pid, title, text, str(pid)))
    c.execute("INSERT INTO chunks_fts(rowid, title, section_path, text) VALUES (?,?,'',?)", (pid, title, text))
    c.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?,?)", (pid, json.dumps([1.0] + [0.0] * (DIM - 1))))


def turn(c, sid, asked, answer, *, standalone=None, sources=(), cited=(), mode="search", kind="general"):
    memory.add_message(c, sid, "user", asked, standalone_q=standalone or asked, mode=mode, kind=kind)
    return memory.add_message(c, sid, "assistant", answer, standalone_q=standalone or asked, mode=mode, kind=kind,
                              sources=[{"n": n, "title": t} for n, t in enumerate(sources, 1)], cited=list(cited))


class Model:
    def __init__(self, plan=None, reply="Binu is a Necromancer [1]."):
        self.plan, self.reply = plan or {}, reply
        self.planner_prompts, self.answer_messages = [], []

    async def chat_json(self, messages, *a, **k):
        self.planner_prompts.append(messages[-1]["content"])
        return {"mode": "search", "kind": "character", "follow_up": False, **self.plan}

    async def embed(self, texts, **k):
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

    async def chat_stream(self, messages, **k):
        self.answer_messages.append(messages)
        yield self.reply


def ask(c, sid, message, model, **kw):
    async def go():
        return [e async for e in Assistant(Settings(), c, model).answer_stream(sid, message, **kw)]

    return asyncio.run(go())


# ------------------------------------------------------------------------------------------- what the answer is given

def test_the_summary_is_each_earlier_question_and_its_cited_pages_oldest_first(conn):
    sid = memory.new_session(conn, "web")
    turn(conn, sid, "who is he", "A mage [1].", standalone="Who is Binu?", sources=["Binu", "Innu"], cited=[1])
    turn(conn, sid, "and his guild", "Necromancer [2].", standalone="What guild is Binu in?", sources=["Innu", "Necromancer"], cited=[2])
    turn(conn, sid, "and his race", "Human [1].", standalone="What race is Binu?", sources=["Binu"], cited=[1])
    text = memory.summary(conn, sid, skip=1)
    assert text.index("Who is Binu? (pages: Binu)") < text.index("What guild is Binu in? (pages: Necromancer)")
    assert "What race is Binu?" not in text  # the last exchange reaches the model on its own
    assert "A mage" not in text and "Innu" not in text  # no answers, and only pages that were cited


def test_the_summary_leaves_out_what_was_set_aside_small_talk_and_clarifying_questions(conn):
    sid = memory.new_session(conn, "web")
    turn(conn, sid, "old", "x [1].", standalone="Old question?", sources=["Old"], cited=[1])
    memory.clear_context(conn, sid)
    turn(conn, sid, "hi", "Hello.", standalone="Hi", kind="meta")
    turn(conn, sid, "which", "Which did you mean?", standalone="Which?", mode="clarify")
    turn(conn, sid, "new", "y [1].", standalone="New question?", sources=["New"], cited=[1])
    turn(conn, sid, "last", "z [1].", standalone="Last question?", sources=["Last"], cited=[1])
    text = memory.summary(conn, sid, skip=1)
    assert "New question?" in text
    assert "Old question?" not in text and "Hi" not in text and "Which?" not in text
    assert memory.summary(conn, memory.new_session(conn, "web")) == ""


def test_only_the_last_exchange_reaches_the_answer_with_its_answer_clipped():
    hist = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "second"}, {"role": "assistant", "content": "x" * 2000}]
    got = memory.recent_exchange(hist, 1)
    assert [m["content"][:6] for m in got] == ["second", "xxxxxx"] and len(got[1]["content"]) < 620
    assert memory.recent_exchange(hist, 0) == []


def test_the_answer_step_gets_the_summary_and_the_last_exchange_while_the_planner_keeps_more(conn):
    page(conn, 1, "Binu", "pc", "Binu is a Necromancer.")
    sid = memory.new_session(conn, "web")
    turn(conn, sid, "q one", "A1 " * 300, standalone="Standalone one?", sources=["Binu"], cited=[1])
    turn(conn, sid, "q two", "A2 " * 300, standalone="Standalone two?", sources=["Binu"], cited=[1])
    model = Model({"standalone_question": "What is Binu?"})
    ask(conn, sid, "what is Binu?", model)
    system, *rest = model.answer_messages[0]
    assert "Standalone one? (pages: Binu)" in system["content"] and "Standalone two?" not in system["content"]
    assert [m["content"][:6] for m in rest[:-1]] == ["q two", "A2 A2 "] and "A1" not in str(rest)
    assert "q one" in model.planner_prompts[0]  # still knows what was said, to work out what a message means


def test_what_the_user_said_long_ago_still_counts_as_known_to_the_checks(conn):
    page(conn, 1, "Binu", "pc", "Binu is a Necromancer of great skill and renown.")
    sid = memory.new_session(conn, "web")
    turn(conn, sid, "My character is called Zorblax", "Noted.", sources=["Binu"], cited=[])
    turn(conn, sid, "another", "Yes [1].", sources=["Binu"], cited=[1])
    model = Model({"standalone_question": "Who is Binu?"}, "Binu is a Necromancer, unlike Zorblax [1].")
    done = ask(conn, sid, "who is Binu?", model)[-1]
    assert "Zorblax" not in done.verification["unknown_entities"]


def test_a_challenge_still_gets_the_whole_of_the_last_answer(conn):
    page(conn, 1, "Binu", "pc", "Binu is a Necromancer.")
    sid = memory.new_session(conn, "web")
    long_answer = "Binu is a Necromancer. " * 60
    turn(conn, sid, "who is Binu?", long_answer, standalone="Who is Binu?", sources=["Binu"], cited=[1])
    model = Model(reply="Yes, the page says so [1].")
    ask(conn, sid, "are you sure?", model)
    assert len(model.answer_messages[0][-2]["content"]) > 700


def test_the_summary_goes_beside_the_passages():
    system = prompts.build_answer_system("general", "[1] x", "Fennick", continuing=True, earlier="Earlier: Q")
    assert system.index("Earlier: Q") < system.index("Passages:")


# ------------------------------------------------------------------------------------------- setting a topic aside

def test_a_set_aside_conversation_keeps_none_of_it(conn):
    sid = memory.new_session(conn, "web")
    turn(conn, sid, "who", "A clarifying question?", mode="clarify")
    assert memory.last_was_clarification(conn, sid)
    memory.add_message(conn, sid, "assistant", "x", sources=[{"n": 1, "title": "Binu"}], related=["Innu"])
    memory.set_anchor(conn, sid, "Binu")
    assert memory.seen_titles(conn, sid) == {"Binu", "Innu"} and memory.last_assistant(conn, sid)
    memory.add_message(conn, sid, "user", "I have 200 ranks")
    memory.clear_context(conn, sid)
    assert memory.seen_titles(conn, sid) == set()
    assert memory.last_assistant(conn, sid) is None and not memory.last_was_clarification(conn, sid)
    assert memory.user_messages(conn, sid) == [] and memory.get_anchor(conn, sid) is None


def test_a_set_aside_answer_is_not_what_a_follow_up_reads(conn):
    page(conn, 1, "Binu", "pc", "Binu is a Necromancer.")
    sid = memory.new_session(conn, "web")
    memory.add_message(conn, sid, "user", "who is Binu?")
    memory.add_message(conn, sid, "assistant", "x", sources=[{"n": 1, "title": "Binu", "chunk_id": 1}], cited=[1])
    a = Assistant(Settings(), conn, Model())
    assert a._prior_chunk_ids(sid) == [1]
    memory.clear_context(conn, sid)
    assert a._prior_chunk_ids(sid) == []


def test_ranks_given_earlier_in_the_conversation_are_used(conn):
    sid = memory.new_session(conn, "web")
    memory.add_message(conn, sid, "user", "I have 200 offense and 180 defense")
    a = Assistant(Settings(), conn, Model())
    plan = type("P", (), {"standalone_question": "What should I hunt?"})()
    assert a._hunt_ranks(plan, "what should I hunt?", sid) == [200, 180]
    memory.clear_context(conn, sid)
    assert a._hunt_ranks(plan, "what should I hunt?", sid) == []


# ------------------------------------------------------------------------------------------- the page it is about

def test_a_question_that_names_a_page_and_rests_on_it_sets_the_subject(conn):
    page(conn, 1, "Binu", "pc", "Binu is a Necromancer of the Moon.")
    sid = memory.new_session(conn, "web")
    ask(conn, sid, "Who is Binu?", Model({"standalone_question": "Who is Binu?"}))
    assert memory.get_anchor(conn, sid) == "Binu"


def test_a_question_that_names_nothing_leaves_the_subject_where_it_is(conn):
    page(conn, 1, "Binu", "pc", "Binu is a Necromancer.")
    sid = memory.new_session(conn, "web")
    memory.add_message(conn, sid, "user", "who?")
    memory.add_message(conn, sid, "assistant", "Binu [1].", sources=[{"n": 1, "title": "Binu"}], cited=[1])
    memory.set_anchor(conn, sid, "Binu")
    ask(conn, sid, "how tall?", Model({"standalone_question": "How tall?"}))
    assert memory.get_anchor(conn, sid) == "Binu"


def test_a_new_subject_replaces_the_old_one(conn):
    page(conn, 1, "Binu", "pc", "Binu is a Necromancer.")
    page(conn, 2, "Innu", "place", "Innu is a town by the river.")
    sid = memory.new_session(conn, "web")
    memory.add_message(conn, sid, "user", "who is Binu?")
    memory.add_message(conn, sid, "assistant", "Binu [1].", sources=[{"n": 1, "title": "Binu"}], cited=[1])
    memory.set_anchor(conn, sid, "Binu")
    ask(conn, sid, "What is Innu?", Model({"standalone_question": "What is Innu?"}, "Innu is a town by the river [1]."))
    assert memory.get_anchor(conn, sid) == "Innu"


def test_a_pronoun_shows_the_planner_the_subject_and_code_fills_in_what_it_left_standing(conn):
    page(conn, 1, "Binu", "pc", "Binu is a Necromancer.")
    sid = memory.new_session(conn, "web")
    memory.add_message(conn, sid, "user", "who is Binu?")
    memory.add_message(conn, sid, "assistant", "Binu [1].", sources=[{"n": 1, "title": "Binu"}], cited=[1])
    memory.set_anchor(conn, sid, "Binu")
    model = Model({"standalone_question": "what guild is he in?"})
    a = Assistant(Settings(), conn, model)
    hist = memory.history(conn, sid, 4)
    plan = asyncio.run(a.plan(hist, "what guild is he in?", sid))
    assert "Current subject of the conversation: Binu" in model.planner_prompts[0]
    assert plan.standalone_question == "what guild is he in (about Binu)?" and plan.follow_up


def test_a_rewrite_the_planner_managed_itself_is_left_alone_and_no_pronoun_means_no_hint(conn):
    sid = memory.new_session(conn, "web")
    memory.add_message(conn, sid, "user", "who is Binu?")
    memory.add_message(conn, sid, "assistant", "Binu [1].")
    memory.set_anchor(conn, sid, "Concept:Binu")
    hist = memory.history(conn, sid, 4)
    model = Model({"standalone_question": "What guild is Binu in?"})
    plan = asyncio.run(Assistant(Settings(), conn, model).plan(hist, "what guild is he in?", sid))
    assert plan.standalone_question == "What guild is Binu in?"
    model = Model({"standalone_question": "Who is Innu?"})
    asyncio.run(Assistant(Settings(), conn, model).plan(hist, "who is Innu?", sid))
    assert "Current subject" not in model.planner_prompts[0]


def test_a_clarifying_question_is_trusted_later_only_when_there_is_no_subject_to_draw_on(conn):
    sid = memory.new_session(conn, "web")
    memory.add_message(conn, sid, "user", "hello")
    memory.add_message(conn, sid, "assistant", "Hi there.")
    hist = memory.history(conn, sid, 4)
    plan = {"standalone_question": "How much?", "follow_up": False, "clarify": "How much of what?"}
    # "how much?" was not rewritten, leans on nothing it can name, and no page or anchor answers it
    got = asyncio.run(Assistant(Settings(), conn, Model(plan)).plan(hist, "how much?", sid))
    assert got.clarify == "How much of what?"
    memory.set_anchor(conn, sid, "Binu")
    got = asyncio.run(Assistant(Settings(), conn, Model(plan)).plan(hist, "how much?", sid))
    assert got.clarify == ""


# ------------------------------------------------------------------------------------------- clarifying with buttons

@pytest.fixture
def two_sedras(conn):
    page(conn, 1, "Sidasas Sedra", "spell", "A spell that smites a horde.")
    page(conn, 2, "Sidasas Sedra (1)", "creature", "A creature of the deep.")
    conn.execute("DELETE FROM titles_fts WHERE page_id=2")
    conn.execute("INSERT INTO titles_fts(title_norm, page_id, is_redirect) VALUES ('sidasas sedra', 2, 0)")
    return conn


def test_a_name_of_two_sorts_of_thing_asks_with_buttons_that_say_how_each_opens(two_sedras):
    sid = memory.new_session(two_sedras, "web")
    done = ask(two_sedras, sid, "Tell me about Sidasas Sedra", Model({"standalone_question": "Tell me about Sidasas Sedra"}))[-1]
    assert isinstance(done, Done) and "Which did you mean?" in done.answer
    assert {c["page_id"] for c in done.choices} == {1, 2}
    spell = next(c for c in done.choices if c["page_id"] == 1)
    assert spell["text"] == "the spell Sidasas Sedra" and spell["lead"].startswith("A spell that smites")


def test_real_ambiguity_beats_the_planners_vaguer_question(two_sedras):
    sid = memory.new_session(two_sedras, "web")
    plan = {"standalone_question": "Tell me about Sidasas Sedra", "clarify": "Could you say more?"}
    done = ask(two_sedras, sid, "Tell me about Sidasas Sedra", Model(plan))[-1]
    assert "Which did you mean?" in done.answer and done.choices


def test_a_button_answers_from_that_page_without_asking_again_and_sets_the_subject(two_sedras):
    sid = memory.new_session(two_sedras, "web")
    ask(two_sedras, sid, "Tell me about Sidasas Sedra", Model({"standalone_question": "Tell me about Sidasas Sedra"}))
    model = Model({"standalone_question": "Tell me about the spell Sidasas Sedra"}, "It smites a horde [1].")
    events = ask(two_sedras, sid, "I meant the spell Sidasas Sedra", model, pin=1)
    done = events[-1]
    assert done.sources[0].title == "Sidasas Sedra" and "smites" in "".join(e.text for e in events if isinstance(e, Token))
    assert not done.choices and memory.get_anchor(two_sedras, sid) == "Sidasas Sedra"
    assert memory.recent_questions(two_sedras, sid)[-1] == "Tell me about Sidasas Sedra (the spell Sidasas Sedra)?"


def test_a_pin_counts_only_as_the_answer_to_a_clarifying_question_and_only_for_a_real_page(two_sedras):
    a = Assistant(Settings(), two_sedras, Model())
    sid = memory.new_session(two_sedras, "web")
    assert a._picked(1, sid) is None  # nothing was asked
    turn(two_sedras, sid, "x", "Which did you mean?", mode="clarify")
    assert a._picked(1, sid) == "Sidasas Sedra"
    assert a._picked(999, sid) is None and a._picked(None, sid) is None
