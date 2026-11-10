import json
import os
import sqlite3

import duckdb
import pytest

from crossql.config import load
from crossql.engine import QueryError, Session, check_writes
from crossql.snapshot import referenced

JOIN = ("SELECT l.channel, sum(i.amount_cents) AS cents FROM app.users u "
        "JOIN billing.invoices i ON i.user_id = u.id AND i.status = 'paid' "
        "JOIN leads l ON l.email = u.email GROUP BY 1 ORDER BY 1")


def test_join_across_sqlite_files_and_csv(world):
    s = Session(load().sources)
    s.attach_all()
    cols, rows = s.query(JOIN)
    assert cols == ["channel", "cents"]
    assert rows == [("google", 5800), ("newsletter", 9900)]


def test_parquet_source(world):
    s = Session(load().sources)
    s.attach_all()
    _, rows = s.query("SELECT u.email, count(*) FROM events e JOIN app.users u ON u.id = e.user_id GROUP BY 1 ORDER BY 1")
    assert rows == [("ann@a.com", 2), ("di@d.com", 1)]


def test_tables_and_refs(world):
    s = Session(load().sources)
    s.attach_all()
    refs = {t.ref: t for t in s.tables()}
    assert set(refs) == {"app.users", "billing.invoices", "leads", "events"}
    assert [c for c, _ in refs["app.users"].columns] == ["id", "email", "plan"]


def test_sources_are_read_only_even_past_the_guard(world):
    s = Session(load().sources)
    s.attach_all()
    with pytest.raises(QueryError, match="(?i)read.only"):
        s.query("WITH x AS (SELECT 1) INSERT INTO app.users VALUES (9, 'x', 'y')")
    assert sqlite3.connect(world / "app.sqlite").execute("select count(*) from users").fetchone()[0] == 4


@pytest.mark.parametrize("sql", ["insert into a values (1)", "  -- hi\nDROP TABLE x", "/* c */ attach 'x' as y",
                                 "COPY t TO 'x.csv'", "install httpfs", "SET threads=1"])
def test_write_guard(sql):
    with pytest.raises(QueryError, match="only runs read queries"):
        check_writes(sql)


def test_write_guard_allows_reads():
    for sql in ("select 1", "WITH a AS (select 1) select * from a", "from leads", "describe app.users", "show tables",
                "summarize leads", "(select 1) union all (select 2)"):
        check_writes(sql)


def test_write_guard_sees_through_ctes_and_multi_statements():
    with pytest.raises(QueryError, match="got UPDATE"):
        check_writes("WITH x AS (SELECT 1) UPDATE app.users SET plan = 'free'")
    with pytest.raises(QueryError, match="one statement"):
        check_writes("select 1; delete from app.users")


def test_referenced_ignores_strings_and_comments():
    names = ["app", "billing", "leads"]
    assert referenced("select * from app.users -- billing\nwhere x = 'leads'", names) == ["app"]
    assert referenced('select * from "billing".invoices join leads using (id)', names) == ["billing", "leads"]


def test_cli_query_formats(world, cli):
    rc, out, _ = cli("query", JOIN, "-F", "csv")
    assert rc == 0 and out.splitlines() == ["channel,cents", "google,5800", "newsletter,9900"]
    rc, out, _ = cli("query", JOIN, "-F", "json")
    assert json.loads(out) == [{"channel": "google", "cents": 5800}, {"channel": "newsletter", "cents": 9900}]
    rc, out, _ = cli("query", JOIN, "-F", "md")
    assert "| channel | cents |" in out and "| google | 5800 |" in out
    rc, out, _ = cli("query", JOIN)
    assert "google" in out and "(2 rows)" in out


def test_cli_query_stdin_file_and_output(world, cli, tmp_path):
    (world / "q.sql").write_text(JOIN + ";\n")
    rc, out, _ = cli("query", "-f", "q.sql", "-F", "csv")
    assert rc == 0 and "google,5800" in out
    rc, _, err = cli("query", JOIN, "-o", "out.parquet", "--count")
    assert rc == 0 and "(2 rows)" in err
    assert duckdb.sql("select count(*) from 'out.parquet'").fetchall()[0][0] == 2
    rc, _, err = cli("query", JOIN, "-o", "out.xlsx")
    assert rc == 2 and ".parquet" in err


def test_cli_limit(world, cli):
    rc, out, _ = cli("query", "select * from app.users order by id", "--limit", "2", "-F", "csv")
    assert out.splitlines() == ["id,email,plan", "1,ann@a.com,pro", "2,bob@b.com,free"]


def test_only_referenced_sources_are_attached(world, cli):
    rc, out, err = cli("query", "select count(*) as n from leads", "-F", "csv",
                       "-s", "broken=postgresql://nobody:pw@127.0.0.1:1/none")
    assert rc == 0 and out.splitlines() == ["n", "4"]


def test_check_reports_failures_without_leaking_passwords(world, cli):
    rc, out, _ = cli("check", "-s", "broken=postgresql://nobody:hunter2@127.0.0.1:1/none", "-s", "gone=missing.csv")
    assert rc == 1
    assert "ok   app" in out and "ok   leads" in out
    assert "FAIL broken" in out and "FAIL gone" in out and "no such file" in out
    assert "hunter2" not in out
    assert "2 of 6 source(s) failed" in out


def test_tables_cli(world, cli):
    rc, out, _ = cli("tables", "-C")
    assert rc == 0 and "app.users" in out and "billing.invoices" in out and "    amount_cents  BIGINT" in out
    rc, out, _ = cli("tables", "-F", "json")
    assert {t["ref"] for t in json.loads(out)} == {"app.users", "billing.invoices", "leads", "events"}


def test_sources_cli_hides_secrets(world, cli):
    rc, out, _ = cli("sources", "-s", "x=mysql://root:hunter2@h/db")
    assert rc == 0 and "root:***@h/db" in out and "hunter2" not in out


def test_query_errors(world, cli):
    rc, _, err = cli("query", "select * from app.nope")
    assert rc == 1 and "nope" in err
    rc, _, err = cli("query", "delete from app.users")
    assert rc == 1 and "only runs read queries" in err
    rc, _, err = cli("query", "select 1", "--no-config")
    assert rc == 2 and "no sources" in err


def test_explain(world, cli):
    rc, out, _ = cli("explain", "select * from app.users where plan = 'pro'")
    assert rc == 0 and "SCAN" in out.upper()


def test_init(tmp_path, cli):
    rc, out, _ = cli("init", cwd=tmp_path)
    assert rc == 0 and (tmp_path / "crossql.toml").exists()
    rc, _, err = cli("init", cwd=tmp_path)
    assert rc == 2 and "already exists" in err
    cfg = load(str(tmp_path / "crossql.toml"))
    assert set(cfg.sources) == {"app", "billing"}
