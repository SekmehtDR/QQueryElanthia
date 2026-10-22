import pytest

from epedia.core.items_tool import ItemsQueryError, build_ask, validate
from epedia.text.stats import render
from epedia.wiki.smw import parse_properties

PROPS = {"Weight_of": "_num", "Is_combat_type": "_txt", "Appraised_cost_is": "_num", "Page_type_is": "_txt"}


def q(**kw):
    base = {"page_type": "weapon", "filters": [], "sort": None, "limit": 10, "fields": []}
    return validate({**base, **kw}, PROPS)


def test_builds_ask_with_smw_operator_semantics():
    ask = build_ask(q(
        filters=[{"property": "Is_combat_type", "op": "=", "value": "Heavy Edged"},
                 {"property": "Appraised_cost_is", "op": "<=", "value": 100}],
        sort={"property": "Weight_of", "order": "desc"},
        fields=["Weight_of"],
    ))
    assert ask.startswith("[[Page_type_is::weapon]][[Is_combat_type::Heavy Edged]][[Appraised_cost_is::<100]]")
    assert "|?Weight_of" in ask and "|sort=Weight_of|order=desc|limit=10" in ask


def test_strict_operators_double_up():
    ask = build_ask(q(filters=[{"property": "Weight_of", "op": ">", "value": 5}]))
    assert "[[Weight_of::>>5]]" in ask


def test_unknown_property_rejected():
    with pytest.raises(ItemsQueryError):
        q(filters=[{"property": "Drop_table;", "op": "=", "value": "x"}])


def test_numeric_operator_on_text_property_rejected():
    with pytest.raises(ItemsQueryError):
        q(filters=[{"property": "Is_combat_type", "op": ">", "value": 3}])


def test_values_cannot_inject_query_syntax():
    ask = build_ask(q(filters=[{"property": "Is_combat_type", "op": "=", "value": "x]][[Page_type_is::title|?foo"}]))
    assert "]][[Page_type_is::title" not in ask.split("[[Is_combat_type::")[1]


def test_numeric_filter_values_sent_as_strings_are_coerced_to_numbers():
    f = q(filters=[{"property": "Weight_of", "op": "<", "value": "10"}])["filters"][0]
    assert f["value"] == 10 and isinstance(f["value"], int)
    assert q(filters=[{"property": "Weight_of", "op": ">", "value": "2.5"}])["filters"][0]["value"] == 2.5
    assert q(filters=[{"property": "Appraised_cost_is", "op": "<=", "value": "1,500"}])["filters"][0]["value"] == 1500
    with pytest.raises(ItemsQueryError):
        q(filters=[{"property": "Weight_of", "op": "<", "value": "heavy"}])


def test_local_fallback_compares_numbers_numerically_not_as_text():
    import sqlite3

    from epedia.core.items_tool import local_rows

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE pages(page_id INTEGER PRIMARY KEY, title TEXT);"
        "CREATE TABLE item_properties(page_id INT, prop TEXT, value_text TEXT, value_num REAL, value_type TEXT);"
    )
    for pid, (title, w) in enumerate([("light", 9), ("medium", 10), ("heavy", 100)], 1):
        c.execute("INSERT INTO pages VALUES (?, ?)", (pid, title))
        c.execute("INSERT INTO item_properties VALUES (?, 'Page_type_is', 'weapon', NULL, '_txt')", (pid,))
        c.execute("INSERT INTO item_properties VALUES (?, 'Weight_of', ?, ?, '_num')", (pid, str(w), float(w)))
    query = q(filters=[{"property": "Weight_of", "op": "<", "value": "10"}], fields=["Weight_of"])
    assert [r["title"] for r in local_rows(c, query)] == ["light"]  # as text, '9' < '10' would be False


def test_limit_is_clamped():
    assert q(limit=500)["limit"] == 25


def test_table_reads_smw_labels_with_spaces_and_trims_suffixes():
    from epedia.core.items_tool import render_table

    rows = [{"title": "Weapon:Axe", "printouts": {"Weight of": [35], "Is combat type": ["Heavy Edged"]}}]
    table = render_table(rows, ["Weight_of", "Is_combat_type"])
    assert table.splitlines()[0] == "| Item | Weight | Is combat type |"
    assert table.splitlines()[2] == "| Weapon:Axe | 35 | Heavy Edged |"


def test_ranking_queries_get_at_least_five_rows():
    assert q(sort={"property": "Weight_of", "order": "desc"}, limit=1)["limit"] == 5


def test_title_match_ignores_page_type_words():
    import sqlite3

    from epedia.retrieval.search import title_matches

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE VIRTUAL TABLE titles_fts USING fts5(title_norm, page_id UNINDEXED, is_redirect UNINDEXED)")
    c.executemany("INSERT INTO titles_fts VALUES (?, ?, 0)", [("athletics skill", 1), ("stealth skill", 2), ("lich", 3)])
    exact, partial = title_matches(c, "What is Athletics used for?")
    assert 1 in partial and 2 not in partial
    exact, _ = title_matches(c, "stealth")
    assert 2 in exact


def test_parse_properties_types_and_internal_props():
    data = {"query": {"data": [
        {"property": "Weight_of", "dataitem": [{"type": 1, "item": "35"}]},
        {"property": "Source_is", "dataitem": [{"type": 2, "item": "<span>Zerek</span>"}]},
        {"property": "Page_source_is", "dataitem": [{"type": 9, "item": "Zerek's_General_Store#0##"}]},
        {"property": "_MDAT", "dataitem": [{"type": 6, "item": "1/2015"}]},
    ]}}
    props = {p["prop"]: p for p in parse_properties(data)}
    assert props["Weight_of"]["value_num"] == 35.0
    assert props["Source_is"]["value_text"] == "Zerek"
    assert props["Page_source_is"]["value_text"] == "Zerek's General Store"
    assert "_MDAT" not in props


def test_stat_sheet_merges_text_and_number_and_hides_noise():
    props = [
        {"prop": "Slice_damage_is", "value_text": "somewhat heavy", "value_num": None, "value_type": "_txt"},
        {"prop": "Slice_damage_is_number", "value_text": "8", "value_num": 8.0, "value_type": "_num"},
        {"prop": "Weight_of", "value_text": "35", "value_num": 35.0, "value_type": "_num"},
        {"prop": "Needs_checking", "value_text": "t", "value_num": None, "value_type": "_boo"},
        {"prop": "Page_type_is", "value_text": "weapon", "value_num": None, "value_type": "_txt"},
    ]
    sheet = render("Weapon:Broadsword (1)", props)
    assert sheet.splitlines()[0] == "Broadsword (1) — weapon stats"
    assert "Slice damage: somewhat heavy (8)" in sheet
    assert "Weight: 35" in sheet
    assert "Needs checking" not in sheet
