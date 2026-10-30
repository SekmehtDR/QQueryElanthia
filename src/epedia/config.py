"""Settings loaded from epedia.toml with EPEDIA_<SECTION>__<KEY> environment overrides."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .kinds import DEFAULT_KIND_WEIGHTS


class DbCfg(BaseModel):
    path: str = "data/wiki.db"


class SourceCfg(BaseModel):
    """One MediaWiki API we can read from (the original site or a mirror)."""

    api_url: str
    rps: float = 2.5
    concurrency: int = 3
    maxlag: int = 5


def _default_sources() -> dict[str, SourceCfg]:
    # Only the original wiki is built in. A mirror is added in epedia.toml ([wiki.sources.<name>]).
    return {"original": SourceCfg(api_url="https://elanthipedia.play.net/api.php", rps=2.5)}


class WikiCfg(BaseModel):
    # Which source crawl/sync/smw read from. Override per command with `epedia --source <name> ...`.
    source: str = "original"
    # A second source tried when the active one is unreachable or does not have a page yet (a mirror's fallback is
    # normally the original). Unset means no fallback.
    fallback_source: str | None = None
    # Which source `sync` (the delta update) reads. Unset means the active source. With a mirror as the active source
    # this is normally the original: bulk work (full refresh, crawl) comes from the fast mirror, while "what changed"
    # always comes from the authoritative wiki, which is never behind.
    delta_source: str | None = None
    sources: dict[str, SourceCfg] = Field(default_factory=_default_sources)
    # Canonical site used for citation links, whichever source the content was read from.
    base_url: str = "https://elanthipedia.play.net/"
    # "{version}" is replaced with the running version, so the wiki always sees which release is calling.
    # validate_default: pydantic skips validators on defaults, and the {version} fill-in must run on this one too.
    user_agent: str = Field(
        default="QQueryElanthia/{version} (personal Elanthipedia Q&A indexer; contact: CHANGE-ME) python-httpx",
        validate_default=True,
    )
    namespaces: list[int] = Field(default_factory=lambda: [0, 208, 114, 110, 112, 118, 116, 120, 14])
    item_namespaces: list[int] = Field(default_factory=lambda: [110, 112, 114, 116])
    category_min_bytes: int = 200

    @field_validator("user_agent")
    @classmethod
    def _fill_version(cls, value: str) -> str:
        from . import __version__

        return value.replace("{version}", __version__)

    @property
    def active(self) -> SourceCfg:
        try:
            return self.sources[self.source]
        except KeyError:
            raise ValueError(f"unknown wiki source {self.source!r}; configured: {', '.join(self.sources)}") from None

    @property
    def api_url(self) -> str:
        return self.active.api_url

    @property
    def rps(self) -> float:
        return self.active.rps

    @property
    def concurrency(self) -> int:
        return self.active.concurrency

    @property
    def maxlag(self) -> int:
        return self.active.maxlag


class OllamaCfg(BaseModel):
    base_url: str = "http://localhost:11434"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    chat_model: str = "qwen3:8b"
    num_ctx: int = 16384
    keep_alive: str = "30m"
    # An answer that fails before its first word (the model was loading, a dropped connection) is tried once more.
    # Never after a word has been shown: the reader would see the answer start twice.
    retry_before_first_token: bool = True
    # Room kept free in the context window for the answer itself, when checking that the prompt fits.
    answer_reserve_tokens: int = 1500


class ChunkingCfg(BaseModel):
    target_tokens: int = 450
    max_tokens: int = 700
    overlap_tokens: int = 60
    min_section_tokens: int = 80


class RetrievalCfg(BaseModel):
    k_fts: int = 40
    k_vec: int = 40
    rrf_k: int = 60
    k_final: int = 8
    per_page_cap: int = 3
    title_exact_boost: float = 0.05
    title_partial_boost: float = 0.02
    topic_boost: float = 0.25  # score multiplier (x1 + this x weight) for pages in the topic family the question names
    prior_boost: float = 0.012  # bonus for chunks the previous answer used (follow-up continuity)
    ns_weights: dict[str, float] = Field(
        default_factory=lambda: {"120": 0.8, "14": 0.7, "4": 0.9, "12": 0.9}
    )
    disambig_weight: float = 0.8
    log_weight: float = 0.5  # role-play log subpages (Character/Logs/...)
    obsolete_weight: float = 0.75  # pages the wiki itself marks obsolete
    offer_related: bool = True  # end answers with one offer drawn from related pages
    related_k: int = 3
    history_edits: int = 10  # recent edits of a page shown with answers; changeable from the page (0 = off)
    # question kind -> page kind -> multiplier. Values from epedia.toml are merged over the defaults.
    kind_weights: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {q: dict(r) for q, r in DEFAULT_KIND_WEIGHTS.items()}
    )

    @field_validator("kind_weights", mode="before")
    @classmethod
    def _merge_kind_weights(cls, value: Any) -> dict[str, dict[str, float]]:
        merged = {q: dict(r) for q, r in DEFAULT_KIND_WEIGHTS.items()}
        for question_kind, row in (value or {}).items():
            merged.setdefault(question_kind, {}).update(row)
        return merged


class PersonaCfg(BaseModel):
    name: str = "Fennick"
    enabled: bool = True


class ChatCfg(BaseModel):
    history_turns: int = 4
    context_token_budget: int = 6000
    temperature: float = 0.2
    # A draft that names things no passage has, or pretends to look something up, is written once more (core/verify.py):
    # "fabrication" (the default) or "off". Anything else the checks find is marked, never rewritten.
    regenerate: Literal["fabrication", "off"] = "fabrication"


class WebCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    # Sharing through a tunnel: visitors who are not at this computer must enter this code (set it in the git-ignored
    # epedia.local.toml). Unset means no login, which is only safe while the page is not shared.
    access_code: str | None = None
    max_concurrent: int = 2  # answers being written at once (one GPU); others wait their turn
    max_message_chars: int = 6000  # longest message accepted (a pasted post fits)
    # While the server runs it starts a delta update this often (minutes); 0 turns it off. Nothing runs when it is not.
    auto_sync_minutes: int = 60
    # Questions a visitor through the tunnel may ask per hour (one GPU is shared); 0 means no limit. The owner is never limited.
    visitor_turns_per_hour: int = 40

    @field_validator("auto_sync_minutes")
    @classmethod
    def _sync_interval(cls, value: int) -> int:
        if value != 0 and value < 5:
            raise ValueError("web.auto_sync_minutes must be 0 (off) or at least 5")
        return value

    @field_validator("access_code")
    @classmethod
    def _code_strength(cls, value: str | None) -> str | None:
        value = (value or "").strip() or None
        if value is not None and len(value) < 8:
            raise ValueError("web.access_code must be at least 8 characters")
        return value


class Settings(BaseModel):
    db: DbCfg = Field(default_factory=DbCfg)
    wiki: WikiCfg = Field(default_factory=WikiCfg)
    ollama: OllamaCfg = Field(default_factory=OllamaCfg)
    chunking: ChunkingCfg = Field(default_factory=ChunkingCfg)
    retrieval: RetrievalCfg = Field(default_factory=RetrievalCfg)
    chat: ChatCfg = Field(default_factory=ChatCfg)
    persona: PersonaCfg = Field(default_factory=PersonaCfg)
    web: WebCfg = Field(default_factory=WebCfg)
    root: Path = Field(default_factory=Path.cwd)
    config_path: Path | None = None  # the epedia.toml these settings came from (child processes reuse it)

    @property
    def db_path(self) -> Path:
        p = Path(self.db.path)
        return p if p.is_absolute() else self.root / p


def _find_config(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    env = os.environ.get("EPEDIA_CONFIG")
    if env:
        return Path(env)
    here = Path.cwd()
    for d in [here, *here.parents]:
        candidate = d / "epedia.toml"
        if candidate.exists():
            return candidate
    return None


SOURCE_ALIASES = ("rps", "concurrency", "maxlag")  # EPEDIA_WIKI__RPS etc. apply to the active source


def _env_items() -> list[tuple[list[str], Any]]:
    """(path parts, parsed value) for every EPEDIA_<A>__<B>[__<C>...] environment variable."""
    out: list[tuple[list[str], Any]] = []
    for key, raw in os.environ.items():
        if not key.startswith("EPEDIA_") or "__" not in key:
            continue
        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        out.append((key[len("EPEDIA_"):].lower().split("__"), value))
    return out


def _apply_env(data: dict[str, Any]) -> None:
    """Plain section/key overrides (EPEDIA_CHAT__TEMPERATURE=0.5), applied before validation."""
    for parts, value in _env_items():
        if len(parts) == 2 and not (parts[0] == "wiki" and parts[1] in SOURCE_ALIASES):
            data.setdefault(parts[0], {})[parts[1]] = value


def _apply_env_to_sources(settings: Settings) -> None:
    """Overrides for per-source rates, applied after validation (a source needs its api_url to validate):
    EPEDIA_WIKI__RPS=2 changes the active source; EPEDIA_WIKI__SOURCES__MIRROR__RPS=4 a named one."""
    for parts, value in _env_items():
        if parts[0] != "wiki":
            continue
        if len(parts) == 2 and parts[1] in SOURCE_ALIASES:
            setattr(settings.wiki.active, parts[1], value)
        elif len(parts) == 4 and parts[1] == "sources" and parts[3] in SOURCE_ALIASES:
            source = settings.wiki.sources.get(parts[2])
            if source is not None:
                setattr(source, parts[3], value)


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> None:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def load_settings(path: str | None = None) -> Settings:
    cfg_path = _find_config(path)
    data: dict[str, Any] = {}
    root = Path.cwd()
    if cfg_path and cfg_path.exists():
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        root = cfg_path.parent.resolve()
        # Optional machine-local overrides (git-ignored): extra wiki sources, contact details, ...
        local = cfg_path.with_name(cfg_path.stem + ".local.toml")
        if local.exists():
            _deep_merge(data, tomllib.loads(local.read_text(encoding="utf-8")))
    _apply_env(data)
    settings = Settings.model_validate(data)
    settings.root = root
    settings.config_path = cfg_path.resolve() if cfg_path and cfg_path.exists() else None
    _apply_env_to_sources(settings)
    return settings
