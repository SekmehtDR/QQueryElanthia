"""A thumbs-down keeps its own copy of the conversation, and code says where it thinks the answer first went wrong. The shape of the
main test is a real vote: a raffle page named outright, its prize tables never sent, and the page's editing instructions filling the slots."""

import json

import pytest

from epedia.config import Settings
from epedia.core import diagnose, feedback, memory, turns
from epedia.db import connect

BLOCK = "Editing Instructions. To add a new auction, copy the below blank set and paste it at the end of the current list. " * 3
TABLE_1 = "| Item | Winner | Notes |\n|---|---|---|\n| infuser stone | Zemfreed |  |\n| treasure map | Erolith |  |"
TABLE_2 = "| Item | Winner | Notes |\n|---|---|---|\n| dawnbreak diamond | Merend |  |"
PAGE = "Hollow Eve Festival 458/Raffles"


def page(c, pid, title, *texts):
    c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, updated_at) VALUES (?, 0, ?, ?, 1, 'x')", (pid, title, title.lower()))
    for i, text in enumerate(texts):
        c.execute("INSERT INTO chunks(page_id, revid, ord, kind, title, section_path, text, token_est, content_hash) VALUES (?, 1, ?, 'text', ?, '', ?, 10, ?)",
                  (pid, i, title, text, f"{pid}-{i}"))


def said(c, sid, question, planned, answer, passages, verify=None):
    u = memory.add_message(c, sid, "user", question)
    memory.note_plan(c, u, planned, "search", "event_news")
    t = turns.Turn.start(c, sid, u)
    t.chain["passages"] = passages
    a = memory.add_message(c, sid, "assistant", answer, standalone_q=planned, mode="search", kind="event_news", turn_id=t.turn_id)
    t.finish(a, verify=verify)
    return t.turn_id


def block(title):
    return {"title": title, "section": "", "text": BLOCK}


@pytest.fixture
def raffle(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    page(c, 1, PAGE, BLOCK, TABLE_1, TABLE_2)
    page(c, 2, "Hollow Eve Festival 454/Raffles", BLOCK, "| Item | Winner | Notes |\n|---|---|---|\n| old prize | Someone |  |")
    page(c, 3, "Hollow Eve Festival 450/Raffles", BLOCK)
    sid = memory.new_session(c, "web", owner="visitor-1")
    said(c, sid, "What are the raffle prizes at this years HE?", "What are the raffle prizes at Hollow Eve?",
         "The exact prizes are not listed in the notes.", [block(PAGE), block("Hollow Eve Festival 454/Raffles")])
    said(c, sid, "show me the raffles", "Show me the raffles and their outcomes at Hollow Eve.",
         "The prizes are not fully documented.", [block(PAGE)])
    last = said(c, sid, f"Give me a list of the items from here {PAGE.replace(' ', '_')}", "Give me a list of the raffle items from that page.",
                "Here they are:\n- infuser stone\n- treasure map\n- made up thing",
                [block(PAGE), block("Hollow Eve Festival 454/Raffles"), block("Hollow Eve Festival 450/Raffles"),
                 {"title": PAGE, "section": "Raffle 001", "text": TABLE_1, "chunk_id": None}, {"title": "Hollow Eve Festival 454/Raffles", "section": "", "text": "old prize"}])
    yield s, c, sid, last
    c.close()


def test_a_page_named_with_underscores_or_a_slash_is_found_and_the_longest_name_wins(raffle):
    _, c, _, _ = raffle
    page(c, 4, "Hollow Eve Festival", "hub")
    assert diagnose.named_pages(c, f"items from here {PAGE.replace(' ', '_')}?") == [PAGE]
    assert diagnose.named_pages(c, "tell me about the hollow eve festival please") == ["Hollow Eve Festival"]
    assert diagnose.named_pages(c, "no page is named here") == []


def test_the_diagnosis_finds_the_first_failure_and_what_to_try(raffle):
    _, c, _, last = raffle
    feedback.record(c, last, "down", ["missed", "context"], "It was painful to get this list.")
    d = feedback.stored(c, last)["diagnosis"]
    assert d["first_failure"] == "retrieval"
    says = {f["check"]: f["says"] for f in d["findings"]}
    assert "only 2 of its 3 sections were sent" in says["named_page_unread"]
    assert says["other_pages"] == "3 of the 5 passages were from other pages."
    assert says["repeated_block"] == "3 of the 5 passages were the same block of text on different pages."
    declined = [f["says"] for f in d["findings"] if f["check"] == "declined_but_there"]
    assert len(declined) == 2 and "question 1 of 3" in declined[0] and "question 2 of 3" in declined[1]
    assert all("2 sections with tables" in x for x in declined)
    assert "list_from_table" in says and says["asked_again"] == "The person asked about the same thing 3 times in a row."
    assert any("Read a page the question names directly" in x for x in d["suggestions"])
    assert any("code write this answer" in x for x in d["suggestions"])


def test_a_failure_with_no_check_says_it_needs_a_look(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    sid = memory.new_session(c, "web", owner="local")
    t = said(c, sid, "Who is Binu?", "Who is Binu?", "Binu is a Necromancer [1].", [{"title": "Binu", "section": "", "text": "Binu is a Necromancer."}])
    feedback.record(c, t, "down")
    d = feedback.stored(c, t)["diagnosis"]
    assert d["first_failure"] == "unclear" and d["findings"] == [] and "needs a look" in d["suggestions"][0]
    c.close()


def test_the_record_has_every_turn_up_to_the_vote_and_nothing_after(raffle):
    _, c, sid, last = raffle
    feedback.record(c, last, "down")
    said(c, sid, "thanks", "thanks", "You're welcome.", [])
    got = feedback.stored(c, last)
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"] * 3
    assert len(got["earlier"]) == 2 and all(e["chain"]["passages"] for e in got["earlier"])
    assert got["turn"]["chain"]["passages"]


def test_the_record_outlives_the_chat_and_is_still_in_the_queue_and_the_review(raffle):
    _, c, sid, last = raffle
    feedback.record(c, last, "down", ["missed"], "painful")
    memory.delete_session(c, sid)
    assert c.execute("SELECT count(*) FROM turns").fetchone()[0] == 0
    row = next(r for r in feedback.queue(c) if r["turn"]["turn_id"] == last)
    assert row["turn"]["note"] == "painful" and len(row["messages"]) == 6
    listed = feedback.review(c)
    assert [(r["turn_id"], r["chat_deleted"], r["session_id"]) for r in listed] == [(last, True, None)]
    assert listed[0]["diagnosis"]["first_failure"] == "retrieval"
    assert feedback.review(c, owner="local") == [] and len(feedback.review(c, owner="visitors")) == 1
    assert "Give me a list of the items" in feedback.eval_item(c, last)  # and it can still be exported


def test_delete_all_leaves_the_record_too(raffle):
    _, c, _, last = raffle
    feedback.record(c, last, "down")
    assert memory.delete_sessions(c) == 1
    assert feedback.stored(c, last)["turn"]["turn_id"] == last


def test_working_through_a_vote_drops_the_conversation_and_keeps_the_note(raffle):
    _, c, sid, last = raffle
    feedback.record(c, last, "down", ["missed"], "painful")
    memory.delete_session(c, sid)
    assert feedback.mark_processed(c, last, "Fixed in 1.12.0")
    assert feedback.queue(c) == [] and feedback.review(c) == []
    got = feedback.queue(c, include_processed=True)[0]
    assert got["messages"] == [] and got["earlier"] == [] and got["turn"]["chain"] == {}
    assert got["turn"]["processed_note"] == "Fixed in 1.12.0" and got["turn"]["note"] == "painful"


def test_taking_the_vote_back_removes_the_record_and_changing_it_returns_it(raffle):
    _, c, _, last = raffle
    feedback.record(c, last, "down")
    feedback.record(c, last, None)
    assert feedback.stored(c, last) is None and feedback.queue(c) == []
    feedback.record(c, last, "down")
    feedback.mark_processed(c, last, "done")
    feedback.record(c, last, "down", ["wrong"])
    assert feedback.stored(c, last)["turn"]["processed_at"] is None  # a changed vote comes back


def test_a_thumbs_up_keeps_the_conversation_too_without_a_diagnosis(raffle):
    _, c, sid, last = raffle
    feedback.record(c, last, "up")
    memory.delete_session(c, sid)
    got = feedback.queue(c)[0]
    assert got["turn"]["feedback"] == "up" and len(got["messages"]) == 6 and len(got["earlier"]) == 2 and got["diagnosis"] is None
    assert feedback.review(c) == []  # nothing to look at: it is the queue that learns from it


def test_thumbs_downs_come_first_in_the_queue(raffle):
    _, c, sid, last = raffle
    earlier = feedback.conversation(c, last)["earlier"]
    feedback.record(c, earlier[0]["turn_id"], "down", ["wrong"], "first")
    feedback.record(c, last, "up")
    assert [r["turn"]["feedback"] for r in feedback.queue(c)] == ["down", "up"]


def test_the_owners_thumbs_up_does_not_remove_a_visitors_thumbs_down(raffle):
    _, c, _, last = raffle
    feedback.record(c, last, "down")
    feedback.record(c, last, "up", reviewing=True)
    assert feedback.stored(c, last) is not None


def test_a_thumbs_down_from_before_records_is_captured(raffle):
    _, c, _, last = raffle
    c.execute("UPDATE turns SET feedback='down' WHERE turn_id=?", (last,))
    assert feedback.capture_missing(c) == 1 and feedback.capture_missing(c) == 0
    assert feedback.stored(c, last)["diagnosis"]["first_failure"] == "retrieval"


def test_a_failure_while_keeping_the_record_does_not_lose_the_vote(raffle, monkeypatch):
    _, c, _, last = raffle

    def boom(*a):
        raise RuntimeError("no")

    monkeypatch.setattr(diagnose, "diagnose", boom)
    assert feedback.record(c, last, "down")["vote"] == "down"
    assert c.execute("SELECT feedback FROM turns WHERE turn_id=?", (last,)).fetchone()[0] == "down"


def test_the_eval_export_carries_the_earlier_answers_as_comments_only(raffle):
    import yaml

    _, c, _, last = raffle
    text = feedback.eval_item(c, last)
    assert "# assistant: The exact prizes are not listed in the notes." in text
    item = yaml.safe_load(text)[0]
    assert item["history"] == ["What are the raffle prizes at this years HE?", "show me the raffles"] and "answers" not in item


class _Retriever:
    def __init__(self, passages):
        self.passages = passages

    async def retrieve(self, plan, session_id=None):
        self.plan = plan
        return self.passages


@pytest.mark.parametrize("passages,fixed,by_code", [
    ([{"title": PAGE, "text": BLOCK}, {"title": PAGE, "text": TABLE_1}, {"title": PAGE, "text": TABLE_2}], True, False),
    ([{"title": "Hollow Eve Festival 454/Raffles", "text": "x"}], False, False),
    ([{"title": PAGE, "text": "written", "answer": True}], True, True),
])
def test_retesting_says_whether_retrieval_is_fixed_on_this_version(raffle, passages, fixed, by_code):
    import asyncio

    _, c, _, last = raffle
    feedback.record(c, last, "down")
    got = asyncio.run(diagnose.retest(_Retriever(passages), c, feedback.stored(c, last)))
    assert got["looks_fixed"] is fixed and got["written_by_code"] is by_code


def test_retesting_survives_the_model_being_down(raffle):
    import asyncio

    class Down:
        async def retrieve(self, plan, session_id=None):
            raise ConnectionError("ollama is not running")

    _, c, _, last = raffle
    feedback.record(c, last, "down")
    assert "ollama" in asyncio.run(diagnose.retest(Down(), c, feedback.stored(c, last)))["error"]


def test_nothing_in_a_record_is_lost_in_the_round_trip(raffle):
    _, c, _, last = raffle
    feedback.record(c, last, "down")
    assert json.dumps(feedback.stored(c, last), default=str)
