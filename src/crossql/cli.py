"""crossql command line."""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__
from .config import ConfigError, load, parse_duration, parse_views_sql
from .engine import QueryError, Session, SourceError, check_writes, _lit
from .output import FORMATS, paint, render
from .snapshot import build, is_fresh, referenced

EXAMPLE = '''# crossql.toml: every source is attached READ-ONLY.
# URLs may use ${ENV_VAR} or ${ENV_VAR:-default}, so secrets stay out of this file.

[sources.app]                      # query as app.<table> (Postgres schema "public")
url = "${APP_DATABASE_URL:-postgresql://localhost/app}"

[sources.billing]                  # query as billing.<table>
url = "${BILLING_DATABASE_URL:-mysql://reader@localhost:3306/billing}"

# [sources.events]                 # a SQLite or DuckDB file
# url = "data/events.sqlite"

# [sources.leads]                  # a CSV / Parquet / JSON file or glob: query as leads
# path = "exports/leads-*.csv"

# Named queries for `crossql snapshot` (one table per view in the output file).
# [views.revenue_by_plan]
# sql = """
# SELECT u.plan, count(DISTINCT u.id) AS customers, sum(i.amount_cents) / 100.0 AS revenue
# FROM app.users u JOIN billing.invoices i ON i.user_id = u.id
# GROUP BY 1 ORDER BY revenue DESC
# """

# [snapshot]
# output = "analytics.sqlite"      # point Metabase / Superset / Grafana at this file
# max_age = "15m"                  # skip the rebuild if the file is newer than this
'''


def _err(msg: str) -> None:
    print(paint("crossql: ", "31;1", sys.stderr) + msg, file=sys.stderr)


def _config(args, need_sources=True):
    cfg = load(args.config, args.source, use_file=not args.no_config)
    if need_sources and not cfg.sources:
        raise ConfigError("no sources: create crossql.toml (crossql init) or pass --source NAME=URL")
    return cfg


def _session(cfg, names=None, keep_going=False, threads=None):
    s = Session(cfg.sources, threads=threads)
    s.attach_all(names, keep_going=keep_going)
    return s


def cmd_init(args):
    path = args.path or "crossql.toml"
    if os.path.exists(path) and not args.force:
        _err(f"{path} already exists (use --force to overwrite)")
        return 2
    with open(path, "w") as fh:
        fh.write(EXAMPLE)
    print(f"wrote {path}: edit the sources, then run `crossql check`")
    return 0


def cmd_sources(args):
    cfg = _config(args)
    w = max(len(n) for n in cfg.sources)
    for name, src in cfg.sources.items():
        print(f"{name.ljust(w)}  {src.type:<8}  {src.display()}")
    if cfg.path:
        print(paint(f"(from {cfg.path})", "2"))
    return 0


def cmd_check(args):
    cfg = _config(args)
    names = args.names or list(cfg.sources)
    unknown = [n for n in names if n not in cfg.sources]
    if unknown:
        raise ConfigError(f"unknown source {unknown[0]!r} (have: {', '.join(cfg.sources)})")
    s = Session(cfg.sources)
    bad = 0
    w = max(len(n) for n in names)
    for name in names:
        src = cfg.sources[name]
        t0 = time.monotonic()
        try:
            s.attach(src)
            n = len(s.tables(name, columns=False))
            ms = (time.monotonic() - t0) * 1000
            what = "columns" if src.is_file else "tables"
            if src.is_file:
                n = len(s.tables(name)[0].columns)
            print(f"{paint('ok  ', '32;1')} {name.ljust(w)}  {src.type:<8} {n:>4} {what:<8} {ms:7.0f} ms  {src.display()}")
        except SourceError as e:
            bad += 1
            msg = str(e).split(": ", 1)[1]
            print(f"{paint('FAIL', '31;1')} {name.ljust(w)}  {src.type:<8} {msg}")
    s.close()
    if bad:
        print(paint(f"{bad} of {len(names)} source(s) failed", "31"))
    return 1 if bad else 0


def cmd_tables(args):
    cfg = _config(args)
    if args.name and args.name not in cfg.sources:
        raise ConfigError(f"unknown source {args.name!r} (have: {', '.join(cfg.sources)})")
    s = _session(cfg, [args.name] if args.name else None, keep_going=not args.name)
    for name, msg in s.errors.items():
        _err(f"skipped {msg}")
    tables = s.tables(args.name, columns=args.columns or args.format != "table")
    if args.format != "table":
        cols = ["ref", "source", "schema", "table", "kind", "columns"]
        rows = [(t.ref, t.source, t.schema, t.name, t.kind,
                 ", ".join(f"{c} {ty}" for c, ty in t.columns)) for t in tables]
        print(render(cols, rows, args.format))
        return 1 if s.errors else 0
    for t in tables:
        line = f"{paint(t.ref, '36')}  {paint(t.kind, '2')}"
        print(line)
        if args.columns:
            for c, ty in t.columns:
                print(f"    {c}  {paint(ty, '2')}")
    print(paint(f"({len(tables)} tables in {len(s.attached)} source(s))", "2"))
    return 1 if s.errors else 0


def _read_sql(args) -> str:
    if args.file:
        with open(args.file) as fh:
            return fh.read().strip().rstrip(";")
    if args.sql == "-" or (args.sql is None and not sys.stdin.isatty()):
        return sys.stdin.read().strip().rstrip(";")
    if not args.sql:
        raise ConfigError("give a query: crossql query \"SELECT ...\" (or -f FILE.sql, or pipe it on stdin)")
    return args.sql.strip().rstrip(";")


def _needed(cfg, sql):
    names = referenced(sql, cfg.sources)
    return names or None


def cmd_query(args):
    cfg = _config(args)
    sql = _read_sql(args)
    check_writes(sql)
    s = _session(cfg, _needed(cfg, sql), threads=args.threads)
    if args.limit is not None:
        sql = f"SELECT * FROM ({sql}) AS q LIMIT {int(args.limit)}"
    t0 = time.monotonic()
    if args.output:
        out = args.output
        ext = os.path.splitext(out)[1].lower()
        fmt = {".parquet": "(FORMAT parquet)", ".csv": "(FORMAT csv, HEADER)", ".tsv": "(FORMAT csv, HEADER, DELIMITER '\t')",
               ".json": "(FORMAT json, ARRAY true)", ".jsonl": "(FORMAT json)", ".ndjson": "(FORMAT json)"}.get(ext)
        if not fmt:
            raise ConfigError(f"-o {out}: use .parquet, .csv, .tsv, .json or .jsonl")
        try:
            s.con.execute(f"COPY ({sql}) TO {_lit(out)} {fmt}")
            n = s.con.execute(f"SELECT count(*) FROM ({sql}) AS q").fetchone()[0] if args.count else None
        except Exception as e:  # noqa: BLE001
            raise QueryError(str(e).splitlines()[0]) from None
        msg = f"wrote {out}" + (f" ({n} rows)" if n is not None else "") + f" in {time.monotonic() - t0:.2f}s"
        print(msg, file=sys.stderr)
        return 0
    cols, rows = s.query(sql)
    print(render(cols, rows, args.format, max_rows=args.max_rows))
    if args.timing:
        print(paint(f"{time.monotonic() - t0:.3f}s", "2"), file=sys.stderr)
    return 0


def cmd_explain(args):
    cfg = _config(args)
    sql = _read_sql(args)
    check_writes(sql)
    s = _session(cfg, _needed(cfg, sql))
    print(s.explain(sql))
    return 0


def _views(args, cfg):
    views = dict(cfg.views)
    for f in args.views or ():
        with open(f) as fh:
            views.update(parse_views_sql(fh.read(), f))
    if args.only:
        missing = [v for v in args.only if v not in views]
        if missing:
            raise ConfigError(f"unknown view {missing[0]!r} (have: {', '.join(views) or 'none'})")
        views = {k: v for k, v in views.items() if k in args.only}
    return views


def cmd_snapshot(args):
    cfg = _config(args)
    views = _views(args, cfg)
    out = args.output or cfg.snapshot.get("output")
    if not out:
        raise ConfigError("where to? pass -o analytics.sqlite (or .duckdb, or a directory) or set [snapshot] output")
    max_age = parse_duration(args.max_age or cfg.snapshot.get("max_age") or "0")
    every = parse_duration(args.every) if args.every else 0
    while True:
        if is_fresh(out, max_age):
            print(f"{out} is fresh (newer than {args.max_age or cfg.snapshot.get('max_age')}), nothing to do")
        else:
            needed = sorted({n for sql in views.values() for n in referenced(sql, cfg.sources)})
            t0 = time.monotonic()
            s = _session(cfg, needed or None, threads=args.threads)

            def log(name, rows, dt):
                print(f"  {paint('built', '32')} {name:<28} {rows:>9} rows  {dt:6.2f}s")

            try:
                for sql in views.values():
                    check_writes(sql)
                build(s, views, out, args.file_format, log=log)
            finally:
                s.close()
            print(f"snapshot {out}: {len(views)} view(s) from {len(needed)} source(s) in {time.monotonic() - t0:.2f}s")
        if not every:
            return 0
        time.sleep(every)


def build_parser():
    p = argparse.ArgumentParser(
        prog="crossql",
        description="Join tables across Postgres, MySQL, SQLite, DuckDB and CSV/Parquet/JSON files with one SQL query, "
                    "and snapshot the results into a file your BI tool can read. Sources are attached read-only.",
    )
    p.add_argument("--version", action="version", version=f"crossql {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", help="config file (default: crossql.toml in this or a parent directory)")
    common.add_argument("-s", "--source", action="append", default=[], metavar="NAME=URL",
                        help="add a source, e.g. -s app=postgresql://localhost/app -s leads=leads.csv (repeatable)")
    common.add_argument("--no-config", action="store_true", help="ignore crossql.toml, use only --source flags")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    a = sub.add_parser("init", help="write an example crossql.toml", parents=[common])
    a.add_argument("path", nargs="?")
    a.add_argument("--force", action="store_true")
    a.set_defaults(func=cmd_init)

    a = sub.add_parser("sources", help="list configured sources (passwords hidden)", parents=[common])
    a.set_defaults(func=cmd_sources)

    a = sub.add_parser("check", help="connect to each source; exit 1 if any fail", parents=[common])
    a.add_argument("names", nargs="*")
    a.set_defaults(func=cmd_check)

    a = sub.add_parser("tables", help="list tables you can query, and how to name them", parents=[common])
    a.add_argument("name", nargs="?", help="only this source")
    a.add_argument("-C", "--columns", action="store_true", help="show columns and types")
    a.add_argument("-F", "--format", choices=FORMATS, default="table")
    a.set_defaults(func=cmd_tables)

    for name, func, hlp in (("query", cmd_query, "run one SQL query across sources"),
                            ("explain", cmd_explain, "show DuckDB's plan (what is pushed down to each source)")):
        a = sub.add_parser(name, help=hlp, parents=[common])
        a.add_argument("sql", nargs="?", help="SQL; reference tables as SOURCE.table (or SOURCE.schema.table)")
        a.add_argument("-f", "--file", help="read SQL from a file")
        if name == "query":
            a.add_argument("-F", "--format", choices=FORMATS, default="table")
            a.add_argument("-o", "--output", help="write all rows to .parquet/.csv/.tsv/.json/.jsonl")
            a.add_argument("--count", action="store_true", help="with -o: also report the row count")
            a.add_argument("--limit", type=int)
            a.add_argument("--max-rows", type=int, default=200, help="rows to print in table format (default 200)")
            a.add_argument("--timing", action="store_true")
            a.add_argument("--threads", type=int)
        a.set_defaults(func=func)

    a = sub.add_parser("snapshot", help="materialize named views into a .sqlite/.duckdb file or Parquet directory",
                       parents=[common])
    a.add_argument("-o", "--output", help="analytics.sqlite | analytics.duckdb | a directory (Parquet)")
    a.add_argument("--views", action="append", metavar="FILE.sql",
                   help="SQL file with '-- name: view' blocks (adds to [views] in crossql.toml)")
    a.add_argument("--only", action="append", metavar="VIEW", help="build only these views (repeatable)")
    a.add_argument("--max-age", help="skip if the output is newer than this (e.g. 15m)")
    a.add_argument("--every", help="keep running and rebuild at this interval (e.g. 15m)")
    a.add_argument("--file-format", choices=("parquet", "csv"), default="parquet", help="for directory output")
    a.add_argument("--threads", type=int)
    a.set_defaults(func=cmd_snapshot)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        _err(str(e))
        return 2
    except (SourceError, QueryError) as e:
        _err(str(e))
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
