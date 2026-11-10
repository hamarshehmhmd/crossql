# r/selfhosted draft (post yourself; flair as a project / check rules)

**Title:** crossql: one SQL query across your Postgres, MySQL/MariaDB, SQLite files and CSVs

If you run a bunch of apps at home, your data ends up split across Postgres, MariaDB and SQLite files. I wanted one query across them for a Grafana dashboard without setting up a warehouse.

crossql (CLI, built on DuckDB):
- `crossql check` / `crossql tables` to see what's reachable
- `crossql query "... app.users JOIN billing.invoices ..."` with normal SQL
- `crossql snapshot -o stats.sqlite --every 15m` and add that file to Grafana/Metabase as SQLite
- read-only attach, passwords masked

One-line install: `curl -fsSL https://raw.githubusercontent.com/hamarshehmhmd/crossql/main/install.sh | sh`

https://github.com/hamarshehmhmd/crossql (AGPL-3.0, I'm the author)
