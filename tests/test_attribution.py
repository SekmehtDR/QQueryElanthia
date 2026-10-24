from epedia.core.attribution import attribute

PASSAGES = [
    "Broadsword (1)\nType: Heavy Edged\nWeight: 35 stones\nAppraised Cost: 287 Kronars\nBalance: fairly (5/17)",
    "The Crossing is the main hub of trade and government in the province of Zoluren. "
    "Currency: Kronar. Guild Halls: Bard, Barbarian, Cleric, Empath.",
    "Hodierna is goddess of life, morning, and light, as well as springtime, harvest, and crops. "
    "Farmers, herbalists, healers, and Empaths venerate her.",
]


def test_uncited_sentence_gets_the_tag_of_the_passage_it_overlaps():
    r = attribute("The broadsword weighs 35 stones and costs 287 Kronars.", PASSAGES)
    assert r.text == "The broadsword weighs 35 stones and costs 287 Kronars [1*]."
    assert r.added == 1


def test_lines_the_model_already_cited_are_left_untouched():
    text = "The broadsword weighs 35 stones [1]. Hodierna is goddess of life, morning, and light [3]."
    r = attribute(text, PASSAGES)
    assert r.text == text and r.added == 0


def test_each_uncited_sentence_is_matched_independently():
    r = attribute("The broadsword weighs 35 stones. Hodierna is goddess of life, morning, and light.", PASSAGES)
    assert "stones [1*]." in r.text and "light [3*]." in r.text and r.added == 2


def test_list_items_and_table_rows_are_tagged_without_breaking_markup():
    answer = "- Currency: Kronar in the main hub of trade\n- Weight: 35 stones\n\n| Item | Cost |\n|---|---|\n| broadsword | 287 Kronars |\n"
    r = attribute(answer, PASSAGES)
    lines = r.text.split("\n")
    assert lines[0].endswith("trade [2*]") and lines[1].endswith("35 stones [1*]")
    assert lines[5] == "| broadsword | 287 Kronars [1*] |"  # inside the last cell, still a valid row
    assert lines[4] == "|---|---|"  # the separator row is never touched


def test_framing_and_unsupported_lines_are_not_tagged():
    r = attribute("Here is what I found. The weather in Shard is always lovely and mild.", PASSAGES)
    assert "*]" not in r.text and r.added == 0
    assert r.uncited >= 1


def test_a_full_match_in_two_passages_is_credited_to_the_higher_ranked_one():
    twins = ["The axe weighs 80 stones and is made of steel.", "The axe weighs 80 stones and is made of steel."]
    assert attribute("The axe weighs 80 stones and is made of steel.", twins).text.endswith("steel [1*].")


def test_a_partial_match_that_fits_two_passages_equally_is_left_alone():
    twins = ["The axe weighs 80 stones, steel, and gold trim.", "The axe weighs 80 stones, steel, and gold trim."]
    # half of this line's words are in both passages and neither is clearly better: no guess
    r = attribute("The axe weighs 80 stones but the hilt is oak with silver rivets and leather wrapping.", twins)
    assert r.added == 0


def test_headings_code_blocks_and_the_closing_offer_are_skipped():
    answer = "## Broadsword weighs 35 stones\n```\nWeight: 35 stones\n```\nIf you like, I can tell you about Crossing Guild Halls."
    assert attribute(answer, PASSAGES).added == 0


def test_numbers_weigh_more_than_common_words():
    r = attribute("It weighs 35 stones.", PASSAGES + ["It weighs 99 stones in the old guild records of Ilithi."])
    assert "[1*]" in r.text


def test_a_citation_placed_after_the_period_stays_with_its_sentence():
    text = "The broadsword weighs 35 stones and costs 287 Kronars. [1] Hodierna is goddess of life, morning, and light."
    r = attribute(text, PASSAGES)
    assert r.text.startswith("The broadsword weighs 35 stones and costs 287 Kronars. [1] ")  # not tagged a second time
    assert r.text.count("[1") == 1 and r.text.endswith("light [3*].")


def test_plural_forms_match_but_words_are_not_mangled():
    from epedia.core.attribution import _stem

    assert _stem("stones") == _stem("stone") == "stone"
    assert _stem("cities") == "city" and _stem("boxes") == "box" and _stem("uses") == "use"
    assert _stem("glass") == "glass" and _stem("bonus") == "bonus" and _stem("axis") == "axis"
    r = attribute("It weighs 35 stone.", ["Weight: 35 stones. Appraised cost: 287 Kronars."])
    assert "[1*]" in r.text


def test_a_line_with_a_number_the_passage_does_not_contain_is_never_tagged():
    passage = ["The hammer weighs 35 stones and costs 287 coppers at the forge."]
    assert attribute("The hammer weighs 35 stones and costs 287 coppers.", passage).added == 1
    wrong = attribute("The hammer weighs 40 stones and costs 287 coppers.", passage)
    assert wrong.added == 0 and "*]" not in wrong.text  # same words, different number: no badge, no tooltip


def test_empty_input_is_safe():
    assert attribute("", PASSAGES).text == "" and attribute("Some text here.", []).added == 0
