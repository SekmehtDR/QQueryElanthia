"""Jobs the model does around an answer: a shorter one, questions to ask next, a line on what changed on the wiki, and a thumbs-down
tried again. In each, code decides whether what the model wrote is used, so most of these feed it something it should not get away with."""

import asyncio
import json
import struct

import pytest

from epedia.config import Settings
from epedia.core import followups, lately, memory, replay, rewrite, turns
from epedia.core.assistant import Assistant
from epedia.db import connect
from epedia.llm.ollama import Ollama

FORGING = "Forging is the craft of working metal at an anvil. Quenching hot steel in oil hardens the blade. A steel sword weighs 8 stones."
PASSAGES = [{"title": "Forging", "section": "", "text": FORGING, "url": "http://x/Forging", "chunk_id": None}]
BODY = "Forging is working metal at an anvil [1]. Quenching in oil hardens the blade [1]. A steel sword weighs 8 stones [1]."
NOTE = "\n\n(From a player's log, not official lore.)"


class FakeOllama:
    """Says what the test tells it to, and notes what it was asked."""

    def __init__(self, stream="", data=None):
        self.stream, self.data, self.calls = stream, data or {}, []

    async def chat_stream(self, messages, **_):
        self.calls.append(messages)
        yield self.stream

    async def chat_json(self, messages, schema, **_):
        self.calls.append(messages)
        return self.data

    async def embed(self, texts, **_):
        return [[0.1] * 768 for _ in texts]


def page(c, pid, title, text, kind="general"):
    c.execute("INSERT INTO pages(page_id, ns, title, title_norm, latest_revid, indexed_revid, page_kind, length, updated_at) VALUES (?,0,?,?,1,1,?,?, 'x')",
              (pid, title, title.lower(), kind, len(text) + 400))
    cur = c.execute("INSERT INTO chunks(page_id, revid, ord, kind, title, section_path, text, token_est, content_hash) VALUES (?,1,0,'text',?,'',?,10,?)",
                    (pid, title, text, f"h{pid}"))
    c.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)", (cur.lastrowid, struct.pack("768f", *([0.1] * 768))))


def answered(c, sid="s1", mode="search", kind="crafting", body=BODY, tail=NOTE, question="What is forging?"):
    memory.new_session(c, "web", session_id=sid, owner="local")
    u = memory.add_message(c, sid, "user", question)
    t = turns.Turn.start(c, sid, u)
    t.chain.update(passages=PASSAGES, model_body=body, code_tail=tail)
    t.plan = {"standalone_question": question, "asked": question, "mode": mode, "kind": kind}
    a = memory.add_message(c, sid, "assistant", body + tail, standalone_q=question, mode=mode, kind=kind, turn_id=t.turn_id,
                           sources=[{"n": 1, "title": "Forging"}], cited=[1])
    t.finish(a)
    return t.turn_id


@pytest.fixture
def db(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    page(c, 1, "Forging", FORGING, "crafting")
    yield s, c
    c.close()


def helper(db, model):
    s, c = db
    return Assistant(s, c, model)


def test_questions_use_the_same_context_as_answers_so_the_model_never_reloads_between_them():
    sent = {}

    class Client:
        async def post(self, path, json):
            sent.update(json)

            class R:
                status_code = 200
                text = ""

                def json(self):
                    return {"message": {"content": "{}"}}
            return R()

    s = Settings()
    ol = Ollama(s.ollama)
    real, ol._client = ol._client, Client()
    asyncio.run(ol.chat_json([], {}))
    asyncio.run(real.aclose())
    assert sent["options"]["num_ctx"] == s.ollama.num_ctx


def test_shorter_keeps_codes_notes_word_for_word(db):
    model = FakeOllama("Forging is working metal at an anvil [1].")
    got = asyncio.run(rewrite.shorter(helper(db, model), db[1], answered(db[1])))
    assert got.answer == "Forging is working metal at an anvil [1]." + NOTE and got.cited == [1]
    assert BODY in model.calls[0][1]["content"] and NOTE not in model.calls[0][1]["content"]


def test_a_note_about_a_passage_no_longer_cited_goes_with_it(db):
    tail = "\n\n(OOC) [8] is a player's role-play log, not official lore.\n\nOne caution: check the page."
    got = asyncio.run(rewrite.shorter(helper(db, FakeOllama("Forging is working metal at an anvil [1].")), db[1], answered(db[1], tail=tail)))
    assert "[8]" not in got.answer and got.answer.endswith("One caution: check the page.")


def test_shorter_drops_a_title_that_repeats_the_question(db):
    model = FakeOllama("**What is forging?** [1*]\n\nForging is working metal at an anvil [1].")
    assert asyncio.run(rewrite.shorter(helper(db, model), db[1], answered(db[1], tail=""))).answer == "Forging is working metal at an anvil [1]."


@pytest.mark.parametrize("bad", [
    "A steel sword weighs 99 stones [1].",  # a figure the page does not have
    "Many people enjoy this relaxing hobby on quiet weekends.",  # no citation, and nothing a page says for code to tie it to
    "Forging is what Lord Zarmoth of Kaldermoor teaches at his anvil [1].",  # a name no page has
    BODY,  # no shorter
])
def test_a_result_the_checks_refuse_is_never_shown(db, bad):
    with pytest.raises(rewrite.CannotRewrite):
        asyncio.run(rewrite.shorter(helper(db, FakeOllama(bad)), db[1], answered(db[1])))


def test_shorter_may_not_add_doubts_the_answer_did_not_have(db):
    body = BODY + " Forging also rewards patience at the anvil [1]."
    model = FakeOllama("Dragons taught forging long ago in distant lands [1].")
    with pytest.raises(rewrite.CannotRewrite):
        asyncio.run(rewrite.shorter(helper(db, model), db[1], answered(db[1], body=body)))


def test_an_answer_already_short_is_left_alone_without_asking_the_model(db):
    model = FakeOllama("x")
    with pytest.raises(rewrite.CannotRewrite):
        asyncio.run(rewrite.shorter(helper(db, model), db[1], answered(db[1], body="Forging is working metal at an anvil [1].")))
    assert not model.calls


@pytest.mark.parametrize("mode,kind", [("clock", "meta"), ("search", "meta"), ("clarify", "general"), ("declined", "general")])
def test_only_what_the_model_wrote_about_the_game_can_be_shortened(db, mode, kind):
    model = FakeOllama("anything [1]")
    with pytest.raises(rewrite.CannotRewrite):
        asyncio.run(rewrite.shorter(helper(db, model), db[1], answered(db[1], mode=mode, kind=kind)))
    assert not model.calls


def test_an_answer_from_before_this_version_says_it_cannot_be_shortened(db):
    with pytest.raises(rewrite.CannotRewrite):
        asyncio.run(rewrite.shorter(helper(db, FakeOllama("x")), db[1], answered(db[1], body="")))


def test_followups_keep_only_what_a_search_can_back(db):
    page(db[1], 2, "Quenching", "Quenching in oil hardens hot steel faster than water does.", "crafting")
    model = FakeOllama(data={"questions": [
        "What does quenching in oil do to steel?",  # the pages say
        "How do I bake a blueberry pie for a wedding?",  # nothing on it
        "What is forging?",  # already asked
        "Why?",  # no subject
        "x" * 120 + "?",  # not a chip
        42,  # not even text
    ]})
    assert asyncio.run(followups.suggest(helper(db, model), answered(db[1]))) == ["What does quenching in oil do to steel?"]


def test_followups_need_a_model_answer(db):
    model = FakeOllama(data={"questions": ["What does quenching do to steel?"]})
    assert asyncio.run(followups.suggest(helper(db, model), answered(db[1], kind="meta"))) == []
    assert not model.calls


def synced(c, title, action="edited", at="2999-01-01T00:00:00Z"):
    cur = c.execute("INSERT INTO sync_log(run_at, kind) VALUES (?, 'rev:x')", (at,))
    c.execute("INSERT INTO sync_pages(sync_id, title, action) VALUES (?,?,?)", (cur.lastrowid, title, action))


def test_lately_lists_what_people_would_read_and_one_row_for_a_festival(db):
    c = db[1]
    page(c, 10, "Item:Blue pin", "A pin.", "item")
    page(c, 11, "Some Shop (1)", "Stock.", "shop")
    page(c, 12, "Disambig", "Many.", "disambig")
    page(c, 13, "Tideskin", "Tideskin is a material from sea creatures.", "material")
    for i in range(14, 18):
        page(c, i, f"Hollow Eve Festival 458/Part{i}", "Festival text.", "event")
        synced(c, f"Hollow Eve Festival 458/Part{i}")
    page(c, 18, "Old news", "Old.", "news")
    synced(c, "Old news", at="2001-01-01T00:00:00Z")
    page(c, 19, "Rerendered", "Text.", "creature")
    synced(c, "Rerendered", action="re-rendered")
    for t in ("Item:Blue pin", "Some Shop (1)", "Disambig"):
        synced(c, t)
    synced(c, "Tideskin", action="new")
    picked = [p["title"] for p in lately.changed_pages(c)]
    assert "Tideskin" in picked and sum(t.startswith("Hollow Eve") for t in picked) == 4
    assert not {"Item:Blue pin", "Some Shop (1)", "Disambig", "Old news", "Rerendered"} & set(picked)
    got = lately.recent(c)
    assert got["total"] == 5 and got["checked"] and got["since"]
    festival = next(r for r in got["rows"] if r["label"] == "Hollow Eve Festival 458")
    assert festival["status"] == "4 updated" and [m["label"] for m in festival["members"]] == [f"Part{i}" for i in range(14, 18)]
    assert festival["members"][0]["title"] == "Hollow Eve Festival 458/Part14"


def test_lately_a_page_heads_its_subpages_and_fabrics_share_a_row_but_other_kinds_do_not(db):
    c = db[1]
    page(c, 20, "Moot 458", "The moot.", "event")
    page(c, 21, "Moot 458/Shops", "The shops.", "event")
    page(c, 22, "Abysselle lawn", "Abysselle lawn is a fabric.", "material")
    page(c, 23, "Abysselle fiber", "Abysselle fiber is what the fabric is made from, and rather more is said of it here.", "material")
    page(c, 24, "Shadowed Charger", "A shop.", "place")
    page(c, 25, "Shadowed Hall", "A hall.", "place")
    page(c, 26, "Post:Official-announcements - Avaari - 10/27/2026 - 17:18", "An announcement.", "post")
    page(c, 27, "Post:Official-announcements - Irenos - 10/23/2026 - 10:42", "Another.", "post")
    for t in ("Moot 458", "Moot 458/Shops", "Abysselle lawn", "Abysselle fiber", "Shadowed Charger", "Shadowed Hall",
              "Post:Official-announcements - Avaari - 10/27/2026 - 17:18", "Post:Official-announcements - Irenos - 10/23/2026 - 10:42"):
        synced(c, t, "new")
    c.execute("UPDATE pages SET ns=120 WHERE title LIKE 'Post:%'")
    c.execute("UPDATE pages SET length=length+1000 WHERE title='Abysselle fiber'")
    rows = {r["label"]: r for r in lately.recent(c)["rows"]}
    assert rows["Moot 458"]["title"] == "Moot 458" and [m["label"] for m in rows["Moot 458"]["members"]] == ["Shops"]
    assert rows["Abysselle"]["title"] == "Abysselle fiber" and rows["Abysselle"]["status"] == "2 new"
    assert [m["label"] for m in rows["Abysselle"]["members"]] == ["fiber", "lawn"]
    assert "Shadowed Charger" in rows and "Shadowed Hall" in rows
    post = rows["Official announcement from Avaari"]
    assert post["title"].startswith("Post:") and not post["members"]  # asked for by its whole title, or the page is not read


def test_the_count_lately_gives_is_exactly_the_pages_a_person_can_reach(db):
    c = db[1]
    titles = ["Moot 458", "Moot 458/Shops", "Moot 458/Raffles", "Abysselle lawn", "Abysselle fiber", "Tideskin", "Spiritjet", "Hall of Echoes"]
    for i, t in enumerate(titles):
        page(c, 40 + i, t, f"{t} text.", "material" if " " not in t or t.startswith("Abysselle") else "event")
        synced(c, t, "new")
    got = lately.recent(c)
    reachable = [m["title"] for r in got["rows"] for m in r["members"]]
    reachable += [r["title"] for r in got["rows"] if r["title"] not in reachable]  # a card's own page, when it is not also a button
    assert got["total"] == len(titles) and sorted(reachable) == sorted(titles)


def test_lately_says_how_far_back_its_list_really_goes(db, monkeypatch):
    c = db[1]
    for i, t in enumerate(("Older", "Middle", "Newest")):
        page(c, 30 + i, t, f"{t} text.", "event")
        synced(c, t, "new", at=f"299{i}-01-01T00:00:00Z")
    assert lately.recent(c)["total"] == 3
    monkeypatch.setattr(lately, "MAX_ROWS", 2)
    got = lately.recent(c)
    assert [r["label"] for r in got["rows"]] == ["Newest", "Middle"] and got["total"] == 2 and got["since"].startswith("2991")


def test_a_line_naming_what_its_page_does_not_is_thrown_away_and_not_shown(db):
    c = db[1]
    page(c, 13, "Tideskin", "Tideskin is a material taken from sea creatures, worth 40 kronars.", "material")
    synced(c, "Tideskin", "new")
    asyncio.run(lately.write_missing(c, FakeOllama(data={"summary": "Tideskin is worth 900 kronars."})))
    assert lately.recent(c)["rows"][0]["note"] == ""  # still listed, by name
    model = FakeOllama(data={"summary": "Tideskin is a material from sea creatures."})
    assert asyncio.run(lately.write_missing(c, model)) == 0 and not model.calls  # that revision was judged: not tried again
    c.execute("UPDATE pages SET indexed_revid=2 WHERE title='Tideskin'")
    assert asyncio.run(lately.write_missing(c, model)) == 1
    shown = lately.recent(c)["rows"]
    assert shown[0]["note"] == "Tideskin is a material from sea creatures." and shown[0]["status"] == "new"


def test_a_line_starts_with_its_subject_whatever_the_model_was_told(db):
    c = db[1]
    page(c, 13, "Tideskin", "Tideskin is a material. It lists the shops.", "material")
    synced(c, "Tideskin", "new")
    asyncio.run(lately.write_missing(c, FakeOllama(data={"summary": "This page covers tideskin, a material."})))
    assert lately.recent(c)["rows"][0]["note"] == "Tideskin, a material."


def test_lately_stops_when_someone_is_waiting(db):
    c = db[1]
    page(c, 13, "Tideskin", "Tideskin is a material.", "material")
    synced(c, "Tideskin", "new")
    model = FakeOllama(data={"summary": "Tideskin is a material."})
    assert asyncio.run(lately.write_missing(c, model, idle=lambda: False)) == 0 and not model.calls


def record(c, tid):
    from epedia.core import feedback
    return feedback.conversation(c, tid)


def test_a_replay_answers_again_and_leaves_nothing_behind(db):
    s, c = db
    tid = answered(c, question="Tell me about forging at an anvil")
    before = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("sessions", "messages", "turns")}
    votes = c.execute("SELECT count(*) FROM turns WHERE feedback IS NOT NULL").fetchone()[0]
    model = FakeOllama("Forging is working metal at an anvil [1].", {"standalone_question": "Tell me about forging at an anvil", "kind": "crafting", "mode": "search"})
    got = asyncio.run(replay.replay(Assistant(s, c, model), c, record(c, tid)))
    assert "error" not in got and got["answer"].startswith("Forging is working metal") and got["was"].startswith("Forging is working metal")
    assert before == {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    assert votes == c.execute("SELECT count(*) FROM turns WHERE feedback IS NOT NULL").fetchone()[0]
    assert c.execute("SELECT count(*) FROM sessions WHERE frontend=?", (replay.FRONTEND,)).fetchone()[0] == 0


def test_a_replay_that_fails_says_so_and_still_cleans_up(db):
    s, c = db
    tid = answered(c)

    class Broken(FakeOllama):
        async def chat_json(self, *a, **k):
            raise RuntimeError("Ollama is not running")

        async def embed(self, *a, **k):
            raise RuntimeError("Ollama is not running")

    got = asyncio.run(replay.replay(Assistant(s, c, Broken()), c, record(c, tid)))
    assert "Ollama" in got["error"]
    assert c.execute("SELECT count(*) FROM sessions WHERE frontend=?", (replay.FRONTEND,)).fetchone()[0] == 0


def test_a_replay_clears_one_stopped_halfway(db):
    c = db[1]
    memory.new_session(c, replay.FRONTEND, session_id="stale", owner=replay.FRONTEND)
    replay.clear_leftovers(c)
    assert not memory.session_exists(c, "stale")


@pytest.mark.parametrize("tried,still,reached,says", [
    (5, 3, 5, "wider"), (5, 0, 1, "depends on the wording"), (5, 0, 4, "narrow"), (5, 1, 3, "narrow"), (0, 0, 0, "unclear"),
])
def test_what_asking_other_ways_says(tried, still, reached, says):
    assert replay.verdict(tried, still, reached).startswith(says)


def test_rewordings_go_through_the_planner_and_search(db):
    s, c = db
    tid = answered(c, question="What is forging?")
    model = FakeOllama(data={"questions": ["How does forging work?", "What does forging involve?"], "standalone_question": "How does forging work?", "kind": "crafting", "mode": "search"})
    got = asyncio.run(replay.rewordings(Assistant(s, c, model), c, record(c, tid)))
    assert got["tried"] == 2 and got["read_that_page"] == 2 and got["verdict"].startswith("narrow")
    assert all("Forging" in x["pages"] for x in got["questions"])
    json.dumps(got)  # it goes into queue.json


@pytest.fixture
def web(db, monkeypatch):
    from fastapi.testclient import TestClient

    from epedia.frontends.web import app as web_app
    from epedia.frontends.web.app import create_app

    s, c = db
    s.web.auto_sync_minutes = 0
    s.web.lately_minutes = 0
    monkeypatch.setattr(web_app, "Ollama", lambda cfg: _Closing("Forging is shaping metal at an anvil [1].", {"questions": ["What is quenching in oil at an anvil?"]}))
    with TestClient(create_app(s), headers={"Host": "localhost:8765", "X-QQE": "1"}) as client:
        yield client, c


class _Closing(FakeOllama):
    async def aclose(self):
        pass


def test_the_page_can_ask_for_a_shorter_answer_and_for_questions(web):
    client, c = web
    tid = answered(c)
    got = client.post(f"/api/turns/{tid}/shorter").json()
    assert got["ok"] and got["answer"].endswith(NOTE) and got["cited"] == [1]
    assert "questions" in client.post(f"/api/turns/{tid}/followups").json()
    got = client.get("/api/lately").json()
    assert got["rows"] == [] and got["total"] == 0


def test_a_visitor_cannot_reach_someone_elses_answer(web):
    client, c = web
    tid = answered(c)
    visitor = {"cf-connecting-ip": "203.0.113.9", "x-forwarded-for": "203.0.113.9"}
    assert client.post(f"/api/turns/{tid}/shorter", headers=visitor).status_code in (401, 403, 404)
    assert client.post(f"/api/turns/{tid}/followups", headers=visitor).status_code in (401, 403, 404)
    assert client.post("/api/turns/999999/followups").status_code == 404


def test_the_page_uses_them():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "src/epedia/frontends/web/static/index.html").read_text(encoding="utf-8")
    assert "function addShorter(" in html and "function addFollowups(" in html and "function loadLately(" in html
    assert "data.rewritable" in html and "'/api/lately'" in html
