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
# Each material's own page opens with an "at a Glance" box, as a table on most pages and as plain lines on a few.
GLANCES = {
    "Telothian": "| Telothian at a Glance |  |  |\n|---|---|---|\n|  | Material Type | Metals |\n|  | Colors | Purple |\n"
                 "|  | Rarity | Auction-Tier |\n\nA deep purple metal. Rarity: whatever the auction says.",
    "Haralun": "Haralun at a Glance\nMaterial Type Metals\nColors: Blue, Black\nRarity: Rare\n\nA dark metal.",
    "Bronze": "| Bronze at a Glance |  |  |\n|---|---|---|\n|  | Material Type | Metals |\n|  | Colors | Indigo |\n"
              "|  | Rarity | Common |\n",
    "Steel (HCS)": "| Steel (HCS) at a Glance |  |  |\n|---|---|---|\n|  | Material Type | Metals, Steels |\n"
                   "|  | Colors | Unknown |\n|  | Rarity | Common |\n",
    "Oak": "| Oak at a Glance |  |  |\n|---|---|---|\n|  | Material Type | Woods |\n|  | Colors | Blue |\n"
           "|  | Rarity | Common |\n",
    # Not in the crafting tables at all: an alteration metal, a gem and a fabric are only described by their own box.
    "Loveglass": "| Loveglass at a Glance |  |  |\n|---|---|---|\n|  | Material Type | Metals |\n|  | Colors | Red, Pink |\n"
                 "|  | Rarity | Rare |\n|  | Required for Alterations | Yes |\n",
    "Blood ruby": "Blood ruby at a Glance\nMaterial Type Gems, Rubies\nColors: Red\nRarity: Rare\n\nA deep red gem.",
    "Firesilk": "| Firesilk at a Glance |  |  |\n|---|---|---|\n|  | Material Type | Fabric, Silks |\n|  | Colors | Red, Orange |\n"
                "|  | Rarity | Rare |\n",
}
BAG = ("I'm working on alteration for a bag for my Rakash empath and her theme is red and hearts. Are there any rare metals "
       "or gems you recommend I use for it?")


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
    for pid, (title, text) in enumerate(GLANCES.items(), start=10):
        c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, fetched_revid, indexed_revid, is_redirect,"
                  " page_kind, updated_at) VALUES (?, 0, ?, ?, 3, 3, 3, 0, 'material', 'x')", (pid, title, title.lower()))
        c.execute("INSERT INTO chunks(page_id, revid, ord, kind, title, section_path, text, token_est, content_hash)"
                  " VALUES (?, 3, 0, 'material', ?, '', ?, 1, ?)", (pid, title, text, f"m{pid}"))
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


def test_the_glance_box_is_read_in_both_layouts_and_nothing_below_it():
    from epedia.index.extract.materials import glance

    tel = {a: v for _e, _g, a, _n, v, _s in glance([("", GLANCES["Telothian"])], "Telothian")}
    assert tel == {"type": "Metals", "colors": "Purple", "rarity": "Auction-Tier"}  # the prose "Rarity:" is not read
    har = glance([("", GLANCES["Haralun"])], "Haralun")
    assert {a: v for _e, _g, a, _n, v, _s in har} == {"type": "Metals", "colors": "Blue, Black", "rarity": "Rare"}
    assert har[0][:2] == ("Haralun", "metal")
    assert glance([("", "No box here.\nColors: Red")], "X") == []


def test_enrich_reads_every_material_page_listed_in_the_tables(conn):
    rows = conn.execute("SELECT entity FROM facts WHERE domain='material_page' AND attribute='colors'").fetchall()
    assert {r[0] for r in rows} == set(GLANCES)
    assert enrich_all(conn)["pages"] == 0  # keyed by revision like the tables
    conn.execute("UPDATE pages SET fetched_revid=4, indexed_revid=4 WHERE title='Haralun'")
    assert enrich_all(conn)["pages"] == 1


def test_colour_questions_are_recognised():
    ask = materials.wants_colour("Give me all metals that are blue or partially blue")
    assert ask.colours == ["blue"] and ask.matters == {"metal"}
    assert materials.wants_colour("Take a look at the metal item types, give me all that are blue in color").matters == {"metal"}
    assert materials.wants_colour("which rare leathers are black").rarity == "Rare"
    assert materials.wants_colour("what materials are green").matters == set()  # every kind
    assert materials.wants_colour("what metals are silver colored").colours == ["silver"]
    for q in ("which metals are silver", "which shop sells blue metal shields?", "What is blue gold?",
              "what is the best metal for a sword", "what stone is the black obelisk made of?", "What color is glaes?"):
        assert materials.wants_colour(q) is None, q


def test_metals_by_colour_are_listed_by_code_each_cited_to_its_own_page(conn):
    out = materials.passage(conn, "Give me all metals that are blue or partially blue", "https://x")
    answer = out[0]["answer"]
    assert answer.index("Only blue:") < answer.index("**Bronze**") < answer.index("Blue with other colours:")  # indigo
    assert "**Haralun** (rare): blue, black [2]" in answer
    assert "Oak" not in answer and "Telothian" not in answer  # a wood, and a purple metal
    assert [p["title"] for p in out] == ["Bronze", "Haralun"] and all(p["chunk_id"] for p in out)
    assert 'listed only as "various" or "unknown"' in answer and "Steel (HCS)" in answer.split("(OOC)")[1]


def test_no_material_of_that_colour_is_said_plainly(conn):
    answer = materials.passage(conn, "are there any orange metals?", "https://x")[0]["answer"]
    assert answer.startswith("None of the metals in my notes is listed as orange.")
    assert "blue" in answer.split("come in:")[1]


def test_a_material_the_tables_leave_out_is_read_from_its_own_page(conn):
    rows = {(e, g): v for e, g, v in conn.execute(
        "SELECT entity, grp, value_text FROM facts WHERE domain='material_page' AND attribute='colors'")}
    assert rows[("Loveglass", "metal")] == "Red, Pink"
    assert rows[("Blood ruby", "gem")] == "Red" and rows[("Firesilk", "cloth")] == "Red, Orange"


def test_metals_and_gems_are_listed_kind_by_kind_in_the_order_asked(conn):
    assert materials.wants_colour(BAG).matters == {"metal", "gem"}
    out = materials.passage(conn, BAG, "https://x")
    answer = out[0]["answer"]
    assert "these rare metals and gems are listed as red" in answer
    assert answer.index("Metals:") < answer.index("**Loveglass**") < answer.index("Gems:") < answer.index("**Blood ruby**")
    assert "Firesilk" not in answer and "Haralun" not in answer  # a fabric, and a blue metal
    assert {p["title"] for p in out} == {"Loveglass", "Blood ruby"}


def test_a_long_list_says_how_many_more_there_are(conn, monkeypatch):
    monkeypatch.setattr(materials, "SHOWN_PER_KIND", 1)
    answer = materials.passage(conn, "Which materials are red?", "https://x")[0]["answer"]
    assert answer.count("- **") == 3  # one of each kind
    monkeypatch.setattr(materials, "SHOWN_PER_KIND", 0)
    answer = materials.passage(conn, "Which gems are red?", "https://x")[0]["answer"]
    assert "...and 1 more gems listed as red." in answer


def test_a_follow_up_keeps_the_colour_asked_before_it(conn):
    """The red bag: a cloth question after red metals and gems is about red cloth, not the cloth with the best protection,
    and "just the looks" after that keeps both."""
    cloth = materials.passage(conn, "Do you have any recommendations for cloth?", "https://x", [BAG])[0]["answer"]
    assert "these cloths are listed as red" in cloth and "**Firesilk**" in cloth and "physical" not in cloth
    looks = materials.passage(conn, "I don't need resistance this is just aesthetics", "https://x",
                              ["Do you have any recommendations for cloth?", BAG])[0]["answer"]
    assert "**Firesilk**" in looks
    assert materials.follow_colour("Who is Hodierna?", [BAG]) is None  # a new subject
    assert materials.follow_colour("Is there a red sword?", [BAG]) is None  # an item, not a list of materials
    assert materials.follow_colour("Do you have any recommendations for cloth?", ["What is the hardest metal?"]) is None


def test_protection_is_still_ranked_and_looks_are_never_ranked_by_it(conn):
    ranked = materials.passage(conn, "Which leather gives the best protection?", "https://x", [BAG])[0]["answer"]
    assert "physical resistance" in ranked  # asked about protection: the red before it does not matter
    assert materials.passage(conn, "best leather for an alteration", "https://x") is None  # cosmetic: not a protection ranking


def test_the_assistant_answers_a_colour_follow_up_in_code(conn, monkeypatch):
    from epedia.core import assistant as am

    a = am.Assistant.__new__(am.Assistant)
    a.conn, a.s = conn, Settings()
    asked = "Do you have any recommendations for cloth?"
    monkeypatch.setattr(am.memory, "recent_questions", lambda c, s: [BAG, asked])  # oldest first, this question last
    out = a._material_colours(asked, am.Plan(asked), "s1")
    assert out and "**Firesilk**" in out[0]["answer"]
    monkeypatch.setattr(am.memory, "recent_questions", lambda c, s: [asked])  # nothing before it: not a follow-up
    assert a._material_colours(asked, am.Plan(asked), "s1") is None
    monkeypatch.setattr(am.memory, "recent_questions", lambda c, s: [BAG, "What is the workability of firesilk?"])
    assert a._material_colours("What is the workability of firesilk?", am.Plan("x"), "s1") is None  # the table's stats


def test_doctor_checks_material_colours_are_read(conn):
    from epedia.index.doctor import run

    check = {c.name: c for c in run(conn)}["material colours are readable"]
    assert check.ok and "1 of them gems" in check.detail


def test_a_colour_question_skips_the_planner(conn):
    from epedia.core.assistant import Assistant

    a = Assistant.__new__(Assistant)
    a.conn, a.s = conn, Settings()
    plan = a._early_plan("Which metals are blue?", "", [])
    assert plan and plan.kind == "crafting" and plan.prebuilt[0]["answer"].startswith("Going by")


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
