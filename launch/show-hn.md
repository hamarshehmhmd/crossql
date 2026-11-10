# Show HN draft (post yourself)

**Title:** Show HN: Crossql – join Postgres, MySQL and CSV files in one SQL query, no warehouse

**URL:** https://github.com/hamarshehmhmd/crossql

**Text (first comment):**

Users in Postgres, invoices in a MySQL billing DB, leads in a CSV from marketing. "Revenue by signup channel" needs all three and BI tools can't join across DBs (the Metabase issue for it is open since 2016 with 2.1k reactions). Usual answer is a warehouse + ELT, or Trino.

crossql is a small CLI on top of DuckDB. You name the sources once in a toml (env vars for secrets), then write normal SQL with `app.users JOIN billing.invoices JOIN leads`. Filters get pushed down, the join runs locally.

Bits I cared about:
- read-only: every DB is attached READ_ONLY, and the SQL goes through DuckDB's parser first so only a single SELECT gets through (a CTE + UPDATE doesn't). Tests check real Postgres/MySQL refuse writes even with that guard bypassed
- `crossql snapshot` writes your cross-db queries into one .sqlite/.duckdb file or Parquet folder, atomically, so you can point Metabase/Superset/Grafana at it today
- `explain` shows what got pushed down
- passwords masked everywhere

Limits: no live wire-protocol server yet (snapshot is the bridge), only what DuckDB can attach (Postgres, MySQL/MariaDB, SQLite, DuckDB, files), and big scans still pull rows over the network so use a replica.

Python, AGPL-3.0. Curious what sources people would want next.
