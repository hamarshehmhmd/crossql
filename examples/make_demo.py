"""Create the demo data: a Postgres "app" DB, a MySQL "billing" DB, a SQLite support DB and a CSV export.

Usage: python examples/make_demo.py OUT_DIR [--pg URL] [--mysql URL]
Without --pg / --mysql the same tables are written to SQLite files instead, so the demo works anywhere.
Needs psql / mysql clients only for the server-backed variants.
"""
import argparse
import csv
import os
import random
import shlex
import sqlite3
import subprocess

PLANS = ["free", "starter", "pro", "team"]
PRICE = {"free": 0, "starter": 900, "pro": 2900, "team": 9900}
CHANNELS = ["google", "linkedin", "reddit", "newsletter", "organic"]


def data(seed=7):
    r = random.Random(seed)
    users, invoices, tickets, leads = [], [], [], []
    inv_id = 1
    for uid in range(1, 241):
        plan = r.choices(PLANS, [50, 25, 18, 7])[0]
        ch = r.choice(CHANNELS)
        signup = f"2026-{r.randint(5, 9):02d}-{r.randint(1, 28):02d}"
        email = f"user{uid}@example{uid % 17}.com"
        users.append((uid, email, plan, signup))
        leads.append((email, ch, r.choice(["spring-launch", "hn-post", "webinar", "retargeting"])))
        if PRICE[plan]:
            for m in range(int(signup[5:7]), 11):
                if r.random() < 0.93:
                    invoices.append((inv_id, uid, PRICE[plan], f"2026-{m:02d}-{r.randint(1, 28):02d}", "paid" if r.random() < 0.96 else "failed"))
                    inv_id += 1
        for t in range(r.choice([0, 0, 0, 1, 1, 2, 4])):
            tickets.append((len(tickets) + 1, email, r.choice(["billing", "bug", "how-to", "sso"]), r.choice(["open", "closed", "closed"])))
    for i in range(30):  # leads that never signed up
        leads.append((f"prospect{i}@example.org", r.choice(CHANNELS), "webinar"))
    return users, invoices, tickets, leads


def sql_values(rows):
    def v(x):
        return str(x) if isinstance(x, int) else "'" + str(x).replace("'", "''") + "'"
    return ",\n".join("(" + ", ".join(v(x) for x in row) + ")" for row in rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--pg")
    ap.add_argument("--mysql", help="mysql client args, e.g. --mysql='-h127.0.0.1 -P3306 -uroot -ppw billing'")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    users, invoices, tickets, leads = data()

    users_sql = ("DROP TABLE IF EXISTS users; CREATE TABLE users (id INT PRIMARY KEY, email TEXT NOT NULL, "
                 "plan TEXT NOT NULL, signed_up DATE NOT NULL);\nINSERT INTO users VALUES\n" + sql_values(users) + ";")
    inv_sql = ("DROP TABLE IF EXISTS invoices; CREATE TABLE invoices (id INT PRIMARY KEY, user_id INT NOT NULL, "
               "amount_cents INT NOT NULL, issued_on DATE NOT NULL, status VARCHAR(10) NOT NULL);\nINSERT INTO invoices VALUES\n"
               + sql_values(invoices) + ";")
    if a.pg:
        subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", a.pg], input=users_sql, text=True, check=True)
    else:
        db = sqlite3.connect(os.path.join(a.out, "app.sqlite"))
        db.executescript(users_sql)
        db.close()
    if a.mysql:
        subprocess.run(["mysql", *shlex.split(a.mysql)], input=inv_sql, text=True, check=True)
    else:
        db = sqlite3.connect(os.path.join(a.out, "billing.sqlite"))
        db.executescript(inv_sql)
        db.close()
    db = sqlite3.connect(os.path.join(a.out, "support.sqlite"))
    db.executescript("DROP TABLE IF EXISTS tickets; CREATE TABLE tickets (id INTEGER PRIMARY KEY, email TEXT, topic TEXT, status TEXT);"
                     "INSERT INTO tickets VALUES " + sql_values(tickets) + ";")
    db.close()
    with open(os.path.join(a.out, "leads.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["email", "channel", "campaign"])
        w.writerows(leads)
    print(f"demo data: {len(users)} users, {len(invoices)} invoices, {len(tickets)} tickets, {len(leads)} leads -> {a.out}")


if __name__ == "__main__":
    main()
