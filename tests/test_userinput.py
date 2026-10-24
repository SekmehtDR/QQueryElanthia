from epedia.core.assistant import strip_closing_offer
from epedia.core.userinput import PURE_PASTE_QUESTION, split_message

POST = ("Tirost — 7:03 PM\nGot a request from Yraggahh, an anti-Dragon Priest S'Kra Barb for any interested fighters "
        "to join an attack on the Black Spire on December 11th. This is a part of Suulnir storyline.")


def test_a_question_followed_by_a_pasted_post_is_split():
    q, material = split_message("What can you tell me about Tirosts message?\n" + POST)
    assert q == "What can you tell me about Tirosts message?" and material.startswith("Tirost") and "Black Spire" in material


def test_a_pasted_post_followed_by_the_question_is_split_too():
    q, material = split_message(POST + "\nCan you explain the lore behind this?")
    assert q == "Can you explain the lore behind this?" and "Suulnir" in material


def test_ordinary_messages_are_left_alone():
    assert split_message("Who is Hodierna?") == ("Who is Hodierna?", "")
    assert split_message("Who is Hodierna?\nAnd Lanival?") == ("Who is Hodierna?\nAnd Lanival?", "")  # too short to be a paste


def test_a_long_paste_with_no_question_gets_a_default_one():
    q, material = split_message(POST.replace("\n", " ") * 6)
    assert q == PURE_PASTE_QUESTION and "Black Spire" in material


def test_a_closing_offer_the_model_wrote_itself_is_removed_but_content_is_kept():
    body = "The Spire is the Dragon Priests' temple [1].\n\nSuulnir leads them [2]."
    assert strip_closing_offer(body + "\n\nIf you're interested, I can also tell you about the Empaths.") == body
    assert strip_closing_offer(body + "\n\nLet me know what you'd like to explore next.") == body
    assert strip_closing_offer(body) == body  # nothing to remove
    assert strip_closing_offer("If you're interested, see the guide [1].") == "If you're interested, see the guide [1]."  # a lone cited line is content


def test_a_follow_up_about_what_i_sent_you_finds_the_earlier_paste(tmp_path):
    from epedia.config import Settings
    from epedia.core import memory
    from epedia.core.assistant import REFERS_TO_PASTE
    from epedia.db import connect

    for text in ("Can you help me understand the lore of what I sent you though?", "What does that message mean?",
                 "explain the post above"):
        assert REFERS_TO_PASTE.search(text)
    assert not REFERS_TO_PASTE.search("Who is Hodierna?") and not REFERS_TO_PASTE.search("What shops are in Riverhaven?")

    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    conn = connect(s)
    sid = memory.new_session(conn, "web")
    assert memory.last_pasted(conn, sid) == ""
    memory.add_message(conn, sid, "user", "What can you tell me about this message?\n" + POST)
    memory.add_message(conn, sid, "assistant", "It mentions the Black Spire.")
    memory.add_message(conn, sid, "user", "And Hodierna?")
    assert "Black Spire" in memory.last_pasted(conn, sid)
    memory.clear_context(conn, sid)
    assert memory.last_pasted(conn, sid) == ""  # setting the topic aside forgets the paste too
    conn.close()


def test_old_offer_lines_are_not_fed_back_to_the_model_and_several_model_offers_are_all_removed():
    import pathlib
    import tempfile

    from epedia.config import Settings
    from epedia.core import memory
    from epedia.db import connect

    body = "The Spire is the Dragon Priests' temple [1]."
    two = body + "\n\nIf you're curious, I can tell you about Zoluren.\n\nI can also tell you about Humans, if you like."
    assert strip_closing_offer(two) == body

    s = Settings()
    s.db.path = str(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    conn = connect(s)
    sid = memory.new_session(conn, "web")
    memory.add_message(conn, sid, "user", "Tell me about the Spire")
    memory.add_message(conn, sid, "assistant", body + "\n\nWant to hear about Shard next?")
    assert memory.history(conn, sid, 4)[1]["content"] == "The Spire is the Dragon Priests' temple."  # no offer, no [1]
    conn.close()


def test_advice_that_starts_like_an_offer_is_kept_but_real_offers_go():
    advice = "The shop is in the Crossing.\n\nIf you want to start forging, buy a hammer and tongs first."
    assert strip_closing_offer(advice) == advice  # this is the answer, not an offer
    assert strip_closing_offer("Body.\n\nIf you want, I can also tell you about forging.") == "Body."


def test_a_long_question_with_that_in_it_is_not_treated_as_leaning_on_context():
    from epedia.core.assistant import SHORT_MESSAGE_WORDS

    long_q = "Which guild offers spells that heal broken bones and also work on poison for friends in the group"
    assert len(long_q.split()) > SHORT_MESSAGE_WORDS
