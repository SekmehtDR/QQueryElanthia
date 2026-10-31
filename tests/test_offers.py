"""The closing offer: a page about what the answer is about, not a hub everything links to or a list that mentions it."""

import struct

import pytest

from epedia.config import Settings
from epedia.core import offers
from epedia.db import connect

DIM = 768


def vec(*xs: float) -> bytes:
    v = list(xs) + [0.0] * (DIM - len(xs))
    norm = sum(x * x for x in v) ** 0.5 or 1.0
    return struct.pack(f"{DIM}f", *[x / norm for x in v])


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    yield c
    c.close()


def page(c, pid, title, kind, direction, text="x", ns=0, is_log=0):
    c.execute("INSERT INTO pages(page_id, ns, title, title_norm, updated_at, page_kind, is_log) VALUES (?,?,?,?,?,?,?)",
              (pid, ns, title, title.lower(), "2026-01-01", kind, is_log))
    c.execute("INSERT INTO chunks(chunk_id, page_id, revid, ord, kind, title, section_path, text, token_est, content_hash) "
              "VALUES (?, ?, 1, 0, 'section', ?, '', ?, 1, ?)", (pid, pid, title, text, str(pid)))
    c.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)", (pid, vec(*direction)))


def link(c, a, b, both=True):
    c.execute("INSERT OR IGNORE INTO page_links VALUES (?, ?)", (a, b))
    if both:
        c.execute("INSERT OR IGNORE INTO page_links VALUES (?, ?)", (b, a))


def popular(c, pid, n, start):
    """n filler pages that link to pid: how often a page is linked from elsewhere."""
    for i in range(n):
        page(c, start + i, f"Filler {start + i}", "general", (0, 0, 1))
        link(c, start + i, pid, both=False)


@pytest.fixture
def god(conn):
    """Hodierna's page: a fellow Immortal (links back), a city everything links to, an auction listing her, a log."""
    page(conn, 1, "Hodierna", "lore", (1, 0.1), "Hodierna is the goddess of the Shard and the sister of Damaris.")
    page(conn, 2, "Damaris", "lore", (1, 0.2))
    page(conn, 3, "Shard", "place", (0.6, 0.8))
    page(conn, 4, "Moot 433/Auction", "event", (1, 0.15))
    page(conn, 5, "Hodierna/Logs/Night", "log", (1, 0.1), is_log=1)
    page(conn, 6, "Ring Visions", "general", (1, 0.3))  # an unsorted page she links to, which does not link back
    for other in (2, 3, 4, 5):
        link(conn, 1, other)
    link(conn, 1, 6, both=False)
    popular(conn, 2, 10, 100)
    popular(conn, 3, 450, 1000)  # named in her opening, but linked from everywhere (525 articles on the wiki)
    popular(conn, 4, 10, 500)
    popular(conn, 6, 10, 600)
    return conn


def test_an_offer_is_about_the_same_subject_not_a_hub_a_listing_or_a_log(god):
    cfg = Settings().retrieval
    q = list(struct.unpack(f"{DIM}f", vec(1, 0.1)))
    assert offers.related(god, cfg, "lore", "Hodierna", set(), q) == ["Damaris"]


def test_nothing_is_offered_when_no_candidate_is_near_the_question(god):
    cfg = Settings().retrieval
    far = list(struct.unpack(f"{DIM}f", vec(0, 0, 0, 1)))
    assert offers.related(god, cfg, "lore", "Hodierna", set(), far) == []


def test_pages_already_shown_or_offered_and_unfavoured_kinds_are_not_offered(god):
    cfg = Settings().retrieval
    assert offers.related(god, cfg, "lore", "Hodierna", {"Damaris"}, None) == []
    assert offers.related(god, cfg, "meta", "Hodierna", set(), None) == []
    assert offers.related(god, cfg, "lore", None, set(), None) == []


def test_an_events_booths_a_players_character_and_a_category_answer_are_not_threads(conn):
    page(conn, 1, "Guildfest 416", "event", (1, 0))
    page(conn, 2, "Large bin of miniature figurines", "shop", (1, 0.1))
    page(conn, 3, "Ayrell", "pc", (1, 0.1))
    page(conn, 4, "Arthe Dale", "place", (1, 0.3))
    page(conn, 5, "Category:Guilds", "category", (1, 0), ns=14)
    for other in (2, 3, 4):
        link(conn, 1, other)
    link(conn, 5, 4)
    for pid, start in ((2, 100), (3, 200), (4, 300)):
        popular(conn, pid, 10, start)
    cfg = Settings().retrieval
    assert offers.related(conn, cfg, "event_news", "Guildfest 416", set(), None) == ["Arthe Dale"]
    assert offers.related(conn, cfg, "character", "Guildfest 416", set(), None) == ["Ayrell", "Arthe Dale"]
    assert offers.related(conn, cfg, "general", "Category:Guilds", set(), None) == []


def test_the_replay_puts_then_next_to_now_and_checks_the_labelled_cases(god, tmp_path):
    import asyncio

    from epedia.core import memory
    from epedia.evalx.offers import run_offers

    sid = memory.new_session(god, "web")
    memory.add_message(god, sid, "user", "Who is Hodierna?", standalone_q="Who is Hodierna?")
    memory.add_message(god, sid, "assistant", "A goddess [1].\n\nWant to hear about Shard next?", standalone_q="Who is Hodierna?",
                       kind="lore", sources=[{"n": 1, "title": "Hodierna"}], related=["Shard"], cited=[1])
    cases = tmp_path / "offers.yaml"
    cases.write_text("- q: Who is Hodierna?\n  kind: lore\n  anchor: Hodierna\n  was: Shard\n  must_not: [Shard]\n",
                     encoding="utf-8")

    class Embed:
        async def embed(self, texts, **k):
            return [list(struct.unpack(f"{DIM}f", vec(1, 0.1))) for _ in texts]

    rows, metrics = asyncio.run(run_offers(Settings(), god, Embed(), cases))
    logged, case = rows
    assert (logged["old"], logged["new"], logged["anchor"]) == ("Shard", "Damaris", "Hodierna")
    assert case["ok"] and metrics["cases_ok"] == 1.0 and metrics["changed"] == 1.0


def test_listing_titles_are_recognised():
    from epedia.kinds import OFFER_LISTING_TITLE

    for t in ("Drathrok's Duskruin 430/Auction", "Morum Melgorehn/End loot", "Drathrok's Duskruin 426/Incidental loot",
              "DragonRealms Updates (2013)", "Rakash Moot 442/Merchants", "Hollow Eve Festival 436/Darkbox"):
        assert OFFER_LISTING_TITLE.search(t), t
    for t in ("Damaris", "Moot 433", "Auction of Gwens", "Arthe Dale"):
        assert not OFFER_LISTING_TITLE.search(t), t


def test_a_page_the_opening_names_counts_even_without_a_link_back(conn):
    page(conn, 1, "Svidaw sis", "item", (1, 0), "The svidaw sis (Rakash for \"throwing beater\") was made by the Rakash.")
    page(conn, 2, "Concept:Rakash", "race", (1, 0.2), ns=208)
    page(conn, 3, "Kronars", "general", (1, 0.1))  # linked one way and not named in the opening
    link(conn, 1, 2, both=False)
    link(conn, 1, 3, both=False)
    popular(conn, 2, 10, 100)
    popular(conn, 3, 10, 200)
    assert offers.related(conn, Settings().retrieval, "item", "Svidaw sis", set(), None) == ["Concept:Rakash"]


def test_the_offer_is_chosen_after_the_answer_from_the_page_it_cites(conn, monkeypatch):
    import asyncio

    from epedia.core import memory
    from epedia.core.assistant import Assistant, Done, Retrieval

    passages = [{"title": "Innu", "section": "", "url": "u1", "text": "Innu is a small town by the river.", "chunk_id": None},
                {"title": "Binu", "section": "", "url": "u2", "text": "Binu is a Barbarian who fights trolls.", "chunk_id": None}]

    async def fake_retrieve(self, plan, session_id=None):
        return [dict(p) for p in passages], []

    asked = {}

    def fake_related(conn, cfg, kind, anchor, used, qvec):
        asked.update(anchor=anchor, used=set(used))
        return ["Hodierna"]

    class Model:
        async def chat_json(self, *a, **k):
            return {"standalone_question": "Who is Binu?", "mode": "search", "kind": "character", "follow_up": False}

        async def embed(self, texts, **k):
            return [[0.0] * DIM for _ in texts]

        async def chat_stream(self, *a, **k):
            for w in ("Binu is a Barbarian [2]. ", "Binu fights trolls [2]. ", "Innu is a small town [1]."):
                yield w

    monkeypatch.setattr(Assistant, "retrieve_with_extras", fake_retrieve)
    monkeypatch.setattr(offers, "related", fake_related)
    sid = memory.new_session(conn, "web")

    async def go():
        return [e async for e in Assistant(Settings(), conn, Model()).answer_stream(sid, "Who is Binu?")]

    events = asyncio.run(go())
    (ret,) = [e for e in events if isinstance(e, Retrieval)]
    done = events[-1]
    assert isinstance(done, Done) and asked["anchor"] == "Binu"  # cited twice; the top passage was Innu
    assert {"Innu", "Binu"} <= asked["used"]
    assert ret.related == [] and done.related == ["Hodierna"] and "Hodierna" in done.answer


def test_the_anchor_is_the_page_cited_most_not_the_first_source():
    sources = [{"title": "Innu"}, {"title": "Binu"}, {"title": "Binu: recent edits"}]
    assert offers.anchor_title(sources, [1, 2, 2]) == "Binu"
    assert offers.anchor_title(sources, [2, 1]) == "Binu"  # a tie goes to the one cited first
    assert offers.anchor_title(sources, [3]) == "Innu"  # the edit-history passage is not a page to anchor on
    assert offers.anchor_title(sources, []) == "Innu"
    assert offers.anchor_title([], []) is None
