import csv
import os
import sqlite3
import subprocess
import sys

import duckdb
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PG_URL = os.environ.get("CROSSQL_TEST_PG")  # e.g. postgresql://postgres:pw@localhost:5432/postgres
MYSQL_URL = os.environ.get("CROSSQL_TEST_MYSQL")  # e.g. mysql://root:pw@127.0.0.1:3306/billing
MYSQL_ARGS = os.environ.get("CROSSQL_TEST_MYSQL_ARGS")  # mysql client args for loading data


@pytest.fixture
def world(tmp_path):
    """A small company: users in SQLite, invoices in SQLite, leads in CSV, events in Parquet."""
    app = tmp_path / "app.sqlite"
    db = sqlite3.connect(app)
    db.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, plan TEXT);
        INSERT INTO users VALUES (1,'ann@a.com','pro'), (2,'bob@b.com','free'), (3,'cy@c.com','pro'), (4,'di@d.com','team');
    """)
    db.close()
    bill = tmp_path / "billing.sqlite"
    db = sqlite3.connect(bill)
    db.executescript("""
        CREATE TABLE invoices (id INTEGER PRIMARY KEY, user_id INTEGER, amount_cents INTEGER, status TEXT);
        INSERT INTO invoices VALUES (1,1,2900,'paid'), (2,1,2900,'paid'), (3,3,2900,'failed'), (4,4,9900,'paid');
    """)
    db.close()
    with open(tmp_path / "leads.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["email", "channel"])
        w.writerows([("ann@a.com", "google"), ("bob@b.com", "reddit"), ("cy@c.com", "google"), ("di@d.com", "newsletter")])
    duckdb.sql(f"COPY (SELECT * FROM (VALUES (1, 'login'), (1, 'export'), (4, 'login')) t(user_id, event)) "
               f"TO '{tmp_path / 'events.parquet'}' (FORMAT parquet)")
    (tmp_path / "crossql.toml").write_text("""
[sources.app]
url = "app.sqlite"
[sources.billing]
url = "sqlite:///${BILLING_DIR}/billing.sqlite"
[sources.leads]
path = "leads.csv"
[sources.events]
path = "events.parquet"

[views.revenue_by_channel]
sql = '''
SELECT l.channel, sum(i.amount_cents) / 100 AS revenue
FROM app.users u
JOIN billing.invoices i ON i.user_id = u.id AND i.status = 'paid'
JOIN leads l ON l.email = u.email
GROUP BY 1 ORDER BY 1
'''
[views.active_users]
sql = "SELECT DISTINCT u.email FROM app.users u JOIN events e ON e.user_id = u.id ORDER BY 1"
""")
    os.environ["BILLING_DIR"] = str(tmp_path)
    old = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(old)


def run_cli(*args, cwd=None, env=None):
    e = dict(os.environ, NO_COLOR="1", **(env or {}))
    e.pop("FORCE_COLOR", None)
    p = subprocess.run([sys.executable, "-m", "crossql", *args], capture_output=True, text=True, cwd=cwd, env=e)
    return p.returncode, p.stdout, p.stderr


@pytest.fixture
def cli():
    return run_cli
