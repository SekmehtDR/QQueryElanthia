from epedia.core.assistant import PARTIAL_NOTE, _snippet, soften_refusal
from epedia.llm.prompts import NO_PASSAGES_ANSWER

REAL = (
    "Musk hogs are small, scruffy creatures with a strong odour, found along the Woodland Path (4) [2]. "
    "They have an overall level of 1."
)


def test_a_refusal_tacked_onto_a_real_answer_becomes_an_honest_caveat_not_a_contradiction():
    out = soften_refusal(REAL + "\n\n" + NO_PASSAGES_ANSWER)
    assert out == REAL + "\n\n" + PARTIAL_NOTE
    assert "nothing on that" not in out


def test_a_missing_detail_disclaimer_is_kept_in_softened_form_never_silently_deleted():
    out = soften_refusal(REAL + " The wiki doesn't seem to cover that.")
    assert out.endswith(PARTIAL_NOTE) and out.startswith(REAL)  # the reader still learns something is missing


def test_a_genuine_refusal_on_its_own_is_kept_verbatim():
    assert soften_refusal(NO_PASSAGES_ANSWER) == NO_PASSAGES_ANSWER
    assert soften_refusal("Short.\n" + NO_PASSAGES_ANSWER).endswith(NO_PASSAGES_ANSWER)  # too little content to call it an answer


def test_answers_without_a_refusal_are_untouched():
    assert soften_refusal(REAL) == REAL


def test_snippet_is_one_clean_line_without_the_maintenance_note():
    text = "(Marked incomplete on the wiki.)\nMusk Hog\nOverall: 1\n" + "x " * 300
    s = _snippet(text, limit=80)
    assert "\n" not in s and not s.startswith("(Marked") and s.startswith("Musk Hog") and s.endswith("…")
    assert len(s) <= 81


def test_meta_cues_separate_small_talk_from_questions_about_people():
    from epedia.core.assistant import META_CUES

    for text in ("Hello! Who are you?", "What is Elanthipedia?", "thanks!", "Can you help me?"):
        assert META_CUES.search(text)
    for text in ("Who is Bobba Bandicoot?", "Who is Hodierna?", "What is a lich?"):
        assert not META_CUES.search(text)


def test_the_older_formal_refusal_wording_is_softened_too():
    from epedia.llm.prompts import OLD_NO_PASSAGES_ANSWER

    out = soften_refusal(REAL + "\n\n" + OLD_NO_PASSAGES_ANSWER)
    assert out == REAL + "\n\n" + PARTIAL_NOTE
