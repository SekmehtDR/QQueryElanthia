"""The verifier: model citations checked against their passages, names no passage has, and pretended lookups.
Examples are taken from the real conversation log."""

from epedia.core import verify
from epedia.core.attribution import attribute

HOG = {"title": "Musk hog", "section": "", "text": "Musk hogs are found in the Crossing hog pens. They weigh 40 stones "
       "and are rated level 3. Skinning yields a musk hog hide."}
LADDER = {"title": "Living hunting ladder", "section": "Zoluren", "text": "| Creature | Level | Ranks |\n"
          "| Elder razortusk boar | 35 | 130-230 |\n| River sprite | 31 | 140-190 |"}
BINU = {"title": "Binu", "section": "", "text": "Race: Human\nGuild: Necromancer\nInstance: Prime\n"
        "Binu has greenish granite-like skin and sapphire eyes."}


def test_a_citation_the_passage_bears_out_is_kept():
    v = verify.run("Musk hogs live in the Crossing hog pens [1]. They weigh 40 stones [1].", [HOG])
    assert v.unsupported == [] and "[1?]" not in v.text and v.cited_units == 2


def test_a_cited_line_with_a_number_the_passage_lacks_is_marked():
    v = verify.run("Musk hogs weigh 35 stones and are found in the Crossing hog pens [1].", [HOG])
    assert v.numbers_bad == 1 and "[1?]" in v.text and len(v.unsupported) == 1


def test_a_citation_pointing_at_the_wrong_passage_is_marked():
    v = verify.run("Binu has greenish granite-like skin and sapphire eyes [1].", [HOG, BINU])
    assert "[1?]" in v.text
    ok = verify.run("Binu has greenish granite-like skin and sapphire eyes [2].", [HOG, BINU])
    assert ok.unsupported == []


def test_a_citation_number_with_no_passage_is_marked_even_on_a_good_line():
    v = verify.run("Musk hogs live in the Crossing hog pens [1][9].", [HOG])
    assert "[1]" in v.text and "[9?]" in v.text and v.unsupported == []


def test_numbers_written_differently_still_match():
    v = verify.run("The elder razortusk boar is level 35, for ranks 130 to 230 [1].", [LADDER])
    assert v.numbers_bad == 0 and v.unsupported == []
    dated = {"title": "Event", "section": "", "text": "Next window: 2027-01-02 to 2027-01-17."}
    assert verify.run("The next window opens on January 2, 2027 [1].", [dated]).numbers_bad == 0
    big = {"title": "Ladder", "section": "", "text": "Drakes need 1500 ranks."}
    assert verify.run("Drakes need about 1,500 ranks of combat [1].", [big]).numbers_bad == 0


def test_list_numbers_and_numbers_the_user_gave_are_not_claims():
    v = verify.run("1. Musk hogs live in the Crossing hog pens [1].", [HOG])
    assert v.numbers_bad == 0
    v = verify.run("With 200 offense, the elder razortusk boar at level 35 suits you [1].", [LADDER],
                   message="About 200 offense and 180 defense")
    assert v.numbers_bad == 0


def test_invented_creatures_are_named_as_unknown():
    answer = ("As a Circle 100 Moon Mage, among the most fitting targets are the **Shadow Serpents** and "
              "**Moonwraiths**, which dwell near ancient ruins [1].")
    got = verify.unknown_entities(answer, [LADDER["title"] + "\n" + LADDER["text"]],
                                  "What creature should I hunt as a circle 100 moon mage?")
    assert "Shadow Serpents" in got and "Moonwraiths" in got
    assert "Moon Mage" not in got and "Circle" not in " ".join(got)  # the question named those


def test_names_from_passages_titles_question_persona_and_earlier_turns_are_known():
    sources = [BINU["title"] + "\n" + BINU["text"]]
    answer = ("Binu's page lists him as a Human Necromancer in the Prime instance [1]. From what I've read in "
              "Elanthipedia, travelers in Zoluren and Therengia speak of the Immortals in October.")
    assert verify.unknown_entities(answer, sources, "Who is Binu?") == []
    assert verify.unknown_entities("He was blessed by Hodierna, I hear.", sources) == ["Hodierna"]
    assert verify.unknown_entities("He was blessed by Hodierna, I hear.", sources, context="what about Hodierna?") == []


def test_ordinary_words_at_a_sentence_start_and_openers_are_not_names():
    sources = [HOG["text"]]
    assert verify.unknown_entities("However, musk hogs are small. Shadows fall early there.", sources) == []
    assert verify.unknown_entities("- **Location:** the Crossing hog pens.\n| Level | 3 |", sources) == []
    assert verify.unknown_entities("Read The Musk Hog Guide first.", ["Musk Hog Guide"]) == []


def test_pretended_lookups_are_found_and_ordinary_phrases_are_not():
    for line in ("Let me check that for Binu.", "Let me take a closer look at that.", "I'll look into the page history.",
                 "Give me a moment to pull up his page.", "I've checked the page history, and it agrees.",
                 "Let me just double-check the edits."):
        assert verify.fake_actions(line), line
    for line in ("Let me know if you want more.", "Let me explain how forging works.", "Let's look at the ladder.",
                 "I can tell you about the Crossing.", "Check the board at the guild for work orders."):
        assert not verify.fake_actions(line), line


def test_verification_counts_and_its_record():
    v = verify.run("Musk hogs live in the Crossing hog pens [1]. Their hide sells to tanners for a good price "
                   "in every town. At night the Moonwraiths hunt them.\n\nWant to hear about Crossing next?", [HOG])
    d = v.as_dict()
    assert v.factual_units == 3 and v.cited_units == 1 and v.uncited == 2 and "text" not in d
    assert d["ratio_uncited"] == round(2 / 3, 3) and v.fabricated and "Moonwraiths" in v.unknown_entities
    assert not verify.run("Musk hogs live in the Crossing hog pens [1].", [HOG]).fabricated


def test_attribution_ignores_list_numbers_now():
    # "2." used to count as a number the passage lacked, so numbered steps never got an automatic tag
    got = attribute("2. Musk hogs are found in the Crossing hog pens.", [HOG["text"]]).text
    assert got.endswith("[1*].")
