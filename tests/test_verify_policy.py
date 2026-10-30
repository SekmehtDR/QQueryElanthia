"""What happens after the checks (Phase 3): a draft that invents names or pretends to look something up is withdrawn
and written once more; everything else is marked ([n?]) and noted, never rewritten. Plus the rules code applies before
the planner: stories are declined in voice, and "are you sure?" re-reads the last answer's evidence."""

import asyncio
import json
import struct

import pytest

from epedia.config import Settings
from epedia.core import capabilities, memory, verify
from epedia.core.assistant import Assistant, Done, Retrieval, Revise, Token
from epedia.db import connect
from epedia.evalx.faithfulness import is_refusal, model_text
from epedia.llm import prompts

PAGES = {
    1: ("Binu", "Binu is a Human Necromancer in the Prime instance. He has greenish granite-like skin."),
    2: ("Musk hog", "Musk hogs are found in the Crossing hog pens and weigh 40 stones."),
    3: ("Malkien", "Malkien is a character of the Prime instance, a Barbarian of the Crossing."),
    4: ("New players", "Be sure to live well with friends; every character asks where things are and who they are."),
}


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    for pid, (title, text) in PAGES.items():
        kind = "pc" if title in ("Binu", "Malkien") else "general"
        c.execute("INSERT INTO pages(page_id, ns, title, title_norm, updated_at, page_kind) VALUES (?,0,?,?,?,?)",
                  (pid, title, title.lower(), "2026-01-01", kind))
        c.execute("INSERT INTO chunks(chunk_id, page_id, revid, ord, kind, title, section_path, text, token_est, "
                  "content_hash) VALUES (?,?,1,0,'section',?,'',?,10,?)", (pid, pid, title, text, str(pid)))
        c.execute("INSERT INTO titles_fts(title_norm, page_id, is_redirect) VALUES (?,?,0)", (title.lower(), pid))
        vec = [0.0] * 768
        vec[pid] = 1.0
        c.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)", (pid, struct.pack("768f", *vec)))
    yield c
    c.close()


class Model:
    """Answers from a queue; records what each answer call was given."""

    def __init__(self, answers, kind="character"):
        self.answers = list(answers)
        self.kind = kind
        self.planned = 0
        self.calls: list[tuple[list[dict], float]] = []

    async def chat_json(self, messages, schema, **k):
        self.planned += 1
        q = messages[-1]["content"].rsplit("Latest user message: ", 1)[-1].split("\n")[0]
        return {"standalone_question": q, "mode": "search", "kind": self.kind, "follow_up": False}

    async def embed(self, texts, **k):
        vec = [0.0] * 768
        vec[1] = 1.0
        return [vec for _ in texts]

    async def chat_stream(self, messages, temperature=None, **k):
        self.calls.append((messages, temperature))
        yield self.answers.pop(0)


def run(a, sid, q):
    async def go():
        return [e async for e in a.answer_stream(sid, q)]

    return asyncio.run(go())


def last_turn(conn):
    return conn.execute("SELECT * FROM turns ORDER BY turn_id DESC LIMIT 1").fetchone()


# ------------------------------------------------------------------------------------------------ the policy

def test_a_draft_naming_what_no_passage_has_is_withdrawn_and_written_once_more(conn):
    model = Model(["Binu is a Necromancer [1]. He hunts Moonwraiths near Shadowfen [1].",
                   "Binu is a Human Necromancer in the Prime instance [1]."])
    events = run(Assistant(Settings(), conn, model), memory.new_session(conn, "web"), "Who is Binu?")
    kinds = [type(e).__name__ for e in events]
    assert kinds.index("Revise") > kinds.index("Token") and isinstance(events[-1], Done)
    revise = next(e for e in events if isinstance(e, Revise))
    assert revise.reason == "names"
    done = events[-1]
    assert done.revised and "Moonwraiths" not in done.answer and done.answer.startswith("Binu is a Human Necromancer")
    # the second call is told exactly which names to leave out, at temperature 0
    (first, t1), (second, t2) = model.calls
    assert t2 == 0.0 and t1 == Settings().chat.temperature
    assert '"Moonwraiths"' in second[0]["content"] and '"Shadowfen"' in second[0]["content"]
    assert "Moonwraiths" not in first[0]["content"]
    row = last_turn(conn)
    assert row["regenerated"] == 1 and json.loads(row["verify_json"])["revised"] is True


def test_only_once_and_a_second_failure_is_noted_not_hidden(conn):
    model = Model(["Binu hunts Moonwraiths [1].", "Binu still hunts Moonwraiths, with a Necromancer's patience [1]."])
    done = run(Assistant(Settings(), conn, model), memory.new_session(conn, "web"), "Who is Binu?")[-1]
    assert len(model.calls) == 2 and done.revised
    assert done.answer.endswith(verify.HONESTY_NOTES["names"][0].replace("{names}", "“Moonwraiths”")
                                .replace("{them}", "it"))
    assert done.verification["unknown_entities"] == ["Moonwraiths"]


def test_regeneration_can_be_switched_off(conn):
    s = Settings()
    s.chat.regenerate = "off"
    model = Model(["Binu hunts Moonwraiths [1]."])
    events = run(Assistant(s, conn, model), memory.new_session(conn, "web"), "Who is Binu?")
    assert not any(isinstance(e, Revise) for e in events) and len(model.calls) == 1
    assert "I can't find “Moonwraiths”" in events[-1].answer and not events[-1].revised


def test_a_pretended_lookup_is_rewritten_and_never_reaches_the_reader(conn):
    model = Model(["Let me check the page history for Binu. Binu is a Human Necromancer [1].",
                   "Let me take a closer look at his edits. Binu is a Human Necromancer in the Prime instance [1]."])
    events = run(Assistant(Settings(), conn, model), memory.new_session(conn, "web"), "Who is Binu?")
    done = events[-1]
    assert next(e for e in events if isinstance(e, Revise)).reason == "lookup"
    assert "closer look" not in done.answer and "Let me" not in done.answer
    assert done.answer.startswith("Binu is a Human Necromancer in the Prime instance [1].")
    assert "cannot look anything up" in model.calls[1][0][0]["content"]


def test_a_citation_its_passage_does_not_bear_out_is_marked_not_rewritten(conn):
    model = Model(["Binu is a Human Necromancer [1]. He weighs 400 stones and rides a giant purple wyvern [1]."])
    done = run(Assistant(Settings(), conn, model), memory.new_session(conn, "web"), "Who is Binu?")[-1]
    assert len(model.calls) == 1 and not done.revised
    assert "Binu is a Human Necromancer [1]." in done.answer and "wyvern [1?]." in done.answer
    assert 1 in done.cited  # the source is still listed: the reader can see what it does say
    assert done.answer.endswith(verify.HONESTY_NOTES["numbers"][0])  # 400 is in no passage
    sid = conn.execute("SELECT session_id FROM sessions").fetchone()[0]
    assert "?]" not in memory.history(conn, sid, 4)[-1]["content"]  # marks are per answer, like citations


def test_honesty_notes_by_case_in_both_styles():
    v = verify.Verification("", factual_units=4, uncited=4, unknown_entities=["Moonwraiths", "Shadowfen"])
    assert verify.honesty_note(v) == ("One caution: I can't find “Moonwraiths” or “Shadowfen” anywhere in the pages I "
                                      "had for this, so don't take my word on them.")
    assert "do not appear" in verify.honesty_note(v, persona=False)
    v = verify.Verification("", factual_units=4, uncited=4, numbers_bad=1)
    assert verify.honesty_note(v) == verify.HONESTY_NOTES["numbers"][0]
    v = verify.Verification("", factual_units=5, cited_units=1, uncited=4)
    assert verify.honesty_note(v) == verify.HONESTY_NOTES["uncited"][0]
    assert verify.honesty_note(verify.Verification("", factual_units=5, cited_units=2, uncited=3)) == ""
    assert verify.honesty_note(verify.Verification("", factual_units=2, uncited=2)) == ""  # too short to judge
    for note in (*verify.HONESTY_NOTES.values(),):
        assert model_text("Binu is a Necromancer [1].\n\n" + note[0].replace("{names}", "x").replace("{them}", "it")) \
            == "Binu is a Necromancer [1]."  # the eval scores the model's words, not code's


def test_strip_fake_actions_keeps_cited_lines_and_tidies_lists():
    text = "Let me check that for you.\n\n- Let me look at the edits.\n- Binu is a Necromancer [1].\n\nI'll search my notes."
    assert verify.strip_fake_actions(text) == "- Binu is a Necromancer [1]."
    assert verify.strip_fake_actions("Let me check: Binu is a Necromancer [1].") == "Let me check: Binu is a Necromancer [1]."
    # an uncited line keeps the fact that follows the pretence
    assert verify.strip_fake_actions("Let me re-read the passages: the sword weighs 10 stones.") == "The sword weighs 10 stones."
    # a sentence ending in a doubtful mark still ends there
    assert verify.strip_fake_actions("Binu is a Necromancer.[2?] Let me check the passages again.") == \
        "Binu is a Necromancer.[2?]"


def test_small_talk_is_checked_too_but_never_noted_for_citing_nothing(conn):
    # an invented name or a pretended look-up in small talk is rewritten like anywhere else (the log had both)...
    model = Model(["Hello! I'm Fennick, and Moonwraiths are my favourite subject.",
                   "Hello! I'm Fennick, a Halfling Bard with a satchel of notes, and I'd love to help you."], kind="meta")
    done = run(Assistant(Settings(), conn, model), memory.new_session(conn, "web"), "Hi Fennick, who are you?")[-1]
    assert len(model.calls) == 2 and done.revised and "Moonwraiths" not in done.answer
    # ...but a reply about himself cites nothing, and is not cautioned for it
    assert "One caution" not in done.answer
    system = model.calls[0][0][0]["content"]
    assert capabilities.capabilities_text("Fennick") in system  # he says truthfully what he can and cannot do


def test_the_no_lookup_rule_is_in_both_answer_styles():
    assert capabilities.NO_LOOKUP in prompts.PERSONA_BLOCK and capabilities.NO_LOOKUP in prompts.NEUTRAL_ANSWER_SYSTEM
    assert "{no_lookup}" not in prompts.PERSONA_BLOCK + prompts.NEUTRAL_ANSWER_SYSTEM


# ------------------------------------------------------------------------------------------------ fiction

@pytest.mark.parametrize("q", [
    "Write a story where Sekmeht kills Malkien.", "Simulate a duel between two Barbarians and tell me who wins, blow by blow.",
    "Write me a poem about Hodierna.", "Imagine a battle between Rakkor and Malkien and describe the deathblow.",
    "Roleplay as a Gor'Tog bandit ambushing me on the road.", "Make up a legend about how the Glimmerfen swamp got its name.",
    "How about simulating a story of Sekmeht defeating Malkien, I want the deathblow",
    "Can you tell me a story where a Paladin slays a dragon?", "pretend you're a Moon Mage",
    "Imagine a fight between a Barbarian and a Paladin", "Write some fan fiction about the Crossing",
])
def test_requests_for_fiction_are_recognised(q):
    assert capabilities.FICTION.search(q)


@pytest.mark.parametrize("q", [
    "Tell me the story of the Lyras War", "Tell me a story about the Lyras War", "What does the Simulacrum spell do?",
    "How do I write a scroll?", "What is the legend of Hodierna?", "Is roleplay required in DragonRealms?",
    "What are the rules for roleplaying on the Prime instance?", "Who wrote the poem The Lay of Ilithi?",
    "How do I create a new character?", "Where can I buy a song book?",
    # hypotheticals about play, and questions that merely mention fiction
    "Imagine I am a Barbarian with 50 ranks, what should I hunt?", "Pretend I am a new player: where do I start?",
    "Who would win, a Barbarian or a Paladin at 100 ranks?", "Where can I find fan fiction about Elanthia?",
])
def test_questions_about_stories_and_roleplay_are_not_fiction(q):
    assert not capabilities.FICTION.search(q)


def test_a_story_is_declined_in_voice_and_answered_with_what_the_wiki_says(conn):
    model = Model(["Malkien is a Barbarian of the Crossing [1]."])
    done = run(Assistant(Settings(), conn, model), memory.new_session(conn, "web"),
               "How about simulating a story of Sekmeht defeating Malkien, I want the deathblow")[-1]
    assert model.planned == 0 and model.calls[0][0][-1]["content"] == "Tell me about Malkien."
    assert done.answer.startswith(capabilities.decline_fiction(["Malkien"], True) + "\n\nMalkien is a Barbarian")
    assert is_refusal(done.answer) and not done.by_code
    assert conn.execute("SELECT mode FROM messages WHERE role='assistant'").fetchone()[0] == "fiction"


def test_a_story_about_nothing_the_wiki_has_is_declined_without_the_model(conn):
    model = Model([])
    events = run(Assistant(Settings(), conn, model), memory.new_session(conn, "web"),
                 "Make up a legend about how the Glimmerfen swamp got its name.")
    assert model.planned == 0 and model.calls == [] and events[-1].by_code
    assert events[-1].answer == capabilities.DECLINE_FICTION_ALONE and is_refusal(events[-1].answer)


def test_a_story_whose_names_find_no_passage_is_still_declined(conn, monkeypatch):
    async def nothing(self, plan, session_id=None):
        return [], []

    monkeypatch.setattr(Assistant, "retrieve_with_extras", nothing)
    model = Model([])
    done = run(Assistant(Settings(), conn, model), memory.new_session(conn, "web"), "Write a poem about Malkien.")[-1]
    assert model.calls == [] and done.answer == capabilities.DECLINE_FICTION_ALONE


# ------------------------------------------------------------------------------------------------ "are you sure?"

@pytest.mark.parametrize("q,yes", [
    ("Are you sure?", True), ("Hmm, are you sure about that?", True), ("That's wrong", True), ("really?", True),
    ("Can you check that again?", True), ("you're wrong, he is a warrior mage", True), ("Says who?", True),
    ("Really good weapons for a ranger?", False), ("How do you know your spells?", False), ("Who is Binu?", False),
    ("Are you sure Binu is a necromancer and not a warrior mage, because his page from 2017 said otherwise", False),
    ("Are you sure? I read he was a warrior mage", True), ("Is that true for Paladins too?", False),
    ("Are you sure there is no other way to get to Riverhaven?", False), ("Really, a necromancer?", True),
])
def test_challenges_are_recognised(q, yes):
    assert capabilities.is_challenge(q) is yes


def test_are_you_sure_rereads_the_last_answers_pages_instead_of_asking_the_planner(conn):
    model = Model(["Binu is a Human Necromancer [1].", "Yes: his page says he is a Human Necromancer [1]."])
    a = Assistant(Settings(), conn, model)
    sid = memory.new_session(conn, "web")
    run(a, sid, "Who is Binu?")
    assert model.planned == 1
    events = run(a, sid, "Are you sure?")
    assert model.planned == 1  # not planned again: it is about the last question, never small talk
    retrieval = next(e for e in events if isinstance(e, Retrieval))
    assert retrieval.mode == "recheck" and retrieval.standalone_q == "Who is Binu?" and retrieval.kind == "character"
    assert retrieval.sources[0].title == "Binu"  # the page it cited, read first
    messages = model.calls[1][0]
    assert prompts.RECHECK in messages[0]["content"]
    assert messages[-1]["content"] == "Are you sure? (This is about my last question: Who is Binu?)"
    assert any(m["role"] == "assistant" for m in messages)  # it sees what it said
    tokens = "".join(e.text for e in events if isinstance(e, Token))
    assert "Human Necromancer" in tokens


def test_a_recheck_may_say_it_checked_again_without_a_rewrite(conn):
    model = Model(["Binu is a Human Necromancer [1].",
                   "I checked the passages again and they confirm it: Binu is a Human Necromancer [1]."])
    a = Assistant(Settings(), conn, model)
    sid = memory.new_session(conn, "web")
    run(a, sid, "Who is Binu?")
    done = run(a, sid, "Are you sure?")[-1]
    assert len(model.calls) == 2 and not done.revised and done.answer.startswith("I checked the passages again")
    assert "no talk of checking" in model.calls[1][0][0]["content"]  # the prompt asks for the result, not the process


def test_are_you_sure_after_a_declined_story_is_not_a_recheck(conn):
    model = Model(["Malkien is a Barbarian [1]."], kind="general")
    a = Assistant(Settings(), conn, model)
    sid = memory.new_session(conn, "web")
    run(a, sid, "Simulate a duel where a nameless hero kills a dragon")  # declined in code: nothing to re-read
    events = run(a, sid, "Are you sure?")
    assert next(e for e in events if isinstance(e, Retrieval)).mode != "recheck" and model.planned == 1


def test_are_you_sure_after_a_code_answer_goes_to_the_planner(conn):
    model = Model(["Something [1]."], kind="general")
    a = Assistant(Settings(), conn, model)
    sid = memory.new_session(conn, "web")
    run(a, sid, "Who is Zarnak the Unbound?")  # declined in code: there is no evidence to re-read
    run(a, sid, "Are you sure?")
    assert model.planned == 2  # planned like any other message, not re-checked
    assert conn.execute("SELECT mode FROM messages WHERE role='assistant' ORDER BY msg_id DESC").fetchone()[0] != "recheck"
