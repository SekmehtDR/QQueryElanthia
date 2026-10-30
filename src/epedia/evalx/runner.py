"""Evaluation: retrieval (hit@k, MRR, what reaches the prompt), planner mode and kind accuracy, and with `--full` the
answers themselves (citations borne out, invented names, pretended lookups, refusals, multi-turn scripts, latency).
Every run is saved under eval/runs/ so the next one can say what changed."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from ..config import Settings
from ..core import memory
from ..core.assistant import Assistant, Done, Retrieval
from ..llm import prompts
from ..llm.ollama import Ollama
from . import faithfulness

# Reviewer's checklist for judging an answer's voice by hand (score each 0 or 1; aim for 5 of 6).
VOICE_CHECKLIST = (
    "voice check: (1) direct answer in the first two sentences  (2) every factual sentence has a valid [n]  "
    "(3) two spot-checked facts really appear in the cited passages  (4) the wiki's hedges (outdated, obsolete, "
    "incomplete, era, source tier) are relayed  (5) mechanics plain, (OOC) at most once, commands in CAPS  "
    "(6) at most one closing offer, taken from the related list, none when it is empty"
)
RUNS_DIR = Path("eval/runs")


@dataclass
class Result:
    q: str
    standalone: str
    mode: str
    expected_mode: str
    expect: list[str]
    titles: list[str]
    rank: int | None
    kind: str = "general"
    expected_kind: str | None = None
    answer: str | None = None
    related: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    in_prompt: bool | None = None  # did an expected page survive the cut to the prompt's passage budget?
    truth: dict | None = None  # faithfulness.check_done(...) of the answer, with --full


@dataclass
class Answered:
    done: Done
    mode: str
    seconds: float


async def answer(assistant: Assistant, sid: str, q: str) -> Answered:
    """One real turn through answer_stream (planner, gates, retrieval, model), timed."""
    t0 = time.perf_counter()
    mode, done = "search", None
    async for ev in assistant.answer_stream(sid, q):
        if isinstance(ev, Retrieval):
            mode = ev.mode
        elif isinstance(ev, Done):
            done = ev
    assert done is not None
    return Answered(done, mode, time.perf_counter() - t0)


def _throwaway(conn: sqlite3.Connection) -> str:
    """A session that must not see, or leave behind, real chat history (deleted with its turns afterwards)."""
    return memory.new_session(conn, "eval", f"eval-{uuid.uuid4().hex[:10]}")


async def run_eval(
    settings: Settings, conn: sqlite3.Connection, ollama: Ollama, path: Path, ks: list[int], answers: bool,
    full: bool = False,
) -> tuple[list[Result], dict[str, float]]:
    questions = yaml.safe_load(path.read_text(encoding="utf-8"))
    assistant = Assistant(settings, conn, ollama)
    results: list[Result] = []
    k_max = max(ks)
    truths: list[dict] = []
    for item in questions:
        hist: list[dict[str, str]] = []
        for earlier in item.get("history", []):
            hist += [{"role": "user", "content": earlier}, {"role": "assistant", "content": "(answered)"}]
        plan = await assistant.plan(hist, item["q"])
        old_k = settings.retrieval.k_final
        settings.retrieval.k_final = k_max
        try:
            passages = await assistant.retrieve(plan)
        finally:
            settings.retrieval.k_final = old_k
        titles: list[str] = []
        for p in passages:
            if p["title"] not in titles:
                titles.append(p["title"])
        expect = item.get("expect_pages", [])
        want = {t.lower() for t in expect}
        rank = next((i + 1 for i, t in enumerate(titles) if t.lower() in want), None)
        res = Result(item["q"], plan.standalone_question, plan.mode, item.get("mode", "search"), expect, titles, rank,
                     kind=plan.kind, expected_kind=item.get("kind"))
        if expect:  # what the model would really be shown: the live k, cut to the passage budget
            live = await assistant.retrieve(plan)
            _, used = prompts.format_passages(live, settings.chat.context_token_budget, date.today())
            res.in_prompt = any(p["title"].lower() in want for p in live[:used])
        if answers or full:
            sid = _throwaway(conn)
            try:
                got = await answer(assistant, sid, item["q"])
                res.answer, res.related = got.done.answer, got.done.related
                if full:
                    res.truth = {**faithfulness.check_done(conn, got.done, plan.standalone_question, item["q"],
                                                           " ".join(item.get("history", []))),
                                 "seconds": got.seconds}
                    truths.append(res.truth)
            finally:
                memory.delete_session(conn, sid)
        results.append(res)

    scored = [r for r in results if r.expect]
    metrics: dict[str, float] = {"questions": float(len(results)), "scored": float(len(scored))}
    for k in ks:
        metrics[f"hit@{k}"] = sum(1 for r in scored if r.rank and r.rank <= k) / max(len(scored), 1)
    metrics["mrr"] = sum(1 / r.rank for r in scored if r.rank) / max(len(scored), 1)
    metrics["hit_in_prompt"] = sum(1 for r in scored if r.in_prompt) / max(len(scored), 1)
    metrics["mode_accuracy"] = sum(1 for r in results if r.mode == r.expected_mode) / max(len(results), 1)
    labelled = [r for r in results if r.expected_kind]
    if labelled:
        metrics["kind_accuracy"] = sum(1 for r in labelled if r.kind == r.expected_kind) / len(labelled)
    if truths:
        metrics.update({f"answer_{k}": v for k, v in faithfulness.aggregate(truths).items()})
        metrics["false_refusal_rate"] = sum(1 for r in scored if r.truth and r.truth["refused"]) / max(len(scored), 1)
    return results, metrics


async def run_refusals(settings: Settings, conn: sqlite3.Connection, ollama: Ollama, path: Path) -> tuple[list[dict], dict]:
    """Questions that must NOT be answered from the wiki: not about DragonRealms, about things the wiki does not have,
    or asking for invention. Correct when the answer declines and names nothing no passage has."""
    items = yaml.safe_load(path.read_text(encoding="utf-8"))
    assistant = Assistant(settings, conn, ollama)
    rows = []
    for item in items:
        sid = _throwaway(conn)
        try:
            got = await answer(assistant, sid, item["q"])
            truth = faithfulness.check_done(conn, got.done, item["q"], item["q"])
            ok = truth["refused"]
            rows.append({"q": item["q"], "why": item.get("why", ""), "ok": ok, "answer": got.done.answer,
                         "unknown": truth["unknown_entities"], "seconds": got.seconds})
        finally:
            memory.delete_session(conn, sid)
    metrics = {"refusals": float(len(rows)), "refusal_recall": sum(r["ok"] for r in rows) / max(len(rows), 1)}
    return rows, metrics


def _turn_problems(turn: dict, got: Answered, truth: dict) -> list[str]:
    problems = []
    text = got.done.answer.casefold()
    if "mode" in turn and got.mode != turn["mode"]:
        problems.append(f"mode={got.mode}, wanted {turn['mode']}")
    if turn.get("cited") and not got.done.cited:
        problems.append("no citation")
    for word in turn.get("answer_has", []):
        if word.casefold() not in text:
            problems.append(f"answer lacks '{word}'")
    for word in turn.get("answer_lacks", []):
        if word.casefold() in text:
            problems.append(f"answer says '{word}'")
    if "refused" in turn and truth["refused"] != turn["refused"]:
        problems.append(f"refused={truth['refused']}, wanted {turn['refused']}")
    if "max_unknown" in turn and len(truth["unknown_entities"]) > turn["max_unknown"]:
        problems.append(f"names no passage has: {truth['unknown_entities'][:5]}")
    if turn.get("no_fake", True) and truth["fake_actions"]:
        problems.append(f"pretends to look something up: {truth['fake_actions'][0][:80]}")
    return problems


async def run_scripts(settings: Settings, conn: sqlite3.Connection, ollama: Ollama, path: Path) -> tuple[list[dict], dict]:
    """Multi-turn conversations end to end through answer_stream, with a real session: follow-ups, clarify replies,
    challenges ("are you sure?"), topic switches. Each turn may expect: mode, cited (bool), answer_has, answer_lacks,
    refused (bool), max_unknown (int), no_fake (default true)."""
    scripts = yaml.safe_load(path.read_text(encoding="utf-8"))
    assistant = Assistant(settings, conn, ollama)
    rows = []
    for script in scripts:
        sid = _throwaway(conn)
        said: list[str] = []
        try:
            for i, turn in enumerate(script["turns"], 1):
                got = await answer(assistant, sid, turn["q"])
                truth = faithfulness.check_done(conn, got.done, turn["q"], turn["q"], " ".join(said))
                said.append(turn["q"])
                rows.append({"script": script["name"], "turn": i, "q": turn["q"], "mode": got.mode,
                             "answer": got.done.answer, "problems": _turn_problems(turn, got, truth),
                             "seconds": got.seconds, "revised": got.done.revised})
        finally:
            memory.delete_session(conn, sid)
    passed = sum(1 for r in rows if not r["problems"])
    return rows, {"script_turns": float(len(rows)), "script_turn_pass": passed / max(len(rows), 1),
                  "answers_revised": sum(1 for r in rows if r["revised"]) / max(len(rows), 1),
                  "scripts_clean": float(sum(1 for s in scripts if all(not r["problems"] for r in rows
                                                                         if r["script"] == s["name"])))}


# ------------------------------------------------------------------------------------------------ stored runs


def save_run(suite: str, metrics: dict[str, float], rows: list[dict], settings: Settings,
             runs_dir: Path = RUNS_DIR) -> tuple[Path, dict[str, float] | None]:
    """Write this run as JSON and return (its path, the metrics of the previous run of the same suite, if any)."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    previous = None
    for old in sorted(runs_dir.glob(f"*-{suite}.json"), reverse=True):
        try:
            previous = json.loads(old.read_text(encoding="utf-8"))["metrics"]
            break
        except (ValueError, KeyError):
            continue
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    from .. import __version__

    path = runs_dir / f"{stamp}-{suite}.json"
    path.write_text(json.dumps({"suite": suite, "at": stamp, "version": __version__,
                                "model": settings.ollama.chat_model, "embed_model": settings.ollama.embed_model,
                                "metrics": metrics, "rows": rows}, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")
    return path, previous


def deltas(now: dict[str, float], before: dict[str, float] | None) -> dict[str, str]:
    """Each metric with its change since the previous run ("0.912 (+0.020)")."""
    out = {}
    for k, v in now.items():
        prev = (before or {}).get(k)
        out[k] = f"{v:.3f}" if prev is None or prev == v else f"{v:.3f} ({v - prev:+.3f})"
    return out


async def run_conversations(settings: Settings, conn: sqlite3.Connection, ollama: Ollama, path: Path) -> list[dict]:
    """Scripted multi-turn dialogues, planner only: does each turn follow the thread, name its subject, ask when unclear?

    Each turn may expect `follow_up` (bool), `standalone_has` (words the rewritten question must contain) and
    `clarify` (bool). Canned answers (`a:`) stand in for the model's, so a run takes seconds and is repeatable."""
    scripts = yaml.safe_load(path.read_text(encoding="utf-8"))
    assistant = Assistant(settings, conn, ollama)
    rows: list[dict] = []
    for script in scripts:
        sid = memory.new_session(conn, "eval", f"eval-{uuid.uuid4().hex[:8]}")
        try:
            for i, turn in enumerate(script["turns"], 1):
                hist = memory.history(conn, sid, settings.chat.history_turns)
                plan = await assistant.plan(hist, turn["q"], sid)
                problems: list[str] = []
                if "follow_up" in turn and plan.follow_up != turn["follow_up"]:
                    problems.append(f"follow_up={plan.follow_up}, wanted {turn['follow_up']}")
                for word in turn.get("standalone_has", []):
                    if word.lower() not in plan.standalone_question.lower():
                        problems.append(f"standalone lacks '{word}'")
                if "clarify" in turn and bool(plan.clarify) != turn["clarify"]:
                    problems.append(f"clarify={bool(plan.clarify)}, wanted {turn['clarify']}")
                rows.append({"script": script["name"], "turn": i, "q": turn["q"], "standalone": plan.standalone_question,
                             "problems": problems})
                memory.add_message(conn, sid, "user", turn["q"], standalone_q=plan.standalone_question)
                memory.add_message(conn, sid, "assistant", turn.get("a", "(answered)"), standalone_q=plan.standalone_question)
        finally:
            memory.delete_session(conn, sid)
    return rows
