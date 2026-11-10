"""End-to-end against real Postgres and MySQL servers (set CROSSQL_TEST_PG / CROSSQL_TEST_MYSQL; CI does)."""
import os
import shlex
import sqlite3
import subprocess
import sys

import pytest

from conftest import MYSQL_ARGS, MYSQL_URL, PG_URL, ROOT

need = pytest.mark.skipif(not (PG_URL and MYSQL_URL and MYSQL_ARGS),
                          reason="set CROSSQL_TEST_PG, CROSSQL_TEST_MYSQL and CROSSQL_TEST_MYSQL_ARGS")


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    out = tmp_path_factory.mktemp("demo")
    subprocess.run([sys.executable, os.path.join(ROOT, "examples", "make_demo.py"), str(out),
                    "--pg", PG_URL, "--mysql=" + MYSQL_ARGS], check=True)
    (out / "crossql.toml").write_text(f"""
[sources.app]
url = "{PG_URL}"
[sources.billing]
url = "{MYSQL_URL}"
[sources.support]
url = "support.sqlite"
[sources.leads]
path = "leads.csv"
""")
    return out


JOIN = """
WITH paid AS (SELECT user_id, sum(amount_cents) AS cents FROM billing.invoices WHERE status = 'paid' GROUP BY 1),
     t AS (SELECT email, count(*) AS n FROM support.tickets GROUP BY 1)
SELECT l.channel, count(*) AS customers, sum(p.cents) AS cents, coalesce(sum(t.n), 0) AS tickets
FROM app.users u JOIN paid p ON p.user_id = u.id
JOIN leads l ON l.email = u.email LEFT JOIN t ON t.email = u.email
GROUP BY 1 ORDER BY 1
"""


def expected(demo_dir):
    """Recompute the same answer in plain Python from the generator's data."""
    sys.path.insert(0, os.path.join(ROOT, "examples"))
    import make_demo

    users, invoices, tickets, leads = make_demo.data()
    paid = {}
    for _, uid, cents, _, status in invoices:
        if status == "paid":
            paid[uid] = paid.get(uid, 0) + cents
    chan = {e: c for e, c, _ in leads}
    tk = {}
    for _, e, _, _ in tickets:
        tk[e] = tk.get(e, 0) + 1
    agg = {}
    for uid, email, _, _ in users:
        if uid in paid:
            a = agg.setdefault(chan[email], [0, 0, 0])
            a[0] += 1
            a[1] += paid[uid]
            a[2] += tk.get(email, 0)
    return [f"{c},{a[0]},{a[1]},{a[2]}" for c, a in sorted(agg.items())]


@need
def test_four_way_join_matches_python(demo, cli):
    rc, out, err = cli("check", cwd=demo)
    assert rc == 0, out + err
    assert "postgres" in out and "mysql" in out
    rc, out, _ = cli("tables", "-F", "json", cwd=demo)
    import json
    assert {"app.users", "billing.invoices", "support.tickets", "leads"} <= {t["ref"] for t in json.loads(out)}
    assert all(t["schema"] == "billing" for t in json.loads(out) if t["source"] == "billing")
    rc, out, err = cli("query", JOIN, "-F", "csv", cwd=demo)
    assert rc == 0, err
    assert out.splitlines()[1:] == expected(demo)


@need
def test_filter_pushdown_and_read_only(demo, cli):
    rc, out, _ = cli("explain", "select * from app.users where plan = 'pro'", cwd=demo)
    assert rc == 0 and "POSTGRES" in out.upper() and "plan='pro'" in out.replace(' ', '')
    from crossql.config import load
    from crossql.engine import QueryError, Session

    s = Session(load(str(demo / "crossql.toml")).sources)
    s.attach_all()
    # bypass crossql's own guard: the databases themselves must refuse
    for sql in ("INSERT INTO app.users VALUES (999, 'x', 'free', DATE '2026-01-01')",
                "UPDATE app.users SET plan = 'free'",
                "UPDATE billing.invoices SET amount_cents = 0",
                "DELETE FROM billing.invoices"):
        with pytest.raises(QueryError, match="(?i)read.only"):
            s.query(sql)
    _, rows = s.query("SELECT (SELECT count(*) FROM billing.invoices WHERE amount_cents = 0), (SELECT count(*) FROM app.users)")
    assert rows == [(0, 240)]


@need
def test_snapshot_from_servers(demo, cli):
    (demo / "views.sql").write_text("-- name: revenue_by_channel\n" + JOIN)
    rc, out, err = cli("snapshot", "-o", "bi.sqlite", "--views", "views.sql", cwd=demo)
    assert rc == 0, err
    rows = sqlite3.connect(demo / "bi.sqlite").execute("select * from revenue_by_channel order by 1").fetchall()
    assert [",".join(map(str, r)) for r in rows] == expected(demo)


@need
def test_bad_password_is_reported_not_leaked(demo, cli):
    bad = MYSQL_URL.replace("://", "://", 1)
    user_part = bad.split("://", 1)[1].split("@", 1)[0]
    bad = bad.replace(user_part + "@", user_part.split(":")[0] + ":wrong-hunter2@", 1)
    rc, out, _ = cli("check", "billing", "-s", "billing=" + bad, cwd=demo)
    assert rc == 1 and "FAIL billing" in out and "wrong-hunter2" not in out
