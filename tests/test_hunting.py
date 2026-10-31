import sqlite3

from epedia.core import hunting

ROWS = """| | Creature; Level | Level; Varies | Min; Ranks | Max; Ranks | Place | Cursed |
|---|---|---|---|---|---|---|
| Rat | 0 |  | 0 | 30 | Zoluren; Crossing | - |
| Fell hog | 2 |  | 10 | 40 | Zoluren | - |
| Brigand | 40 |  | 120 | 210 | Zoluren; Crossing | - |
| Dragon | 90 |  | 400 | 600 | Ilithi | - |
| Mystery | 5 |  | 2,000 | 0 | Nowhere | - |
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE chunks (chunk_id INTEGER PRIMARY KEY, title TEXT, ord INTEGER, text TEXT)")
    c.execute("INSERT INTO chunks (title, ord, text) VALUES (?, 0, ?)", (hunting.LADDER_TITLE, ROWS))
    return c


def test_cue_separates_fit_questions_from_lookups():
    assert hunting.HUNT_FIT.search("What creature should I hunt as a circle 100 moon mage?")
    assert hunting.HUNT_FIT.search("which creatures can I fight at 150 ranks")
    assert not hunting.HUNT_FIT.search("How many ranks do I need to hunt a rat?")


def test_parse_ranks():
    assert hunting.parse_ranks("I have 150 ranks") == [150]
    assert hunting.parse_ranks("offense 210, defense 190") == [210, 190]
    assert hunting.parse_ranks("offense 200 defense 180") == [200, 180]
    assert hunting.parse_ranks("About 200 offense and 180 defense, I like Zoluren") == [200, 180]
    assert hunting.parse_ranks("I have 150 ranks of offense") == [150]
    assert hunting.parse_ranks("circle 100 moon mage") == []


def test_ladder_skips_rows_without_data():
    names = [c.name for c in hunting.ladder(_conn())]
    assert names == ["Rat", "Fell hog", "Brigand", "Dragon"]


def test_fits_and_passage_use_the_lower_rank():
    p = hunting.passage(_conn(), [200, 125], "https://elanthipedia.play.net")
    assert p and "Brigand" in p["text"] and "Dragon" not in p["text"] and "Rat" not in p["text"]
    assert p["url"].endswith("/Estimated_Ranks_hunting_ladder")


def test_no_passage_when_nothing_fits_or_no_ranks():
    assert hunting.passage(_conn(), [], "https://x") is None
    assert hunting.passage(_conn(), [3000], "https://x") is None


def test_areas_filter_and_answer_is_written_from_the_table():
    c = _conn()
    p = hunting.passage(c, [130], "https://elanthipedia.play.net", "I like Ilithi or Crossing")
    assert p and "Fell hog" not in p["answer"]  # in range, but only found in Zoluren
    assert "**Brigand**" in p["answer"] and "**Rat**" not in p["answer"]
    p = hunting.passage(c, [15], "https://elanthipedia.play.net", "")
    assert p and "**Rat**" in p["answer"] and "**Fell hog**" in p["answer"] and p["answer"].count("[1]") == 2
