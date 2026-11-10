# crossql

[![CI](https://github.com/hamarshehmhmd/crossql/actions/workflows/ci.yml/badge.svg)](https://github.com/hamarshehmhmd/crossql/actions/workflows/ci.yml)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Built on DuckDB](https://img.shields.io/badge/built%20on-DuckDB-yellow.svg)

**Join your Postgres app DB with your MySQL billing DB and a CSV export in one SQL query, without a warehouse.**

Your users are in Postgres, invoices are in MySQL (or another Postgres), support tickets are in a SQLite file and marketing sends a CSV. "Revenue by signup channel" needs all of them, and BI tools can't join across databases: [Metabase #3953 "Join across multiple databases"](https://github.com/metabase/metabase/issues/3953) has been open since 2016 with **2,110 reactions**, and ["Join between tables in two different databases?"](https://stackoverflow.com/questions/5698378/join-between-tables-in-two-different-databases) has **191k views** ([the SQLite version](https://stackoverflow.com/questions/6824717/sqlite-how-do-you-join-tables-from-different-databases) another 101k). The usual answers are a warehouse plus ELT (costly, slow to set up) or Trino / foreign data wrappers (real engineering).

`crossql` is a small CLI on top of [DuckDB](https://duckdb.org): name your sources once, then write normal SQL with `source.table` and it attaches each database **read-only**, pushes filters down, and joins the rest locally. `crossql snapshot` writes your cross-source queries into a single `.sqlite` / `.duckdb` file (or Parquet folder) so Metabase, Superset, Grafana or Redash can chart them today.

![crossql demo](docs/demo.gif)

```sh
curl -fsSL https://raw.githubusercontent.com/hamarshehmhmd/crossql/main/install.sh | sh
```

(or `pipx install git+https://github.com/hamarshehmhmd/crossql` / `uv tool install git+https://github.com/hamarshehmhmd/crossql`. Python 3.10+, the only dependency is `duckdb`. Linux and macOS.)

## Quick start

```sh
crossql init                      # writes an example crossql.toml
```

```toml
# crossql.toml (URLs can use ${ENV_VARS} so secrets stay out of the file)
[sources.app]
url = "${APP_DATABASE_URL}"                 # postgresql://reader@db/app
[sources.billing]
url = "${BILLING_DATABASE_URL}"             # mysql://reader:...@billing-db:3306/billing
[sources.support]
url = "support.sqlite"
[sources.leads]
path = "exports/leads.csv"                  # files and globs: query as `leads`
```

```sh
crossql check                     # connects to each source, exit 1 if any fail
crossql tables -C                 # what you can query, and how to name it
crossql query "
  SELECT l.channel, count(DISTINCT u.id) AS customers, sum(i.amount_cents) / 100 AS revenue
  FROM app.users u
  JOIN billing.invoices i ON i.user_id = u.id AND i.status = 'paid'
  JOIN leads l ON l.email = u.email
  GROUP BY 1 ORDER BY revenue DESC"
```

No config file? Pass sources inline: `crossql query -s app=postgresql://localhost/app -s leads=leads.csv "SELECT ..."`.

## Commands

| command | what it does |
|---|---|
| `crossql init` | write an example `crossql.toml` |
| `crossql sources` | list sources (passwords shown as `***`, `${VARS}` shown unexpanded) |
| `crossql check [NAME...]` | connect to each source, show table count and latency; exit 1 on any failure |
| `crossql tables [NAME] [-C]` | list tables with the name to use in SQL (`-C` for columns, `-F json` for scripts) |
| `crossql query SQL` | run one query; `-f file.sql` or stdin also work; `-F table\|csv\|tsv\|json\|jsonl\|md`; `-o out.parquet\|.csv\|.json`; `--limit N` |
| `crossql explain SQL` | DuckDB's plan, so you can see which filters are pushed down to each database |
| `crossql snapshot -o FILE` | build named views into `.sqlite`, `.duckdb` or a Parquet/CSV directory; `--max-age 15m`, `--every 15m` |

### Sources

| type | URL / path | name it in SQL as |
|---|---|---|
| Postgres | `postgresql://user:pass@host:5432/db` or `host=... dbname=...` | `app.table` (schema `public`; set `schema = "x"` or use `app.schema.table`) |
| MySQL / MariaDB | `mysql://user:pass@host:3306/db` | `billing.table` |
| SQLite | `file.sqlite`, `file.db`, `sqlite:///abs/path.db` | `support.table` |
| DuckDB | `file.duckdb` | `name.table` |
| CSV / TSV | `leads.csv`, `exports/*.csv` (extra keys are passed to `read_csv_auto`, e.g. `delim = ";"`) | `leads` |
| Parquet / JSON | `events.parquet`, `logs/*.jsonl` | `events` |

The type is detected from the URL scheme or file extension; set `type = "..."` (or `-s name:csv=file.txt`) to override. Relative paths are resolved from the config file's directory.

Only the sources a query mentions are attached, so one database being down doesn't break queries that don't use it.

## Put it in your BI tool: `crossql snapshot`

Define the cross-source questions once:

```toml
[views.revenue_by_channel]
sql = """
SELECT l.channel, count(DISTINCT u.id) AS customers, sum(i.amount_cents) / 100 AS revenue
FROM app.users u JOIN billing.invoices i ON i.user_id = u.id AND i.status = 'paid'
JOIN leads l ON l.email = u.email GROUP BY 1
"""

[snapshot]
output = "analytics.sqlite"
max_age = "15m"
```

(or keep them in a `.sql` file with `-- name: revenue_by_channel` headers and pass `--views views.sql`.)

```sh
crossql snapshot                         # one table per view + a _crossql_snapshot table (rows, time, sources, built_at)
crossql snapshot --every 15m             # keep it fresh (or run it from cron)
```

Then add `analytics.sqlite` to Metabase / Superset / Grafana / Redash as a SQLite database (or write `.duckdb` for Metabase's DuckDB driver, Superset, Evidence or Rill, or a directory for Parquet). Each build is written to a temp file and renamed into place, so dashboards never read a half-written snapshot, and a failed build leaves the previous one untouched.

## Safety

- **Read-only by design.** Every database is attached with DuckDB's `READ_ONLY` (for MySQL the server-side transaction is read-only too), and before anything is sent crossql parses the SQL with DuckDB's parser and refuses anything that isn't a single `SELECT` (so `WITH x AS (...) UPDATE ...` doesn't slip through). The test suite checks that `INSERT`/`UPDATE`/`DELETE` are refused by real Postgres and MySQL servers even with crossql's own guard bypassed. Still, give it a read-only database user.
- **Secrets.** Use `${ENV_VAR}` in `crossql.toml`; passwords are masked as `***` in `sources`, `check` and error messages.
- **Load.** Filters and projections are pushed down, but joins and aggregates run locally in DuckDB, so a query that scans a big table pulls those rows over the network. Use `crossql explain` to check, point it at a replica for heavy queries, and use snapshots so dashboards don't hit production on every refresh.

## Limits

- Not a server: there's no Postgres-wire endpoint for BI tools to query live; `snapshot` is the bridge (live federation is on the roadmap).
- Only what DuckDB's [postgres](https://duckdb.org/docs/extensions/postgres), [mysql](https://duckdb.org/docs/extensions/mysql) and [sqlite](https://duckdb.org/docs/extensions/sqlite) extensions support; SQL Server, Snowflake, BigQuery and SaaS APIs are not covered yet. You write DuckDB's SQL dialect.
- Extensions are downloaded by DuckDB on first use. If your network blocks DuckDB's plain-http extension download, crossql retries over https; offline, download the `.duckdb_extension` files and set `CROSSQL_EXTENSION_DIR`.

## Alternatives

- **DuckDB directly**: crossql is a thin layer over DuckDB's `ATTACH`. If you're happy writing the `INSTALL`/`ATTACH` statements yourself, the DuckDB CLI does the same joins. crossql adds the named-source config, read-only guard, secret masking, `check`/`tables`, and snapshots.
- **[pgfederate](https://github.com/Jwrede/pgfederate)**: also DuckDB-based, but serves the Postgres wire protocol so Metabase can query sources live. Pick it if you need live queries from the BI tool; crossql is a CLI plus file snapshots.
- **Trino / Postgres foreign data wrappers**: the right call for many sources, big data, or many users; more to run and secure.
- **A warehouse + ELT**: the long-term answer once you have a data team.

## Demo

[`demo.mp4`](https://github.com/hamarshehmhmd/crossql/releases/download/v0.1.0/demo.mp4) (2 min 47 s, narrated) shows `check`, `tables`, a four-way join across Postgres, MySQL (MariaDB), SQLite and CSV, `explain`, the read-only guard and a snapshot opened as a plain SQLite file. The data comes from [`examples/make_demo.py`](examples/make_demo.py), which you can run yourself:

```sh
python examples/make_demo.py demo-data                 # SQLite + CSV only, works anywhere
python examples/make_demo.py demo-data --pg postgresql://localhost/postgres --mysql='-h127.0.0.1 -uroot -ppw billing'
```

## Development

```sh
pip install -e . pytest
pytest -q                                   # 60 tests on SQLite/CSV/Parquet/DuckDB sources
CROSSQL_TEST_PG=postgresql://postgres:pw@localhost/postgres \
CROSSQL_TEST_MYSQL=mysql://root:pw@127.0.0.1:3306/billing \
CROSSQL_TEST_MYSQL_ARGS='-h127.0.0.1 -uroot -ppw billing' pytest -q   # + 4 against real servers
```

CI runs the suite on Python 3.10 to 3.13 (Linux) and macOS, the server tests against Postgres 16 and MySQL 8.4 service containers, and the one-line installer.

## License

GNU Affero General Public License v3.0 or later ([AGPL-3.0-or-later](LICENSE)). Commercial licenses without the AGPL obligations are available from the author, Mohammad Hamarsheh ([@hamarshehmhmd](https://github.com/hamarshehmhmd)).
