"""Index jobs started from the web page: a delta update, a full refresh, and stopping either.

Each job is the same command you would type (`sync`, `sync --full-scan`) run as a child process. That keeps a long
index from freezing the chat (it has its own interpreter and database connection), makes Stop a real process kill
(work is committed in small batches, so nothing is left half-written), and means the page and the command line can
never disagree about what an update does. The child gets the same config file and wiki source as the server.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .config import Settings

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_LINE_BREAKS = re.compile(r"[\r\n]+")
LOG_LINES = 40
MODES = {
    "delta": ("Delta update", ["sync"]),
    "full": ("Full refresh", ["sync", "--full-scan"]),
    "reindex": ("Re-index", ["index", "--reembed-all"]),
}
STALE_AFTER_SECONDS = 180  # a re-index that has not reported for this long is taken to have been interrupted


class JobBusy(RuntimeError):
    """A job is already running; only one at a time."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Job:
    """Everything about one run, replaced as a whole on the next start so a late log line can never leak across."""

    mode: str
    started_at: str
    t0: float
    proc: asyncio.subprocess.Process | None = None
    reader: asyncio.Task | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    stopped: bool = False
    log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def finish(self, exit_code: int | None) -> None:
        if self.finished_at is None:  # first writer wins: the reader and stop() can race at the end
            self.exit_code, self.finished_at = exit_code, _now()


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """End the process and everything it started. On Windows a venv's python.exe is a launcher that spawns the real
    interpreter, so terminating just the launcher would leave the actual job running."""
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(proc.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)  # started in its own session, so this reaches the children too
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=8)
        except asyncio.TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)


class JobManager:
    def __init__(self, settings: Settings):
        self.s = settings
        self.job: Job | None = None
        self._lock = asyncio.Lock()  # serialises start/stop so a double click cannot launch two jobs

    # ------------------------------------------------------------------------------------ commands

    def command(self, mode: str) -> list[str]:
        """The child process for a mode: the console command's entry point, with the server's own config and source."""
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
        cmd = [sys.executable, "-m", "epedia.cli"]
        if self.s.config_path:
            cmd += ["--config", str(self.s.config_path)]
        cmd += ["--source", self.s.wiki.source, *MODES[mode][1]]
        return cmd

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "TERM": "dumb"})
        return env

    # ------------------------------------------------------------------------------------ control

    @property
    def running(self) -> bool:
        return self.job is not None and self.job.running

    async def start(self, mode: str) -> dict[str, Any]:
        cmd = self.command(mode)
        async with self._lock:
            if self.running:
                raise JobBusy(f"a {MODES[self.job.mode][0].lower()} is already running")  # type: ignore[union-attr]
            job = Job(mode, _now(), time.monotonic())
            self.job = job
            kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console window pops up
            else:
                kwargs["start_new_session"] = True
            try:
                job.proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=str(self.s.root), env=self._env(),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **kwargs,
                )
            except OSError as exc:  # report it as a failed run, not an error page with a half-started state
                job.log.append(f"could not start: {exc}")
                job.finish(-1)
                return self.snapshot()
            job.reader = asyncio.create_task(self._pump(job))
            return self.snapshot()

    async def _pump(self, job: Job) -> None:
        """Collect the child's output into the log tail. Reads raw chunks and splits lines itself, so a progress bar
        redrawn with carriage returns (or one very long line) can neither stall the pipe nor kill the reader."""
        proc = job.proc
        assert proc is not None and proc.stdout is not None
        buf = ""
        try:
            while chunk := await proc.stdout.read(65536):
                parts = _LINE_BREAKS.split(buf + _ANSI.sub("", chunk.decode("utf-8", "replace")))
                buf = parts.pop()
                for line in parts:
                    if line.strip():
                        job.log.append(line.strip()[:300])
            if buf.strip():
                job.log.append(buf.strip()[:300])
        except Exception as exc:  # never leave the child blocked on a pipe nobody reads
            job.log.append(f"(log reader stopped: {exc})")
            await _kill_tree(proc)
        finally:
            job.finish(await proc.wait())

    async def stop(self) -> dict[str, Any]:
        """End the running job. Safe at any moment: every stage commits in small batches and resumes where it left off."""
        async with self._lock:
            job = self.job
            if job is not None and job.running and job.proc is not None:
                job.stopped = True
                await _kill_tree(job.proc)
                if job.reader is not None:
                    await asyncio.wait({job.reader}, timeout=10)
                job.finish(job.proc.returncode if job.proc.returncode is not None else -1)
            return self.snapshot()

    async def wait(self, timeout: float = 30.0) -> None:
        """Wait for the current job to end (used by tests)."""
        if self.job and self.job.reader:
            await asyncio.wait_for(asyncio.shield(self.job.reader), timeout)

    def snapshot(self) -> dict[str, Any]:
        job = self.job
        if job is None:
            return {"running": False, "mode": None, "label": None, "started_at": None, "finished_at": None,
                    "elapsed_s": None, "exit_code": None, "stopped": False, "log": []}
        return {
            "running": job.running,
            "mode": job.mode,
            "label": MODES[job.mode][0],
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "elapsed_s": round(time.monotonic() - job.t0) if job.running else None,
            "exit_code": job.exit_code,
            "stopped": job.stopped,
            "log": list(job.log),
        }


def find_reindex_pids() -> list[int]:
    """Processes running `index --reembed-all` from outside the page (a terminal, a scheduled task), found by command
    line: only the top of each process tree, since stopping that ends its children too."""
    pairs: list[tuple[int, int]] = []
    if sys.platform == "win32":
        script = (
            "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(python|pythonw|qqueryelanthia|epedia)(\\.exe)?$' "
            "-and $_.CommandLine -match 'index --reembed-all' } | ForEach-Object { \"$($_.ProcessId),$($_.ParentProcessId)\" }"
        )
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                                 timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        pairs = [(int(a), int(b)) for a, b in (ln.split(",") for ln in out.split() if "," in ln)]
    else:
        for entry in os.listdir("/proc") if os.path.isdir("/proc") else []:
            if not entry.isdigit():
                continue
            try:
                cmd = open(f"/proc/{entry}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
                ppid = int(open(f"/proc/{entry}/stat").read().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            if "epedia" in cmd and "index --reembed-all" in cmd:
                pairs.append((int(entry), ppid))
    ids = {p for p, _ in pairs}
    return [p for p, parent in pairs if parent not in ids]


def kill_pid_tree(pid: int) -> None:
    """End a process and what it started (taskkill /T on Windows; a plain terminate elsewhere)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def mark_reindex_interrupted(conn: sqlite3.Connection) -> None:
    """After stopping a re-index from the page: show it as stopped now, not three minutes from now."""
    row = conn.execute("SELECT value FROM state WHERE key='reindex.progress'").fetchone()
    if not row:
        return
    try:
        info = json.loads(row["value"])
    except ValueError:
        return
    if info.get("finished_at"):
        return
    info["updated_at"] = "1970-01-01T00:00:00Z"
    conn.execute("UPDATE state SET value=? WHERE key='reindex.progress'", (json.dumps(info),))


def reindex_status(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Progress of a whole-index re-chunk (from the command line or the page), or None if there never was one."""
    try:
        row = conn.execute("SELECT value FROM state WHERE key='reindex.progress'").fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        info = json.loads(row["value"])
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(info["updated_at"].replace("Z", "+00:00"))).total_seconds()
    except (ValueError, KeyError, TypeError):
        return None
    total, done = info.get("total") or 0, info.get("done") or 0
    state = "finished" if info.get("finished_at") else "running" if age < STALE_AFTER_SECONDS else "interrupted"
    return {"state": state, "done": done, "total": total, "pct": round(100 * done / total, 1) if total else 100.0,
            "started_at": info.get("started_at"), "updated_at": info.get("updated_at"),
            "finished_at": info.get("finished_at")}


def index_info(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """When the index was last updated, by what, and how much work is waiting. Tolerates an unbuilt database."""
    def one(sql: str, *args: Any) -> sqlite3.Row | None:
        try:
            return conn.execute(sql, args).fetchone()
        except sqlite3.OperationalError:
            return None

    def run(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {"id": row["id"], "at": row["run_at"], "changed": row["changed"], "deleted": row["deleted"],
                "errors": row["errors"], "seconds": row["seconds"]}

    delta = one("SELECT * FROM sync_log WHERE kind LIKE 'rev:%' OR kind='rc' ORDER BY id DESC LIMIT 1")
    full = one("SELECT * FROM sync_log WHERE kind='full' ORDER BY id DESC LIMIT 1")
    state = {}
    try:
        state = {r["key"]: r["value"] for r in conn.execute(
            "SELECT key, value FROM state WHERE key IN ('sync.cursor', 'primary_source')")}
    except sqlite3.OperationalError:
        pass
    pending = one(
        "SELECT SUM(is_redirect=0 AND (fetched_revid IS NOT latest_revid OR fetched_revid IS NULL)) AS crawl, "
        "SUM(is_redirect=0 AND fetched_revid IS NOT NULL AND indexed_revid IS NOT fetched_revid) AS idx FROM pages"
    )
    return {
        "last_delta": run(delta),
        "last_full": run(full),
        "newest_edit_seen": state.get("sync.cursor"),
        "source": settings.wiki.source,
        "delta_source": settings.wiki.delta_source or settings.wiki.source,
        "primary_source": state.get("primary_source"),
        "pending_crawl": (pending["crawl"] or 0) if pending else 0,
        "pending_index": (pending["idx"] or 0) if pending else 0,
        "reindex": reindex_status(conn),
        "chunks": (one("SELECT count(*) AS n FROM chunks") or {"n": 0})["n"],
    }


URL_SAFE = ":/(),!*'"


def index_changes(conn: sqlite3.Connection, settings: Settings, sync_id: int, limit: int = 2000) -> dict[str, Any] | None:
    """The pages one sync run touched, for the "View details" list. None if there is no such run."""
    try:
        run = conn.execute("SELECT * FROM sync_log WHERE id=?", (sync_id,)).fetchone()
        if run is None:
            return None
        rows = conn.execute(
            "SELECT title, action FROM sync_pages WHERE sync_id=? ORDER BY action, title LIMIT ?", (sync_id, limit)
        ).fetchall()
        total = conn.execute("SELECT count(*) FROM sync_pages WHERE sync_id=?", (sync_id,)).fetchone()[0]
    except sqlite3.OperationalError:
        return None
    base = settings.wiki.base_url.rstrip("/")
    return {
        "id": sync_id,
        "kind": "full" if run["kind"] == "full" else "delta",
        "at": run["run_at"],
        "changed": run["changed"],
        "deleted": run["deleted"],
        "recorded": total > 0,  # runs from before this feature kept only counts
        "total": total,
        "expected": (run["changed"] or 0) + (run["deleted"] or 0),  # more than total when the list was capped
        "pages": [{"title": r["title"], "action": r["action"],
                   "url": f"{base}/{quote(r['title'].replace(' ', '_'), safe=URL_SAFE)}"} for r in rows],
    }
