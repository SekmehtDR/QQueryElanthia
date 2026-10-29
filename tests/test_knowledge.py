import json

import pytest

from epedia import db
from epedia.config import Settings
from epedia.index import topics
from epedia.index.indexer import rebuild_titles
from epedia.kinds import intent_groups
from epedia.retrieval import resolve


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = db.connect(s)
    pages = [
        (1, 0, "Sidasas Sedra", "spell", None), (2, 0, "Svidaw sis", "mechanics", None),
        (3, 114, "Weapon:Svidaw sis", "item", None), (4, 0, "Mo", "npc", None), (5, 0, "Marshal Order", "spell", None),
        (6, 0, "Rat", "creature", None), (7, 118, "Item:Rat", "item", None), (8, 116, "Title:Thief", "item", None),
        (9, 0, "Thief", "guild", None),
        (10, 0, "Crafting", "crafting", None), (11, 0, "Forging skill", "skill", json.dumps(["Crafting"])),
        (12, 0, "Weaponsmithing discipline", "skill", None), (13, 114, "Weapon:Greatsword", "item", json.dumps(["Crafted weapons"])),
        (14, 14, "Category:Crafted weapons", "category", None), (15, 0, "Crafting/Feedback", "crafting", None),
        (16, 0, "Binu", "pc", None), (17, 0, "Smite Horde", "spell", None), (18, 0, "Haste", "spell", None),
    ]
    for pid, ns, title, kind, cats in pages:
        c.execute("INSERT INTO pages(page_id, ns, title, title_norm, is_redirect, page_kind, categories, updated_at,"
                  " classified_revid, fetched_revid) VALUES (?,?,?,?,0,?,?, '2026-01-01', 1, 1)",
                  (pid, ns, title, title.split(":")[-1].lower(), kind, cats))
    c.execute("INSERT INTO redirects(from_title, from_norm, to_title, to_page_id) VALUES ('SIS', 'sis', 'Sidasas Sedra', 1),"
              " ('Mo', 'mo', 'Marshal Order', 5)")
    # links: Crafting <-> Forging skill (both ways), Crafting -> Weaponsmithing discipline (one way), a bystander links nowhere
    c.executemany("INSERT INTO page_links VALUES (?, ?)",
                  [(10, 11), (11, 10), (10, 12), (13, 14), (15, 10), (10, 16), (17, 18)])
    rebuild_titles(c)
    yield c
    c.close()


def test_an_acronym_reaches_its_page_and_sets_aside_pages_that_only_contain_the_letters(conn):
    res = resolve.resolve(conn, "What is SIS?")
    assert [c.title for c in res.chosen] == ["Sidasas Sedra"] and res.acronym and not res.ambiguous
    assert resolve.set_aside(conn, res) == {2, 3}  # the Svidaw sis weapon is a different thing
    other = resolve.resolve(conn, "What is a Svidaw sis?")
    assert not other.acronym and {c.page_id for c in other.chosen} == {2, 3}


def test_one_name_reaching_two_specific_kinds_is_asked_about_unless_the_question_says(conn):
    res = resolve.resolve(conn, "What is Mo?")
    assert res.ambiguous and "Which did you mean?" in resolve.clarify(res)
    spell = resolve.resolve(conn, "How do I cast Mo?")
    assert [c.title for c in spell.chosen] == ["Marshal Order"] and not spell.ambiguous
    who = resolve.resolve(conn, "Who is Mo?")
    assert [c.title for c in who.chosen] == ["Mo"]


def test_an_item_twin_does_not_force_a_question_and_title_pages_are_not_names(conn):
    res = resolve.resolve(conn, "What is a rat?")
    assert not res.ambiguous and [c.title for c in res.chosen] == ["Rat"]
    guild = resolve.resolve(conn, "Tell me about the Thief")
    assert [c.title for c in guild.candidates] == ["Thief"]  # Title:Thief (a character title) is not a rival


def test_common_words_and_the_game_name_are_not_names(conn):
    assert resolve.resolve(conn, "What is the best way to do that?") is None


def test_kind_rules_read_the_questions_own_words():
    assert "spell" in intent_groups("how do I cast it")
    assert "thing" in intent_groups("what weapon is it") and "creature" in intent_groups("where do I hunt it")
    assert intent_groups("tell me about Binu") == set()


def test_the_topic_graph_is_built_from_categories_subpages_and_links(conn):
    out = topics.build(conn, force=True)
    assert out["hubs"] >= 1
    fam = topics.family(conn, "How does crafting work?")
    assert fam[11] == 0.8  # linked both ways: a close member
    assert fam[12] == 0.4  # linked one way: a looser one
    assert fam[15] == 1.0  # a subpage of the hub (and links back)
    assert 17 not in fam
    assert topics.build(conn) == {"hubs": 0, "links": 0}  # nothing classified since: left alone


def test_a_subpage_belongs_to_every_hub_above_it_and_names_match_in_any_case(conn):
    conn.execute("INSERT INTO pages(page_id, ns, title, title_norm, is_redirect, page_kind, updated_at, classified_revid,"
                 " fetched_revid) VALUES (30, 0, 'crafting/Feedback/Archive', 'crafting/feedback/archive', 0, 'crafting',"
                 " '2026-01-01', 1, 1)")
    topics.build(conn, force=True)
    assert conn.execute("SELECT weight, via FROM page_topics WHERE hub_page_id=10 AND page_id=30").fetchone()[:] == (1.0, "subpage")


def test_the_most_specific_name_wins_among_equals_and_shouting_is_not_an_acronym(conn):
    conn.execute("INSERT INTO pages(page_id, ns, title, title_norm, is_redirect, page_kind, updated_at, classified_revid,"
                 " fetched_revid) VALUES (31, 0, 'Hunt command', 'hunt command', 0, 'command', '2026-01-01', 1, 1)")
    conn.execute("INSERT INTO redirects(from_title, from_norm, to_title, to_page_id) VALUES ('hunt', 'hunt', 'Hunt command', 31)")
    rebuild_titles(conn)
    assert resolve.resolve(conn, "Where do I hunt rats?").chosen[0].title == "Rat"  # the creature, not the command
    loud = resolve.resolve(conn, "WHAT IS SIS")
    assert loud is not None and not loud.acronym and resolve.set_aside(conn, loud) == set()


def test_a_forum_post_that_merely_links_to_a_hub_is_not_in_its_family(conn):
    conn.execute("INSERT INTO pages(page_id, ns, title, title_norm, is_redirect, page_kind, updated_at, classified_revid,"
                 " fetched_revid) VALUES (40, 0, 'Post:Crafting rant', 'post:crafting rant', 0, 'post', '2026-01-01', 1, 1)")
    conn.execute("INSERT INTO page_links VALUES (40, 10)")
    topics.build(conn, force=True)
    assert 40 not in topics.family(conn, "How does crafting work?") and 12 in topics.family(conn, "How does crafting work?")
