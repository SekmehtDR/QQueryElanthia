"""What a name means: read only from fixed phrasings that name a language, each kept on what the sentence is naming."""

import asyncio

import pytest

from epedia.config import Settings
from epedia.core import memory, names
from epedia.core.assistant import Assistant
from epedia.db import connect
from epedia.index.doctor import run as doctor_run
from epedia.index.doctor import unknown_languages
from epedia.index.enrich import enrich_all
from epedia.index.extract import REGISTRY, Extractor
from epedia.index.extract.names import bare_name, read_meanings


def read(text, title="Some page", section=""):
    return {(f[0], f[1]): f[4] for f in read_meanings([(section, text)], title)}


@pytest.mark.parametrize("text, title, expected", [
    ('The svidaw sis (Rakash for "throwing beater") has its grip in the middle.', "Svidaw sis", ("Svidaw sis", "Rakash", "throwing beater")),
    ('Kermoria (Gamgweth for "mountain country") is a continent.', "Kermoria", ("Kermoria", "Gamgweth", "mountain country")),
    ('"Gwererest" is Gamgweth for "speak far."', "Gwererest shell", ("Gwererest", "Gamgweth", "speak far")),
    ('Nehdalata is S\'Kra for "long blade."', "Weapon:Dark bronze nehdalata", ("Nehdalata", "S'Kra", "long blade")),
    ("Uaro’s’sugi is S’Kra for “head guard.”", "Item:Thin uaro's'sugi", ("Uaro's'sugi", "S'Kra", "head guard")),
    ('The name is Elven for "cross." They are large horses.', "Zeharca horse", ("Zeharca horse", "Elven", "cross")),
    ('He was paid with the ship Slohekepi (name is S\'Kra for "Blood Sea").', "Althiras", ("Slohekepi", "S'Kra", "Blood Sea")),
    ('Elven for "sea lizard", the asaren celpeze hunts here.', "Asaren celpeze (2)", ("Asaren celpeze", "Elven", "sea lizard")),
    ('Avrenka, whose name means "Moon Tree" in Gamgweth, was born here.', "Avrenka", ("Avrenka", "Gamgweth", "Moon Tree")),
    ('Rufjs zobens means "rude scimitar" in Rakash.', "Weapon:Rufjs zobens", ("Rufjs zobens", "Rakash", "rude scimitar")),
    ("The word cuska means 'snake' in the Rakash language.", "Weapon:Darkstone cuska", ("Cuska", "Rakash", "snake")),
    ('The word "andu" (pl. andaen) literally means "day" in High Gamgweth.', "Elanthian time", ("Andu", "High Gamgweth", "day")),
    ('They are led by the Khalaen, a Gamgweth word meaning "leaders."', "Empath", ("Khalaen", "Gamgweth", "leaders")),
    ('Bestowed her "lucid" side with the name Dzive, which means "life" in Rakash.', "Klusarlaik", ("Dzive", "Rakash", "life")),
    ('Additionally, all grusana kaft means "putting knife" in Rakash.', "Grusana kaft", ("Grusana kaft", "Rakash", "putting knife")),
    ('Eldring means "lightning" or "electricity" in Haakish.', "Eldring", ("Eldring", "Haakish", "lightning or electricity")),
    ('Cashek gand Aldam (Gamgweth for "Fighter against Fate") is a spell.', "Cashek gand Aldam", ("Cashek gand Aldam", "Gamgweth", "Fighter against Fate")),
])
def test_each_phrasing_reads_the_name_the_language_and_the_meaning(text, title, expected):
    assert read(text, title) == {expected[:2]: expected[2]}


@pytest.mark.parametrize("text", [
    'This means "nothing much" to most people.',  # no language
    'A week means "a month" in Fix It language.',  # not a language
    'The trader sold it to the Rakash for "free" on Tuesday.',  # a race, in an ordinary sentence
    'Best Tale for "Jack the Giant Killer"',
    "You asked for a sword and that means trouble in Rakash.",  # no quoted meaning
])
def test_everyday_wording_is_not_a_meaning(text):
    assert read(text) == {}


def test_a_bare_name_means_is_about_its_heading_only_where_the_page_lists_several_things():
    listing = read_meanings([("Grazhir's Legacy > Aliaes", 'Name means "White Fire" in Gamgweth. Originally on Taisgath.'),
                             ("Grazhir's Legacy > Mintais", 'Name means "Silver Sky" in Gamgweth. Located in Throne City.')], "Grazhir")
    assert {f[0]: f[4] for f in listing} == {"Aliaes": "White Fire", "Mintais": "Silver Sky"}
    alone = read_meanings([("Of Note", 'Name means "God Hammer" in Gor\'Tog.')], "Dawan")
    assert [(f[0], f[4]) for f in alone] == [("Dawan", "God Hammer")]


def test_a_bare_name_means_on_a_post_a_book_or_an_item_says_nothing_about_that_page():
    for title in ("Post:Newly released rare metals - 05/10/2016", "Forest of Night (book)", "Item:Dokora-shaped credentials"):
        assert read('Its name means "Moneychanger" in Gerenshuge.', title) == {}
    assert read('"Alavern" means "cave child" in Rakash.', "Post:Newly released rare metals - 05/10/2016") == {
        ("Alavern", "Rakash"): "cave child"}


def test_bare_name():
    assert bare_name("Weapon:Rufjs zobens") == "Rufjs zobens"
    assert bare_name("Asaren celpeze (2)") == "Asaren celpeze"


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    yield c
    c.close()


def add_page(c, pid, title, text, ns=0, kind="general", revid=1, is_log=0, categories="[]"):
    c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, fetched_revid, indexed_revid, is_redirect, page_kind, "
              "is_log, categories, updated_at) VALUES (?,?,?,?,?,?,?,0,?,?,?,'2026-01-01')",
              (pid, ns, title, title.lower(), revid, revid, revid, kind, is_log, categories))
    c.execute("INSERT INTO chunks(chunk_id, page_id, revid, ord, kind, title, section_path, text, token_est, content_hash) "
              "VALUES (?,?,?,0,'section',?,'',?,1,?)", (pid, pid, revid, title, text, str(pid)))
    c.execute("INSERT INTO chunks_fts(rowid, title, section_path, text) VALUES (?,?,'',?)", (pid, title, text))


SVIDAW = 'The svidaw sis (Rakash for "throwing beater") is thrown.'


def test_enrich_reads_meanings_but_not_logs_or_talk_and_a_later_edit_can_remove_one(conn):
    add_page(conn, 1, "Svidaw sis", SVIDAW, kind="item")
    add_page(conn, 2, "Binu/Logs/Night", 'Binu says "Ulu" means "dinner" in Toggish.', kind="log", is_log=1)
    add_page(conn, 3, "Talk:Ulu", '"Ulu" means "dinner" in Toggish.', ns=1, kind="talk")
    enrich_all(conn)
    rows = conn.execute("SELECT entity, grp, value_text FROM facts WHERE domain='name_meaning'").fetchall()
    assert [tuple(r) for r in rows] == [("Svidaw sis", "Rakash", "throwing beater")]

    conn.execute("UPDATE chunks SET text='Now it is just a club.', revid=2 WHERE page_id=1")
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    conn.execute("UPDATE pages SET latest_revid=2, fetched_revid=2, indexed_revid=2 WHERE page_id=1")
    assert enrich_all(conn)["suspect"] == 0  # one line off a page is an edit, not a changed layout
    assert not conn.execute("SELECT 1 FROM facts WHERE domain='name_meaning'").fetchall()
    assert {c.name: c for c in doctor_run(conn)}["facts are current"].ok


def test_two_extractors_on_one_page_each_read_it_and_are_each_skipped_once_current(conn, monkeypatch):
    add_page(conn, 1, "Svidaw sis", SVIDAW)
    calls = []

    def reader(domain):
        def run(chunks, title):
            calls.append(domain)
            return [(title, "g", domain, None, "v", "")]
        return Extractor(domain, (), run, lambda c: ["Svidaw sis"])

    monkeypatch.setattr("epedia.index.enrich.REGISTRY", [reader("one")])
    enrich_all(conn)
    assert calls == ["one"]
    monkeypatch.setattr("epedia.index.enrich.REGISTRY", [reader("one"), reader("two")])
    enrich_all(conn)
    assert calls == ["one", "two"]  # a new extractor reads the page the first already made current
    enrich_all(conn)
    assert calls == ["one", "two"]
    assert {r[0] for r in conn.execute("SELECT domain FROM enriched")} == {"one", "two"}
    assert any(e.domain == "name_meaning" for e in REGISTRY)


def test_doctor_names_a_language_the_reader_does_not_know(conn):
    add_page(conn, 1, "Rakash (language)", "x", categories='["Languages", "Rakash"]')
    add_page(conn, 2, "Common", "x", categories='["Languages"]')
    assert unknown_languages(conn) == []
    add_page(conn, 3, "Frostspeech", "x", categories='["Languages"]')
    assert unknown_languages(conn) == ["Frostspeech"]


@pytest.fixture
def words(conn):
    add_page(conn, 1, "Svidaw sis", SVIDAW, kind="item")
    add_page(conn, 2, "Weapon:Svidaw sis", 'Svidaw sis means "throw(ing) beat(er)" in Rakash.', ns=114, kind="item")
    add_page(conn, 3, "Weapon:Dark bronze nehdalata", 'Nehdalata is S\'Kra for "long blade."', ns=114, kind="item")
    add_page(conn, 4, "Language", "A word, a term and a name each have a meaning.")  # the wiki does use these words
    enrich_all(conn)
    return conn


def test_one_meaning_written_two_ways_is_stated_once_from_the_article(words):
    found = names.meanings(words, ["Svidaw sis", "Weapon:Svidaw sis"])
    assert [(m.entity, m.language, m.meaning, m.title) for m in found] == [("Svidaw sis", "Rakash", "throwing beater", "Svidaw sis")]


def test_a_long_item_title_finds_the_word_it_carries_and_only_that(words):
    assert [m.entity for m in names.meanings(words, ["Weapon:Dark bronze nehdalata"])] == ["Nehdalata"]
    assert names.meanings(words, ["Dark bronze sword"]) == []


def test_a_passage_only_when_the_question_asks_what_something_means(words):
    base = "https://elanthipedia.play.net"
    assert names.passages(words, "How much does a svidaw sis weigh?", ["Svidaw sis"], base) == []
    (p,) = names.passages(words, "What does svidaw sis mean?", ["Svidaw sis"], base)
    assert p["title"] == "Svidaw sis" and p["chunk_id"] is None
    assert p["text"] == '"Svidaw sis" is Rakash for "throwing beater". (The language: Rakash (language).)'
    (p,) = names.passages(words, "What does nehdalata mean?", [], base)  # a word with no page of its own
    assert '"Nehdalata" is S\'Kra for "long blade".' in p["text"] and p["title"] == "Weapon:Dark bronze nehdalata"
    assert names.passages(words, "What does this sword mean?", [], base) == []


def meaning_model(question):
    shown = {}

    class Model:
        async def chat_json(self, *a, **k):
            return {"standalone_question": question, "mode": "search", "kind": "item", "follow_up": False}

        async def embed(self, texts, **k):
            return [[0.0] * 768 for _ in texts]

        async def chat_stream(self, messages, **k):
            shown["prompt"] = str(messages)
            yield 'It is Rakash for "throwing beater" [1].'

    return Model(), shown


def ask(conn, model, question):
    sid = memory.new_session(conn, "web")

    async def go():
        return [e async for e in Assistant(Settings(), conn, model).answer_stream(sid, question)]

    return asyncio.run(go())[-1]


def test_a_question_that_also_asks_more_goes_to_the_model_with_the_meaning_first(words):
    model, shown = meaning_model("What does svidaw sis mean to the Rakash people?")
    done = ask(words, model, "What does svidaw sis mean to the Rakash people?")
    assert done.sources[0].title == "Svidaw sis" and "throwing beater" in done.answer
    assert '"Svidaw sis" is Rakash for "throwing beater"' in shown["prompt"]


@pytest.mark.parametrize("question", ["What does svidaw sis mean?", "What does 'svidaw sis' mean?", "what does the word svidaw sis mean",
                                      "What is the meaning of the name svidaw sis?", "What does a svidaw sis mean?"])
def test_a_question_only_about_the_meaning_is_answered_by_code(words, question):
    model, shown = meaning_model(question)
    done = ask(words, model, question)
    assert done.answer.startswith('"Svidaw sis" is Rakash for "throwing beater" [1].')
    assert done.sources[0].title == "Svidaw sis" and "prompt" not in shown  # the model wrote none of it


def test_a_word_the_wiki_does_not_explain_goes_to_the_model(words):
    assert names.answer(words, "What does dragon mean?", "https://x") == []
    assert names.answer(words, "How much does a svidaw sis weigh?", "https://x") == []
