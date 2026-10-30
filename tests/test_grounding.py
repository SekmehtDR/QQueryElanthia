"""The grounding gate: a question about something the wiki never mentions is declined in code, without the model;
real questions, typos and incidental unknown words are not. Plus the retrieval fixes that came with it."""

import asyncio
import struct

import pytest

from epedia.config import Settings
from epedia.core import grounding, memory
from epedia.core.assistant import Assistant, Done, Retrieval, Token
from epedia.db import connect
from epedia.evalx.faithfulness import is_refusal
from epedia.retrieval.search import search

PAGES = {
    1: ("Binu", "Binu is a Human Necromancer in the Prime instance. He has greenish granite-like skin."),
    2: ("Musk hog", "Musk hogs are found in the Crossing hog pens and weigh 40 stones."),
    3: ("Moon Mage", "Moon Mages read the stars and cast spells of the moons. Their guild is in the Crossing."),
    4: ("Hollow Eve Festival", "The Hollow Eve Festival is held in autumn on the Hollow Eve island."),
    # the real wiki has every everyday word somewhere; a five-page test index needs them spelled out
    5: ("New players", "Be sure to live well with friends; every character asks where things are."),
}


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    for pid, (title, text) in PAGES.items():
        c.execute("INSERT INTO pages(page_id, ns, title, title_norm, updated_at, page_kind) VALUES (?,0,?,?,?, 'general')",
                  (pid, title, title.lower(), "2026-01-01"))
        c.execute("INSERT INTO chunks(chunk_id, page_id, revid, ord, kind, title, section_path, text, token_est, "
                  "content_hash) VALUES (?,?,1,0,'section',?,'',?,10,?)", (pid, pid, title, text, str(pid)))
        c.execute("INSERT INTO titles_fts(title_norm, page_id, is_redirect) VALUES (?,?,0)", (title.lower(), pid))
        vec = [0.0] * 768
        vec[pid] = 1.0
        c.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)", (pid, struct.pack("768f", *vec)))
    yield c
    c.close()


def test_names_are_read_without_the_words_that_open_the_question():
    assert grounding.names_in("Who is Zarnak the Unbound?") == ["Zarnak the Unbound"]
    assert grounding.names_in("Where is Mistral Cove?") == ["Mistral Cove"]
    assert grounding.names_in("What can you tell me about Tirost's message?") == ["Tirost"]
    assert grounding.names_in("Binu has a brother?") == []  # the first word alone is never a name: questions start capitalised
    assert grounding.names_in("Tell me about the Moon Mage guild") == ["Moon Mage"]
    assert grounding.names_in("what is a musk hog") == []


def test_a_name_the_wiki_never_mentions_is_declined(conn):
    for q in ("Who is Zarnak the Unbound?", "Where is Mistral Cove?", "What does the spell Moonwraith Veil do?",
              "who is zarnak the unbound"):  # lower case: the unknown word gives it away
        g = grounding.assess(conn, q)
        assert g.level == "none", (q, g)
    text = grounding.decline(grounding.assess(conn, "Who is Zarnak the Unbound?"), persona=True)
    assert "Zarnak the Unbound" in text and is_refusal(text)
    assert "Zarnak the Unbound" in grounding.decline(grounding.assess(conn, "Who is Zarnak the Unbound?"), persona=False)


def test_real_questions_typos_and_incidental_unknown_words_are_answered(conn):
    for q in ("Who is Binu?", "Where do musk hogs live?", "Is Binu a Moon Mage?", "What is the Hollow Eve Festival?",
              "Are you sure it's not hollowseve and that Binu is a necromancer?",  # one typo among real words
              "My character Jobrien asks: where do musk hogs live?"):  # the asker's own name is incidental
        g = grounding.assess(conn, q)
        assert g.level == "answer", (q, g)
    # one unknown name among real ones in a question that asks more: answered, the missing name recorded
    g = grounding.assess(conn, "Is Binu friends with Zarnak, the Moon Mage from the Crossing?")
    assert g.level == "answer" and g.missing_names == ["Zarnak"]
    assert grounding.assess(conn, "Is Binu friends with Zarnak?").level == "none"  # Zarnak is a third of what it asks


def test_an_empty_index_never_declines(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "empty.db")
    c = connect(s)
    assert grounding.assess(c, "Who is Zarnak the Unbound?").level == "answer"
    c.close()


class Model:
    def __init__(self):
        self.streamed = 0

    async def chat_json(self, messages, schema, **k):
        q = messages[-1]["content"].rsplit("Latest user message: ", 1)[-1].split("\n")[0]
        return {"standalone_question": q, "mode": "search", "kind": "character", "follow_up": False}

    async def embed(self, texts, **k):
        vec = [0.0] * 768
        vec[1] = 1.0
        return [vec for _ in texts]

    async def chat_stream(self, *a, **k):
        self.streamed += 1
        yield "Binu is a Human Necromancer [1]."


def run(a, sid, q):
    async def go():
        return [e async for e in a.answer_stream(sid, q)]

    return asyncio.run(go())


def test_the_gate_declines_without_the_model_and_records_why(conn):
    import json

    model = Model()
    a = Assistant(Settings(), conn, model)
    events = run(a, memory.new_session(conn, "web"), "Who is Zarnak the Unbound?")
    assert model.streamed == 0 and isinstance(events[0], Retrieval) and events[0].mode == "declined"
    assert "Zarnak the Unbound" in "".join(e.text for e in events if isinstance(e, Token))
    row = conn.execute("SELECT grounding_json, error FROM turns ORDER BY turn_id DESC LIMIT 1").fetchone()
    assert json.loads(row[0])["missing_names"] == ["Zarnak the Unbound"] and row[1] is None

    assert events[-1].by_code and events[-1].verification is None  # code wrote it: nothing to verify

    events = run(a, memory.new_session(conn, "web"), "Who is Binu?")
    done = events[-1]
    assert model.streamed == 1 and isinstance(done, Done) and done.cited == [1] and not done.by_code
    row = conn.execute("SELECT grounding_json, verify_json FROM turns ORDER BY turn_id DESC LIMIT 1").fetchone()
    rec = json.loads(row[0])
    assert rec["level"] == "answer" and rec["best_cos"] == 0.0 and rec["lexical"] == 1.0  # recorded, not used
    # the answer is verified against the passages it was written from, and the result recorded with the turn
    assert done.verification["cited_units"] == 1 and done.verification["unsupported"] == []
    assert json.loads(row[1]) == done.verification


def test_search_reports_cosine_distance_and_honours_k_final_zero(conn):
    s = Settings()
    q = [0.0] * 768
    q[2] = 1.0
    hits = search(conn, "musk hogs", s.retrieval, s.wiki.base_url, q, k_final=10)
    assert hits[0].title == "Musk hog" and hits[0].vec_dist == pytest.approx(0.0, abs=1e-6)
    far = next(h for h in hits if h.title == "Binu")
    assert far.vec_dist == pytest.approx(1.0, abs=1e-6)  # orthogonal unit vectors: cosine distance 1
    assert search(conn, "musk hogs", s.retrieval, s.wiki.base_url, q, k_final=0) == []  # 0 used to mean "8"


def test_the_planner_prompt_lists_every_kind_from_kinds_py():
    from epedia.kinds import KIND_DESCRIPTIONS, QUESTION_KINDS
    from epedia.llm.prompts import ROUTER_SYSTEM

    assert set(KIND_DESCRIPTIONS) == set(QUESTION_KINDS)
    for k in QUESTION_KINDS:
        assert f'  "{k}": {KIND_DESCRIPTIONS[k]}' in ROUTER_SYSTEM
    assert "{kind_lines}" not in ROUTER_SYSTEM and "{property_catalog}" in ROUTER_SYSTEM


def test_doctor_checks_vectors_are_unit_length(conn):
    from epedia.index import doctor

    check = next(c for c in doctor.run(conn) if c.name == "vectors are unit length")
    assert check.ok
    conn.execute("UPDATE chunk_vec SET embedding=? WHERE chunk_id=1", (struct.pack("768f", *([0.5] * 768)),))
    assert not next(c for c in doctor.run(conn) if c.name == "vectors are unit length").ok
