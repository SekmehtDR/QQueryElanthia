"""Layered answers: an open question about one thing reads each part from its own sort of page, code supplies the headings
and drops what nothing supports, and a narrow question stays narrow and offers the rest."""

import asyncio
import struct

import pytest

from epedia.config import Settings
from epedia.core import layers, memory, verify
from epedia.core.assistant import Assistant, Done, Retrieval
from epedia.db import connect
from epedia.index.enrich import enrich_all
from epedia.kinds import LAYER_OFFER, LAYER_TABLE, LAYERS
from epedia.llm import prompts

DIM = 768
BASE = "https://elanthipedia.play.net"
THING = LAYERS["thing"]


def vec(*xs: float) -> bytes:
    v = list(xs) + [0.0] * (DIM - len(xs))
    norm = sum(x * x for x in v) ** 0.5 or 1.0
    return struct.pack(f"{DIM}f", *[x / norm for x in v])


NEAR = (1, 0.1)  # about 0.005 from the subject
MENTION_OK = (1, 0.6)  # about 0.14
FAR_FOR_MENTION = (1, 1.6)  # about 0.47: past both limits
LINK_ONLY = (1, 1.1)  # about 0.33


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    yield c
    c.close()


_next_chunk = [1000]


def page(c, pid, title, kind, text, direction=NEAR, ns=0, is_log=0, name=None):
    c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, fetched_revid, indexed_revid, updated_at, page_kind, "
              "is_log, is_redirect) VALUES (?,?,?,?,1,1,1,'2026-01-01',?,?,0)", (pid, ns, title, title.lower(), kind, is_log))
    c.execute("INSERT INTO titles_fts(title_norm, page_id, is_redirect) VALUES (?,?,0)", ((name or title).lower(), pid))
    chunk(c, pid, title, text, direction, 0)


def chunk(c, pid, title, text, direction=NEAR, order=1, section=""):
    _next_chunk[0] += 1
    cid = _next_chunk[0]
    c.execute("INSERT INTO chunks(chunk_id, page_id, revid, ord, kind, title, section_path, text, token_est, content_hash) "
              "VALUES (?,?,1,?,'section',?,?,?,1,?)", (cid, pid, order, title, section, text, str(cid)))
    c.execute("INSERT INTO chunks_fts(rowid, title, section_path, text) VALUES (?,?,?,?)", (cid, title, section, text))
    c.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?,?)", (cid, vec(*direction)))


def link(c, a, b):
    c.execute("INSERT OR IGNORE INTO page_links VALUES (?,?)", (a, b))


@pytest.fixture
def family(conn):
    """The svidaw sis family: the article, its item page, a shop, a people, a language, a Moot, a log and a passing mention."""
    page(conn, 1, "Svidaw sis", "item", 'The svidaw sis (Rakash for "throwing beater") is a heavy weapon thrown across the body.', (1, 0))
    chunk(conn, 1, "Svidaw sis", "Variations of the svidaw sis are sold at a shop.")
    page(conn, 2, "Weapon:Svidaw sis", "item", 'Svidaw sis means "throw(ing) beat(er)" in Rakash. It weighs 60 stones.', NEAR, ns=114, name="Svidaw sis")
    page(conn, 3, "Kifa Awrocis", "shop", "Kifa Awrocis sells Rakash weapons in Siksraja.", LINK_ONLY)
    page(conn, 4, "Concept:Rakash", "race", "The Rakash are a moon-touched people.", MENTION_OK, ns=208)
    page(conn, 5, "Rakash (language)", "lore", "The native language of the Rakash is simply called Rakash.", MENTION_OK)
    page(conn, 6, "Rakash Moot 442", "event", "A svidaw sis was presented as a prize at the Moot.", MENTION_OK)
    page(conn, 7, "Ayrell/Logs/Night", "log", "Bralinson strikes the drake with his svidaw sis.", MENTION_OK, is_log=1)
    page(conn, 8, "Estate Rights (book)", "lore", "A svidaw sis, a fine horse, a mortgage and a house.", FAR_FOR_MENTION)
    page(conn, 9, "Moot 433/Auction", "event", "Lot 7 is a svidaw sis.", NEAR)
    page(conn, 10, "Trade Route", "general", "Caravans carry the svidaw sis along the road.", NEAR)
    page(conn, 11, "Shard", "place", "A city with nothing to do with weapons.", NEAR)
    link(conn, 1, 4)
    link(conn, 2, 3)
    enrich_all(conn)
    return conn


def parts(found):
    out = {}
    for p in found:
        out.setdefault(p["layer"], []).append(p["title"])
    return out


@pytest.mark.parametrize("question", [
    "Tell me about svidaw sis", "tell me all about the svidaw sis.", "What is a svidaw sis?", "What's a svidaw sis", "Describe the svidaw sis",
    "Explain svidaw sis", "What do you know about svidaw sis?", 'What is "svidaw sis"?', "Please tell me about svidaw sis", "Why is svidaw sis important?",
])
def test_a_question_about_the_whole_thing_is_open(family, question):
    found = layers.open_question(family, question)
    assert found and found.main.title == "Svidaw sis" and not found.focused


@pytest.mark.parametrize("question", [
    "What is the weight of a svidaw sis?", "How much does a svidaw sis weigh?", "Where can I buy a svidaw sis?", "What does svidaw sis mean?",
    "What is the best svidaw sis?", "Tell me about it", "What is a flibbertigibbet?", "Tell me about svidaw sis and Kifa Awrocis",
])
def test_anything_narrower_is_not(family, question):
    assert layers.open_question(family, question) is None


@pytest.mark.parametrize("question", ["What is the story behind svidaw sis?", "Tell me about the story behind Svidaw sis", "the lore of svidaw sis",
                                      "Give me the history of the svidaw sis", "What's the background of svidaw sis?"])
def test_asking_for_the_story_is_focused(family, question):
    found = layers.open_question(family, question)
    assert found and found.focused and found.main.title == "Svidaw sis"


def test_the_article_says_what_the_thing_is_and_same_named_pages_follow(family):
    found = layers.open_question(family, "What is a svidaw sis?")
    assert [c.title for c in found.pages] == ["Svidaw sis", "Weapon:Svidaw sis"]


def test_a_players_own_page_is_not_part_of_their_character(conn):
    page(conn, 1, "Binu", "pc", "Binu is a Necromancer.")
    page(conn, 2, "User:Binu", "user", "Notes.", ns=2, name="Binu")
    found = layers.open_question(conn, "Who is Binu?")
    assert [c.title for c in found.pages] == ["Binu"]


def test_a_sort_of_page_with_no_table_is_answered_as_usual(conn):
    page(conn, 1, "Moot 433", "event", "A gathering.")
    assert layers.open_question(conn, "What is Moot 433?") is None


def test_every_part_reads_its_own_sort_of_page_and_mentions_must_be_close(family):
    found = layers.gather(family, layers.open_question(family, "Tell me about svidaw sis"), 8, BASE)
    got = parts(found)
    assert got["source"] == ["Kifa Awrocis"]
    assert got["people"][0] == "Svidaw sis"  # the code-written meaning leads, in the article's words
    assert "Concept:Rakash" in got["people"] or "Rakash (language)" in got["people"]
    assert got["story"] == ["Rakash Moot 442", "Ayrell/Logs/Night"]
    titles = [p["title"] for p in found]
    assert "Estate Rights (book)" not in titles  # names it in passing
    assert "Moot 433/Auction" not in titles and "Trade Route" not in titles and "Shard" not in titles  # a listing; unsorted; unrelated
    meaning = next(p for p in found if p["layer"] == "people")
    assert '"Svidaw sis" is Rakash for "throwing beater"' in meaning["text"]


def test_a_log_is_labelled_in_its_passage_and_only_fills_what_nothing_else_does(family):
    found = layers.gather(family, layers.open_question(family, "Tell me about svidaw sis"), 8, BASE)
    log = next(p for p in found if p["title"] == "Ayrell/Logs/Night")
    assert log["text"].startswith(layers.LOG_LABEL) and log["log"]
    for n in (20, 21):
        page(family, n, f"Moot {n}", "event", "A svidaw sis was a prize.", MENTION_OK)
    found = layers.gather(family, layers.open_question(family, "Tell me about svidaw sis"), 8, BASE)
    assert "Ayrell/Logs/Night" not in [p["title"] for p in found]


def test_a_mention_inside_another_word_is_not_a_mention(conn):
    page(conn, 1, "Binu", "pc", "Binu is a Necromancer.", (1, 0))
    page(conn, 2, "Fair", "event", "The Binus family and binuran weavers came.", NEAR)
    assert layers.gather(conn, layers.open_question(conn, "Who is Binu?"), 8, BASE) == []


def test_only_its_own_pages_means_an_ordinary_answer(conn):
    page(conn, 1, "Musk hog", "creature", "A musk hog is a scruffy creature.", (1, 0))
    page(conn, 2, "Ayrell/Logs/Night", "log", "A musk hog lunges at Imroth.", MENTION_OK, is_log=1)  # creatures: no logs
    assert layers.gather(conn, layers.open_question(conn, "What is a musk hog?"), 8, BASE) == []


def test_an_empty_part_gives_its_slots_to_what_the_thing_is(conn):
    page(conn, 1, "Lanival", "npc", "Lanival led the war.", (1, 0))
    for n in range(2, 8):
        chunk(conn, 1, "Lanival", f"Lanival fought in battle number {n}.", NEAR, n)
    page(conn, 20, "Dragon Clan", "lore", "Founded by Lanival himself.", MENTION_OK)
    got = parts(layers.gather(conn, layers.open_question(conn, "Who is Lanival?"), 8, BASE))
    assert got["deeds"] == ["Dragon Clan"] and "ties" not in got
    assert len(got["own"]) == 7


def test_the_story_asked_for_says_little_about_the_rest(conn):
    page(conn, 1, "Lanival", "npc", "Lanival led the war.", (1, 0))
    for n in range(2, 8):
        chunk(conn, 1, "Lanival", f"Lanival fought in battle number {n}.", NEAR, n)
    page(conn, 20, "Dragon Clan", "lore", "Founded by Lanival himself.", MENTION_OK)
    got = parts(layers.gather(conn, layers.open_question(conn, "What is the story of Lanival?"), 8, BASE))
    assert len(got["own"]) == 2 and got["deeds"] == ["Dragon Clan"]


def test_the_story_asked_for_leaves_out_shops(family):
    got = parts(layers.gather(family, layers.open_question(family, "What is the story behind svidaw sis?"), 8, BASE))
    assert "source" not in got and got["people"][0] == "Svidaw sis" and got["story"]


def test_the_outline_tells_the_model_the_parts_their_passages_and_no_empty_ones():
    passages = [{"layer": "own"}, {"layer": "own"}, {"layer": "people"}, {"layer": "story", "log": True}]
    text = layers.outline(passages, THING)
    assert "**The name and its people**" in text and "[3]" in text and "**In the story**" in text and "[4]" in text
    assert "Where it comes from" not in text
    assert "role-play log" in text and "never talk about the passages" in text
    assert layers.headings(passages, THING) == ["The name and its people", "In the story"]


def test_the_outline_takes_the_place_of_the_shape_for_the_kind():
    system = prompts.build_answer_system("item", "[1] x", "Fennick", outline="Answer in these parts")
    assert "Answer in these parts" in system and "compact stats table" not in system
    assert "compact stats table" in prompts.build_answer_system("item", "[1] x", "Fennick")
    assert "Answer in these parts" in prompts.build_answer_system("item", "[1] x", "Fennick", persona=False, outline="Answer in these parts")


PASSAGES = [
    {"layer": "own", "text": "The svidaw sis is a heavy thrown weapon."},
    {"layer": "source", "text": "Kifa Awrocis sells Rakash weapons in Siksraja."},
    {"layer": "people", "text": '"Svidaw sis" is Rakash for "throwing beater". The Rakash designed this weapon.'},
    {"layer": "story", "text": "A svidaw sis was presented as a prize at the Rakash Moot."},
]


def test_near_miss_headings_become_exact_and_the_parts_fall_into_order():
    draft = ("A heavy thrown weapon [1].\n\n### In The Story:\nA prize at the Moot [4].\n\n"
             "**Where it comes from**\nSold at Kifa Awrocis [2].\n\n__The Name and It's People__\nRakash for a beater [3].")
    out = layers.tidy(draft, THING, PASSAGES)
    assert out == ("A heavy thrown weapon [1].\n\n**Where it comes from**\n\nSold at Kifa Awrocis [2].\n\n**The name and its people**\n\n"
                   "Rakash for a beater [3].\n\n**In the story**\n\nA prize at the Moot [4].")


def test_a_heading_on_the_same_line_as_its_text_is_split_off():
    out = layers.tidy("A weapon [1].\n\n**In the story:** A prize at the Moot [4].", THING, PASSAGES)
    assert out == "A weapon [1].\n\n**In the story**\n\nA prize at the Moot [4]."


def test_an_uncited_part_its_own_passages_say_is_tied_to_them_and_keeps_its_place():
    out = layers.tidy('A weapon [1].\n\n**The name and its people**\nThe name svidaw sis is Rakash for "throwing beater".', THING, PASSAGES)
    assert out.endswith('"throwing beater". [3*]') and "**The name and its people**" in out


def test_an_uncited_part_nothing_backs_goes_with_its_heading():
    out = layers.tidy("A weapon [1].\n\n**In the story**\nDragons once ruled the eastern sea with fire and thunder.\n\n**Where it comes from**\nSold at Kifa [2].",
                      THING, PASSAGES)
    assert "In the story" not in out and "Dragons" not in out and "**Where it comes from**" in out


def test_a_part_with_nothing_under_it_goes():
    out = layers.tidy("A weapon [1].\n\n**In the story**\n\n**Where it comes from**\nSold at Kifa [2].", THING, PASSAGES)
    assert "In the story" not in out and "Sold at Kifa [2]." in out


def test_the_same_heading_twice_is_one_part():
    out = layers.tidy("A weapon [1].\n\n**In the story**\nA prize [4].\n\n**In the story**\nAt the Moot [4].", THING, PASSAGES)
    assert out.count("In the story") == 1 and "A prize [4].\n\nAt the Moot [4]." in out


def test_a_draft_with_no_headings_is_left_as_written():
    draft = "A weapon [1]. It is thrown.\n\n- Sold at Kifa [2]."
    assert layers.tidy(draft, THING, PASSAGES) == draft


def test_ordinary_text_is_not_taken_for_a_heading():
    draft = "A weapon [1].\n\nIn the story of the Moot it was a prize [4]."
    assert layers.tidy(draft, THING, PASSAGES) == draft


def test_code_says_when_an_answer_leans_on_a_log():
    passages = [{"text": "a"}, {"text": "b", "log": True}, {"text": "c", "log": True}]
    assert layers.log_note([1], passages) == ""
    assert layers.log_note([1, 2], passages) == "(OOC) [2] is a player's role-play log, not official lore."
    assert layers.log_note([2, 3], passages) == "(OOC) [2], [3] are players' role-play logs, not official lore."


def test_a_number_in_a_page_title_is_something_that_page_says():
    answer = "It was a prize at the Rakash Moot 442, given by Ena to the winners [1]."
    passages = [{"title": "Rakash Moot 442", "section": "Prizes", "text": "A svidaw sis was a prize, given by Ena to the winners."}]
    assert verify.run(answer, passages).numbers_bad == 0


def test_every_page_kind_with_a_table_has_an_offer_and_exact_headings_are_unique():
    for table in set(LAYER_TABLE.values()):
        assert table in LAYER_OFFER and "{x}" in LAYER_OFFER[table] and LAYERS[table][0].heading == ""
        heads = [ly.heading for ly in LAYERS[table][1:]]
        assert all(heads) and len(heads) == len(set(heads))
        assert sum(ly.slots for ly in LAYERS[table]) <= 8


def model(question, reply, shown):
    class Model:
        async def chat_json(self, *a, **k):
            return {"standalone_question": question, "mode": "search", "kind": "item", "follow_up": False}

        async def embed(self, texts, **k):
            return [[0.0] * DIM for _ in texts]

        async def chat_stream(self, messages, **k):
            shown["system"] = messages[0]["content"]
            yield reply

    return Model()


def ask(conn, question, reply, settings=None, shown=None, session=None):
    shown = {} if shown is None else shown
    sid = session or memory.new_session(conn, "web")

    async def go():
        return [e async for e in Assistant(settings or Settings(), conn, model(question, reply, shown)).answer_stream(sid, question)]

    return asyncio.run(go()), shown


REPLY = ("The svidaw sis is a heavy weapon thrown across the body [1].\n\n**In the story**\nBralinson strikes the drake with his svidaw sis [{log}].\n\n"
         '**The name and its people**\nThe svidaw sis is Rakash for "throwing beater" [{meaning}].')


def numbered(conn):
    found = layers.gather(conn, layers.open_question(conn, "Tell me about svidaw sis"), 8, BASE)
    return (next(n for n, p in enumerate(found, 1) if p["title"] == "Ayrell/Logs/Night"),
            next(n for n, p in enumerate(found, 1) if p["section"] == "What the name means"))


def test_an_open_question_is_answered_in_parts_and_code_labels_the_log(family):
    log, meaning = numbered(family)
    events, shown = ask(family, "Tell me about svidaw sis", REPLY.replace("{log}", str(log)).replace("{meaning}", str(meaning)))
    done = events[-1]
    assert isinstance(done, Done)
    assert "Answer in these parts" in shown["system"] and "**In the story**" in shown["system"]
    assert done.answer.index("**The name and its people**") < done.answer.index("**In the story**")  # the table's order, not the draft's
    assert f"(OOC) [{log}] is a player's role-play log, not official lore." in done.answer
    assert "the story behind" not in " ".join(done.related)  # it has just been told


def test_an_open_question_with_only_its_own_page_is_an_ordinary_answer(conn):
    page(conn, 1, "Musk hog", "creature", "A musk hog is a scruffy creature.", (1, 0))
    events, shown = ask(conn, "What is a musk hog?", "A scruffy creature [1].")
    assert "Answer in these parts" not in shown["system"]
    assert isinstance(events[-1], Done)


def test_layered_answers_can_be_turned_off(family):
    s = Settings()
    s.chat.layered_answers = False
    _, shown = ask(family, "Tell me about svidaw sis", "A weapon [1].", settings=s)
    assert "Answer in these parts" not in shown["system"]


def test_a_narrow_question_stays_narrow_and_offers_the_rest(family):
    events, shown = ask(family, "How much does a svidaw sis weigh?", "A svidaw sis weighs 60 stones [1].")
    done = events[-1]
    assert "Answer in these parts" not in shown["system"]
    assert any(isinstance(e, Retrieval) for e in events)
    assert done.related[0] == "the story behind Svidaw sis" and "the story behind Svidaw sis" in done.answer


def test_the_rest_is_not_offered_twice_in_a_conversation(family):
    sid = memory.new_session(family, "web")
    first, _ = ask(family, "How much does a svidaw sis weigh?", "A svidaw sis weighs 60 stones [1].", session=sid)
    second, _ = ask(family, "How much does a svidaw sis weigh?", "A svidaw sis weighs 60 stones [1].", session=sid)
    assert first[-1].related[0] == "the story behind Svidaw sis"
    assert "the story behind Svidaw sis" not in second[-1].related


def test_the_offer_of_the_rest_reads_as_an_open_focused_question_when_the_page_asks_it(family):
    subject = layers.about(family, "How much does a svidaw sis weigh?")
    phrase = layers.offer(family, subject, BASE, 8)
    assert phrase == "the story behind Svidaw sis"
    asked = layers.open_question(family, "Tell me about " + phrase)  # exactly what the page's chip sends
    assert asked and asked.focused and asked.main.title == "Svidaw sis"
