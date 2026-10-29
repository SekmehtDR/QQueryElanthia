import pytest

from epedia import db
from epedia.config import Settings
from epedia.core import materials
from epedia.index.enrich import enrich_all
from epedia.index.extract.materials import extract

METALS = """|  | Hardness | Durability | Workability | Electrical | Thermal | Physical | Ductility | Capacity | Density | Value* |
|---|---|---|---|---|---|---|---|---|---|---|
| Steel (HCS) | 90 | 25 | 35 | 30 | 50 | 90 | 20 | 5 | 5 | 250 Kronars |
| Bronze | 80 | 65 | 70 | 35 | 55 | 80 | 85 | 40 | 6.2 | 125 Kronars |
| Lead (material) | 20 | 30 | 80 | 40 | 70 | 20 | 75 | 30 | 8 | 50 Kronars |
"""
SPECIAL = """|  | Hardness | Durability | Workability | Electrical | Thermal | Physical | Ductility | Capacity | Density | Value* |
|---|---|---|---|---|---|---|---|---|---|---|
| Telothian | 104 | 80 | 25 | 90 | 70 | 45 | 90 | 5 | 6.4 | 87,500 Kronars |
| Haralun | 104 | 80 | 25 | 90 | 70 | 45 | 90 | 5 | 9.5 | 87,500 Kronars |
"""
WOODS = """|  | Durability | Workability | Hardness | Stiffness | Physical Resistance | Shortbow / Arbalest Affinity | Longbow / HX Affinity | Composite bow / LX Affinity | Capacity | Density | Value* |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Ash (wood) | 40 | 70 | 72 | 61 | 51 | 85 | 70 | 75 | 18 | 7.0 | 198 Kronars |
| Oak | 80 | 60 | 80 | 60 | 49 | 80 | 50 | 42 | 14 | 5.6 | 300 Kronars |
"""
# The chunker folds a small section into its neighbour, leaving only its heading line above the table.
STONE = "Engineering materials\nEngineering materials are currently stone, bone and wood.\n\nCommon stone\n" + \
    "|  | Hardness | Durability | Workability | Capacity | Density | Boulder Value* |\n|---|---|---|---|---|---|---|\n" + \
    "| Granite | 75 | 50 | 40 | 50 | 6.75 | 371 Kronars |\n"
CHUNKS = [("Forging materials > Common metals", METALS), ("Forging materials > Special metals", SPECIAL),
          ("Engineering materials > Common wood", WOODS), ("Forging materials > Metal nugget/ingot sizes", STONE),
          ("Alchemy materials > Rare metals", "|  | Workability | Potency |\n|---|---|---|\n| Coralite | 40 | 99 |\n"),
          ("Outfitting materials > Common leather (part 1)",
           "|  | Workability | Durability | Electrical | Thermal | Physical | Density |\n|---|---|---|---|---|---|---|\n"
           "| Onyx-hide leather | 40 | 60 | 65 | 55 | 92 | 3.9 |\n| Rat-pelt leather | 75 | 35 | 30 | 40 | 45 | 4.5 |\n"),
          ("Engineering materials > Common bone",
           "|  | Hardness | Durability | Workability | Electrical | Thermal | Physical | Capacity | Density | Value |\n"
           "|---|---|---|---|---|---|---|---|---|---|\n| Barghest bone | 80 | 85 | 15 | 50 | 50 | 80 | 45 | 5.40 |  |\n"
           "| Badger bone | 40 | 45 | 45 | 40 | 45 | 40 | 45 | 3.25 |  |\n")]
GUIDE = ("Metallurgy > Mixing Metals to Adjust Density/Weight (part 1)", "Weapon templates \"cap\" at 7.5 density.")


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = db.connect(s)
    c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, fetched_revid, indexed_revid, is_redirect,"
              " page_kind, updated_at) VALUES (1, 14, 'Category:Crafting materials', 'category:crafting materials',"
              " 5, 5, 5, 0, 'category', 'x'), (2, 0, 'Forging guide', 'forging guide', 6, 6, 6, 0, 'guide', 'x')")
    for i, (sec, text) in enumerate(CHUNKS):
        c.execute("INSERT INTO chunks(page_id, revid, ord, kind, title, section_path, text, token_est, content_hash)"
                  " VALUES (1, 5, ?, 'category', 'Category:Crafting materials', ?, ?, 1, ?)", (i, sec, text, str(i)))
    c.execute("INSERT INTO chunks(page_id, revid, ord, kind, title, section_path, text, token_est, content_hash)"
              " VALUES (2, 6, 0, 'guide', 'Forging guide', ?, ?, 1, 'g')", GUIDE)
    enrich_all(c)
    yield c
    c.close()


def test_the_extractor_reads_every_table_with_its_group_and_a_folded_section_heading():
    facts = {(e, g, a): n for e, g, a, n, _t, _s in extract(CHUNKS)}
    assert facts[("Steel (HCS)", "metal|Common|Forging", "hardness")] == 90
    assert facts[("Telothian", "metal|Special|Forging", "hardness")] == 104
    assert facts[("Oak", "wood|Common|Engineering", "affinity_short")] == 80
    assert facts[("Granite", "stone|Common|Engineering", "density")] == 6.75  # found by its inline heading
    assert facts[("Coralite", "metal|Rare|Alchemy", "potency")] == 99


def test_enrich_is_keyed_by_revision(conn):
    assert enrich_all(conn)["pages"] == 0  # already enriched at this revision
    conn.execute("UPDATE pages SET fetched_revid=7, indexed_revid=7 WHERE page_id=1")
    assert enrich_all(conn)["pages"] == 1


def test_questions_are_recognised():
    assert materials.wants("Whats the best material to make a greatsword with?").kind == "weapon"
    assert materials.wants("best metal for plate armor").kind == "armor"
    ask = materials.wants("What's the best wood for a shortbow?")
    assert ask.kind == "bow" and ask.bow == "short"
    assert materials.wants("What is a greatsword?") is None
    assert materials.wants("Who is Binu?") is None


def test_a_weapon_ranking_by_hardness_with_density_capped_and_cited(conn):
    out = materials.passage(conn, "best material for a greatsword", "https://x")
    answer = out[0]["answer"]
    assert answer.index("Steel (HCS)") < answer.index("Bronze")  # common metals, hardest first
    assert answer.index("Haralun") < answer.index("Telothian")  # equal hardness: more density helps, up to the cap
    assert "Coralite" not in answer  # an alchemy catalyst is not a forging metal
    assert "cap at 7.5" in answer and "[2]" in answer
    assert out[1]["title"] == "Forging guide" and out[1]["chunk_id"]


def test_armor_ranks_on_physical_resistance(conn):
    answer = materials.passage(conn, "best metal for plate armor", "https://x")[0]["answer"]
    assert "physical 90" in answer and "keeps helping past 7.5" in answer


def test_woods_rank_by_the_affinity_for_the_bow_type(conn):
    answer = materials.passage(conn, "best wood for a shortbow", "https://x")[0]["answer"]
    assert answer.index("Ash") < answer.index("Oak") and "shortbow" in answer


def test_a_name_that_is_not_a_material_is_said_plainly(conn):
    answer = materials.passage(conn, "Is damascus steel the best material for a greatsword?", "https://x")[0]["answer"]
    assert answer.startswith('I don\'t have "damascus steel"')
    assert "Steel (HCS)" in answer
    plain = materials.passage(conn, "is carbon steel the best material for a greatsword?", "https://x")[0]["answer"]
    assert not plain.startswith("I don't have")


def test_doctor_reports_on_a_small_index(conn):
    from epedia.index.doctor import run

    checks = {c.name: c for c in run(conn)}
    assert checks["SQLite integrity_check"].ok and checks["facts are current"].ok
    assert checks["rule still on Forging guide"].ok  # the fixture guide chunk states the 7.5 cap
    assert not checks["vector rows = chunks"].ok  # the fixture has chunks but no embeddings: doctor says so


def test_a_code_answer_cautions_when_its_source_page_was_heavily_rewritten(conn):
    from epedia.wiki import history

    assert history.caution(conn, ["Category:Crafting materials"]) == ""  # no history stored: nothing to say
    conn.executemany("INSERT INTO revisions(revid, title, ts, user, comment, size) VALUES (?,?,?,?,?,?)", [
        (1, "Forging guide", "2026-10-01T10:00:00Z", "A", "", 1000), (2, "Forging guide", "2026-10-02T10:00:00Z", "B", "", 9000),
        (3, "Forging guide", "2026-10-03T10:00:00Z", "C", "Undo revision 2 by B", 1000)])
    note = history.caution(conn, ["Category:Crafting materials", "Forging guide"])
    assert note.startswith("Mind you, my notes on the Forging guide page") and "Crafting materials" not in note


def test_the_game_name_is_not_part_of_the_question():
    from epedia.retrieval.titles import strip_game_name

    assert strip_game_name("How does crafting work in MUD DragonRealms?") == "How does crafting work?"
    assert strip_game_name("What is DragonRealms?") == "What is DragonRealms?"


def test_leather_and_bone_rank_on_the_stat_that_decides_the_item(conn):
    leather = materials.passage(conn, "best leather for gloves", "https://x")[0]["answer"]
    assert leather.index("Onyx-hide") < leather.index("Rat-pelt") and "physical resistance" in leather
    assert "how much each one counts" in leather  # no invented formula for what the wiki does not state
    bone = materials.passage(conn, "what is the best bone for a weapon", "https://x")[0]["answer"]
    assert bone.index("Barghest") < bone.index("Badger") and "hardness" in bone
    assert materials.wants("best bone for a shortbow").kind == "other"  # bows are answered as bone, not as bow wood


def test_describing_words_are_not_missing_materials(conn):
    for q in ("what's the strongest metal for a greatsword?", "best common metal for a sword", "best light metal for a dagger"):
        answer = materials.passage(conn, q, "https://x")[0]["answer"]
        assert not answer.startswith("I don't have"), q


def test_a_layout_change_does_not_wipe_the_facts_already_known(conn):
    before = conn.execute("SELECT count(*) FROM facts").fetchone()[0]
    conn.execute("DELETE FROM chunks WHERE page_id=1")
    conn.execute("UPDATE pages SET fetched_revid=9, indexed_revid=9 WHERE page_id=1")
    stats = enrich_all(conn)
    assert stats.get("suspect") == 1
    from epedia.index.doctor import run

    assert not {c.name: c for c in run(conn)}["extractors still read what they used to"].ok  # and doctor says so
    assert conn.execute("SELECT count(*) FROM facts").fetchone()[0] == before  # kept, and retried next run
    assert conn.execute("SELECT enriched_revid FROM pages WHERE page_id=1").fetchone()[0] != 9
