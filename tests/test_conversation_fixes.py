"""Fixes from a real conversation ("which player knows the most about the game's time mechanics?"): "try again" and "none
of those pages had anything to do with it" re-search without the pages already shown; a message that says "you" is not
small talk once a conversation is under way; a pasted wiki link reads that page; "I'll have to look through the pages"
counts as a pretended look-up; and Talk and User pages are indexed, below articles, never competing for names."""

import asyncio
import struct

import pytest

from epedia.config import Settings
from epedia.core import capabilities, memory, verify
from epedia.core.assistant import Assistant, Retrieval
from epedia.db import connect
from epedia.index.classify import classify_page
from epedia.index.indexer import rebuild_titles
from epedia.kinds import OFFER_EXCLUDED_PAGE_KINDS, namespace_weight
from epedia.llm import prompts
from epedia.retrieval import resolve

PAGES = {
    # page_id: (ns, title, kind, text)
    1: (0, "Binu", "pc", "Binu is a Human Necromancer in the Prime instance."),
    2: (0, "Musk hog", "creature", "Musk hogs are found in the Crossing hog pens and weigh 40 stones."),
    3: (0, "Elanthian time", "mechanics", "The Elanthian day is divided into 12 anlaslae."),
    4: (1, "Talk:Elanthian time", "talk", "According to my original calculations, the DR Epoch occurred in 1901. --Dandin"),
    5: (2, "User:Dandin", "user", "Formulas I'm maintaining: TDP related. - Dandin"),
    6: (1, "Talk:Binu", "talk", "Is Binu still played? --Someone"),
    # the real wiki has every everyday word somewhere; a small test index needs them spelled out
    7: (0, "New players", "general", "Be sure to ask where things are; time and players are explained here. Which "
                                     "player worked out what is told on talk pages."),
}


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    for pid, (ns, title, kind, text) in PAGES.items():
        norm = title.split(":", 1)[-1].lower()
        c.execute("INSERT INTO pages(page_id, ns, title, title_norm, updated_at, page_kind) VALUES (?,?,?,?,?,?)",
                  (pid, ns, title, norm, "2026-01-01", kind))
        c.execute("INSERT INTO chunks(chunk_id, page_id, revid, ord, kind, title, section_path, text, token_est, "
                  "content_hash) VALUES (?,?,1,0,'section',?,'',?,10,?)", (pid, pid, title, text, str(pid)))
        vec = [0.0] * 768
        vec[pid] = 1.0
        c.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)", (pid, struct.pack("768f", *vec)))
    c.execute("INSERT INTO redirects(from_title, from_norm, to_title, to_page_id) VALUES ('Musk hogs', 'musk hogs', "
              "'Musk hog', 2)")
    rebuild_titles(c)
    yield c
    c.close()


class Model:
    def __init__(self, answers, kind="character"):
        self.answers = list(answers)
        self.kind = kind
        self.planned: list[str] = []
        self.calls: list[list[dict]] = []

    async def chat_json(self, messages, schema, **k):
        q = messages[-1]["content"].rsplit("Latest user message: ", 1)[-1].split("\n")[0]
        self.planned.append(q)
        return {"standalone_question": q, "mode": "search", "kind": self.kind, "follow_up": False}

    async def embed(self, texts, **k):
        vec = [0.0] * 768
        vec[1] = 1.0
        return [vec for _ in texts]

    async def chat_stream(self, messages, temperature=None, **k):
        self.calls.append(messages)
        yield self.answers.pop(0)


def run(a, sid, q):
    async def go():
        return [e async for e in a.answer_stream(sid, q)]

    return asyncio.run(go())


def retrieval(events) -> Retrieval:
    return next(e for e in events if isinstance(e, Retrieval))


# ------------------------------------------------------------------------------------------------ try again

@pytest.mark.parametrize("q,yes", [
    ("Are you going to try again or not?", True), ("try again", True), ("That's not what I asked", True),
    ("None of the pages you consulted had anything to do with the time mechanics of the game.", True),
    ("You didn't answer my question", True), ("this has nothing to do with it", True), ("wrong pages", True),
    ("How do I try again at a failed craft?", False), ("None of the guilds have anything to do with forging?", False),
    ("What is the wrong way to skin a hog?", False), ("Who is Binu?", False),
])
def test_rejections_are_recognised(q, yes):
    assert capabilities.is_rejection(q) is yes


def test_try_again_searches_the_last_question_without_the_pages_already_shown(conn):
    model = Model(["Binu is a Human Necromancer [1].", "Nothing new on him [1].", "Still nothing [1]."])
    s = Settings()
    s.retrieval.k_final, s.retrieval.offer_related = 2, False  # a small index: each answer shows only a few pages
    a = Assistant(s, conn, model)
    sid = memory.new_session(conn, "web")
    first = run(a, sid, "Who is Binu?")
    assert retrieval(first).sources[0].title == "Binu"
    events = run(a, sid, "Are you going to try again or not?")
    r = retrieval(events)
    assert model.planned == ["Who is Binu?"]  # not planned again, and never filed as small talk
    assert r.mode == "retry" and r.standalone_q == "Who is Binu?"
    assert "Binu" not in [s.title for s in r.sources]  # the page that missed is left out
    assert prompts.RETRY in model.calls[1][0]["content"]
    assert model.calls[1][-1]["content"] == "Are you going to try again or not? (This is about my last question: Who is Binu?)"
    # a second complaint, after the retry, is still about the same question, and leaves out both answers' pages
    events = run(a, sid, "None of the pages you consulted had anything to do with my question.")
    r = retrieval(events)
    assert r.mode == "retry" and r.standalone_q == "Who is Binu?" and model.planned == ["Who is Binu?"]
    shown_before = {s["title"] for m in memory.messages(conn, sid)[:4] if m["role"] == "assistant" for s in m["sources"]}
    assert not shown_before & {s.title for s in r.sources}


def test_in_a_conversation_only_greetings_and_questions_about_fennick_are_small_talk(conn):
    model = Model(["Hello there!", "Musk hogs live in the Crossing [1].", "You're welcome!"], kind="meta")
    a = Assistant(Settings(), conn, model)
    sid = memory.new_session(conn, "web")
    assert retrieval(run(a, sid, "Hi Fennick!")).kind == "meta"
    # the planner says "meta" (it has "you" in it), but with a conversation under way it is about the subject
    assert retrieval(run(a, sid, "Do you know where musk hogs live?")).kind == "general"
    assert retrieval(run(a, sid, "Thanks, that helps!")).kind == "meta"


# ------------------------------------------------------------------------------------------------ links

def test_a_pasted_wiki_link_reads_that_page_first(conn):
    model = Model(["The day has 12 anlaslae [1]."], kind="mechanics")
    a = Assistant(Settings(), conn, model)
    events = run(a, memory.new_session(conn, "web"), "Check the correct wiki article: https://elanthipedia.play.net/Elanthian_time")
    assert model.planned == ['Check the correct wiki article: "Elanthian time"']  # the planner reads the title
    assert retrieval(events).sources[0].title == "Elanthian time"
    saved = conn.execute("SELECT content FROM messages WHERE role='user'").fetchone()[0]
    assert saved.endswith("https://elanthipedia.play.net/Elanthian_time")  # the question is kept as typed


def test_links_through_redirects_and_to_unknown_pages(conn):
    a = Assistant(Settings(), conn, Model([]))
    text, ids = a._linked_pages("see https://elanthipedia.play.net/index.php?title=Musk_hogs&oldid=5 please")
    assert text == 'see "Musk hog" please' and ids == [2]
    text, ids = a._linked_pages("and https://elanthipedia.play.net/Nowhere_Special")
    assert text == 'and "Nowhere Special"' and ids == []  # not indexed: still searched by its title
    assert a._linked_pages("https://example.com/Binu") == ("https://example.com/Binu", [])


# ------------------------------------------------------------------------------------------------ look-ups

@pytest.mark.parametrize("line,fake", [
    ("I'll have to look through the pages to find any other names.", True),
    ("I think I mentioned Phez before, but maybe I should check if there are other players.", True),
    ("I need to search my notes.", True), ("I'd have to dig into the edits.", True),
    ("I should note that the page is outdated.", False), ("I'd check the auction house first.", False),
    ("You should check the TIME command.", False), ("Let me know if you want more.", False),
])
def test_pretended_look_ups_in_more_words(line, fake):
    assert bool(verify.FAKE_ACTION.search(line)) is fake


# ------------------------------------------------------------------------------------------------ talk and user pages

def test_every_namespace_has_a_kind_and_ranks_below_articles_unless_it_is_content():
    assert classify_page(1, "Talk:Elanthian time", ["Mechanics"]).kind == "talk"
    assert classify_page(3, "User talk:Naeya", []).kind == "talk"  # every odd namespace is discussion
    assert classify_page(119, "Item talk:Alabaster flask", []).kind == "talk"
    assert classify_page(2, "User:Dandin", ["Player Characters"]).kind == "user"
    assert classify_page(2, "User:Dandin/Expending TDPs on Attributes", []).kind == "user"
    assert classify_page(6, "File:Tools.png", []).kind == "file"
    for ns, title in ((8, "MediaWiki:Common.css"), (10, "Template:Armor"), (106, "Form:Armor"),
                      (202, "Property:Uses material"), (212, "smw/schema:Group:Predefined properties"), (828, "Module:WordCount")):
        assert classify_page(ns, title, []).kind == "site", title
    assert classify_page(0, "Binu", ["Player Characters"]).kind == "pc"  # content is unchanged
    assert {"talk", "user", "file", "site"} <= OFFER_EXCLUDED_PAGE_KINDS  # context, never offered as a thread
    s = Settings()
    weight = lambda ns: s.retrieval.ns_weights.get(str(ns), namespace_weight(ns))  # noqa: E731 (as search applies it)
    assert weight(0) == 1.0 and weight(208) == 1.0
    assert all(weight(ns) < 1 for ns in (1, 2, 3, 6, 8, 10, 119, 202, 828))  # below the articles
    assert weight(8) < weight(6) < weight(1)  # site CSS and scripts lowest of all


def test_a_template_never_competes_for_its_articles_name(conn):
    conn.execute("INSERT INTO pages(page_id, ns, title, title_norm, updated_at, page_kind) VALUES "
                 "(20, 10, 'Template:Binu', 'binu', '2026-01-01', 'site')")
    # an old name redirecting to a talk page is no name either ("Binu talk" -> Talk:Binu)
    conn.execute("INSERT INTO redirects(from_title, from_norm, to_title, to_page_id) VALUES "
                 "('Binu talk', 'binu talk', 'Talk:Binu', 6)")
    rebuild_titles(conn)
    assert conn.execute("SELECT count(*) FROM titles_fts WHERE page_id=6").fetchone()[0] == 0
    assert resolve.resolve(conn, "Who is Binu?").candidates[0].title == "Binu"
    assert conn.execute("SELECT count(*) FROM titles_fts WHERE page_id=20").fetchone()[0] == 0


def test_a_template_edit_refetches_the_template_and_rerenders_its_users():
    from epedia.wiki import sync as syncmod

    class Feed:
        async def paginate(self, params):
            if params.get("list") == "allrevisions":
                yield {"query": {"allrevisions": [
                    {"ns": 10, "title": "Template:Armor", "revisions": [{"timestamp": "2026-10-30T00:00:00Z"}]},
                    {"ns": 1, "title": "Talk:Binu", "revisions": [{"timestamp": "2026-10-30T00:00:01Z"}]},
                ]}}

    s = Settings()
    s.wiki.namespaces = [0, 1, 10]
    ch = asyncio.run(syncmod.collect_changes(Feed(), s, "2026-10-29T00:00:00Z"))
    assert ch.template_titles == {"Template:Armor"} and ch.titles == {"Template:Armor", "Talk:Binu"}
    s.wiki.namespaces = [0]  # not indexed: a template edit only re-renders the pages that use it, as before
    ch = asyncio.run(syncmod.collect_changes(Feed(), s, "2026-10-29T00:00:00Z"))
    assert ch.template_titles == {"Template:Armor"} and ch.titles == set()


def test_a_talk_page_never_competes_for_its_articles_name_but_a_user_page_is_a_name(conn):
    titles = {r[0] for r in conn.execute("SELECT p.title FROM titles_fts t JOIN pages p USING(page_id)")}
    assert "Talk:Binu" not in titles and "Talk:Elanthian time" not in titles and "User:Dandin" in titles
    res = resolve.resolve(conn, "Who is Binu?")
    assert [c.title for c in res.candidates] == ["Binu"] and not res.ambiguous  # not "Binu could mean... the talk page"
    res = resolve.resolve(conn, "Who is Dandin?")
    assert [c.title for c in res.chosen] == ["User:Dandin"]


@pytest.mark.parametrize("q,yes", [
    ("Which player knows the most about the game's time mechanics?", True), ("Who worked out the DragonRealms epoch?", True),
    ("Who is the expert on skinning?", True), ("Which GM wrote the Barbarian guide?", True),
    ("Who is Binu?", False), ("What time is it in Elanthia?", False), ("Which guild can cast Aegis of Granite?", False),
])
def test_questions_about_who_knows_what_are_recognised(q, yes):
    from epedia.retrieval.search import WHO_KNOWS

    assert bool(WHO_KNOWS.search(q)) is yes


def test_who_worked_something_out_reads_the_talk_pages(conn):
    from epedia.retrieval.search import discussion_search

    s = Settings()
    hits = discussion_search(conn, "Who worked out the epoch calculations?", s.retrieval, s.wiki.base_url, None, k=3)
    assert [h.title for h in hits] == ["Talk:Elanthian time"]  # discussion and player pages only, one per page
    model = Model(["Dandin worked out the epoch on the talk page [2]."], kind="general")
    s.retrieval.offer_related = False
    events = run(Assistant(s, conn, model), memory.new_session(conn, "web"), "Which player worked out the DR Epoch?")
    titles = [x.title for x in retrieval(events).sources]
    assert "Talk:Elanthian time" in titles[:3]  # near the top, where the token budget keeps it


def test_the_answer_prompt_says_how_to_credit_talk_and_user_pages():
    assert '"Talk:" page' in prompts.PERSONA_BLOCK and "name who signed" in prompts.PERSONA_BLOCK
    assert '"Talk:" page' in prompts.NEUTRAL_ANSWER_SYSTEM
    assert "Dandin" not in prompts.PERSONA_BLOCK  # an example name would be copied, and would pass the name check


# ------------------------------------------------------------------------------------------------ "give me all ..."

@pytest.mark.parametrize("q,yes", [
    ("Give me all metals that are blue or partially blue", True), ("list every creature in the Crossing", True),
    ("Give me all that are blue in color", True), ("Is there a complete list of spells?", True),
    ("list of hunting areas", True), ("Who is Binu?", False), ("Is that all?", False),
    ("Tell me everything about Binu", False), ("Can I hunt at all at circle 5?", False),
])
def test_questions_asking_for_everything_are_recognised(q, yes):
    assert capabilities.wants_all(q) is yes


def test_a_model_written_list_for_all_says_it_is_partial(conn):
    model = Model(["Here are some:\n- Binu [1]\n- Musk hog [2]", "Binu is a Human Necromancer [1]."])
    s = Settings()
    s.retrieval.offer_related = False
    a = Assistant(s, conn, model)
    sid = memory.new_session(conn, "web")
    answer = run(a, sid, "Give me all players and hogs")[-1].answer
    assert "so there are likely more" in answer and "pages I could read" in answer
    single = run(a, memory.new_session(conn, "web"), "Who is Binu?")[-1].answer
    assert "likely more" not in single  # not a list question


def test_the_partial_list_note_counts_pages():
    assert "the one page I could read" in capabilities.partial_list(1, True)
    assert "the 4 pages retrieved" in capabilities.partial_list(4, False)
