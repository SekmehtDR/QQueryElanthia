"""Retrieval for a festival named without its year, and a passage that names the page the answer is really on.

From a real chat: "available forging tasks at hollows eve" was answered from festivals 403, 436, 439 and 443 at once, and
never named Poltu, the one task giver whose page says his tasks use the Forging system."""

import pytest

from epedia import db
from epedia.config import Settings
from epedia.core.assistant import Assistant, _key_stems, _stems, _this_years

TASKS = ("- You may once again aid the Merelew.\n- Zasele, Bradyn, Poltu, Nezhiri & Dorhum wander the lower island, giving "
         "crafting related tasks in exchange for Random Crafting Material Rewards.")
PAGES = {  # title: (kind, lead text)
    "Hollow Eve Festival 458": ("event", TASKS),
    "Poltu": ("npc", "Poltu / Tasks: craft\n\nPoltu has been available at each Hollow Eve festival. He gives out tasks that "
                     "use the Forging system to complete."),
    "Zasele": ("npc", "Seamstress Zasele gives out tasks that use the Outfitting skill."),
    "Bradyn": ("npc", "Mechanic Bradyn asks for engineered products."),
    "Gaethzen": ("material", "A glowing forging-free stone."),  # linked, but not named in the passage: never followed
}
LINKS = [("Hollow Eve Festival 458", t) for t in ("Poltu", "Zasele", "Bradyn", "Gaethzen")]


def passage(title, text=None, kind="event"):
    return {"title": title, "section": "", "text": text or f"Text of {title}.", "chunk_id": None, "page_kind": kind}


@pytest.fixture
def assistant(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = db.connect(s)
    for pid, (title, (kind, text)) in enumerate(PAGES.items(), start=1):
        c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, fetched_revid, indexed_revid, is_redirect,"
                  " page_kind, updated_at) VALUES (?, 0, ?, ?, 5, 5, 5, 0, ?, 'x')", (pid, title, title.lower(), kind))
        c.execute("INSERT INTO chunks(page_id, revid, ord, kind, title, section_path, text, token_est, content_hash)"
                  " VALUES (?, 5, 0, ?, ?, '', ?, 1, ?)", (pid, kind, title, text, str(pid)))
    ids = {t: i for i, t in enumerate(PAGES, start=1)}
    c.executemany("INSERT INTO page_links(from_page_id, to_page_id) VALUES (?, ?)", [(ids[a], ids[b]) for a, b in LINKS])
    a = Assistant.__new__(Assistant)
    a.conn, a.s = c, s
    yield a
    c.close()


def test_key_words_are_what_the_question_is_about():
    assert _key_stems("What forging tasks are available at Hollows Eve?") == {"forg", "task", "hollow"}
    assert "forg" in _stems("He gives out tasks that use the Forging system") and "forg" in _stems("a forge")


def test_a_festival_without_a_year_is_this_years():
    passages = [passage("Poltu/Archive", kind="npc"), passage("Hollow Eve Festival 454"), passage("Hollow Eve Festival 436/Aiding the Merelew"),
                passage("Tool as a Cucumber", kind="shop")]
    extras = [passage("Hollow Eve Festival 458"), passage("Hollow Eve Festival 443"), passage("Bradyn", kind="npc")]
    kept = [p["title"] for p in _this_years("forging tasks at hollow eve", passages, extras, 0)]
    assert kept == ["Poltu/Archive", "Tool as a Cucumber", "Hollow Eve Festival 458", "Bradyn"]  # runners-up fill the room
    for q in ("forging tasks at hollow eve 436", "what forging tasks were there at past hollow eves?"):
        assert _this_years(q, passages, extras, 0) == passages, q
    circles = [passage("Barbarian Circle 100", kind="guide"), passage("Barbarian Circle 150", kind="guide")]
    assert _this_years("barbarian circle requirements", circles, [], 0) == circles  # not festival years
    assert _this_years("forging tasks at hollow eve", passages, extras, 2)[:2] == passages[:2]  # what was asked for stays


def test_a_passage_lacking_the_key_word_brings_the_named_page_that_has_it(assistant):
    passages = [passage("Hollow Eve tickets"), passage("Hollow Eve Festival 458", TASKS), passage("Hollow Eve Festival 454"),
                passage("Tool as a Cucumber")]
    out = assistant._follow_names("What forging tasks are available at Hollows Eve?", passages, 0)
    assert [p["title"] for p in out] == ["Hollow Eve tickets", "Hollow Eve Festival 458", "Poltu", "Hollow Eve Festival 454"]
    assert "Forging system" in out[2]["text"]  # Zasele and Bradyn are named too, but their pages say nothing of forging


def test_nothing_is_followed_when_the_passage_already_says_it(assistant):
    passages = [passage("Hollow Eve Festival 458", TASKS)]
    assert assistant._follow_names("What crafting tasks are at Hollow Eve?", passages, 0) == passages
    assert assistant._follow_names("forging tasks", passages, 1) == passages  # a page asked for is never the start
