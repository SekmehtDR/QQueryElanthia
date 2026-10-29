import pytest

from epedia import db
from epedia.config import Settings
from epedia.core import disciplines
from epedia.index.enrich import enrich_all

NOTES = ("Not all the disciplines have been released. Currently, the following are available:\n\n- Alchemy\n  - Remedies\n"
         "- Engineering\n  - Carving\n  - Shaping\n  - Tinkering\n- Forging\n  - Armorsmithing\n  - Blacksmithing\n"
         "  - Weaponsmithing\n")
GUIDES = "The following crafting player guides are available:\n\n- Carving - Guide to Stonecarving\n- Forging - Forging guide\n"


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = db.connect(s)
    c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, fetched_revid, indexed_revid, is_redirect,"
              " page_kind, updated_at) VALUES (1, 0, 'Crafting', 'crafting', 5, 5, 5, 0, 'crafting', 'x')")
    for i, (sec, text) in enumerate([("General Notes", NOTES), ("Guides", GUIDES)]):
        c.execute("INSERT INTO chunks(page_id, revid, ord, kind, title, section_path, text, token_est, content_hash)"
                  " VALUES (1, 5, ?, 'section', 'Crafting', ?, ?, 1, ?)", (i, sec, text, str(i)))
    enrich_all(c)
    yield c
    c.close()


def test_the_disciplines_are_listed_under_their_skill_with_the_guide_the_page_names(conn):
    out = disciplines.passage(conn, "What are the crafting disciplines?", "https://x")
    answer = out["answer"]
    assert "**Forging** skill (guide: Forging guide): Armorsmithing, Blacksmithing, Weaponsmithing" in answer
    assert "Carving (guide: Guide to Stonecarving)" in answer and "3 Lore skills" in answer
    assert out["url"] == "https://x/Crafting#General_Notes"


def test_only_a_question_about_disciplines_gets_the_list(conn):
    assert disciplines.passage(conn, "How does crafting work?", "https://x") is None
    assert disciplines.wants("which crafting skills are there") and disciplines.wants("what can I craft?")
