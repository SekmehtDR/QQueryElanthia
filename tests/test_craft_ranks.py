"""'How many ranks to mastercraft X in Y?', answered in code. From a real thumbs-down: the model took "Tier 6" from a Talk page, read
"11- v.difficult" as 11 ranks, and could not be steered back. The figures are the ones the owner and Binu checked."""

import asyncio
import re

import pytest

from epedia import db
from epedia.config import Settings
from epedia.core import craft_ranks
from epedia.core.assistant import Assistant, Done
from epedia.index.doctor import run as run_doctor
from epedia.index.enrich import enrich_all
from epedia.index.extract.crafting import tier_of

NOTES = ("Currently, the following are available:\n\n- Engineering\n  - Carving\n  - Shaping\n- Forging\n  - Armorsmithing\n"
         "  - Weaponsmithing\n- Enchanting\n  - Artificing\n- Outfitting\n  - Tailoring\n")
CHART = ("|  |  |  |  |  |  |  |\n|---|---|---|---|---|---|---|\n"
         "| Workability Difficulty Multipliers |  |  |  | 300% | 140% | 100% |\n"
         "|  | Instruction Difficulty | Base Ranks* | With Techs* | 0 | 40 | 50 |\n"
         "| Tier 6 | Somewhat Challenging | 425 | ~304 | 1275 / ~912 | 900 / ??? | 425 / ~304 |\n"
         "| Tier 8 | Complicated | 700 | ~500 | 2100 / ~1500 | 980 / ~700 | 700 / ~500 |\n"
         "| Tier 9 | Intricate | ? | ~601 | ? / ~1803 | ? / ~842 | ___ / ~601 |\n"
         "| Tier 10 | Difficult | ? | ~839 | ? / ~2517 | ? / ~1175 | ___ / ~839 |\n"
         "| Tier 11 | Very Difficult | ? | ? | ? / ? | ? / ? |  |\n"
         "| Tier 12 | Extremely Difficult | N/A | ? | ? / ? | ? / ? |  |\n\n"
         "- NOTE: It is currently impossible to mastercraft a T12 recipe with materials below 40 workability. Even with 2100 effective "
         "forging ranks, the corresponding techniques, 99 quality tools and materials, you will fail.")
FORMULA = ("When workability is < 50\n\nMultiplier = 100 + ((100 - workability) - 50) * 4)\n\nWhen workability is >= 50\n\n"
           "Multiplier = (((100 - workability) * 120 / 100) + 40)")
GUIDE = ("| Tier | Difficulty | Base Ranks | Ranks With Techs |\n|---|---|---|---|\n| 9 | intricate | 850 | ~607 |\n"
         "| 10 | difficult | 1175 | ~839 |\n| 11 | very difficult | 1400 | 850-950 |\n| 12 | extremely difficult | 1500 | 900-1200 |\n")
METALS = ("|  | Hardness | Durability | Workability | Density |\n|---|---|---|---|---|\n| Glaes | 95 | 50 | 10 | 4 |\n"
          "| Tyrium | 99 | 80 | 40 | 6 |\n| Steel (HCS) | 90 | 25 | 35 | 5 |\n| Steel (LCS) | 70 | 30 | 40 | 5 |\n| Steel (MCS) | 80 | 30 | 50 | 5 |\n")
WOOD = ("|  | Durability | Workability | Hardness |\n|---|---|---|---|\n| Bloodwood | 70 | 5 | 40 |\n")
STONE = ("Common stone\n|  | Hardness | Durability | Workability |\n|---|---|---|---|\n| Granite | 75 | 50 | 40 |\n")
HEAD = "| Item | Book | Difficulty | Technique | Vol |\n|---|---|---|---|---|\n"
SWAPPABLE = (HEAD + "| <metal> half-handled riste | Master | 11- v.difficult | Advanced Swappable Weapon Design | 7 |\n"
             "| <metal> riste | Master | 12- e.difficult | Advanced Swappable Weapon Design | 9 |\n"
             "| <metal> bastard sword | Master | 09- intricate | Advanced Swappable Weapon Design | 9 |\n")
# a long table split by the chunker: the later piece has neither header nor rule
SPLIT = "| <metal> war axe | Master | 08- complicated | Heavy Edged Design | 8 |\n| <metal> whatsit | Master | Varies | Heavy Edged Design | 8 |\n"
PLATE = ("| Item | Difficulty | Technique | Source |\n|---|---|---|---|\n| Imperial plate | 11 - V.Difficult | Complete Plate Armor Design | "
         "Su Helmas: Revealed! |\n")
FULL = (HEAD + "| light full plate | Journeyman | 08 - complicated | Complete Plate Armor Design | 100 |\n"
        "| full plate | Master | 09 - intricate | Complete Plate Armor Design | 110 |\n"
        "| heavy full plate | Master | 10 - difficult | Complete Plate Armor Design | 120 |\n")
BOWS = "| Chapter | Item | Book | Difficulty | Technique |\n|---|---|---|---|---|\n| 4 | battle bow | Master | 10 - Difficult | Advanced Heavy Bowcraft |\n"
PAGES = {
    "Crafting": [("General Notes", NOTES), ("Difficulty", CHART), ("Difficulty > Workability Difficulty Multiplier", FORMULA),
                 ("Group Assistance", "CRAFT ASSIST will enable a small boost. Up to 3 characters may assist the primary crafter.")],
    "Tailoring player guide": [("Skills and Techniques", GUIDE)],
    "Category:Crafting materials": [("Forging materials > Rare metals", METALS), ("Engineering materials > Rare wood", WOOD),
                                    ("Forging materials > Metal nugget/ingot sizes", STONE)],
    "Weaponsmithing products": [("Swappable", SWAPPABLE), ("Swappable (part 2)", SPLIT)],
    "Armorsmithing products": [("Plate Armor Design", FULL), ("Special Instructions", PLATE)],
    "Shaping products": [("Wood > Composite Bow Crafting", BOWS)],
    "Carving products": [("Stone > Weaponry", HEAD + "| <stone> bludgeon | Master | 03- easy | Stone Weapons | 3 |\n")],
    "Artificing products": [("Overview", HEAD + "| simple gwethsmasher | Journeyman | 07 - challenging | Martial Artificing | 1 |\n")],
    "Player suggestions for Crafting": [("All", "Fully capped out with techniques (1750 ranks) in a craft.")],
    "DragonRealms Updates (2019)": [("March", "Skill/Stat (including crafting skills) bonus buff limits extended from 20% to 30%")],
}
BASE = "https://x"


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    return s


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    for pid, (title, chunks) in enumerate(PAGES.items(), 1):
        c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, fetched_revid, indexed_revid, is_redirect, page_kind, updated_at)"
                  " VALUES (?, 0, ?, ?, 5, 5, 5, 0, 'crafting', 'x')", (pid, title, title.lower()))
        for i, (section, text) in enumerate(chunks):
            c.execute("INSERT INTO chunks(page_id, revid, ord, kind, title, section_path, text, token_est, content_hash)"
                      " VALUES (?, 5, ?, 'section', ?, ?, ?, 1, ?)", (pid, i, title, section, text, f"{pid}.{i}"))
    enrich_all(c)
    yield c
    c.close()


def ask(conn, q, earlier=None) -> str:
    out = craft_ranks.passage(conn, q, BASE, earlier)
    return out[0]["answer"] if out else ""


@pytest.mark.parametrize("workability, percent", [(0, 300), (1, 296), (5, 280), (10, 260), (35, 160), (40, 140), (50, 100), (60, 88), (100, 40)])
def test_the_multiplier_is_the_pages_formula_and_matches_its_chart(workability, percent):
    assert craft_ranks.multiplier(workability) == percent


@pytest.mark.parametrize("cell, tier", [("11- v.difficult", 11), ("09 - Intricate", 9), ("8 - complicated", 8), ("12 - Extremely Difficult", 12),
                                        ("Varies", None), ("?", None), ("", None), ("~50 engineering ranks", None)])
def test_a_tier_is_read_only_from_a_real_difficulty(cell, tier):
    assert tier_of(cell) == tier


def test_the_half_handled_riste_in_glaes_shows_every_step(conn):
    a = ask(conn, "how many ranks to make a half-handled riste in glaes?")
    assert "workability 10" in a and "**260%**" in a and "Tier 11, Very Difficult, in Weaponsmithing" in a
    assert "850-950 × 2.6 = **2,210-2,470 ranks**" in a and "1,400 × 2.6 = **3,640 ranks**" in a
    assert "Only at the very top" in a and "CRAFT ASSIST" in a and "reverse engineered" in a
    assert "Tier 6" not in a and "Tier 10" not in a  # only the item's own tier


def test_names_are_literal_a_riste_is_tier_12_and_the_page_rule_leads(conn):
    a = ask(conn, "how many ranks to make a riste in glaes?")
    assert "Riste:** Tier 12" in a and "half-handled riste is Tier 11" in a
    assert a.index("It can't be mastercrafted") < a.index("2,340-3,120")


def test_the_t12_rule_needs_workability_below_40(conn):
    assert "can't be mastercrafted" not in ask(conn, "how many ranks to make a riste in tyrium?")
    assert "can't be mastercrafted" in ask(conn, "how many ranks for a T12 in glaes?")


def test_a_figure_in_the_chart_beats_the_guide_and_its_own_cell_beats_the_sum(conn):
    a = ask(conn, "how many ranks to mastercraft a tyrium bastard sword?")
    assert "~601 × 1.4 = **about 842 ranks**" in a  # the chart says ~842; 601 x 1.4 is 841.4
    assert "850 × 1.4 = **1,190 ranks**" in a  # the chart's base is "?", so the guide fills it


def test_a_typo_in_the_charts_cell_is_not_believed(conn):
    assert "425 × 1.4 = **595 ranks**" in ask(conn, "how many ranks for a T6 in tyrium?")  # the cell says 900


def test_with_techs_never_costs_more_than_without(conn):
    for tier in (9, 10, 11, 12):
        figs = craft_ranks._figures(conn, tier)
        assert figs["techs"].hi < figs["base"].lo, tier


@pytest.mark.parametrize("q, want", [
    ("how many ranks to make a bloodwood battlebow?", "Battle bow:** Tier 10, Difficult, in Shaping"),
    ("how many ranks to forge glaes full-plate armor?", "Full plate:** Tier 9"),
    ("how many ranks for tyrium imperial plate armor?", "Imperial plate:** Tier 11"),
    ("how many ranks for a high-carbon steel imperial plate?", "workability 35"),
    ("how many ranks for an HCS imperial plate?", "workability 35"),
])
def test_names_are_matched_the_way_people_write_them(conn, q, want):
    assert want in ask(conn, q)


def test_an_item_with_special_instructions_says_where_they_come_from(conn):
    assert "coming from Su Helmas: Revealed!" in ask(conn, "how many ranks for glaes imperial plate?")


def test_a_longer_templates_tier_is_only_a_pointer(conn):
    a = ask(conn, "how many ranks to forge glaes full plate?")
    assert "Full plate:** Tier 9" in a and "heavy full plate is Tier 10" in a and "Heavy full plate:**" not in a


def test_plain_steel_asks_which_one(conn):
    a = ask(conn, "how many ranks to make a steel bastard sword?")
    assert a.startswith("Which steel") and "high carbon steel (35)" in a and "ranks**" not in a


@pytest.mark.parametrize("q, matter, made_of", [("how many ranks to make a bloodwood bastard sword?", "wood", "metal"),
                                                ("how many ranks to carve a glaes bludgeon?", "metal", "stone"),
                                                ("how many ranks to shape a glaes battle bow?", "metal", "wood")])
def test_a_material_that_does_not_fit_the_craft_gets_no_number(conn, q, matter, made_of):
    a = ask(conn, q)
    assert a.startswith("I can't give you a number") and f"is {matter}" in a and f"made from {made_of}" in a and "ranks**" not in a


def test_artificing_has_no_material_to_fit(conn):
    assert "ranks**" not in ask(conn, "how many ranks to make a glaes simple gwethsmasher?")


def test_no_material_asks_for_it_and_no_item_says_so_without_inventing_a_tier(conn):
    a = ask(conn, "how many ranks to make a half-handled riste?")
    assert "Which material" in a and "850-950" in a and "× 2.6" not in a
    b = ask(conn, "how many ranks to craft a frobnicator in glaes?")
    assert "can't find that item" in b and "**260%**" in b and "Tier" not in b
    assert "can't find that item" in ask(conn, "how many ranks for a glaes tabard?")  # no craft word, and no room for the model to guess


def test_a_row_with_no_tier_says_so(conn):
    a = ask(conn, "how many ranks to make a glaes whatsit?")
    assert 'gives "Varies"' in a and "ranks**" not in a


def test_a_tier_with_no_figure_says_so(conn):
    conn.execute("DELETE FROM facts WHERE domain='craft_tier' AND entity='Tier 11'")
    a = ask(conn, "how many ranks to make a half-handled riste in glaes?")
    assert "no rank figure for Tier 11" in a and "ranks**" not in a


def test_a_split_tables_later_rows_are_read(conn):
    assert "Tier 8, Complicated" in ask(conn, "how many ranks to make a glaes war axe?")


@pytest.mark.parametrize("q", ["how many ranks do I need to use a glaes sword?", "what's the workability of glaes?", "how many ranks to hunt a rat?",
                               "Tell me about glaes", "What is the best metal for a sword?", "How does crafting work?"])
def test_other_questions_are_left_alone(conn, q):
    assert craft_ranks.passage(conn, q, BASE) is None


def test_every_citation_has_a_source_and_none_is_skipped_or_repeated(conn):
    for q in ("how many ranks to make a riste in glaes?", "how many ranks to mastercraft a tyrium bastard sword?", "how many ranks for a T12 in glaes?"):
        out = craft_ranks.passage(conn, q, BASE)
        numbers = {int(n) for n in re.findall(r"\[(\d+)\]", out[0]["answer"])}
        assert numbers == set(range(1, len(out) + 1)), q
        chunks = [p["chunk_id"] for p in out]
        assert len(chunks) == len(set(chunks)) and None not in chunks, q


def test_a_follow_up_finds_what_it_leaves_out_and_an_item_named_now_wins(conn):
    earlier = ["Start over for a half handled riste in glaes.", "how many ranks for a T11 in glaes?"]
    assert "2,210-2,470" in ask(conn, "show me the math, how many ranks?", earlier)
    assert "Tier 8" in ask(conn, "how many ranks to make a war axe then?", earlier)
    assert ask(conn, "How is the weather in Riverhaven, and how many ranks should a thief have?", earlier) == ""


class Model:
    """Plans what it is told to; any call to write an answer is a failure."""

    def __init__(self, standalone):
        self.standalone, self.written = standalone, 0

    async def chat_json(self, messages, schema, **k):
        return {"standalone_question": self.standalone, "mode": "search", "kind": "crafting", "follow_up": True}

    async def embed(self, texts, **k):
        return [[0.0] * 768 for _ in texts]

    async def chat_stream(self, messages, temperature=None, **k):
        self.written += 1
        yield "Tier 6 takes 425 ranks, times 2.6 is 1,105."


def turn(a, sid, q) -> Done:
    async def go():
        return [e async for e in a.answer_stream(sid, q)]
    return next(e for e in asyncio.run(go()) if isinstance(e, Done))


def assistant(settings, conn, standalone):
    return Assistant(settings, conn, Model(standalone))


def test_the_thumbs_down_conversation_gets_the_math_and_the_model_writes_none_of_it(settings, conn):
    a = assistant(settings, conn, "How many ranks to make a riste in glaes?")
    done = turn(a, "s1", "how many ranks would it take to make a half-handled riste in glaes?")
    assert "2,210-2,470" in done.answer and done.by_code and a.ollama.written == 0
    a.ollama.standalone = "Show me the math, how many ranks are required to craft a half-handled riste in glaes with a T11 template?"
    done = turn(a, "s1", "show me the math, how many ranks?")
    assert "2,210-2,470" in done.answer and "Tier 6" not in done.answer and a.ollama.written == 0
    assert done.sources and done.sources[0].title == "Category:Crafting materials"


def test_the_persons_own_words_beat_a_planner_that_changes_the_item(settings, conn):
    a = assistant(settings, conn, "How many ranks to make a half-handled riste in glaes?")
    assert "Tier 11" in turn(a, "s2", "first, how are you? I wondered how many ranks a half-handled riste in glaes takes").answer
    assert "Riste:** Tier 12" in turn(a, "s2", "and how many ranks to make a riste in glaes?").answer


def test_doctor_watches_the_rules_and_the_tables(conn):
    checks = {c.name: c.ok for c in run_doctor(conn)}
    assert checks["every crafting tier has a rank figure"] is False  # this little chart stops short of twelve tiers
    assert "crafted products have their tiers" in checks and "rule still on DragonRealms Updates (2019)" in checks
