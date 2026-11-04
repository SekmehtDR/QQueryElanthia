"""Alteration-session schedules, written by code from the festival's own page, and a festival's subpage named in plain words.

From a real thumbs-down: "What alteration sessions are available during the Hollow Eve Festival 458?" was answered with the
Platinum table alone, because the search sent the same "Where" text from five other years' pages instead of Prime."""

from datetime import date

import pytest

from epedia import db
from epedia.config import Settings
from epedia.core import sessions
from epedia.core.assistant import _newer, _same_shape, _shape
from epedia.retrieval.resolve import explicit_pages

HEAD = "| Date | Day | Time (EST) | Merchant | Alterations; Done | Items |\n|---|---|---|---|---|---|\n"
TEMPLATE = "| Date | DayOfWeek | Time | MerchantName | ### |  |\n"
WHERE = ("Merchants sessions are held in Saidakyp of Andreshlew.\n\nWhen\n- Keep an eye on Discord and the calendar for merchant "
         "appearances!\n- Live merchants and alterers could show up unannounced at any time! Be vigilant!\n- A schedule of some "
         "pre-announced sessions is available via the FESTIVAL ALTERATIONS command.")
PAGES = {
    "Hollow Eve Festival 458": [("Tasks", "- You may once again aid the Merelew.")],
    "Hollow Eve Festival 458/Alteration sessions": [
        ("Overview > Where", WHERE),
        ("Session List > Prime", HEAD + "| 2026-11-01 | Sunday | [3:00pm] | Megaya | 40 | Clothing, jewelry and everyday items? |\n"
                                        "| 2026-11-03 | Tuesday | [9:00pm] | Maishi | 5? | Clothing, containers, jewelry |\n"
                                        "| 2026-11-22 | Sunday | [9:00pm] | Nedrina | 5? | Tattoo |\n"),
        # The chunker folded the small Fallen section into Platinum's: only its heading line is left, then the blank template.
        ("Session List > Platinum", HEAD + "| 2026-11-08 | Sunday | [8:00pm] | Nedrina | 5? | Tattoo |\n\nThe Fallen\n" + HEAD + TEMPLATE),
    ],
    "Hollow Eve Festival 458/Raffles": [("Raffle 1", "| Item | Winner |\n|---|---|\n| treasure map | Erolith |\n")],
    "Hollow Eve Festival 454/Alteration sessions": [
        ("Overview > Where", WHERE.replace("Discord", "Discord ")),
        ("Session List > Prime", HEAD + "| 2025-11-01 | Saturday | [3:00pm] | Megaya | 45 | Clothing |\n"),
    ],
    # An old layout: no Day column, the Prime table untitled, and a long table split with no header on its second part.
    "Hollow Eve Festival 425/Alteration sessions": [
        ("", "| Date | Time (EST) | Merchant | Alterations; Done | Items |\n|---|---|---|---|---|\n"
             "| 10/28/2017 | 11:30 PM | Urdesas | 7 | primarily textiles |\n"),
        ("Session List (part 2)", "| 11/05/2017 | 8:00 PM | Phinara | 9 | clothing |\n"),
        ("Platinum", "| Date | Time (EST) | Merchant | Alterations; Done | Items |\n|---|---|---|---|---|\n"
                     "| 11/09/2017 | 8:00 PM | Onem | 3 | weapons |\n"),
    ],
    "Guildfest 445/Alteration sessions": [("Session List > Prime", HEAD + "| 2023-06-18 | Sunday | 1:00pm | Kaavia | 8 | Clothing |\n")],
}
TODAY = date(2026, 11, 3)
ASKED = "What alteration sessions are available during the Hollow Eve Festival 458?"


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = db.connect(s)
    for pid, (title, chunks) in enumerate(PAGES.items(), start=1):
        c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, fetched_revid, indexed_revid, is_redirect,"
                  " page_kind, updated_at) VALUES (?, 0, ?, ?, 5, 5, 5, 0, 'event', 'x')", (pid, title, title.lower()))
        for i, (section, text) in enumerate(chunks):
            c.execute("INSERT INTO chunks(page_id, revid, ord, kind, title, section_path, anchor, text, token_est, content_hash)"
                      " VALUES (?, 5, ?, 'event', ?, ?, ?, ?, 1, ?)",
                      (pid, i, title, section, section.split(" > ")[-1].replace(" ", "_"), text, f"{pid}-{i}"))
    yield c
    c.close()


def test_schedule_questions_are_recognised():
    for q in (ASKED, "when are the alteration merchants on at HE?", "any alterers coming up this week?", "alteration schedule for platinum"):
        assert sessions.wants(q), q
    for q in ("How do alterations work?", "What is required for alterations?", "Can I alter a weapon?", "When is Hollow Eve?"):
        assert not sessions.wants(q), q


def test_the_schedule_asked_about_is_found(conn):
    assert sessions.which_page(conn, ASKED) == "Hollow Eve Festival 458/Alteration sessions"
    assert sessions.which_page(conn, "alteration sessions at hollows eve") == "Hollow Eve Festival 458/Alteration sessions"  # the newest
    assert sessions.which_page(conn, "alteration sessions at HE 454") == "Hollow Eve Festival 454/Alteration sessions"
    assert sessions.which_page(conn, "guildfest alteration merchants") == "Guildfest 445/Alteration sessions"
    assert sessions.which_page(conn, "any alteration sessions coming up?") == "Hollow Eve Festival 458/Alteration sessions"


def test_every_instance_gets_its_own_schedule(conn):
    out = sessions.passage(conn, ASKED, "https://x", TODAY)
    answer = out[0]["answer"]
    assert answer.index("**Prime** [1]") < answer.index("**Platinum** [2]") < answer.index("**The Fallen** [3]: none listed yet.")
    prime = answer[answer.index("**Prime**"):answer.index("**Platinum**")]
    assert prime.index("Coming up:") < prime.index("**Maishi**") < prime.index("**Nedrina**") < prime.index("Already held:")
    assert prime.index("Already held:") < prime.index("**Megaya**") and "(40 alterations)" in prime
    assert "Tue Nov 3, 9:00pm" in prime  # today's session is still to come
    assert "FESTIVAL ALTERATIONS" in answer and "unannounced" in answer
    assert [p["section"] for p in out] == ["Session List > Prime", "Session List > Platinum", "Session List > Platinum"]


def test_one_instance_when_one_is_asked_for(conn):
    answer = sessions.passage(conn, "when are the alteration sessions at hollows eve on platinum?", "https://x", TODAY)[0]["answer"]
    assert "**Platinum**" in answer and "**Prime**" not in answer and "**Nedrina**" in answer


def test_an_old_layout_is_read_by_its_column_names(conn):
    found, _ = sessions.read(conn, "Hollow Eve Festival 425/Alteration sessions")
    assert [s.merchant for s in found["Prime"]] == ["Urdesas", "Phinara"]  # untitled table, and its headerless second part
    assert found["Prime"][0].time == "11:30 PM" and found["Prime"][0].day == "" and found["Platinum"][0].merchant == "Onem"
    answer = sessions.passage(conn, "alteration sessions at HE 425", "https://x", TODAY)[0]["answer"]
    assert "Held:" in answer and "Coming up" not in answer and "FESTIVAL ALTERATIONS" not in answer  # over: nothing to announce
    assert "Sat Oct 28, 11:30 PM: **Urdesas**: primarily textiles (7 alterations)" in answer


def test_a_subpage_named_in_plain_words_is_read_whole(conn):
    assert explicit_pages(conn, ASKED) == ["Hollow Eve Festival 458/Alteration sessions"]
    assert explicit_pages(conn, "what were the raffles at Hollow Eve Festival 458?") == ["Hollow Eve Festival 458/Raffles"]
    assert explicit_pages(conn, "Tell me about Hollow Eve Festival 458") == []  # the festival alone is an ordinary question


def test_the_same_boilerplate_from_another_year_is_a_copy():
    assert _same_shape(_shape(WHERE), _shape(WHERE.replace("Discord", "Discord ")))
    assert not _same_shape(_shape(WHERE), _shape(PAGES["Hollow Eve Festival 458/Alteration sessions"][1][1]))
    assert not _same_shape(_shape("Tasks / Shops"), _shape("Tasks / Shops"))  # too short to say
    assert _newer("Hollow Eve Festival 458", "Hollow Eve Festival 454")
    assert not _newer("Hollow Eve Festival 454", "Hollow Eve Festival 458") and not _newer("Guildfest 445", "Hollow Eve Festival 443")


def test_a_schedule_question_skips_the_planner(conn):
    from epedia.core.assistant import Assistant

    a = Assistant.__new__(Assistant)
    a.conn, a.s = conn, Settings()
    plan = a._early_plan(ASKED, "", [])
    assert plan and plan.kind == "event_news" and "**Platinum**" in plan.prebuilt[0]["answer"]


def test_doctor_checks_the_newest_schedule_reads(conn):
    from epedia.index.doctor import run

    check = {c.name: c for c in run(conn)}["alteration sessions are readable"]
    assert check.ok and "Hollow Eve Festival 458/Alteration sessions: 4 sessions" in check.detail
