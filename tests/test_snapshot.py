import json
import os
import sqlite3
import time

import duckdb
import pytest

from crossql.config import load
from crossql.engine import QueryError, Session
from crossql.snapshot import build


def test_snapshot_sqlite(world, cli):
    rc, out, err = cli("snapshot", "-o", "out/analytics.sqlite")
    assert rc == 0, err
    assert "revenue_by_channel" in out and "snapshot out/analytics.sqlite: 2 view(s) from 4 source(s)" in out
    db = sqlite3.connect(world / "out" / "analytics.sqlite")
    assert db.execute("select * from revenue_by_channel order by 1").fetchall() == [("google", 58), ("newsletter", 99)]
    assert db.execute("select * from active_users").fetchall() == [("ann@a.com",), ("di@d.com",)]
    meta = db.execute("select view, row_count, sources from _crossql_snapshot order by 1").fetchall()
    assert meta == [("active_users", 2, "app,events"), ("revenue_by_channel", 2, "app,billing,leads")]
    assert not [f for f in os.listdir(world / "out") if f.startswith(".crossql-")]


def test_snapshot_duckdb_and_only(world, cli):
    rc, out, err = cli("snapshot", "-o", "a.duckdb", "--only", "active_users")
    assert rc == 0, err
    con = duckdb.connect(str(world / "a.duckdb"), read_only=True)
    assert {r[0] for r in con.execute("show tables").fetchall()} == {"active_users", "_crossql_snapshot"}


def test_snapshot_parquet_dir_and_views_file(world, cli):
    (world / "extra.sql").write_text("-- name: pro_users\nSELECT email FROM app.users WHERE plan = 'pro' ORDER BY 1;\n")
    rc, out, err = cli("snapshot", "-o", "lake/", "--views", "extra.sql")
    assert rc == 0, err
    files = sorted(os.listdir(world / "lake"))
    assert files == ["_crossql_snapshot.json", "active_users.parquet", "pro_users.parquet", "revenue_by_channel.parquet"]
    assert duckdb.sql(f"select count(*) from '{world / 'lake' / 'pro_users.parquet'}'").fetchall()[0][0] == 2
    meta = json.loads((world / "lake" / "_crossql_snapshot.json").read_text())
    assert {v["view"] for v in meta["views"]} == {"active_users", "pro_users", "revenue_by_channel"}
    rc, _, _ = cli("snapshot", "-o", "lake/", "--file-format", "csv", "--only", "pro_users", "--views", "extra.sql")
    assert sorted(os.listdir(world / "lake")) == ["_crossql_snapshot.json", "pro_users.csv"]


def test_max_age_skips_fresh_snapshot(world, cli):
    assert cli("snapshot", "-o", "a.sqlite")[0] == 0
    before = os.path.getmtime(world / "a.sqlite")
    time.sleep(0.05)
    rc, out, _ = cli("snapshot", "-o", "a.sqlite", "--max-age", "10m")
    assert rc == 0 and "is fresh" in out and os.path.getmtime(world / "a.sqlite") == before
    rc, out, _ = cli("snapshot", "-o", "a.sqlite", "--max-age", "0")
    assert "built" in out


def test_failed_rebuild_keeps_previous_snapshot(world):
    cfg = load()
    s = Session(cfg.sources)
    s.attach_all()
    out = str(world / "keep.sqlite")
    build(s, {"ok": "select 1 as x"}, out, log=lambda *a: None)
    with pytest.raises(QueryError, match="view bad"):
        build(s, {"ok2": "select 2 as x", "bad": "select * from app.missing"}, out, log=lambda *a: None)
    db = sqlite3.connect(out)
    assert db.execute("select * from ok").fetchall() == [(1,)]
    assert [f for f in os.listdir(world) if f.startswith(".crossql-")] == []


def test_snapshot_needs_views_and_output(world, cli, tmp_path):
    rc, _, err = cli("snapshot")
    assert rc == 2 and "-o analytics.sqlite" in err
    rc, _, err = cli("snapshot", "-o", "x.sqlite", "--no-config", "-s", "l=leads.csv")
    assert rc == 2 and "nothing to snapshot" in err
    rc, _, err = cli("snapshot", "-o", "x.txt")
    assert rc == 2 and ".sqlite" in err


def test_snapshot_from_config_section(world, cli):
    with open(world / "crossql.toml", "a") as fh:
        fh.write('\n[snapshot]\noutput = "bi/analytics.sqlite"\nmax_age = "1h"\n')
    rc, out, err = cli("snapshot")
    assert rc == 0, err
    assert (world / "bi" / "analytics.sqlite").exists()
    rc, out, _ = cli("snapshot")
    assert "is fresh" in out
