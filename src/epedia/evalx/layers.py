"""Layered answers without the model: which pages each part of "tell me about X" reads, for the owner to judge, plus labelled
cases (eval/layers.yaml): whether a question counts as open at all, and pages a part must or must not read."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

from ..config import Settings
from ..core import layers


def run_layers(settings: Settings, conn: sqlite3.Connection, cases: Path) -> tuple[list[dict], dict]:
    """(a row per question with the pages of each part, metrics)."""
    rows = []
    for item in yaml.safe_load(cases.read_text(encoding="utf-8")):
        subject = layers.open_question(conn, item["q"])
        found = layers.gather(conn, subject, settings.retrieval.k_final, settings.wiki.base_url) if subject else []
        parts: dict[str, list[str]] = {}
        for p in found:
            parts.setdefault(p["layer"], []).append(p["title"])
        problems = []
        if bool(found) != item.get("layered", True):
            problems.append("layered" if found else "not layered")
        for key, titles in item.get("expect", {}).items():
            problems += [f"{key} lacks {t}" for t in titles if t not in parts.get(key, [])]
        for key, titles in item.get("never", {}).items():
            problems += [f"{key} has {t}" for t in titles if t in parts.get(key, [])]
        rows.append({"q": item["q"], "subject": subject.main.title if subject else None, "parts": parts, "problems": problems})
    return rows, {"questions": float(len(rows)), "layered": sum(bool(r["parts"]) for r in rows) / max(len(rows), 1),
                  "cases_ok": sum(not r["problems"] for r in rows) / max(len(rows), 1)}
