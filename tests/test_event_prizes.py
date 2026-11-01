"""Raffles and auctions answered by code, from a real thumbs-down: the model said the prizes were "not listed" and then invented a raffle from the
page's blank template. Also the pieces that failure showed: editing instructions are not knowledge, and a page named outright is read."""

import json

import pytest

from epedia.config import Settings
from epedia.core import event_prizes
from epedia.core.diagnose import named_pages
from epedia.db import connect
from epedia.index import doctor
from epedia.index.enrich import enrich_all
from epedia.retrieval.resolve import explicit_pages
from epedia.text.html2md import html_to_page

BASE = "https://elanthipedia.play.net"
HE458 = "Hollow Eve Festival 458/Raffles"
HE454 = "Hollow Eve Festival 454/Raffles"
HEADER = "| Item | Winner | Notes |\n|---|---|---|\n"


def page(c, pid, title, *chunks):
    c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, fetched_revid, indexed_revid, is_redirect, page_kind, updated_at)"
              " VALUES (?, 0, ?, ?, 5, 5, 5, 0, 'event', 'x')", (pid, title, title.lower()))
    for i, (section, text) in enumerate(chunks):
        c.execute("INSERT INTO chunks(page_id, revid, ord, kind, title, section_path, text, token_est, content_hash)"
                  " VALUES (?, 5, ?, 'section', ?, ?, ?, 1, ?)", (pid, i, title, section, text, f"{pid}.{i}"))


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    page(c, 1, HE458,
         ("", "[Andreshlew's Grand Raffle]\nA wide pavilion."),
         ("Prime", "NOTES:\n\nRaffles 001-002 were on 11/1/2026."),
         ("Prime > Raffle 001", HEADER + "| infuser stone | Zemfreed |  |\n| treasure map | Erolith |  |\n| treasure map | Drachekrieger |  |\n\nRaffle 002\n"
          + HEADER + "| potency crystal | Warfare | held a note |\n| dawnbreak diamond | ? |  |\n|  | ? |  |"))
    page(c, 2, HE454, ("Prime > Raffle 084", HEADER + "| old sword | Zemfreed |  |\n| treasure map | Oldwinner |  |"),
         ("Prime > Raffle 085", HEADER + "".join(f"| item number {i} | Person{i} |  |\n" for i in range(40))))
    page(c, 3, "Hollow Eve Festival 454/Auction",
         ("Prime (June 21, 2025)", "| Item | Buyer | Platinum; Lirums | Platinum; Kronars | Notes |\n|---|---|---|---|---|\n| lightning bolts | Fidon | 67,257 |  |  |\n| warshield amulet | Havoc |  | 88,000 |  |"),
         ("Prime (June 21, 2025)", "| sungold box | Nelis | 55,000 |  |  |"))
    page(c, 4, "Drathrok's Duskruin 430/Auction", ("Prime > Stats", "Platinum Auction (Sunday, March 31st 9pm EST)\n| Item | Buyer | Bloodscrip | Notes |\n|---|---|---|---|\n| haledroth katana | Kol | 20000 |  |"))
    page(c, 5, "Talk:Hollow Eve Festival 999/Raffles", ("", HEADER + "| talk page item | Nobody |  |"))
    enrich_all(c)
    yield c
    c.close()


def ask(c, q, earlier=None):
    got = event_prizes.passage(c, q, BASE, earlier)
    return got[0]["answer"] if got else None


def test_the_voted_question_gets_every_item_with_its_winners_grouped(conn):
    text = ask(conn, "Give me a list of the items from here Hollow_Eve_Festival_458/Raffles")
    assert text.startswith("**Hollow Eve Festival 458 raffles:** 5 prizes so far, in 2 raffles [1]")
    assert "- **treasure map** (2): Erolith, Drachekrieger" in text and "- **infuser stone:** Zemfreed" in text
    assert "- **potency crystal:** Warfare (held a note)" in text and "- **dawnbreak diamond:** no winner listed" in text
    assert text.index("treasure map") < text.index("infuser stone")  # the most won first
    assert "Raffle 000" not in text


@pytest.mark.parametrize("q", ["What have been some of the current raffle prizes at this years HE?", "show me the raffles and their outcomes",
                               "What are the prizes in the Hollow Eve raffle?", "what's being raffled at he this year"])
def test_this_years_means_the_newest(conn, q):
    assert "Hollow Eve Festival 458 raffles" in ask(conn, q)


def test_a_year_picks_that_year_and_a_missing_year_says_so(conn):
    assert "Hollow Eve Festival 454 raffles" in ask(conn, "what were the raffle prizes at HE 454?")
    text = ask(conn, "what were the raffle prizes at HE 123?")
    assert "no raffles page for Hollow Eve Festival 123" in text and "newest I have is Hollow Eve Festival 458" in text


@pytest.mark.parametrize("q", ["how do raffles work?", "when is the raffle?", "how much is a raffle ticket?", "where is the auction held?",
                               "what's the weather", "who won the war"])
def test_other_questions_are_left_alone(conn, q):
    assert event_prizes.passage(conn, q, BASE) is None


def test_a_raffle_question_never_shows_an_auction_and_the_other_way_round(conn):
    raffle = event_prizes.passage(conn, "list the raffle items at HE 454", BASE)
    assert [p["title"] for p in raffle] == [HE454] and "lightning bolts" not in raffle[0]["answer"]
    auction = event_prizes.passage(conn, "list the auction items at HE 454", BASE)
    assert [p["title"] for p in auction] == ["Hollow Eve Festival 454/Auction"] and "old sword" not in auction[0]["answer"]


def test_asking_for_both_shows_both_each_with_its_own_citation(conn):
    got = event_prizes.passage(conn, "show the raffles and auctions at HE 454", BASE)
    assert [p["title"] for p in got] == [HE454, "Hollow Eve Festival 454/Auction"]
    assert "raffles:** " in got[0]["answer"] and "[2]" in got[0]["answer"] and "lightning bolts" in got[0]["answer"]


def test_an_auction_says_who_bought_it_and_for_what_across_a_split_table(conn):
    text = ask(conn, "what sold at the HE auction?")
    assert "- **lightning bolts:** Fidon (67,257 Platinum Lirums)" in text and "- **warshield amulet:** Havoc (88,000 Platinum Kronars)" in text
    assert "- **sungold box:** Nelis (55,000 Platinum Lirums)" in text  # a later piece of the table, with no header
    assert "**Hollow Eve Festival 454 auction:** 3 items sold [1]" in text
    assert "(20000 Bloodscrip)" in ask(conn, "who bought things at the Duskruin auction?")


def test_placeholders_are_not_winners_or_notes_and_price_columns_are_named_plainly():
    from epedia.index.extract.event_prizes import extract

    table = ("Prime\n| Item | Buyer | Winning Bid (Platinum Kronars) | Special Description |\n|---|---|---|---|\n| fine box | x | 5 | ... |\n| grand box | Kol | 6 | holds things |\n"
             "| Item | Buyer | Price in platinum kronar | Notes |\n|---|---|---|---|\n| tall box | - | 7 | - |")
    got = [(f[0], f[5], json.loads(f[4])) for f in extract([("Auction (part 2)", table)], "Guildfest 400/Auction")]
    assert got[0] == ("fine box", "Prime", {"who": "", "notes": "", "price": "5 Platinum Kronars"})
    assert got[1][2] == {"who": "Kol", "notes": "holds things", "price": "6 Platinum Kronars"}
    assert got[2][2] == {"who": "", "notes": "", "price": "7 Platinum Kronar"}
    assert extract([("Prime > Raffle 001 (part 2)", "| Item | Winner | Notes |\n|---|---|---|\n| box | Kol |  |")], "Guildfest 400/Raffles")[0][5] == "Raffle 001"


def test_this_years_auction_says_when_the_festival_has_none_yet(conn):
    text = ask(conn, "what's in the Hollow Eve auction?")
    assert text.startswith("My notes have no auction page for Hollow Eve Festival 458 yet, so this is the latest one, from 454.")


def test_who_won_an_item_and_what_someone_won(conn):
    assert "- **treasure map** (2): Erolith, Drachekrieger" in ask(conn, "who won the treasure map in the raffle?")
    text = ask(conn, "who won the old sword in the raffle?")
    assert "Not at Hollow Eve Festival 458" in text and "Hollow Eve Festival 454" in text and "Zemfreed" in text
    assert "doesn't list anything" in ask(conn, "who won the moon in the HE 458 raffle?")
    text = ask(conn, "what did Zemfreed win in the raffles?")
    assert text.startswith("What Zemfreed won") and "infuser stone" in text and "old sword" in text
    assert "don't list Nobodyatall" in ask(conn, "what did Nobodyatall win in the raffles?")


def test_one_raffle_by_number(conn):
    text = ask(conn, "what was in raffle 84 at HE 454?")
    assert "Raffle 84" in text and "old sword" in text and "item number" not in text
    assert "no Raffle 7" in ask(conn, "what was in raffle 7?")


def test_a_raffle_folded_into_its_neighbour_keeps_its_own_number(conn):
    assert "potency crystal" in ask(conn, "what was in raffle 2 at HE 458?")


def test_a_long_page_shows_the_most_won_and_says_how_many_more(conn):
    text = ask(conn, "list the raffle prizes at HE 454")
    assert text.count("\n- ") == event_prizes.MAX_ITEMS
    assert "the 25 most often won of 42 different items" in text


def test_follow_ups_use_what_was_being_talked_about(conn):
    earlier = ["What are the raffle prizes at this years HE?"]
    assert "Hollow Eve Festival 458" in ask(conn, "who won these?", earlier)
    assert "- **treasure map** (2)" in ask(conn, "who won the treasure map?", earlier)
    assert ask(conn, "who won the war?", earlier) is None  # no such prize: not a raffle question after all
    assert ask(conn, "who won these?", ["how do I forge a sword?"]) is None
    assert ask(conn, "what did Zemfreed win?", earlier).startswith("What Zemfreed won")  # no raffle in the words: a real miss, in which the model made it up
    assert ask(conn, "what did Zemfreed win?") is None and ask(conn, "what did Zemfreed win?", ["how do I forge a sword?"]) is None
    assert ask(conn, "what did Nobodyatall win?", earlier) is None
    assert "Zemfreed" in ask(conn, "who won these?", [f"list the items on {HE458.replace(' ', '_')}"])


def test_talk_pages_never_count(conn):
    assert "talk page item" not in ask(conn, "list the raffle prizes at HE 999") and "no raffles page" in ask(conn, "list the raffle prizes at HE 999")
    assert not conn.execute("SELECT count(*) FROM facts WHERE entity='talk page item'").fetchone()[0]


def test_every_answer_cites_its_page(conn):
    got = event_prizes.passage(conn, "list the raffle prizes", BASE)
    assert got[0]["url"].startswith(BASE) and got[0]["text"].startswith("| Item") and got[0]["chunk_id"]


# ------------------------------------------------------------------------------------------- what the failure showed

RAFFLE_HTML = """<div class="mw-parser-output"><p>A wide pavilion.</p>
<table class="mw-collapsible mw-collapsed wikitable"><tbody><tr><th>Editing Instructions</th></tr>
<tr><td>To add a new auction, copy the below blank set. {{Raffle|i| |}}</td></tr></tbody></table>
<h2><span class="mw-headline" id="Prime">Prime</span></h2><table class="wikitable"><tr><th>Item</th><th>Winner</th></tr><tr><td>infuser stone</td><td>Zemfreed</td></tr></table>
<table class="mw-collapsible wikitable"><tbody><tr><th>Some real collapsed facts</th></tr><tr><td>kept</td></tr></tbody></table></div>"""


def test_editing_instructions_are_not_knowledge_but_a_real_table_stays():
    text = "\n".join(p for b in html_to_page(RAFFLE_HTML).blocks for p in b.parts)
    assert "Editing Instructions" not in text and "blank set" not in text
    assert "A wide pavilion" in text and "infuser stone" in text and "kept" in text


def test_an_explicit_page_is_one_with_a_slash_underscores_or_quotes(conn):
    page(conn, 9, "The Crossing", ("", "A city."))
    assert explicit_pages(conn, f"items from here {HE458.replace(' ', '_')}") == [HE458]
    assert explicit_pages(conn, f"look at {HE458}") == [HE458]
    assert explicit_pages(conn, "read The_Crossing please") == ["The Crossing"]
    assert explicit_pages(conn, 'read "The Crossing" please') == ["The Crossing"]
    assert explicit_pages(conn, "where is the crossing") == [] and named_pages(conn, "where is the crossing") == ["The Crossing"]


def test_doctor_checks_the_prizes_and_the_instructions(conn):
    checks = {c.name: c for c in doctor.run(conn)}
    assert checks["festival prizes are readable"].ok
    assert checks["no editing instructions are searchable"].ok
    page(conn, 20, "Oops", ("", "Editing Instructions\nTo add a new auction, copy the below blank set."))
    assert not {c.name: c for c in doctor.run(conn)}["no editing instructions are searchable"].ok
