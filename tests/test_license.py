"""The license: Sekmeht is credited, and what is not Sekmeht's is never claimed."""

import tomllib
from pathlib import Path

from epedia.frontends.web.app import STATIC

ROOT = Path(__file__).parents[1]


def test_the_license_is_the_unedited_apache_text():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text and "Version 2.0, January 2004" in text
    assert "Copyright [yyyy] [name of copyright owner]" in text  # the appendix's template: the real line belongs in NOTICE


def test_the_notice_credits_sekmeht_and_sets_apart_what_is_not_theirs():
    text = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "Copyright 2026 Sekmeht" in text
    head, rest = text.split("WHAT THEY DO NOT COVER")
    assert "Fennick" in head and "harness" in head
    for other in ("Elanthipedia", "Ollama", "Simutronics", "tests/fixtures/", "Attribution-ShareAlike 3.0"):
        assert other in rest, other


def test_the_package_declares_the_license_and_ships_the_notice():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == "Apache-2.0" and project["license-files"] == ["LICENSE", "NOTICE"]


def test_visitors_can_see_whose_is_what():
    about = (STATIC / "index.html").read_text(encoding="utf-8").split('<dialog id="about"')[1].split("</dialog>")[0]
    for words in ("Apache License 2.0", "CC BY-SA 3.0", "Elanthipedia", "Ollama", "Simutronics"):
        assert words in about, words
