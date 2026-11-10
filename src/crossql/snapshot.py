"""Materialize cross-source views into one file a BI tool can read.

Output kinds, picked from the path:
  *.sqlite / *.db   SQLite: Metabase, Superset, Grafana, Redash and most tools read it natively
  *.duckdb          DuckDB: Metabase (DuckDB driver), Superset, Evidence, Rill
  a directory       one Parquet file per view (or CSV with --file-format csv)

Writes go to a temporary file in the same directory and are renamed into place,
so a dashboard reading the snapshot never sees a half-written file.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import tempfile
import time

from .config import ConfigError
from .engine import QueryError, Session, _lit, _q, ensure_extension

META = "_crossql_snapshot"


def output_kind(path: str) -> str:
    low = path.lower()
    if low.endswith((".sqlite", ".sqlite3", ".db")):
        return "sqlite"
    if low.endswith(".duckdb"):
        return "duckdb"
    if low.endswith(("/", os.sep)) or os.path.isdir(path) or not os.path.splitext(low)[1]:
        return "dir"
    raise ConfigError(f"snapshot output {path!r}: use a .sqlite, .db or .duckdb file, or a directory for Parquet/CSV")


def referenced(sql: str, names) -> list:
    """Source names used in a SQL text (as identifiers, outside quotes and comments)."""
    text = re.sub(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'", " ", sql, flags=re.S)
    tokens = {t.lower() for pair in re.findall(r'"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*)', text) for t in pair if t}
    return [n for n in names if n.lower() in tokens]


def is_fresh(path: str, max_age: float) -> bool:
    if not max_age or not os.path.exists(path):
        return False
    target = os.path.join(path, META + ".json") if os.path.isdir(path) else path
    return os.path.exists(target) and (time.time() - os.path.getmtime(target)) < max_age


def build(session: Session, views: dict, out: str, file_format: str = "parquet", log=print) -> list:
    """Build every view into `out`. Returns [(view, rows, seconds)]."""
    if not views:
        raise ConfigError("nothing to snapshot: add [views.NAME] sql = \"...\" to crossql.toml or pass --views FILE.sql")
    kind = output_kind(out)
    out = os.path.abspath(out.rstrip("/\\") if kind == "dir" else out)
    parent = os.path.dirname(out)
    os.makedirs(parent, exist_ok=True)
    con = session.con
    results = []
    built_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()

    if kind == "dir":
        tmp = tempfile.mkdtemp(prefix=".crossql-", dir=parent)
        try:
            for name, sql in views.items():
                t0 = time.monotonic()
                fname = os.path.join(tmp, f"{name}.{file_format}")
                opts = "(FORMAT parquet)" if file_format == "parquet" else "(FORMAT csv, HEADER)"
                try:
                    con.execute(f"COPY ({sql}) TO {_lit(fname)} {opts}")
                    rows = con.execute(f"SELECT count(*) FROM {'read_parquet' if file_format == 'parquet' else 'read_csv_auto'}({_lit(fname)})").fetchone()[0]
                except Exception as e:  # noqa: BLE001
                    raise QueryError(f"view {name}: {str(e).splitlines()[0]}") from None
                dt = time.monotonic() - t0
                results.append((name, rows, dt))
                log(name, rows, dt)
            with open(os.path.join(tmp, META + ".json"), "w") as fh:
                json.dump({"built_at": built_at, "views": [
                    {"view": n, "rows": r, "seconds": round(s, 3), "sources": referenced(views[n], session.sources)}
                    for n, r, s in results]}, fh, indent=2)
            if os.path.isdir(out):
                old = out + ".old-" + str(os.getpid())
                os.rename(out, old)
                os.rename(tmp, out)
                shutil.rmtree(old, ignore_errors=True)
            else:
                os.rename(tmp, out)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return results

    fd, tmp = tempfile.mkstemp(prefix=".crossql-", suffix=os.path.splitext(out)[1], dir=parent)
    os.close(fd)
    os.remove(tmp)  # DuckDB/SQLite must create the file themselves
    alias = "crossql_out"
    try:
        if kind == "sqlite":
            ensure_extension(con, "sqlite")
        con.execute(f"ATTACH {_lit(tmp)} AS {alias} (TYPE {kind})")
        target_schema = f"{alias}.main" if kind == "duckdb" else alias
        for name, sql in views.items():
            t0 = time.monotonic()
            try:
                con.execute(f"CREATE TABLE {target_schema}.{_q(name)} AS {sql}")
                rows = con.execute(f"SELECT count(*) FROM {target_schema}.{_q(name)}").fetchone()[0]
            except Exception as e:  # noqa: BLE001
                raise QueryError(f"view {name}: {str(e).splitlines()[0]}") from None
            dt = time.monotonic() - t0
            results.append((name, rows, dt))
            log(name, rows, dt)
        con.execute(f"CREATE TABLE {target_schema}.{META} (view VARCHAR, row_count BIGINT, seconds DOUBLE, "
                    f"sources VARCHAR, built_at VARCHAR)")
        for n, r, s in results:
            con.execute(f"INSERT INTO {target_schema}.{META} VALUES (?, ?, ?, ?, ?)",
                        [n, r, round(s, 3), ",".join(referenced(views[n], session.sources)), built_at])
        con.execute(f"DETACH {alias}")
        os.replace(tmp, out)
    except BaseException:
        try:
            con.execute(f"DETACH {alias}")
        except Exception:  # noqa: BLE001
            pass
        for p in (tmp, tmp + ".wal"):
            if os.path.exists(p):
                os.remove(p)
        raise
    return results
