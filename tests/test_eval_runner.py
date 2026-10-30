"""The evaluation harness: refusals recognised, the model's own text separated from code's, scripts run end to end
through a real session and leave nothing behind, and runs saved and compared."""

import asyncio
import json

import pytest

from epedia.config import Settings
from epedia.core.assistant import Assistant
from epedia.db import connect
from epedia.evalx import faithfulness, runner
from epedia.llm import prompts


@pytest.fixture
def conn(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    c = connect(s)
    yield c
    c.close()


def test_refusals_are_recognised_and_a_partial_note_is_not_one():
    assert faithfulness.is_refusal(prompts.NO_PASSAGES_ANSWER)
    assert faithfulness.is_refusal("The wiki doesn't seem to cover that.")
    assert faithfulness.is_refusal("I have no page called Zarnak the Unbound in my satchel.")
    assert not faithfulness.is_refusal("Musk hogs live in the Crossing [1].\n\nI couldn't find every part of that.")
    assert not faithfulness.is_refusal("Musk hogs live in the Crossing [1].\n\nI couldn't find anything on their diet.")


def test_the_models_text_is_scored_without_what_code_appended():
    answer = ("Binu is a Necromancer [1].\n\nThat said, my notes first had Binu's guild as Warrior Mage (2017).\n\n"
              "Want to hear about Necromancer next?")
    assert faithfulness.model_text(answer) == "Binu is a Necromancer [1]."


def test_aggregate_turns_answer_records_into_run_numbers():
    rows = [{"cited_units": 4, "factual_units": 5, "unsupported": ["x"], "unknown_entities": [], "fake_actions": [],
             "numbers_bad": 1, "seconds": 10.0},
            {"cited_units": 0, "factual_units": 3, "unsupported": [], "unknown_entities": ["Moonwraiths"],
             "fake_actions": ["Let me check."], "numbers_bad": 0, "seconds": 20.0}]
    m = faithfulness.aggregate(rows)
    assert m["citation_validity"] == 0.75 and m["cited_share"] == 0.5
    assert m["answers_with_unknown_names"] == 0.5 and m["answers_with_fake_actions"] == 0.5
    assert m["seconds_p50"] == 15.0 and faithfulness.aggregate([]) == {}


class Model:
    def __init__(self, answers):
        self.answers = list(answers)

    async def chat_json(self, messages, schema, **k):
        q = messages[-1]["content"].rsplit("Latest user message: ", 1)[-1].split("\n")[0]
        return {"standalone_question": q, "mode": "search", "kind": "creature", "follow_up": False}

    async def embed(self, texts, **k):
        return [[0.0] * 768 for _ in texts]

    async def chat_stream(self, *a, **k):
        yield self.answers.pop(0)


def test_scripts_run_end_to_end_and_report_each_turns_problems(conn, tmp_path, monkeypatch):
    async def fake_retrieve(self, plan, session_id=None):
        return [{"title": "Musk hog", "section": "", "url": "u", "text": "Musk hogs live in the Crossing hog pens.",
                 "chunk_id": None}], []

    monkeypatch.setattr(Assistant, "retrieve_with_extras", fake_retrieve)
    script = tmp_path / "s.yaml"
    script.write_text(
        "- name: hogs\n  turns:\n"
        "    - q: Where do musk hogs live?\n      cited: true\n      answer_has: [crossing]\n      max_unknown: 0\n"
        "    - q: What do they eat?\n      cited: true\n      max_unknown: 0\n", encoding="utf-8")
    model = Model(["Musk hogs live in the Crossing hog pens [1].",
                   "Let me check that again. The Moonwraiths guard their pens.",
                   "Let me check once more. The Moonwraiths still guard their pens."])  # the rewrite fails too
    rows, m = asyncio.run(runner.run_scripts(Settings(), conn, model, script))
    assert rows[0]["problems"] == []
    second = " ".join(rows[1]["problems"])
    assert "no citation" in second and "Moonwraiths" in second and "pretends to look" in second
    assert model.answers == [] and m["answers_revised"] == 0.5  # the second answer was rewritten once
    assert m["script_turn_pass"] == 0.5 and m["scripts_clean"] == 0.0
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0  # nothing left behind
    assert conn.execute("SELECT count(*) FROM turns").fetchone()[0] == 0


def test_runs_are_saved_and_the_next_one_is_compared_with_it(tmp_path):
    s = Settings()
    path, before = runner.save_run("full", {"mrr": 0.9}, [{"q": "x"}], s, tmp_path)
    assert before is None and json.loads(path.read_text(encoding="utf-8"))["metrics"] == {"mrr": 0.9}
    import time

    time.sleep(1.1)  # the file name carries the second
    _, before = runner.save_run("full", {"mrr": 0.95}, [], s, tmp_path)
    assert before == {"mrr": 0.9}
    assert runner.deltas({"mrr": 0.95, "new": 1.0}, before) == {"mrr": "0.950 (+0.050)", "new": "1.000"}
    _, other = runner.save_run("retrieval", {"mrr": 0.5}, [], s, tmp_path)
    assert other is None  # a different suite is not compared
