import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from epedia import __version__
from epedia.config import Settings
from epedia.frontends.web.app import STATIC, create_app


@pytest.fixture
def client(tmp_path):
    s = Settings()
    s.db.path = str(tmp_path / "t.db")
    with TestClient(create_app(s)) as c:
        yield c


def test_version_reaches_the_user_agent_even_without_a_config_file():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    ua = Settings().wiki.user_agent
    assert ua.startswith(f"QQueryElanthia/{__version__} ") and "{version}" not in ua


def test_pyproject_reads_version_from_the_package():
    from pathlib import Path

    text = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text and "epedia.__version__" in text
    assert not re.search(r'^version\s*=\s*"', text, re.M)


def test_version_endpoint_reports_live_build_details(client):
    info = client.get("/api/version").json()
    assert info["version"] == __version__
    assert info["chat_model"] and info["embed_model"] and info["source"] == "original"
    assert info["pages"] == 0 and info["last_sync"] is None


def test_persona_endpoint_includes_build_info_and_chips(client):
    p = client.get("/api/persona").json()
    assert p["name"] == "Fennick" and p["build"]["version"] == __version__ and len(p["chips"]) == 6


def test_page_has_version_badge_status_toggle_and_thinking_animation(client):
    html = client.get("/").text
    assert 'id="pver"' in html and 'id="statusbtn"' in html
    assert "fennick-dots" in html and "is thinking" in html
    assert "#status { display:none;" in html  # index details start hidden


def test_fennick_portraits_are_served_and_used_by_the_page(client):
    for name in ("fennick.png", "fennick_talk.png"):
        r = client.get(f"/static/{name}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    html = client.get("/").text
    assert "/static/fennick.png" in html and "/static/fennick_talk.png" in html and "makeAvatar" in html


def test_the_two_portrait_frames_differ_only_around_the_mouth():
    from PIL import Image, ImageChops

    closed = Image.open(STATIC / "fennick.png").convert("RGBA")
    talk = Image.open(STATIC / "fennick_talk.png").convert("RGBA")
    assert closed.size == talk.size == (256, 256)
    # RGB only: getbbox() on RGBA looks at the alpha channel, which is identical (opaque) in both frames.
    box = ImageChops.difference(closed.convert("RGB"), talk.convert("RGB")).getbbox()
    assert box is not None, "frames must differ so the mouth can move"
    left, top, right, bottom = box
    assert 80 < left and right < 180 and top > 120 and bottom < 200  # confined to the lower face


def _render_with_node(cases: dict[str, dict]) -> dict[str, str]:
    """Run static/render.js's renderMarkdown under node on {name: {text, streaming, sources}}."""
    script = (
        "const {renderMarkdown} = require(process.argv[1]);"
        "const cases = JSON.parse(require('fs').readFileSync(0, 'utf8')); const out = {};"  # stdin: no length limit
        "for (const [k, c] of Object.entries(cases)) out[k] = renderMarkdown(c.text, c.sources || [], !!c.streaming);"
        "console.log(JSON.stringify(out));"
    )
    import json

    r = subprocess.run(["node", "-e", script, str(STATIC / "render.js")], input=json.dumps(cases),
                       capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(r.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_live_markdown_renders_inline_formatting_as_words_arrive():
    out = _render_with_node({
        "bold": {"text": "A **bold** move and *slanted* words with `code`."},
        "stream_open": {"text": "Hello **wor", "streaming": True},
        "final_open": {"text": "Hello **wor", "streaming": False},
        "star_bullet": {"text": "* not italic"},
        "escape": {"text": "<script>alert(1)</script> & \"q\""},
        "cite": {"text": "Weighs 35 stones [1][2].",
                 "sources": [{"url": "http://x/a", "title": "A", "section": "Stats", "snippet": "Weight: 35 stones"}]},
        "auto": {"text": "Costs 287 Kronars [1*].",
                 "sources": [{"url": "http://x/a", "title": "A", "section": "", "snippet": ""}]},
        "doubt": {"text": "Costs 999 Kronars [1?].",
                  "sources": [{"url": "http://x/a", "title": "A", "section": "", "snippet": ""}]},
    })
    assert "<b>bold</b>" in out["bold"] and "<i>slanted</i>" in out["bold"] and "<code>code</code>" in out["bold"]
    assert out["stream_open"] == "<p>Hello <b>wor</b></p>"  # an unclosed ** is already bold, never raw asterisks
    assert "**" in out["final_open"]  # once the answer is final, a stray marker is left alone
    assert "<li" in out["star_bullet"] and "<i>" not in out["star_bullet"]
    assert "<script>" not in out["escape"] and "&lt;script&gt;" in out["escape"]
    assert out["cite"].count('class="cite"') == 1 and "[2]" in out["cite"]  # [2] has no source: left as text
    assert 'data-n="1"' in out["cite"] and "A — Stats\nWeight: 35 stones" in out["cite"]  # hover shows title + excerpt
    assert 'class="cite auto"' in out["auto"] and "matched to this source automatically" in out["auto"]
    # a citation its page does not bear out: dotted, with the question mark kept and a tooltip saying why
    assert 'class="cite doubt"' in out["doubt"] and ">1?</a>" in out["doubt"] and "check it before" in out["doubt"]
    assert "[1?]" not in out["doubt"] and 'class="nb"' in out["doubt"]  # glued to its word like any badge


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_live_markdown_renders_blocks_lists_headings_and_tables():
    out = _render_with_node({
        "list": {"text": "Intro line\n- one\n- two\n  - nested"},
        "numbered": {"text": "1. first\n2. second"},
        "heading": {"text": "## Stats\nBody"},
        "table": {"text": "| Item | Weight |\n|---|---|\n| axe | 80 |\n| dagger | 5 |"},
        "table_partial": {"text": "| Item | Weight |\n|---|---|\n| axe | 8", "streaming": True},
        "header_only": {"text": "| Item | Weight |", "streaming": True},
        "paragraphs": {"text": "Line one\nline two\n\nSecond paragraph"},
    })
    assert out["list"].startswith("<p>Intro line</p><ul>") and out["list"].count("<li") == 3
    assert "margin-left:1.2em" in out["list"]  # nesting by indentation
    assert out["numbered"].startswith("<ol>") and out["numbered"].count("<li") == 2
    assert out["heading"].startswith('<h4 class="md-h">Stats</h4><p>Body</p>')
    assert "<th>Item</th>" in out["table"] and out["table"].count("<td>") == 4 and "---" not in out["table"]
    assert "<td>8</td>" in out["table_partial"] and "<th>Weight</th>" in out["table_partial"]
    assert "<th>Item</th>" in out["header_only"]
    assert out["paragraphs"] == "<p>Line one<br>line two</p><p>Second paragraph</p>"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_every_prefix_of_a_streamed_answer_renders_without_raw_markers_or_errors():
    answer = (
        "Riverhaven, or **Haven**, is a *principal* city [1].\n\n## Quick facts\n"
        "- **Currency:** Lirum [2]\n- Guildleaders of *every* guild\n  - nested **point**\n\n"
        "| Item | Weight |\n|---|---|\n| **axe** | 80 |\n| dagger | 5 |\n\n1. First step\n2. Second `STEP`\n"
    )
    cases = {f"p{i}": {"text": answer[:i], "streaming": True, "sources": [{"url": "u", "title": "t"}] * 2}
             for i in range(len(answer) + 1)}
    out = _render_with_node(cases)  # raises if node throws on any prefix
    for name, html in out.items():
        i = int(name[1:])
        prefix = answer[:i]
        # a half-typed "*" or "**" at the very end may show for one frame; anything longer must be formatted
        if not prefix.endswith("*"):
            assert "**" not in html, f"raw ** after {prefix[-30:]!r}: {html[-80:]}"
    assert "<table>" in out[f"p{len(answer)}"] and out[f"p{len(answer)}"].count("<li") == 5


def test_page_has_one_app_container_and_a_desktop_mobile_switch(client):
    html = client.get("/").text
    assert '<div id="app">' in html and html.rstrip().endswith("</html>")
    assert 'id="vdesk"' in html and 'id="vmob"' in html and 'aria-pressed="true"' in html
    assert "--col:1280px" in html and 'html[data-view="mobile"] { --col:440px' in html
    # the layout is chosen in <head>, before first paint: saved choice, else phone-sized screen, else desktop
    head = html.split("</head>")[0]
    assert "localStorage.getItem('epedia.view')" in head and "(max-width: 640px)" in head and "root.dataset.view = v" in head
    assert "setView(" in html and "epedia.view" in html.split("</head>")[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_citation_badges_never_strand_themselves_or_their_punctuation_on_a_line():
    out = _render_with_node({"c": {"text": "It weighs 35 stones [1].", "sources": [{"url": "u", "title": "t"}]}})["c"]
    # word + badge + full stop sit in one no-wrap group, so none of them can start a line on its own
    assert out.startswith('<p>It weighs 35 <span class="nb">stones&nbsp;<a class="cite"') and out.endswith("</a>.</span></p>")
    two = _render_with_node({"c": {"text": "A **bold phrase** [1][2].", "sources": [{"url": "u", "title": "t"}] * 2}})["c"]
    assert two.count("<b>") == two.count("</b>") and "</b> <a" in two  # a badge after markup never breaks the tags


def test_page_polish_welcome_input_scroll_and_error_handling(client):
    html = client.get("/").text
    assert "className = 'hero'" in html and "hero-name" in html  # portrait on the welcome screen (built by script)
    assert 'id="kbd"' in html and "Shift+Enter" in html and "isComposing" in html  # key hint; no send mid-IME
    assert "function autosize()" in html and "function syncSend()" in html  # growing input, Ask disabled when empty
    assert 'id="jump"' in html and "function toEnd(" in html  # follow the answer only while reading the bottom
    assert "if (busy) return;" in html  # chips clicked mid-stream must not start a second answer
    assert "function showError(" in html and "Try again" in html and "function addActions(" in html and "Copy answer" in html
    assert 'name="color-scheme"' in html and 'rel="icon"' in html and ":focus-visible" in html
    assert ".sources:empty" in html  # empty note/sources sections take no space (the stray line under user bubbles)


def test_page_has_an_about_fennick_page_with_his_story_and_how_he_works(client):
    html = client.get("/").text
    assert 'id="aboutbtn"' in html and html.index('id="vmob"') < html.index('id="aboutbtn"') < html.index('id="pastbtn"')
    about = html[html.index('<dialog id="about"'):html.index("</dialog>")]
    assert "<h2 id=\"about-title\">Hi, I'm Fennick!</h2>" in about  # it opens with his greeting
    assert about.index("Where I come from") < about.index("Out of character")  # the story, then the OOC part
    assert "Made by Sekmeht" in about and "Grounded in Elanthipedia, and nothing else." in about
    assert "He can be wrong." in about and 'aria-label="Close"' in about
    assert '/static/fennick_large.png' in about and 'width="200"' in about  # the bigger portrait
    assert client.get("/static/fennick_large.png").status_code == 200
    assert "about.showModal()" in html and "e.target === about" in html  # opens as a window; a click outside closes it


def test_page_shows_recent_questions_to_everyone_and_a_delete_all_only_at_this_computer(client):
    html = client.get("/").text
    assert "fetch('/api/recent-questions')" in html and "'Others are asking:'" in html
    # the questions in Past chats replace the starters for everyone, and an empty history says so
    assert "No questions have been asked yet. Ask one now!" in html
    assert "const starters" not in html  # nothing filters the history against canned questions any more
    start = html.index("function hintHtml(")
    body = html[start:html.index("async function loadRecent(", start)]
    assert "loadRecent(recent)" in body and "persona.chips" not in body and "persona.local" not in body
    # Past chats lists visitors' chats too, tagged; opening one shows it, and asking there starts the owner's own chat
    assert '<span class="who">visitor</span>' in html and "A visitor's conversation." in html
    assert "if (viewingVisitor) {  // a question while reading a visitor's chat" in html
    assert "getElementById('pastbtn').hidden = true" in html  # Past chats hidden through the tunnel, like Index
    assert 'id="pastclear"' in html and "fetch('/api/sessions', { method: 'DELETE'" in html
    assert "else if (r.ok) loadRecent();" in html  # a deleted chat's questions leave the start screen at once


def test_page_survives_http_errors_malformed_events_and_dropped_streams(client):
    html = client.get("/").text
    assert "if (!r.ok || !r.body)" in html and "the server answered" in html  # 4xx/5xx is an error, not a stream
    assert "if (!evName || !m) continue;" in html and "try { data = JSON.parse(m[1]); } catch" in html
    assert "if (!finished)" in html and "may be incomplete" in html  # a dropped connection is reported, not silent


def test_chat_endpoint_rejects_an_empty_message_with_a_json_error(client):
    r = client.post("/api/chat", json={"session_id": "s", "message": "   "}, headers={"X-QQE": "1"})
    assert r.status_code == 400  # the page now shows this as an error with a retry button


def test_index_panel_has_start_delta_start_full_stop_and_last_run_details(client):
    html = client.get("/").text
    for needle in ('id="idxdelta"', ">Start delta<", 'id="idxfull"', ">Start full<", 'id="idxstop"', ">Stop<",
                   "Last delta update", "Last full refresh", "Newest wiki edit seen", "Waiting", "Reading from"):
        assert needle in html, needle
    assert "'X-QQE': '1'" in html  # the header the server requires before it starts any work
    assert "confirm('Start a full refresh?" in html  # the slow one asks first; the delta does not
    assert "/api/index/start" in html and "/api/index/stop" in html and "/api/index/info" in html
    assert "setInterval(loadIndexInfo," in html and "reindexRow" in html  # live log and clock while a job runs, and the re-index row
    assert "if (jobTimer) { clearInterval(jobTimer)" in html  # no polling once the panel is closed


def test_header_groups_brand_and_controls_so_mobile_gets_two_tidy_rows(client):
    html = client.get("/").text
    assert 'class="brand"' in html and 'class="controls"' in html
    assert 'html[data-view="mobile"] header { flex-direction:column' in html


def test_page_lists_only_cited_sources_and_links_badges_to_them(client):
    html = client.get("/").text
    assert "Sources used" in html and "Also consulted" in html and "matched to its source automatically" in html
    assert '<details class="src-used">' in html and 'class="src-used" open' not in html  # folded by default
    assert "citeTargets" in html and "data.answer" in html  # final text (with automatic tags) replaces the draft
    assert "fennick-pulse" in html  # reduced-motion users still get a fading, colour-changing indicator


def test_page_streams_through_the_live_renderer_and_dots_ripple_in_colour(client):
    html = client.get("/").text
    assert '<script src="/static/render.js">' in html and "renderMarkdown(full, sources, streaming)" in html
    assert "requestAnimationFrame" in html and 'class="caret"' in html
    assert "fennick-hue" in html and "renderText" not in html  # the old end-of-stream renderer is gone
    assert client.get("/static/render.js").status_code == 200


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_page_script_parses(client, tmp_path):
    # every inline script (the head layout picker and the main one), not src= scripts; non-greedy so tags don't merge
    scripts = re.findall(r"<script>(.*?)</script>", client.get("/").text, re.S)
    assert len(scripts) >= 2
    f = tmp_path / "page.js"
    f.write_text("\n;\n".join(scripts), encoding="utf-8")
    result = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
