# r/dataengineering draft (post yourself; check the sub's self-promo rules first)

**Title:** Made a small CLI to join Postgres + MySQL + CSV in one query without a warehouse

Small-team problem I kept hitting: users in Postgres, billing in MySQL, a CSV from marketing, and the question needs all three. Standing up a warehouse + ELT for that felt silly.

So I wrote crossql, a thin layer on DuckDB:

```
crossql query "SELECT l.channel, sum(i.amount_cents)/100 AS revenue
FROM app.users u JOIN billing.invoices i ON i.user_id = u.id
JOIN leads l USING (email) GROUP BY 1"
```

- sources live in a crossql.toml, `${ENV}` for secrets
- everything attached read-only, non-SELECT refused up front
- `crossql snapshot -o analytics.sqlite` so Metabase/Superset can chart the result

Not trying to replace Trino for big data, it's for the "few DBs and a spreadsheet" case. Joins run locally so point it at a replica.

https://github.com/hamarshehmhmd/crossql (AGPL-3.0, I'm the author). Feedback welcome, esp. on what breaks.
