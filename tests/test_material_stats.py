"""Any column of the crafting materials tables, answered in code. From a player's real question: "What material has the least
workability?" got Glaes (10), and reworded got Lead (80), when the table says Silversteel is the lowest metal at 2."""

import asyncio

import pytest

from epedia import db
from epedia.config import Settings
from epedia.core import material_stats, materials
from epedia.core.assistant import Assistant, Done
from epedia.index.doctor import run as run_doctor
from epedia.index.enrich import enrich_all

METAL = "|  | Hardness | Durability | Workability | Electrical | Thermal | Physical | Ductility | Capacity | Density | Value* |\n|---|---|---|---|---|---|---|---|---|---|---|\n"
BONE = "|  | Hardness | Durability | Workability | Electrical | Thermal | Physical | Capacity | Density | Value |\n|---|---|---|---|---|---|---|---|---|---|\n"
LEATHER = "|  | Workability | Durability | Electrical | Thermal | Physical | Density |\n|---|---|---|---|---|---|---|\n"
CLOTH = "|  | Workability | Durability | Electrical | Thermal | Physical | Density* |\n|---|---|---|---|---|---|---|\n"
WOOD = ("|  | Durability | Workability | Hardness | Stiffness | Physical Resistance | Shortbow / Arbalest Affinity | Longbow / HX Affinity |"
        " Composite bow / LX Affinity | Capacity | Density | Value* |\n|---|---|---|---|---|---|---|---|---|---|---|---|\n")
TABLES = [
    ("Forging materials > Metal properties", "- Workability modifies the difficulty of crafting the item. Lower workability metals require more "
     "skill to masterfully craft than higher workability metals."),
    ("Forging materials > Common metals", METAL + "| Lead (material) | 20 | 30 | 80 | 40 | 70 | 20 | 75 | 30 | 8 | 50 Kronars |\n"
     "| Steel (HCS) | 90 | 25 | 35 | 30 | 50 | 90 | 20 | 5 | 5 | 250 Kronars |\n| Steel (LCS) | 70 | 25 | 40 | 30 | 50 | 70 | 20 | 5 | 5 | 150 Kronars |\n"),
    ("Forging materials > Rare metals", METAL + "| Silversteel | 95 | 90 | 2 | 20 | 80 | 95 | 90 | 10 | 2.2 | 27,500 Kronars |\n"
     "| Glaes | 95 | 50 | 10 | 60 | 40 | 75 | 95 | 10 | 4 | 16,250 Kronars |\n| Damite | 90 | 55 | 10 | 60 | 40 | 75 | 95 | 10 | 7 | 9,000 Kronars |\n"
     "| Darkstone (metal) | 85 | 70 | 75 | 50 | 50 | 60 | 40 | 10 | 6 | 9,500 Kronars |\n"),
    ("Alchemy materials > Rare metals", "|  | Workability | Potency |\n|---|---|---|\n| Silversteel | 2 | 40 |\n"),
    ("Engineering materials > Rare bone", BONE + "| Dragon bone | 118 | 95 | 1 | 85 | 85 | 118 | 30 | 3.9 | 37,500 Kronars |\n"),
    ("Outfitting materials > Special leather", LEATHER + "| Dark dragon-scale hide | 1 | 95 | 85 | 85 | 115 | 5.8 |\n"),
    ("Outfitting materials > Common cloth", CLOTH + "| Silk | 40 | 60 | 10 | 10 | 10 | 2; 2.25; 2.5; 2.75; 3 |\n"),
    ("Engineering materials > Rare wood", WOOD + "| Bloodwood | 60 | 5 | 70 | 50 | 40 | 90 | 60 | 60 | 20 | 5 | 900 Kronars |\n"
     "| Smokewood | 60 | 5 | 60 | 50 | 40 | 96 | 60 | 60 | 20 | 5 | 468,750 Kronars |\n"),
]
BASE = "https://x"


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    return s


@pytest.fixture
def conn(settings):
    c = db.connect(settings)
    c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, fetched_revid, indexed_revid, is_redirect, page_kind, updated_at)"
              " VALUES (1, 14, 'Category:Crafting materials', 'category:crafting materials', 5, 5, 5, 0, 'category', 'x')")
    for i, (section, text) in enumerate(TABLES):
        c.execute("INSERT INTO chunks(page_id, revid, ord, kind, title, section_path, text, token_est, content_hash)"
                  " VALUES (1, 5, ?, 'category', 'Category:Crafting materials', ?, ?, 1, ?)", (i, section, text, str(i)))
    enrich_all(c)
    yield c
    c.close()


def ask(conn, q) -> str:
    out = material_stats.passage(conn, q, BASE)
    return out[0]["answer"] if out else ""


@pytest.mark.parametrize("q", ["What material has the least workability?", "What crafting material has the least workability?",
                               "What material has the lowest workability?", "what materials have the worst workability"])
def test_the_players_question_names_the_lowest_overall_and_the_lowest_metal(conn, q):
    answer = ask(conn, q)
    assert "**1**: Dragon bone" in answer and "Dark dragon-scale hide" in answer
    assert "**Metals:** 2, Silversteel" in answer
    assert "Glaes" not in answer and "Lead" not in answer


@pytest.mark.parametrize("q", ["Which metal has the lowest workability?", "Which metal is the hardest to work with?",
                               "What metal is hardest to craft with?", "What's the most difficult metal to forge?"])
def test_one_kind_keeps_to_that_kind_and_low_means_hard(conn, q):
    answer = ask(conn, q)
    assert answer.startswith("Among metals, the lowest workability is **2**: Silversteel")
    assert "Dragon bone" not in answer
    assert "Higher workability is easier" in answer


def test_easiest_means_the_highest_number(conn):
    for q in ("Which metal is easiest to work with?", "What is the easiest metal to craft with?", "Which metal has the most workability?"):
        assert ask(conn, q).startswith("Among metals, the highest workability is **80**: Lead"), q


def test_hardest_alone_is_the_hardness_column(conn):
    assert ask(conn, "What is the hardest metal?").startswith("Among metals, the highest hardness is **95**: Silversteel [1] and Glaes")
    assert ask(conn, "What is the softest metal?").startswith("Among metals, the lowest hardness is **20**: Lead")


@pytest.mark.parametrize("q,start", [
    ("What is the workability of damite?", "**Damite** has a workability of **10**"),
    ("What is the workabilty of lead?", "**Lead** has a workability of **80**"),  # the table calls it Lead (material)
    ("how hard is it to work with glaes?", "**Glaes** has a workability of **10**"),
    ("workability of silver steel", "**Silversteel** has a workability of **2**"),
    ("What is the durability of Silversteel?", "**Silversteel** has a durability of **90**"),
    ("How dense is lead?", "**Lead** has a density of **8**"),
    ("What is the ductility of Darkstone?", "**Darkstone** has a ductility of **40**"),
    ("What is the thermal resistance of Silversteel?", "**Silversteel** has a thermal resistance of **80**"),
    ("What is the value of Silversteel?", "**Silversteel** has a value of **27,500 Kronars**"),
])
def test_one_material_and_one_column(conn, q, start):
    assert ask(conn, q).startswith(start)


@pytest.mark.parametrize("q", ["What is the most durable leather?", "What is the highest durability leather?"])
def test_every_kind_can_be_asked_about(conn, q):
    assert ask(conn, q).startswith("Among leathers, the highest durability is **95**: Dark dragon-scale hide")


def test_a_column_a_bow_word_names(conn):
    assert ask(conn, "Which wood has the best shortbow affinity?").startswith("Among woods, the highest shortbow / arbalest affinity is **96**: Smokewood")


def test_comparing_two_states_the_winner_or_the_tie(conn):
    answer = ask(conn, "Is glaes or lead easier to work?")
    assert "**Lead:** 80" in answer and "**Glaes:** 10" in answer and "So Lead is the easier to work, and Glaes the harder." in answer
    assert "They're level." in ask(conn, "Is damite or glaes easier to work?")
    assert "So Glaes is the higher." in ask(conn, "Is glaes harder than damite?")


def test_plain_steel_compares_the_steels(conn):
    answer = ask(conn, "What is the workability of steel?")
    assert "**Steel (LCS):** 40" in answer and "**Steel (HCS):** 35" in answer


def test_a_threshold_lists_everything_past_it(conn):
    answer = ask(conn, "Which metals are above 30 workability?")
    assert "**Lead:** 80" in answer and "**Steel (LCS):** 40" in answer and "**Steel (HCS):** 35" in answer and "Glaes" not in answer
    assert "**Lead:** 80" not in ask(conn, "Which metals have a workability below 80?")
    assert "**Lead:** 80" in ask(conn, "Which metals have at most 80 workability?")
    assert "None of the metals" in ask(conn, "Which metals are above 90 workability?")


def test_every_stat_of_one_material(conn):
    answer = ask(conn, "What are the stats of silversteel?")
    assert answer.startswith("**Silversteel** (rare metal): hardness 95") and "workability 2," in answer and "potency 40" in answer


def test_rarity_narrows_it_and_rare_includes_what_the_page_calls_special(conn):
    assert ask(conn, "Which common metal has the lowest workability?").startswith("Among common metals, the lowest workability is **35**: Steel (HCS)")
    assert "**Leathers:** 1, Dark dragon-scale hide" in ask(conn, "What rare material has the lowest workability?")


def test_a_column_the_table_does_not_have_says_so(conn):
    assert "don't give a hardness for leathers" in ask(conn, "What is the hardest leather?")
    assert "don't give a hardness for Dark dragon-scale hide" in ask(conn, "What is the hardness of Dark dragon-scale hide?")
    assert "I don't have \"mithril\"" in ask(conn, "What is the workability of mithril?")


def test_prices_are_never_ranked_across_kinds(conn):
    answer = ask(conn, "What is the most expensive material?")
    assert "**Metals:** 27,500 Kronars, Silversteel" in answer and "**Woods:** 468,750 Kronars, Smokewood" in answer
    assert "in the crafting materials tables is" not in answer and "compare within a kind" in answer


def test_cloth_density_says_which_figure_it_ranked_on(conn):
    answer = ask(conn, "What is the lightest cloth?")
    assert "2; 2.25; 2.5; 2.75; 3" in answer and "depends on thickness" in answer


def test_a_material_in_two_tables_is_one_row(conn):
    answer = ask(conn, "Which metal has the lowest workability?")
    assert answer.count("Silversteel") == 1


@pytest.mark.parametrize("q", [
    "What is the best metal for a sword?", "Whats the best material to make a greatsword with?", "What is the best wood for a longbow?",
    "What is the best metal?", "What does workability do?", "How does workability affect crafting?", "Why does silversteel have low workability?",
    "What is the durability of a silversteel rasp?", "What is the hardest creature?", "What has the highest durability?",
    "Where can I buy the hardest metal?", "What is the value of a svidaw sis?", "What metals are blue?", "How hard is it to learn forging?",
    "What is the hardest metal to mine?", "What is the easiest wood to forage?", "What is durability?", "Who is Hodierna?", "how heavy is a broadsword?",
])
def test_everything_else_is_left_alone(conn, q):
    assert ask(conn, q) == ""


def test_a_bare_best_material_question_still_goes_to_the_ranking(conn):
    assert materials.passage(conn, "What is the best metal for a sword?", BASE)


def test_every_figure_cites_the_table_and_its_section(conn):
    out = material_stats.passage(conn, "What material has the least workability?", BASE)
    assert {p["title"] for p in out} == {"Category:Crafting materials"}
    assert {p["section"] for p in out} >= {"Forging materials > Rare metals", "Engineering materials > Rare bone", "Forging materials > Metal properties"}


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
        yield "Glaes has the least workability, 10."


def turn(a, sid, q) -> Done:
    async def go():
        return [e async for e in a.answer_stream(sid, q)]
    return next(e for e in asyncio.run(go()) if isinstance(e, Done))


def test_through_the_real_answer_path_the_model_writes_none_of_it(settings, conn):
    a = Assistant(settings, conn, Model("What material has the least workability?"))
    done = turn(a, "s1", "What material has the least workability?")
    assert "Silversteel" in done.answer and "Glaes" not in done.answer and done.by_code and a.ollama.written == 0
    assert done.sources[0].title == "Category:Crafting materials"
    a.ollama.standalone = "What is the workability of damite?"
    done = turn(a, "s1", "and what about damite?")  # a follow-up reaches it through what the planner made of it
    assert "**Damite** has a workability of **10**" in done.answer and a.ollama.written == 0


@pytest.mark.parametrize("q", ["What is the affinity of Smokewood?", "What are the bow affinities of Smokewood?"])
def test_no_bow_named_gives_all_three_affinities(conn, q):
    answer = ask(conn, q)
    assert "shortbow / arbalest affinity of **96**" in answer and "longbow / heavy crossbow affinity of **60**" in answer
    assert "composite bow / light crossbow affinity of **60**" in answer


def test_the_highest_affinity_is_each_bows_and_only_wood_has_one(conn):
    answer = ask(conn, "What wood has the highest affinity?")
    assert "shortbow / arbalest affinity is **96**: Smokewood" in answer and "longbow / heavy crossbow affinity" in answer
    assert ask(conn, "What material has the best longbow affinity?").startswith("Among woods,")
    assert "don't give a shortbow / arbalest affinity for metals" in ask(conn, "Which metal has the highest shortbow affinity?")


@pytest.mark.parametrize("q,start", [
    ("Which wood has the highest shortbow?", "Among woods, the highest shortbow / arbalest affinity is **96**"),
    ("What is the longbow of Smokewood?", "**Smokewood** has a longbow / heavy crossbow affinity of **60**"),
    ("Whats the stiffest wood?", "Among woods, the highest stiffness is **50**"),
    ("least stiff wood", "Among woods, the lowest stiffness is **50**"),
    ("What is the weight of Glaes?", "**Glaes** has a density of **4**"),
    ("How much does Silversteel cost?", "**Silversteel** has a value of **27,500 Kronars**"),
    ("How much is Bloodwood worth?", "**Bloodwood** has a value of **900 Kronars**"),
    ("Which metal has the best fire resistance?", "Among metals, the highest thermal resistance is **80**"),
    ("Which metal has the best lightning resistance?", "Among metals, the highest electrical resistance is **60**"),
    ("What is the most potent catalyst?", "The highest potency in the crafting materials tables is **40**: Silversteel"),
])
def test_the_other_ways_of_saying_a_column(conn, q, start):
    assert ask(conn, q).startswith(start)


def test_comparing_with_no_stat_shows_them_all_and_a_named_stat_shows_that(conn):
    answer = ask(conn, "Compare Bloodwood and Smokewood")
    assert answer.count("stiffness 50") == 2 and "**Bloodwood** (rare wood)" in answer and "**Smokewood** (rare wood)" in answer
    assert ask(conn, "Bloodwood vs Smokewood hardness").startswith("Hardness in the crafting materials tables")
    assert "**Smokewood:** 96" in ask(conn, "Is Bloodwood or Smokewood better for shortbow affinity?")


@pytest.mark.parametrize("q", ["What is the best shortbow?", "What is the best longbow for a hunter?", "Is iron worth using?", "What is the weight of a broadsword?",
                               "How much does a broadsword cost?", "What is the cost of living in Shard?", "Compare a sword and an axe", "What is the affinity of a Moon Mage?"])
def test_those_words_do_not_catch_other_questions(conn, q):
    assert ask(conn, q) == ""


def test_doctor_watches_the_rule_and_the_column(conn):
    checks = {c.name: c.ok for c in run_doctor(conn)}
    assert checks["rule still on Category:Crafting materials"] is not None
    assert "material stats are readable" in checks
