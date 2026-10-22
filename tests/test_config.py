from epedia.config import Settings, load_settings


def test_defaults_only_know_the_original_wiki_and_use_it():
    s = Settings()
    assert list(s.wiki.sources) == ["original"]
    assert s.wiki.source == "original"
    assert s.wiki.api_url.startswith("https://elanthipedia.play.net/")


def test_user_agent_identifies_the_tool_and_asks_for_a_contact():
    ua = Settings().wiki.user_agent
    assert ua.startswith("QQueryElanthia/") and "contact" in ua


def test_wiki_client_retry_ladder_is_configurable():
    from epedia.wiki.client import BACKOFF, WikiClient

    cfg = Settings().wiki
    assert WikiClient(cfg).backoff == BACKOFF
    assert WikiClient(cfg, retries=1).backoff == BACKOFF[:1]
    assert WikiClient(cfg, retries=0).backoff == []


def test_local_toml_is_merged_over_main_config(tmp_path):
    (tmp_path / "epedia.toml").write_text(
        '[wiki]\nsource = "original"\n[wiki.sources.original]\napi_url = "https://a.example/api.php"\nrps = 1.0\n',
        encoding="utf-8",
    )
    (tmp_path / "epedia.local.toml").write_text(
        '[wiki.sources.mirror]\napi_url = "https://b.example/api.php"\nrps = 4.0\n[persona]\nname = "Pellam"\n',
        encoding="utf-8",
    )
    s = load_settings(str(tmp_path / "epedia.toml"))
    assert set(s.wiki.sources) == {"original", "mirror"}
    assert s.wiki.sources["original"].rps == 1.0 and s.wiki.sources["mirror"].rps == 4.0
    assert s.persona.name == "Pellam"


def test_env_overrides_reach_properties_and_named_sources(tmp_path, monkeypatch):
    (tmp_path / "epedia.toml").write_text(
        '[wiki]\nsource = "original"\n[wiki.sources.original]\napi_url = "https://a.example/api.php"\nrps = 2.5\n'
        '[wiki.sources.mirror]\napi_url = "https://b.example/api.php"\nrps = 3.0\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("EPEDIA_WIKI__RPS", "1.5")
    monkeypatch.setenv("EPEDIA_WIKI__SOURCES__MIRROR__RPS", "7")
    monkeypatch.setenv("EPEDIA_CHAT__TEMPERATURE", "0.6")
    s = load_settings(str(tmp_path / "epedia.toml"))
    assert s.wiki.rps == 1.5 and s.wiki.sources["original"].rps == 1.5
    assert s.wiki.sources["mirror"].rps == 7
    assert s.chat.temperature == 0.6


def test_env_rate_override_works_without_a_config_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EPEDIA_WIKI__CONCURRENCY", "1")
    assert load_settings().wiki.concurrency == 1


def test_unknown_source_is_a_clear_error():
    s = Settings()
    s.wiki.source = "nope"
    try:
        s.wiki.active
    except ValueError as exc:
        assert "nope" in str(exc) and "original" in str(exc)
    else:
        raise AssertionError("expected ValueError")
