"""epedia command-line interface."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn
from rich.table import Table

from . import __version__, db
from .config import Settings, load_settings
from .util import parse_ns_option, utcnow_iso

app = typer.Typer(
    help="QQueryElanthia: Fennick, a local storyteller's guide to DragonRealms, answering from Elanthipedia.",
    no_args_is_help=True,
)
for _stream in (sys.stdout, sys.stderr):  # wiki text has IPA, accents and symbols a Windows cp1252 console cannot print
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # a stream that cannot be reconfigured (already replaced, or not a text stream) is used as it is
console = Console()
_settings: Settings | None = None


def settings() -> Settings:
    assert _settings is not None
    return _settings


def run(coro):
    return asyncio.run(coro)


@app.callback()
def main(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Path to epedia.toml"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging"),
    source: Optional[str] = typer.Option(
        None, "--source", "-s", help="Wiki to read from for this command: mirror | original (default: config)"
    ),
) -> None:
    global _settings
    _settings = load_settings(config)
    if source:
        if source not in _settings.wiki.sources:
            raise typer.BadParameter(f"unknown source {source!r}; configured: {', '.join(_settings.wiki.sources)}")
        _settings.wiki.source = source
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@app.command()
def version() -> None:
    """Print the version, source commit, models and index size."""
    from .buildinfo import build_info

    s = settings()
    # Printing a version must not create a database as a side effect: with no database yet, use an empty one.
    conn = db.connect(s, readonly=True) if s.db_path.exists() else sqlite3.connect(":memory:")
    try:
        info = build_info(s, conn)
    finally:
        conn.close()
    name = s.persona.name if s.persona.enabled else "QQueryElanthia"
    commit = f" ({info['commit']})" if info["commit"] else ""
    console.print(f"{name} v{__version__}{commit}  [QQueryElanthia]", highlight=False)
    console.print(f"chat model {info['chat_model']}, embeddings {info['embed_model']}, source {info['source']}")
    console.print(f"{info['pages']:,} pages indexed, last updated {info['last_sync'] or 'never'}")


@app.command()
def init(pull: bool = typer.Option(True, help="Pull missing Ollama models")) -> None:
    """Create the database, verify SQLite extensions, check Ollama and pull the models."""
    from .llm.ollama import Ollama

    s = settings()
    conn = db.connect(s)
    sqlite_ver = conn.execute("SELECT sqlite_version()").fetchone()[0]
    fts5 = conn.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0]
    console.print(f"db: {s.db_path}")
    console.print(f"sqlite {sqlite_ver}  fts5={'yes' if fts5 else 'no'}  sqlite-vec {db.vec_version(conn)}")

    async def check_ollama() -> None:
        async with Ollama(s.ollama) as ol:
            if not await ol.alive():
                console.print(f"[red]Ollama not reachable at {s.ollama.base_url}[/red]")
                raise typer.Exit(1)
            console.print(f"ollama {await ol.version()} at {s.ollama.base_url}")
            for name in (s.ollama.embed_model, s.ollama.chat_model):
                if await ol.has_model(name):
                    console.print(f"  model {name}: present")
                    continue
                if not pull:
                    console.print(f"  model {name}: [yellow]missing[/yellow] (run with --pull)")
                    continue
                console.print(f"  model {name}: pulling")
                with Progress(
                    TextColumn("{task.description}"), BarColumn(), DownloadColumn(), TransferSpeedColumn(),
                    console=console,
                ) as prog:
                    task = prog.add_task(name, total=None)

                    def on_progress(status: str, completed: int, total: int) -> None:
                        prog.update(task, description=f"{name} {status}"[:60], completed=completed,
                                    total=total or None)

                    await ol.pull(name, on_progress)
                console.print(f"  model {name}: pulled")

    run(check_ollama())


@app.command()
def sources() -> None:
    """Check each configured wiki source: reachable? how fresh? (the mirror lags the original)."""
    from .wiki.client import WikiClient
    from .wiki.discover import newest_change_timestamp

    s = settings()
    primary = db.get_state(db.connect(s, readonly=True), "primary_source")

    async def probe(name: str) -> tuple[str, str]:
        cfg = s.wiki.model_copy(update={"source": name})
        try:
            async with WikiClient(cfg, rps=1.0) as client:
                info = (await client.get({"action": "query", "meta": "siteinfo", "siprop": "general|statistics"}))["query"]
                newest = await newest_change_timestamp(client)
            return f"{info['general']['generator']}, {info['statistics']['articles']:,} articles", newest
        except Exception as exc:
            return f"[red]unreachable: {exc}[/red]", ""

    async def go() -> None:
        results = {name: await probe(name) for name in s.wiki.sources}
        t = Table(title="wiki sources")
        for col in ("source", "role", "api", "newest revision (UTC)", "behind"):
            t.add_column(col)
        newest_all = max((r[1] for r in results.values() if r[1]), default="")
        for name, (desc, newest) in results.items():
            role = ", ".join(x for x in ("active" if name == s.wiki.source else "", "primary" if name == primary else "") if x)
            behind = ""
            if newest and newest_all and newest != newest_all:
                from datetime import datetime

                fmt = "%Y-%m-%dT%H:%M:%SZ"
                delta = datetime.strptime(newest_all, fmt) - datetime.strptime(newest, fmt)
                behind = f"{delta.total_seconds() / 3600:.1f} h"
            t.add_row(name, role, f"{desc}\n{s.wiki.sources[name].api_url}", newest, behind)
        console.print(t)

    run(go())


@app.command()
def discover(
    ns: Optional[str] = typer.Option(None, help="Comma-separated namespaces (default: config)"),
    force: bool = typer.Option(False, help="Re-walk even if already discovered; also makes the active source primary"),
) -> None:
    """Enumerate pages and redirects per namespace (cheap; ~300 requests for the whole wiki)."""
    from .wiki.client import open_client
    from .wiki.discover import discover as _discover

    s = settings()
    conn = db.connect(s)
    ns_list = parse_ns_option(ns, s.wiki.namespaces)

    async def go() -> None:
        async with open_client(s.wiki) as client:
            totals = await _discover(conn, client, s.wiki, ns_list, force=force)
            console.print(f"discovered pages={totals['pages']} redirects={totals['redirects']} "
                          f"requests={client.requests}")

    run(go())


@app.command()
def adopt() -> None:
    """After switching the primary source: keep already-fetched pages that are provably still current."""
    from .wiki.client import open_client
    from .wiki.discover import adopt_fetched

    s = settings()
    conn = db.connect(s)

    async def go() -> None:
        async with open_client(s.wiki) as client:
            stats = await adopt_fetched(conn, client)
            console.print(f"adopt: {stats} requests={client.requests}")

    run(go())


@app.command()
def crawl(
    ns: Optional[str] = typer.Option(None, help="Comma-separated namespaces (default: config order)"),
    limit: Optional[int] = typer.Option(None, help="Stop after this many pages"),
    retry_errors: bool = typer.Option(False, help="Also retry pages that failed 5+ times"),
    rps: Optional[float] = typer.Option(None, help="Override requests/second"),
) -> None:
    """Fetch rendered HTML for pages that are new or changed (resumable; rerun to continue)."""
    from .wiki.client import open_client
    from .wiki.crawl import crawl as _crawl

    s = settings()
    conn = db.connect(s)
    ns_list = parse_ns_option(ns, s.wiki.namespaces) if ns else None

    async def go() -> None:
        async with open_client(s.wiki, rps=rps) as client:
            stats = await _crawl(conn, client, s.wiki, ns=ns_list, limit=limit, retry_errors=retry_errors)
            console.print(f"crawl done: {stats} requests={client.requests}")

    run(go())


@app.command()
def index(
    ns: Optional[str] = typer.Option(None, help="Comma-separated namespaces"),
    limit: Optional[int] = typer.Option(None, help="Stop after this many pages"),
    follow: bool = typer.Option(False, help="Keep polling for newly crawled pages (run alongside crawl)"),
    reembed_all: bool = typer.Option(False, "--reembed-all", help="Re-chunk every crawled page (vectors reused by hash)"),
    titles: bool = typer.Option(False, help="Rebuild the title index (pages + redirects) and exit"),
) -> None:
    """Chunk crawled pages, build the keyword index and embed chunks via Ollama."""

    from .index.enrich import enrich_all
    from .index.indexer import index_pages, rebuild_titles
    from .llm.ollama import Ollama

    s = settings()
    conn = db.connect(s)
    if titles:
        console.print(f"titles_fts rows: {rebuild_titles(conn)}")
        return
    ns_list = parse_ns_option(ns, []) if ns else None

    async def go() -> None:
        async with Ollama(s.ollama) as ol:
            if not await ol.has_model(s.ollama.embed_model):
                console.print(f"[red]embedding model {s.ollama.embed_model} missing; run `epedia init`[/red]")
                raise typer.Exit(1)
            idle = 0
            while True:
                stats = await index_pages(s, conn, ol, ns=ns_list, limit=limit, reembed_all=reembed_all,
                                          show_progress=not follow)
                console.print(f"index: {stats}")
                if not follow:
                    break
                idle = 0 if stats["pages"] else idle + 1
                if idle >= 10:  # ~10 min with nothing new
                    break
                if not stats["pages"]:
                    await asyncio.sleep(60)
            rebuild_titles(conn)
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
            console.print(f"enrich: {enrich_all(conn)}")

    run(go())


@app.command()
def doctor() -> None:
    """Check the index's integrity and coverage (read-only). Exits non-zero if anything is wrong."""
    from .index.doctor import run as run_doctor

    conn = db.connect(settings(), readonly=True)
    checks = run_doctor(conn)
    for c in checks:
        console.print(f"[{'green' if c.ok else 'red'}]{'ok  ' if c.ok else 'FAIL'}[/] {c.name}"
                      + (f"  [dim]{c.detail}[/dim]" if c.detail else ""))
    if not all(c.ok for c in checks):
        raise typer.Exit(1)


@app.command()
def enrich(
    force: bool = typer.Option(False, "--force", help="Re-read every source page, not just those whose revision lags"),
) -> None:
    """Derive typed facts (crafting materials...) from indexed pages. Local only: no network, no model."""
    from .index.enrich import enrich_all

    conn = db.connect(settings())
    console.print(enrich_all(conn, force=force))
    console.print(f"facts: {conn.execute('SELECT count(*) FROM facts').fetchone()[0]:,}")


@app.command()
def fill() -> None:
    """Fetch every page that is missing or out of date from the active source (the mirror), then classify, extract
    item stats and index them. The counterpart of `sync`, which reads only what changed from the real wiki."""
    from .llm.ollama import Ollama
    from .wiki import sync as syncmod
    from .wiki.client import open_client

    s = settings()
    conn = db.connect(s)

    async def go() -> None:
        async with open_client(s.wiki) as client, Ollama(s.ollama) as ol:
            before = conn.execute(
                "SELECT count(*) FROM pages WHERE is_redirect=0 AND fetched_revid IS NOT latest_revid").fetchone()[0]
            console.print(f"{before:,} pages to fetch from {s.wiki.source}")
            await syncmod.run_pipeline_for_changes(s, conn, client, ol)
            console.print(f"fill done: requests={client.requests}")

    try:
        run(go())
    finally:
        conn.close()


@app.command()
def history(
    fields: bool = typer.Option(True, "--fields/--no-fields",
                                help="Also read the old versions of character pages to see which fact changed"),
) -> None:
    """Read every page's edit history (who, when, how big; no text) from the real wiki, oldest first, then the guild,
    race, gender and status of each character page's old versions. Resumable; run it again to catch up."""
    from .wiki import history as hist
    from .wiki import timeline
    from .wiki.client import open_client

    s = settings()
    conn = db.connect(s)
    source = s.wiki.delta_source or s.wiki.source
    wiki_cfg = s.wiki.model_copy(update={"source": source, "fallback_source": None})

    async def go() -> None:
        async with open_client(wiki_cfg) as client:
            console.print(f"reading edit history from {source}")
            n = await hist.backfill(client, conn, list(s.wiki.namespaces),
                                    lambda total, ts: console.print(f"{total:,} edits stored (up to {ts})"))
            console.print(f"edit history done: {n:,} new edits, requests={client.requests}")
            if fields:
                todo = len(timeline.pending_titles(conn))
                console.print(f"{todo:,} character pages to read for guild/race/gender/status changes")
                done = await timeline.fetch_all(
                    client, conn, lambda d, t, title: console.print(f"{d:,} / {t:,}  {title}"))
                console.print(f"character history done: {done:,} pages, requests={client.requests}")

    try:
        run(go())
    finally:
        conn.close()


@app.command("clear-index")
def clear_index_cmd(
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation"),
) -> None:
    """Empty the search index so a new one can be built (keeps the downloaded pages; rebuild with `index --reembed-all`)."""
    from .index.reset import clear_index, index_size
    from .jobs import find_reindex_pids, reindex_status

    s = settings()
    conn = db.connect(s)
    running = reindex_status(conn)
    if (running and running["state"] == "running") or find_reindex_pids():
        console.print("[red]A re-index is running. Stop it first (Stop on the Index page).[/red]")
        raise typer.Exit(1)
    size = index_size(conn)
    console.print(f"This removes {size['chunks']:,} chunks and their vectors for {size['pages']:,} pages. "
                  "Downloaded pages are kept; rebuild with `index --reembed-all`.")
    if not yes and input("Type CLEAR to continue: ").strip() != "CLEAR":
        console.print("Nothing changed.")
        raise typer.Exit(1)
    removed = clear_index(conn)
    console.print(f"Cleared {removed['chunks']:,} chunks. Rebuild: qqueryelanthia index --reembed-all")


@app.command()
def smw(
    ns: Optional[str] = typer.Option(None, help="Comma-separated namespaces (default: item namespaces)"),
    limit: Optional[int] = typer.Option(None, help="Stop after this many pages"),
    catalog: bool = typer.Option(False, help="Rebuild the property catalog from stored properties and exit"),
) -> None:
    """Fetch Semantic MediaWiki item properties and add a 'stats' chunk to each item page."""
    from .llm.ollama import Ollama
    from .wiki.client import open_client
    from .wiki.smw import build_catalog, smw_pages

    s = settings()
    conn = db.connect(s)
    if catalog:
        console.print(f"catalog: {build_catalog(conn)} properties")
        return
    ns_list = parse_ns_option(ns, s.wiki.item_namespaces) if ns else None

    async def go() -> None:
        async with open_client(s.wiki) as client, Ollama(s.ollama) as ol:
            stats = await smw_pages(s, conn, client, ol, ns=ns_list, limit=limit)
            console.print(f"smw: {stats} requests={client.requests}")
        console.print(f"catalog: {build_catalog(conn)} properties")

    run(go())


@app.command()
def items(question: str = typer.Argument(..., help="A stat-comparison question about items")) -> None:
    """Run only the planner + items tool and print the generated SMW query and result table."""
    from .core.assistant import Assistant
    from .core.items_tool import items_passage
    from .llm.ollama import Ollama

    s = settings()
    conn = db.connect(s, readonly=True)

    async def go() -> None:
        async with Ollama(s.ollama) as ol:
            plan = await Assistant(s, conn, ol).plan([], question)
            console.print(f"mode={plan.mode} standalone={plan.standalone_question!r}", markup=False)
            console.print(f"items_query={plan.items_query}", markup=False)
            if plan.mode == "items" and plan.items_query:
                p = await items_passage(s, conn, plan.items_query)
                console.print(p["text"] if p else "(no rows)", markup=False)

    run(go())


@app.command()
def classify(
    all_pages: bool = typer.Option(False, "--all", help="Reclassify every page, not just changed ones"),
    links: bool = typer.Option(True, "--links/--no-links", help="Also rebuild the internal link graph"),
    stats: bool = typer.Option(False, "--stats", help="Only print the page-kind counts"),
) -> None:
    """Label each page with a kind (shop, creature, guide, ...) and build the link graph. No network, no model."""
    from .index.classify import classify_all, kind_counts

    conn = db.connect(settings())
    if not stats:
        result = classify_all(conn, all_pages=all_pages, links=links)
        console.print(f"classified: {result}")
    t = Table(title="pages by kind")
    t.add_column("kind")
    t.add_column("pages", justify="right")
    for name, n in kind_counts(conn):
        t.add_row(name, f"{n:,}")
    console.print(t)
    console.print(f"page_links: {conn.execute('SELECT count(*) FROM page_links').fetchone()[0]:,}")


@app.command("feedback")
def feedback_cmd(
    turn: Optional[int] = typer.Argument(None, help="A turn id from the list: show its conversation and full record"),
    all_votes: bool = typer.Option(False, "--all", help="List thumbs-up too, not only thumbs-down"),
    since: Optional[str] = typer.Option(None, help="Only votes on or after this date (YYYY-MM-DD)"),
    limit: int = typer.Option(30, help="How many to list"),
    as_json: bool = typer.Option(False, "--json", help="Print JSON, for processing the bad answers elsewhere"),
) -> None:
    """Review the answers people voted on (thumbs down by default), with why, what the checks found, and the turn id
    to look at one in its conversation."""
    import json as _json

    from .core import feedback

    conn = db.connect(settings(), readonly=True)
    if turn is not None:
        got = feedback.conversation(conn, turn)
        if got is None:
            console.print(f"[red]no turn {turn}[/red]")
            raise typer.Exit(1)
        if as_json:
            console.print(_json.dumps(got, ensure_ascii=False, indent=2, default=str), markup=False, highlight=False)
            return
        for m in got["messages"]:
            who = "you" if m["role"] == "user" else "answer"
            mark = f"  \\[{m['feedback']}]" if m.get("feedback") else ""  # escaped: Rich would read [down] as a style
            console.print(f"\n[bold]{who}[/bold]{mark}", highlight=False)
            console.print(m["content"], markup=False, highlight=False)
            if m["role"] == "user" and m.get("standalone_q") and m["standalone_q"] != m["content"]:
                console.print(f"[dim]  planned as: {m['standalone_q']} ({m.get('kind')}, {m.get('mode')})[/dim]",
                              highlight=False)
        t = got["turn"]
        console.print(f"\n[bold]turn {turn}[/bold]: vote {t['feedback'] or '-'}"
                      f"{'; ' + ', '.join(feedback.REASONS.get(r, r) for r in t['reasons']) if t['reasons'] else ''}"
                      f"{'; note: ' + t['note'] if t['note'] else ''}", markup=False, highlight=False)
        for key in ("grounding", "verify"):
            if t.get(key):
                console.print(f"{key}: {_json.dumps(t[key], ensure_ascii=False)}", markup=False, highlight=False)
        console.print(f"times (ms): planner {t['planner_ms']}, retrieve {t['retrieve_ms']}, first word {t['ttft_ms']}, "
                      f"total {t['total_ms']}; rewritten: {bool(t['regenerated'])}; error: {t['error'] or '-'}",
                      markup=False, highlight=False)
        return
    rows = feedback.votes(conn, None if all_votes else "down", since, limit)
    if as_json:
        console.print(_json.dumps(rows, ensure_ascii=False, indent=2, default=str), markup=False, highlight=False)
        return
    if not rows:
        console.print("no votes yet" if all_votes else "no thumbs-down yet")
        return
    for r in rows:
        who = "you" if r["owner"] == "local" else "visitor"
        why = ", ".join(feedback.REASONS.get(x, x) for x in r["reasons"])
        c = r["checks"] or {}
        flags = [f"{len(c.get('unknown_entities') or [])} unknown names" if c.get("unknown_entities") else "",
                 f"{len(c.get('unsupported') or [])} unsupported lines" if c.get("unsupported") else "",
                 "rewritten" if r["regenerated"] else ""]
        console.print(f"\n[bold]#{r['turn_id']}[/bold] {r['feedback']}  {r['feedback_at'][:16]}  {who}  "
                      f"({r['kind']}, {r['mode']})", highlight=False)
        console.print(f"  Q: {r['question'][:200]}", markup=False, highlight=False)
        if why or r["note"]:
            console.print(f"  why: {why}{' | ' if why and r['note'] else ''}{r['note']}", markup=False, highlight=False)
        if any(flags):
            console.print(f"  [dim]checks: {', '.join(f for f in flags if f)}[/dim]", highlight=False)
    console.print(f"\n[dim]{len(rows)} shown. `feedback <turn id>` shows one in its conversation.[/dim]", highlight=False)


@app.command()
def show(
    title: str = typer.Argument(..., help='Page title, e.g. "Barbarian" or "Weapon:Broadsword (1)"'),
    html: bool = typer.Option(False, "--html", help="Print the stored HTML instead of chunks"),
) -> None:
    """Dump how a stored page is converted and chunked (for checking extraction quality)."""
    import zlib

    from .text.chunker import chunk_page
    from .text.html2md import html_to_page

    s = settings()
    conn = db.connect(s, readonly=True)
    page = conn.execute("SELECT * FROM pages WHERE title=? COLLATE NOCASE", (title.replace("_", " "),)).fetchone()
    if not page:
        console.print(f"[red]no page titled {title!r}[/red]")
        raise typer.Exit(1)
    row = conn.execute("SELECT html FROM page_html WHERE page_id=?", (page["page_id"],)).fetchone()
    if not row:
        console.print("[yellow]page known but not crawled yet[/yellow]")
        raise typer.Exit(1)
    text = zlib.decompress(row["html"]).decode("utf-8")
    if html:
        console.print(text, markup=False)
        return
    import json as _json

    cats = _json.loads(page["categories"] or "[]")
    parsed = html_to_page(text)
    console.print(
        f"kind: {page['page_kind'] or '(not classified)'}"
        f"{'  obsolete' if page['is_obsolete'] else ''}{'  log' if page['is_log'] else ''}",
        markup=False,
    )
    if parsed.soft_redirect is not None:
        console.print(f"soft redirect -> {parsed.soft_redirect}", markup=False)
        return
    if parsed.notices:
        console.print(f"notices: {parsed.notices}", markup=False)
    for c in chunk_page(page["title"], parsed.blocks, s.chunking, cats, parsed.notices):
        console.print(f"--- chunk {c.ord}  [{c.section_path or 'Overview'}]  #{c.anchor}  ~{c.token_est} tok", markup=False)
        console.print(c.text, markup=False)


@app.command()
def search(
    query: str = typer.Argument(...),
    k: int = typer.Option(8, "-k"),
    mode: str = typer.Option("hybrid", help="hybrid | fts | vec"),
    explain: bool = typer.Option(False, help="Show per-signal ranks, title-match labels, page kind and weight"),
    kind: Optional[str] = typer.Option(None, "--kind", help="Question kind to favour (crafting, place, ...)"),
) -> None:
    """Run retrieval only, to debug what the assistant would see."""
    from .kinds import QUESTION_KINDS
    from .llm.ollama import Ollama
    from .retrieval.search import search as _search

    if kind and kind not in QUESTION_KINDS:
        raise typer.BadParameter(f"kind must be one of: {', '.join(QUESTION_KINDS)}")
    s = settings()
    conn = db.connect(s, readonly=True)

    async def go() -> None:
        qvec = None
        if mode in ("hybrid", "vec"):
            async with Ollama(s.ollama) as ol:
                qvec = (await ol.embed(["search_query: " + query]))[0]
        hits = _search(conn, query, s.retrieval, s.wiki.base_url, qvec, k_final=k, mode=mode, kind=kind)
        for i, h in enumerate(hits, 1):
            extra = (
                f"  fts={h.fts_rank} vec={h.vec_rank} title={h.title_match or '-'} "
                f"page_kind={h.page_kind or '-'}{' log' if h.is_log else ''}{' obsolete' if h.is_obsolete else ''} "
                f"weight={h.weight:.2f}"
            ) if explain else ""
            console.print(f"{i}. {h.score:.4f}  {h.title} — {h.section_path or 'Overview'}{extra}", markup=False)
            console.print(f"   {h.url}", markup=False)
            console.print("   " + h.text[:160].replace("\n", " "), markup=False)

    run(go())


def _sync_runner(kind: str, dry_run: bool, log_file: Optional[str], **kw):
    from .llm.ollama import Ollama
    from .wiki import sync as syncmod
    from .wiki.client import open_client

    s = settings()
    if kind == "rc" and s.wiki.delta_source and s.wiki.delta_source != s.wiki.source:
        # "What changed?" comes from the authoritative wiki even when pages are normally read from a mirror.
        s.wiki = s.wiki.model_copy(update={"source": s.wiki.delta_source, "fallback_source": None})
    _attach_log_file(log_file)
    conn = db.connect(s)

    async def go():
        async with open_client(s.wiki) as client, Ollama(s.ollama) as ol:
            if kind == "rc":
                return await syncmod.sync(s, conn, client, ol, dry_run=dry_run)
            if kind == "full":
                return await syncmod.full_scan(s, conn, client, ol, dry_run=dry_run)
            return await syncmod.refresh_oldest(s, conn, client, ol, **kw)

    from .wiki.discover import SourceMismatch

    try:
        return run(go())
    except SourceMismatch as exc:  # one clear line instead of a traceback (the Index page shows the job's last lines)
        console.print(f"[red]Cannot run this against the active source:[/red] {exc}", markup=True, highlight=False)
        raise typer.Exit(3) from None
    finally:
        conn.close()  # `watch` calls this in a loop: do not leave a connection (and its WAL lock) per iteration


_log_files: set[str] = set()


def _attach_log_file(log_file: Optional[str]) -> None:
    """Add a file handler for this path once per process, however many times a command runs in a loop."""
    if not log_file or log_file in _log_files:
        return
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)
    _log_files.add(log_file)


@app.command()
def sync(
    full_scan: bool = typer.Option(False, "--full-scan", help="Re-walk every page and compare (weekly safety net)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would change without writing"),
    log_file: Optional[str] = typer.Option(None, "--log", help="Also append log output to this file"),
) -> None:
    """Apply wiki changes since the last run: re-fetch, re-chunk and re-embed only what changed."""
    result = _sync_runner("full" if full_scan else "rc", dry_run, log_file)
    console.print(f"{'full scan' if full_scan else 'sync'}{' (dry run)' if dry_run else ''}: {result}")


@app.command()
def watch(
    every: int = typer.Option(30, help="Minutes between syncs"),
    log_file: Optional[str] = typer.Option(None, "--log"),
) -> None:
    """Run `sync` in a loop (alternative to a scheduled task)."""
    import time

    while True:
        try:
            result = _sync_runner("rc", False, log_file)
            console.print(f"[{utcnow_iso()}] sync: {result}")
        except Exception as exc:
            logging.getLogger("epedia").exception("sync failed: %s", exc)
        time.sleep(every * 60)


@app.command()
def refresh(
    older_than: int = typer.Option(90, "--older-than", help="Days since last fetch"),
    limit: int = typer.Option(1000),
) -> None:
    """Trickle re-parse of the oldest pages (catches list/query pages that change without an edit)."""
    n = _sync_runner("refresh", False, None, older_than_days=older_than, limit=limit)
    console.print(f"refreshed {n} pages")


@app.command()
def serve(port: Optional[int] = typer.Option(None, help="Port (default from config)")) -> None:
    """Start the local web chat."""
    import uvicorn

    from .frontends.web.app import create_app

    s = settings()
    console.print(f"web chat on http://{s.web.host}:{port or s.web.port}")
    uvicorn.run(create_app(s), host=s.web.host, port=port or s.web.port, log_level="warning")


def _render_sources(sources, cited: list[int]) -> None:
    if not sources:
        return
    console.print("\n[bold]Sources[/bold]")
    for src in sources:
        mark = "*" if src.n in cited else " "
        label = f"{src.title}" + (f" — {src.section}" if src.section else "")
        console.print(f" {mark}[{src.n}] {label}\n      {src.url}", markup=False, highlight=False)


async def _stream_answer(
    assistant, session_id: str, message: str, show_plan: bool = True, kind: str | None = None
) -> None:
    from .core.assistant import Done, Retrieval, Revise, Token

    async for ev in assistant.answer_stream(session_id, message, kind_override=kind):
        if isinstance(ev, Retrieval):
            if show_plan and (ev.kind not in ("general", "meta") or ev.standalone_q.strip() != message.strip()):
                console.print(f"[dim]({ev.kind}, {ev.mode}) searching: {ev.standalone_q}[/dim]", highlight=False)
        elif isinstance(ev, Token):
            console.print(ev.text, end="", markup=False, highlight=False)
        elif isinstance(ev, Revise):
            what = "named things no page has" if ev.reason == "names" else "pretended to look something up"
            console.print(f"\n\n[dim](that draft {what}; writing it again)[/dim]\n", highlight=False)
        elif isinstance(ev, Done):
            console.print()
            if re.search(r"\[\d+\?\]", ev.answer):  # the stream showed the raw draft; these marks are only in the final
                console.print("\n[dim]checked answer:[/dim]", highlight=False)
                console.print(ev.answer, markup=False, highlight=False)
            _render_sources(ev.sources, ev.cited)
            if ev.related:
                console.print("\n[dim]related: " + "; ".join(ev.related) + "[/dim]", highlight=False)


@app.command()
def ask(
    question: str = typer.Argument(...),
    session: Optional[str] = typer.Option(None, help="Session id to continue a conversation"),
    kind: Optional[str] = typer.Option(None, "--kind", help="Override the detected question kind (debugging)"),
) -> None:
    """Ask one question and print a cited answer."""
    from .core.assistant import Assistant
    from .core.memory import new_session
    from .kinds import QUESTION_KINDS
    from .llm.ollama import Ollama

    if kind and kind not in QUESTION_KINDS:
        raise typer.BadParameter(f"kind must be one of: {', '.join(QUESTION_KINDS)}")
    s = settings()
    conn = db.connect(s)
    sid = new_session(conn, "cli", session)

    async def go() -> None:
        async with Ollama(s.ollama) as ol:
            await _stream_answer(Assistant(s, conn, ol), sid, question, kind=kind)

    run(go())
    console.print(f"[dim]session: {sid}[/dim]")


@app.command()
def chat(session: Optional[str] = typer.Option(None, help="Resume a session id")) -> None:
    """Interactive conversation with memory. Commands: /new, /quit."""
    from .core.assistant import Assistant
    from .core.memory import new_session
    from .llm.ollama import Ollama, OllamaError

    s = settings()
    conn = db.connect(s)
    sid = new_session(conn, "cli", session)
    greeting = (
        f"{s.persona.name} here, a traveler with a satchel of notes from Elanthipedia. Ask me about DragonRealms."
        if s.persona.enabled else "Ask me a question about DragonRealms."
    )
    console.print(f"{greeting} [dim](/new for a fresh conversation, /quit to exit)[/dim]", highlight=False)

    async def go() -> None:
        nonlocal sid
        async with Ollama(s.ollama) as ol:
            assistant = Assistant(s, conn, ol)
            while True:
                try:
                    q = console.input("\n[bold cyan]> [/bold cyan]").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not q:
                    continue
                if q in ("/quit", "/exit"):
                    break
                if q == "/new":
                    sid = new_session(conn, "cli")
                    console.print("[dim]new conversation[/dim]")
                    continue
                try:
                    await _stream_answer(assistant, sid, q)
                except OllamaError as exc:  # the question is saved and the turn records the error; keep the conversation
                    console.print(f"\n[red]The model failed: {exc}[/red] [dim](ask again, or check that Ollama is running)[/dim]",
                                  highlight=False)

    run(go())
    console.print(f"[dim]session: {sid}[/dim]")


@app.command("eval")
def eval_cmd(
    file: str = typer.Argument("eval/questions.yaml"),
    k: str = typer.Option("5,10", help="Comma-separated cutoffs for hit@k"),
    answers: bool = typer.Option(False, "--answers", help="Also generate answers for manual spot checks"),
    conversations: bool = typer.Option(False, "--conversations", help="Run the scripted multi-turn dialogues instead"),
    full: bool = typer.Option(False, "--full", help="Also answer every question and score the answers for truthfulness, "
                              "run the refusal set and the end-to-end scripts (slow: one model answer each)"),
    offers_: bool = typer.Option(False, "--offers", help="Replay the closing offer for every answer in the conversation "
                                 "log, old next to new, and check the labelled cases in eval/offers.yaml (no model call)"),
    layers_: bool = typer.Option(False, "--layers", help="Show which pages each part of a layered answer reads, and check "
                                 "the labelled cases in eval/layers.yaml (no model call)"),
) -> None:
    """Score retrieval quality on the question set (or, with --conversations, how well follow-ups are tracked).
    Every run is saved under eval/runs/ and compared with the previous run of the same kind."""
    from pathlib import Path

    from .evalx.runner import deltas, run_conversations, run_eval, run_refusals, run_scripts, save_run
    from .llm.ollama import Ollama

    s = settings()
    conn = db.connect(s)
    ks = [int(x) for x in k.split(",")]
    if layers_:
        from .evalx.layers import run_layers

        rows, metrics = run_layers(s, conn, Path("eval/layers.yaml"))
        for r in rows:
            console.print(f"{'BAD' if r['problems'] else 'ok '} {r['q']} -> {r['subject'] or '-'}"
                          + ("  [" + "; ".join(r["problems"]) + "]" if r["problems"] else ""), markup=False, highlight=False)
            for key, titles in r["parts"].items():
                console.print(f"      {key}: {'; '.join(titles)}", markup=False, highlight=False)
        saved, before = save_run("layers", metrics, rows, s)
        console.print(deltas(metrics, before))
        console.print(f"saved {saved}", style="dim")
        return
    if offers_:
        from .evalx.offers import run_offers

        async def replay() -> None:
            async with Ollama(s.ollama) as ol:
                rows, metrics = await run_offers(s, conn, ol, Path("eval/offers.yaml"))
            t = Table(title="closing offers: then and now")
            for col in ("", "kind", "question", "about", "offered then", "offered now"):
                t.add_column(col)
            for r in rows:
                mark = ("ok" if r["ok"] else "BAD") if r.get("case") else ("" if r["old"] == r["new"] else "*")
                t.add_row(mark, r["kind"], r["q"][:60], r["anchor"] or "", r["old"] or "-", r["new"] or "-")
            console.print(t)
            saved, before = save_run("offers", metrics, rows, s)
            console.print(deltas(metrics, before))
            console.print(f"saved {saved}", style="dim")

        run(replay())
        return
    if conversations:
        path = Path(file if file != "eval/questions.yaml" else "eval/conversations.yaml")

        async def talk() -> None:
            async with Ollama(s.ollama) as ol:
                rows = await run_conversations(s, conn, ol, path)
            bad = 0
            for r in rows:
                mark = "ok " if not r["problems"] else "BAD"
                bad += bool(r["problems"])
                console.print(f"{mark} {r['script']} #{r['turn']}: {r['q']} -> {r['standalone']}"
                              + ("  [" + "; ".join(r["problems"]) + "]" if r["problems"] else ""), markup=False, highlight=False)
            console.print({"turns": len(rows), "passed": len(rows) - bad, "accuracy": round((len(rows) - bad) / max(1, len(rows)), 3)})

        run(talk())
        return

    async def go() -> None:
        extra_rows: dict[str, list[dict]] = {}
        async with Ollama(s.ollama) as ol:
            results, metrics = await run_eval(s, conn, ol, Path(file), ks, answers, full=full)
            if full:
                extra_rows["refusals"], m = await run_refusals(s, conn, ol, Path("eval/refusals.yaml"))
                metrics.update(m)
                extra_rows["scripts"], m = await run_scripts(s, conn, ol, Path("eval/scripts.yaml"))
                metrics.update(m)
        t = Table(title="evaluation")
        for col in ("rank", "mode", "kind", "question", "standalone / top pages"):
            t.add_column(col)
        for r in results:
            rank = str(r.rank) if r.rank else ("-" if r.expect else "n/a")
            mode = r.mode + ("" if r.mode == r.expected_mode else f" (want {r.expected_mode})")
            kind = r.kind + ("" if not r.expected_kind or r.kind == r.expected_kind else f" (want {r.expected_kind})")
            top = r.standalone if r.standalone != r.q else ""
            top = (top + "\n" if top else "") + ", ".join(r.titles[:4])
            t.add_row(rank, mode, kind, r.q, top)
        console.print(t)
        from .evalx.runner import VOICE_CHECKLIST

        for r in results:
            if r.answer and answers:
                console.print(f"\n{r.q}  [{r.kind}]", style="bold", markup=False, highlight=False)
                console.print(r.answer, markup=False, highlight=False)
                if r.related:
                    console.print("related: " + "; ".join(r.related), style="dim", markup=False)
                console.print(VOICE_CHECKLIST, style="dim", markup=False)
            if r.truth and (r.truth["unknown_entities"] or r.truth["fake_actions"] or r.truth["unsupported"]):
                console.print(f"[yellow]truth[/yellow] {r.q}: unknown={r.truth['unknown_entities'][:6]} "
                              f"fake={len(r.truth['fake_actions'])} unsupported={len(r.truth['unsupported'])}",
                              highlight=False)
        for row in extra_rows.get("refusals", []):
            if not row["ok"]:
                console.print(f"[red]should have declined[/red] ({row['why']}) {row['q']}\n   "
                              + row["answer"][:160].replace("\n", " "), highlight=False)
        for row in extra_rows.get("scripts", []):
            mark = "ok " if not row["problems"] else "BAD"
            console.print(f"{mark} {row['script']} #{row['turn']} ({row['mode']}): {row['q']}"
                          + ("  [" + "; ".join(row["problems"]) + "]" if row["problems"] else ""),
                          markup=False, highlight=False)
        suite = "full" if full else "retrieval"
        rows = [{k: v for k, v in r.__dict__.items()} for r in results]
        for name, extra in extra_rows.items():
            rows += [{"set": name, **x} for x in extra]
        saved, before = save_run(suite, metrics, rows, s)
        console.print(deltas(metrics, before))
        console.print(f"[dim]saved {saved}" + (" (compared with the previous run)" if before else "") + "[/dim]",
                      highlight=False)

    run(go())


@app.command()
def pipeline() -> None:
    """Unattended full build: discover -> crawl -> classify -> smw -> index -> titles. Safe to rerun."""
    from .index.classify import classify_all
    from .index.enrich import enrich_all
    from .index.indexer import index_pages, rebuild_titles
    from .llm.ollama import Ollama
    from .wiki.client import open_client
    from .wiki.crawl import crawl as _crawl
    from .wiki.discover import discover as _discover
    from .wiki.smw import build_catalog, smw_pages

    s = settings()
    conn = db.connect(s)

    async def go() -> None:
        async with open_client(s.wiki) as client, Ollama(s.ollama) as ol:
            console.print("[bold]discover[/bold]", await _discover(conn, client, s.wiki))
            console.print("[bold]crawl[/bold]", await _crawl(conn, client, s.wiki))
            console.print("[bold]classify[/bold]", classify_all(conn))
            console.print("[bold]smw[/bold]", await smw_pages(s, conn, client, ol))
            build_catalog(conn)
            console.print("[bold]index[/bold]", await index_pages(s, conn, ol))
            console.print("[bold]enrich[/bold]", enrich_all(conn))
            rebuild_titles(conn)
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        console.print("pipeline complete")

    run(go())


@app.command()
def monitor(
    every: float = typer.Option(3.0, help="Seconds between refreshes"),
    once: bool = typer.Option(False, "--once", help="Render one frame and exit"),
) -> None:
    """Live console view of crawl/index progress, rates and ETA (Ctrl-C to close; does not affect the crawl)."""
    import time
    from collections import deque

    from rich.console import Group
    from rich.live import Live
    from rich.progress_bar import ProgressBar
    from rich.text import Text

    s = settings()
    conn = db.connect(s, readonly=True)
    from .progress import NS_NAMES as names

    samples: deque[tuple[float, int, int]] = deque(maxlen=40)  # (time, fetched, indexed)

    def rate(idx: int) -> float:
        if len(samples) < 2:
            return 0.0
        (t0, *a), (t1, *b) = samples[0], samples[-1]
        return max(0.0, (b[idx] - a[idx]) / (t1 - t0) * 60) if t1 > t0 else 0.0

    def fmt_eta(remaining: int, per_min: float) -> str:
        if per_min <= 0:
            return "measuring..."
        mins = remaining / per_min
        return f"{int(mins // 60)}h {int(mins % 60):02d}m"

    def frame() -> Group:
        from .progress import snapshot as shared_snapshot

        snap = shared_snapshot(conn)
        total, fetched, indexed, chunks, errors = (snap[k] for k in ("total", "fetched", "indexed", "chunks", "errors"))
        rows = [{"ns": n["ns"], "pages": n["pages"], "fetched": n["fetched"], "indexed": n["indexed"],
                 "errors": n["errors"]} for n in snap["namespaces"]]
        samples.append((time.time(), fetched, indexed))
        crawl_rate, index_rate = snap["crawl_per_min"], rate(2)
        t = Table(title="Elanthipedia build", expand=True)
        for col, just in (("namespace", "left"), ("pages", "right"), ("crawled", "right"), ("indexed", "right"),
                          ("errors", "right"), ("progress", "left")):
            t.add_column(col, justify=just)
        for r in rows:
            frac = (r["fetched"] or 0) / r["pages"] if r["pages"] else 0
            bar = ProgressBar(total=100, completed=frac * 100, width=24)
            t.add_row(f"{r['ns']} {names.get(r['ns'], '')}", f"{r['pages']:,}", f"{r['fetched'] or 0:,}",
                      f"{r['indexed'] or 0:,}", str(r["errors"] or 0), bar)
        overall = ProgressBar(total=max(total, 1), completed=fetched, width=60)
        head = Text.assemble(
            ("Crawl   ", "bold"), f"{fetched:,} / {total:,} pages ({fetched / max(total, 1):.1%})   "
            f"{crawl_rate:,.0f}/min   ETA {fmt_eta(total - fetched, crawl_rate)}\n",
            ("Index   ", "bold"), f"{indexed:,} pages, {chunks:,} chunks   {index_rate:,.0f}/min   "
            f"{max(fetched - indexed, 0):,} waiting\n",
            ("Errors  ", "bold"), f"{errors:,} pages\n",
        )
        return Group(head, overall, Text(""), t, Text("Ctrl-C closes this view; the crawl keeps running.", style="dim"))

    if once:
        console.print(frame())
        return
    try:
        with Live(frame(), console=console, refresh_per_second=2, screen=False) as live:
            while True:
                time.sleep(every)
                live.update(frame())
    except KeyboardInterrupt:
        pass


@app.command()
def status() -> None:
    """Show counts per stage and namespace, pending work, errors and sync state."""
    s = settings()
    conn = db.connect(s)
    t = Table(title="pages by namespace")
    for col in ("ns", "pages", "redirects", "fetched", "indexed", "smw", "errors"):
        t.add_column(col, justify="right")
    rows = conn.execute(
        """
        SELECT ns,
               SUM(is_redirect=0) AS pages,
               SUM(is_redirect=1) AS redirects,
               SUM(is_redirect=0 AND fetched_revid IS NOT NULL AND fetched_revid = latest_revid) AS fetched,
               SUM(is_redirect=0 AND indexed_revid IS NOT NULL AND indexed_revid = fetched_revid) AS indexed,
               SUM(is_redirect=0 AND smw_revid IS NOT NULL AND smw_revid = fetched_revid) AS smw,
               SUM(error_count>0) AS errors
        FROM pages GROUP BY ns ORDER BY ns
        """
    ).fetchall()
    for r in rows:
        t.add_row(*(str(r[c] or 0) for c in ("ns", "pages", "redirects", "fetched", "indexed", "smw", "errors")))
    console.print(t)

    chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    vecs = conn.execute("SELECT count(*) FROM chunk_vec").fetchone()[0]
    redirects = conn.execute("SELECT count(*) FROM redirects").fetchone()[0]
    props = conn.execute("SELECT count(*) FROM item_properties").fetchone()[0]
    pending_fetch = conn.execute(
        "SELECT count(*) FROM pages WHERE is_redirect=0 AND fetched_revid IS NOT latest_revid AND error_count<5"
    ).fetchone()[0]
    pending_index = conn.execute(
        "SELECT count(*) FROM pages WHERE is_redirect=0 AND fetched_revid IS NOT NULL "
        "AND indexed_revid IS NOT fetched_revid"
    ).fetchone()[0]
    console.print(f"chunks={chunks} vectors={vecs} redirects={redirects} item_properties={props}")
    console.print(f"pending: crawl={pending_fetch} index={pending_index}")
    console.print(f"active source = {s.wiki.source}   primary source = {db.get_state(conn, 'primary_source')}")
    for key in ("discover.started_at", "sync.cursor", "sync.last_run", "fullscan.last_run", "embed_model"):
        console.print(f"{key} = {db.get_state(conn, key)}")
    last = conn.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        console.print(f"last sync: {dict(last)}")


if __name__ == "__main__":
    app()
