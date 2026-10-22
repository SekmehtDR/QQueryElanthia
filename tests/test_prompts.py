from epedia.llm.prompts import (
    NEUTRAL_ANSWER_SYSTEM,
    ROUTER_SCHEMA,
    SHAPE_HINTS,
    build_answer_system,
    clean_passage_text,
    format_passages,
    offer_line,
)

RAW = (
    'There are other pages that use the name "aged leathers." Follow the link for more details.\n\n'
    "Outdated Item\n\n"
    "- This item is outdated, which means that it has missed one of the last five appraisal changes (1) (2).\n"
    "- Please see Category:Outdated Items for more articles that are outdated.\n\n"
    "aged leathers (1)\nLook: This armor appears well worn.\nType: Light Armor"
)


def test_boilerplate_is_removed_but_outdated_marker_kept():
    out = clean_passage_text(RAW)
    assert "There are other pages" not in out
    assert "Please see Category" not in out
    assert "Marked outdated on the wiki" in out
    assert "Type: Light Armor" in out


def test_plain_text_is_untouched():
    assert clean_passage_text("Berserks are short duration abilities.") == "Berserks are short duration abilities."


def test_old_style_banner_lines_are_cleaned_once():
    raw = (
        "Incomplete Article\n- This article is incomplete, which means that it lacks data.\n"
        "- Please see Category:Incomplete articles for more articles that are incomplete.\n\nMusk Hog\nOverall: 1"
    )
    out = clean_passage_text(raw)
    assert "This article is incomplete" not in out and "Please see" not in out
    assert out.startswith("(Marked incomplete or obsolete on the wiki.)") and "Overall: 1" in out
    # text that already carries the index-time marker is not marked twice
    assert clean_passage_text("(Marked incomplete on the wiki.)\nOverall: 1").count("(Marked") == 1


def test_passages_are_numbered_and_budgeted():
    passages = [{"title": f"P{i}", "section": "", "url": f"u{i}", "text": "x" * 4000} for i in range(10)]
    text, used = format_passages(passages, budget_tokens=2500)
    assert text.startswith("[1] P0")
    assert 1 <= used < 10
    assert f"[{used}] P{used - 1}" in text


def test_persona_prompt_has_name_once_hint_and_passages_last():
    out = build_answer_system("crafting", "[1] X\nbody {with braces}", "Fennick")
    assert out.count("Fennick") == 1
    assert SHAPE_HINTS["crafting"] in out
    assert out.rstrip().endswith("body {with braces}")
    assert "Do not end with an offer" in out  # the closing line is added by the program, not the model


def test_every_answer_prompt_reminds_the_model_to_cite_each_factual_line():
    assert "end EVERY sentence" in build_answer_system("item", "[1] X\nbody", "Fennick")
    assert "end EVERY sentence" in build_answer_system("item", "[1] X\nbody", "Fennick", persona=False)


def test_history_strips_both_kinds_of_citation_tag():
    from epedia.core.memory import _CITATION

    assert _CITATION.sub("", "Weighs 35 stones [1][2*] and costs 287 [3].") == "Weighs 35 stones and costs 287."


def test_general_kind_has_no_shape_hint():
    assert "Shape for" not in build_answer_system("general", "[1] X\nbody", "Fennick")


def test_persona_can_be_turned_off():
    out = build_answer_system("item", "[1] X\nbody", "Fennick", persona=False)
    assert out == NEUTRAL_ANSWER_SYSTEM.replace("{passages}", "[1] X\nbody")
    assert "Fennick" not in out


def test_offer_line_names_first_related_page_without_namespace_prefix():
    assert offer_line([]) == ""
    line = offer_line(["Concept:Elves", "Hodierna"])
    assert "Elves" in line and "Concept:" not in line and "Hodierna" not in line
    assert offer_line(["Forging skill"]) == offer_line(["Forging skill"])  # deterministic


def test_router_schema_requires_kind_with_ten_values():
    assert "kind" in ROUTER_SCHEMA["required"]
    assert len(ROUTER_SCHEMA["properties"]["kind"]["enum"]) == 10
