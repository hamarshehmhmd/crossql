import os

import pytest

from crossql.config import (ConfigError, detect_type, expand_env, find_config, load, make_source, parse_duration,
                            parse_flag, parse_views_sql, redact)


@pytest.mark.parametrize("url,t", [
    ("postgresql://u:p@h/db", "postgres"), ("postgres://h/db", "postgres"), ("pg://h/db", "postgres"),
    ("host=localhost dbname=app user=me", "postgres"), ("mysql://r@h:3306/b", "mysql"), ("mariadb://h/b", "mysql"),
    ("sqlite:///tmp/x.db", "sqlite"), ("data/app.sqlite", "sqlite"), ("x.sqlite3", "sqlite"), ("w.duckdb", "duckdb"),
    ("leads.csv", "csv"), ("exports/*.csv", "csv"), ("e.parquet", "parquet"), ("x.jsonl", "json"), ("a.TSV", "csv"),
])
def test_detect_type(url, t):
    assert detect_type(url) == t


def test_detect_type_unknown():
    with pytest.raises(ConfigError, match="cannot tell"):
        detect_type("http://example.com/thing")


@pytest.mark.parametrize("url,want", [
    ("postgresql://alice:s3cret@db:5432/app", "postgresql://alice:***@db:5432/app"),
    ("mysql://root:pw@127.0.0.1/billing", "mysql://root:***@127.0.0.1/billing"),
    ("host=db user=a password=s3cret dbname=x", "host=db user=a password=*** dbname=x"),
    ("postgresql://db/app?user=a&password=s3cret", "postgresql://db/app?user=a&password=***"),
    ("postgresql://db/app", "postgresql://db/app"),
])
def test_redact(url, want):
    assert redact(url) == want


def test_env_expansion(monkeypatch):
    monkeypatch.setenv("CQ_URL", "postgresql://x/y")
    monkeypatch.delenv("CQ_MISSING", raising=False)
    assert expand_env("${CQ_URL}", "t") == "postgresql://x/y"
    assert expand_env("${CQ_MISSING:-mysql://h/db}", "t") == "mysql://h/db"
    with pytest.raises(ConfigError, match=r"\$\{CQ_MISSING\} is not set"):
        expand_env("${CQ_MISSING}", "t")


def test_source_display_hides_expanded_secret(monkeypatch):
    monkeypatch.setenv("CQ_URL", "postgresql://u:topsecret@h/db")
    s = make_source("app", "${CQ_URL}")
    assert s.type == "postgres" and "topsecret" in s.url
    assert s.display() == "${CQ_URL}"


def test_names_validated():
    with pytest.raises(ConfigError, match="SQL identifier"):
        make_source("my-db", "x.csv")
    with pytest.raises(ConfigError, match="reserved"):
        make_source("memory", "x.csv")


def test_parse_flag_with_type(tmp_path):
    s = parse_flag("leads:csv=export.txt")
    assert (s.name, s.type) == ("leads", "csv") and os.path.isabs(s.url)
    with pytest.raises(ConfigError, match="NAME=URL"):
        parse_flag("justaname")


def test_load_relative_paths_and_flags(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    (tmp_path / "crossql.toml").write_text('[sources.a]\nurl = "data/a.sqlite"\n[sources.b]\ntype="csv"\npath="b.txt"\n'
                                           '[snapshot]\noutput = "out.sqlite"\n')
    monkeypatch.chdir(tmp_path / "sub")
    assert find_config() == str(tmp_path / "crossql.toml")
    cfg = load(flags=["c=postgresql://h/db"])
    assert cfg.sources["a"].url == str(tmp_path / "data" / "a.sqlite")
    assert cfg.sources["b"].type == "csv"
    assert cfg.sources["c"].type == "postgres"
    assert cfg.snapshot["output"] == str(tmp_path / "out.sqlite")
    assert load(flags=[], use_file=False).sources == {}


def test_bad_toml(tmp_path):
    p = tmp_path / "crossql.toml"
    p.write_text("[sources.a\n")
    with pytest.raises(ConfigError):
        load(str(p))
    p.write_text("[sources.a]\ntype = 'postgres'\n")
    with pytest.raises(ConfigError, match="needs url"):
        load(str(p))


def test_views_sql():
    v = parse_views_sql("-- comment\n-- name: a\nSELECT 1;\n\n-- name: b_2\nSELECT 2\nFROM x;\n")
    assert v == {"a": "SELECT 1", "b_2": "SELECT 2\nFROM x"}
    with pytest.raises(ConfigError, match="no views"):
        parse_views_sql("SELECT 1")


def test_duration():
    assert parse_duration("90s") == 90 and parse_duration("15m") == 900 and parse_duration("1d") == 86400
    assert parse_duration("0") == 0
    with pytest.raises(ConfigError):
        parse_duration("soon")
