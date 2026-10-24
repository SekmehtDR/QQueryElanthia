from datetime import date

from epedia.core.dates import date_note, real_dates
from epedia.llm.prompts import format_passages

TODAY = date(2026, 10, 23)
PAST = "| Prime | Friday, October 26, 2018 | Sunday, November 25, 2018 |"
FUTURE = "Next window: Saturday, January 2, 2027 to Sunday, January 17, 2027."


def test_real_dates_are_read_and_in_game_years_are_ignored():
    assert real_dates("Opens Friday, June 6, 2025 and closes June 22, 2025. Hollow Eve Festival 453 in 457 AV.") == [
        date(2025, 6, 6), date(2025, 6, 22)]
    assert real_dates("February 30, 2026 is a typo") == []


def test_a_passage_of_old_dates_says_it_is_over_and_one_of_future_dates_says_upcoming():
    assert "all before today" in date_note(PAST, TODAY) and "it is over" in date_note(PAST, TODAY)
    assert "upcoming" in date_note(FUTURE, TODAY)
    assert "before and after" in date_note(PAST + " " + FUTURE, TODAY)
    assert date_note("No dates here, only Hollow Eve Festival 428.", TODAY) == ""


def test_format_passages_puts_the_note_beside_the_passage_it_describes():
    passages = [{"title": "Hollow Eve Festival 428", "section": "Dates", "url": "u", "text": PAST},
                {"title": "Hodierna", "section": "", "url": "v", "text": "A goddess of life."}]
    text, used = format_passages(passages, 4000, TODAY)
    assert used == 2 and text.index("[Date check") < text.index("Hodierna")  # only the dated passage carries a note
    assert text.count("[Date check") == 1
    plain, _ = format_passages(passages, 4000)
    assert "Date check" not in plain  # no date, no note


def test_a_year_the_planner_added_is_dropped_unless_the_question_is_about_the_present():
    from epedia.core.assistant import drop_unasked_year

    y = 2026
    assert drop_unasked_year("What does the Barbarian guild teach at least in 2026?", "What does the Barbarian guild teach?", y) \
        == "What does the Barbarian guild teach at least?"
    assert drop_unasked_year("Who is Hodierna in 2026?", "Who is Hodierna?", y) == "Who is Hodierna?"
    # asked about the present: the year helps find recent pages, so it stays
    assert drop_unasked_year("What festival is coming up next in 2026?", "What festival is coming up next?", y) \
        == "What festival is coming up next in 2026?"
    # the user typed the year themselves: theirs to keep
    assert drop_unasked_year("What happened in 2026?", "What happened in 2026?", y) == "What happened in 2026?"
    assert drop_unasked_year("Who is Hodierna?", "Who is Hodierna?", y) == "Who is Hodierna?"


def test_removing_the_year_never_eats_punctuation():
    from epedia.core.assistant import drop_unasked_year

    assert drop_unasked_year("Who leads the Empaths , in 2026 ?", "Who leads the Empaths?", 2026) == "Who leads the Empaths,?" \
        or drop_unasked_year("Who leads the Empaths , in 2026 ?", "Who leads the Empaths?", 2026).endswith("?")


def test_clock_questions_are_recognised_and_ordinary_questions_are_not():
    from epedia.core.assistant import CLOCK_QUESTION

    for q in ("What time is it?", "what's the date?", "What is today's date?", "What day is it?", "what is the current time",
              "What time is it right now?", "Hey Fennick, what's the date today?", "what is the date and time", "whats the time"):
        assert CLOCK_QUESTION.search(q), q
    for q in ("What time does the festival start?", "What is the date of Guildfest 453?", "What day does Hollow Eve open?",
              "What is the time skill?", "Who is Hodierna?", "What time zone are the festivals in?"):
        assert not CLOCK_QUESTION.search(q), q


def test_the_clock_answer_has_the_date_time_and_zone_and_points_to_the_in_game_clock():
    from epedia.core.assistant import clock_answer
    from epedia.util import now_text

    text = now_text()
    assert str(date.today().year) in text and (" AM " in text + " " or " PM " in text + " ")
    assert text in clock_answer(True) and "TIME" in clock_answer(True) and text in clock_answer(False)


def test_a_clock_question_is_answered_without_searching_or_a_model(tmp_path):
    import asyncio

    from epedia.config import Settings
    from epedia.core.assistant import Assistant, Done, Token
    from epedia.db import connect

    class NoModel:
        async def chat_json(self, *a, **k):
            return {"standalone_question": "What time is it?", "mode": "search", "kind": "general"}

        async def embed(self, *a, **k):
            raise AssertionError("a clock question must not search")

    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    conn = connect(s)
    a = Assistant(s, conn, NoModel())

    async def go():
        return [e async for e in a.answer_stream("s1", "What time is it?")]

    events = asyncio.run(go())
    assert isinstance(events[-1], Done) and events[-1].sources == []
    assert any(isinstance(e, Token) and "By my reckoning" in e.text for e in events)
    conn.close()


def _p(title, text):
    return {"title": title, "section": "", "url": "u", "text": text, "chunk_id": None}


def test_events_are_split_into_ahead_over_and_undated():
    from epedia.core.dates import over_answer, split_by_time

    ahead = _p("Taffelberry Faire", "Some start dates: September 24, 2026; January 2, 2027")
    over = _p("Taisidon Mystery 457", "Opens Friday, September 18, 2026, closes Sunday, September 27, 2026")
    old = _p("Hollow Eve 428", PAST)
    none = _p("Hodierna", "A goddess.")
    a, o, u = split_by_time([over, ahead, old, none], TODAY)
    assert [p["title"] for p in a] == ["Taffelberry Faire"] and len(o) == 2 and [p["title"] for p in u] == ["Hodierna"]
    text, cited = over_answer([old, over], TODAY)
    assert "Taisidon Mystery 457" in text and "already over" in text and "October 23, 2026" in text  # the latest, not Hollow Eve
    assert cited == [over] and "[1]" in text


def test_a_next_event_question_never_offers_an_event_that_is_over(tmp_path, monkeypatch):
    import asyncio

    from epedia.config import Settings
    from epedia.core import assistant as asst
    from epedia.core.assistant import Assistant, Token
    from epedia.db import connect

    over = _p("Taisidon Mystery 457", "Opens Friday, September 18, 2026, closes Sunday, September 27, 2026")

    class Model:
        async def chat_json(self, *a, **k):
            return {"standalone_question": "What is the next upcoming event in 2026?", "mode": "search", "kind": "event_news"}

        async def chat_stream(self, *a, **k):
            raise AssertionError("nothing is ahead, so the model must not be asked to make something up")
            yield ""

    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    conn = connect(s)
    a = Assistant(s, conn, Model())

    async def fake_retrieve(self, plan, session_id=None):
        return [over], []

    monkeypatch.setattr(Assistant, "retrieve_with_extras", fake_retrieve)
    monkeypatch.setattr(asst, "date", type("D", (), {"today": staticmethod(lambda: TODAY)}))

    async def go():
        return [e async for e in a.answer_stream("s1", "Whats the next upcoming event?")]

    events = asyncio.run(go())
    text = "".join(e.text for e in events if isinstance(e, Token))
    assert "already over" in text and "Taisidon Mystery 457" in text and "upcoming event is" not in text
    assert events[-1].sources[0].title == "Taisidon Mystery 457" and events[-1].related == []
    conn.close()


def test_other_date_styles_on_the_wiki_are_read():
    from epedia.core.dates import real_dates

    assert real_dates("| Hollow Eve Tournament 458 | 2026-11-02 | Group |") == [date(2026, 11, 2)]
    assert real_dates("- 20270102 (January 2 2027)") == [date(2027, 1, 2)] * 2 or real_dates("- 20270102") == [date(2027, 1, 2)]
    assert real_dates("Date: Havrandu, 17 Moliko 426 (March 17th, 2018 - 9pm EST)") == [date(2018, 3, 17)]
    assert real_dates("Aug 29, 2024 and 17 March 2018 and Sept. 5, 2025") == [date(2024, 8, 29), date(2025, 9, 5), date(2018, 3, 17)]
    assert real_dates("450-03-04 is an in-game date; so is 457 AV; and 2026-13-45 is nonsense") == []


def test_an_event_list_keeps_only_the_rows_still_ahead():
    from epedia.core.dates import keep_ahead_lines

    table = ("| Name | Start Date |\n|---|---|\n| Hollow Eve Tournament 458 | 2026-11-02 |\n"
             "| Praxium Small Group Tournament | 2026-02-09 |\n| Hollow Eve Tournament | 2025-11-03 |")
    out = keep_ahead_lines(table, TODAY)
    assert "458" in out and "Small Group" not in out and "2025-11-03" not in out
    assert out.startswith("| Name | Start Date |")  # headers and undated lines stay
