"""DuckDB session that attaches every source read-only and runs SQL across them."""
from __future__ import annotations

import gzip
import os
import shutil
import tempfile
import time
import urllib.request
from dataclasses import dataclass

import duckdb

from .config import ConfigError, Source, redact

EXT = {"postgres": "postgres_scanner", "mysql": "mysql_scanner", "sqlite": "sqlite_scanner"}
ATTACH_TYPE = {"postgres": "postgres", "mysql": "mysql", "sqlite": "sqlite", "duckdb": "duckdb"}
HIDDEN_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast", "mysql", "sys", "performance_schema"}
READERS = {"csv": "read_csv_auto", "parquet": "read_parquet", "json": "read_json_auto"}


class SourceError(Exception):
    def __init__(self, source: Source, msg: str):
        super().__init__(f"{source.name} ({source.type}): {msg}")
        self.source = source


def _clean_error(e: Exception, source: Source | None = None) -> str:
    msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    if source is not None and source.url and source.url in msg:
        msg = msg.replace(source.url, redact(source.url))
    return redact(msg)


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def ensure_extension(con, name: str) -> None:
    """LOAD a DuckDB extension, installing it if needed.

    DuckDB's own installer fetches over plain http, which many corporate proxies
    block; if that fails we fetch the same signed file over https ourselves.
    """
    ext = EXT[name]
    try:
        con.execute(f"LOAD {ext}")
        return
    except duckdb.Error:
        pass
    local = os.environ.get("CROSSQL_EXTENSION_DIR")
    if local:
        path = os.path.join(local, f"{ext}.duckdb_extension")
        if os.path.exists(path):
            con.execute(f"INSTALL {_lit(path)}")
            con.execute(f"LOAD {ext}")
            return
    try:
        con.execute(f"INSTALL {ext}")
        con.execute(f"LOAD {ext}")
        return
    except duckdb.Error as first:
        err = first
    try:
        platform = con.execute("PRAGMA platform").fetchone()[0]
        version = con.execute("SELECT library_version FROM pragma_version()").fetchone()[0]
        url = f"https://extensions.duckdb.org/{version}/{platform}/{ext}.duckdb_extension.gz"
        tmp = tempfile.mkdtemp(prefix="crossql-ext-")
        try:
            path = os.path.join(tmp, f"{ext}.duckdb_extension")
            req = urllib.request.Request(url, headers={"User-Agent": "crossql (+https://github.com/hamarshehmhmd/crossql)"})
            with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as out:
                shutil.copyfileobj(gzip.GzipFile(fileobj=r), out)
            con.execute(f"INSTALL {_lit(path)}")
            con.execute(f"LOAD {ext}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        raise ConfigError(
            f"could not install the DuckDB {name} extension ({_clean_error(err)}; https fallback: {_clean_error(e)}). "
            f"Download {ext}.duckdb_extension for your DuckDB version and set CROSSQL_EXTENSION_DIR."
        )


@dataclass
class Table:
    source: str
    schema: str
    name: str
    kind: str  # table | view | file
    columns: list  # [(name, type)]
    default_schema: str = ""

    @property
    def ref(self) -> str:
        """How to write this table in a query."""
        if self.kind == "file":
            return self.source
        if self.schema == self.default_schema:
            return f"{self.source}.{self.name}"
        return f"{self.source}.{self.schema}.{self.name}"


def mysql_database(url: str) -> str:
    """The database named in a MySQL URL or key=value string ('' if none)."""
    import re
    from urllib.parse import unquote, urlsplit

    if "://" in url:
        path = urlsplit(url).path.strip("/")
        return unquote(path.split("/", 1)[0]) if path else ""
    m = re.search(r"\b(?:database|db)\s*=\s*([^\s]+)", url)
    return m.group(1) if m else ""


class Session:
    def __init__(self, sources: dict, threads: int | None = None):
        self.sources = sources
        self.con = duckdb.connect(":memory:")
        if threads:
            self.con.execute(f"SET threads = {int(threads)}")
        self.attached = {}  # name -> seconds
        self.errors = {}  # name -> message
        self.default_schemas = {}

    # -- attaching ---------------------------------------------------------
    def attach(self, source: Source) -> float:
        t0 = time.monotonic()
        con = self.con
        try:
            if source.is_file:
                reader = READERS[source.type]
                opts = "".join(f", {k} = {_lit(str(v)) if isinstance(v, str) else str(v).lower() if isinstance(v, bool) else v}"
                               for k, v in source.options.items())
                if not any(ch in source.url for ch in "*?[") and not os.path.exists(source.url):
                    raise FileNotFoundError(f"no such file: {source.url}")
                con.execute(f"CREATE OR REPLACE VIEW {_q(source.name)} AS SELECT * FROM {reader}({_lit(source.url)}{opts})")
                con.execute(f"SELECT * FROM {_q(source.name)} LIMIT 0")
            else:
                if source.type in EXT:
                    ensure_extension(con, source.type)
                if source.type in ("sqlite", "duckdb") and not os.path.exists(source.url):
                    raise FileNotFoundError(f"no such file: {source.url}")
                schema = source.options.get("schema")
                extra = f", SCHEMA {_lit(schema)}" if schema and source.type == "postgres" else ""
                con.execute(f"ATTACH {_lit(source.url)} AS {_q(source.name)} (TYPE {ATTACH_TYPE[source.type]}, READ_ONLY{extra})")
                self.default_schemas[source.name] = self._default_schema(source)
        except ConfigError:
            raise
        except Exception as e:  # noqa: BLE001
            raise SourceError(source, _clean_error(e, source))
        dt = time.monotonic() - t0
        self.attached[source.name] = dt
        return dt

    def _default_schema(self, source: Source) -> str:
        if source.type == "postgres":
            return source.options.get("schema") or "public"
        if source.type == "mysql":
            db = mysql_database(source.url)
            if db:
                return db
            rows = self.con.execute(
                "SELECT DISTINCT schema_name FROM duckdb_tables() WHERE database_name = ?", [source.name]
            ).fetchall()
            names = [r[0] for r in rows if r[0] not in HIDDEN_SCHEMAS]
            return names[0] if len(names) == 1 else ""
        return "main"

    def attach_all(self, names=None, keep_going=False) -> None:
        for name, src in self.sources.items():
            if names and name not in names:
                continue
            try:
                self.attach(src)
            except SourceError as e:
                if not keep_going:
                    raise
                self.errors[name] = str(e)

    # -- introspection -----------------------------------------------------
    def tables(self, source: str | None = None, columns=True) -> list:
        out = []
        for name, src in self.sources.items():
            if source and name != source:
                continue
            if name not in self.attached:
                continue
            if src.is_file:
                cols = self.con.execute(f"DESCRIBE {_q(name)}").fetchall() if columns else []
                out.append(Table(name, "", name, "file", [(c[0], c[1]) for c in cols]))
                continue
            rows = self.con.execute(
                """SELECT schema_name, table_name, 'table' FROM duckdb_tables() WHERE database_name = ?
                   UNION ALL
                   SELECT schema_name, view_name, 'view' FROM duckdb_views() WHERE database_name = ? AND NOT internal
                   ORDER BY 1, 2""",
                [name, name],
            ).fetchall()
            only = self.default_schemas.get(name) if src.type == "mysql" else None
            for schema, tname, kind in rows:
                if schema in HIDDEN_SCHEMAS or (only and schema != only):
                    continue
                cols = []
                if columns:
                    cols = self.con.execute(
                        "SELECT column_name, data_type FROM duckdb_columns() WHERE database_name = ? AND schema_name = ? "
                        "AND table_name = ? ORDER BY column_index",
                        [name, schema, tname],
                    ).fetchall()
                out.append(Table(name, schema, tname, kind, [(c[0], c[1]) for c in cols],
                                 self.default_schemas.get(name, "")))
        return out

    # -- querying ----------------------------------------------------------
    def query(self, sql: str):
        """Run SQL; returns (columns, rows)."""
        try:
            rel = self.con.execute(sql)
        except duckdb.Error as e:
            raise QueryError(_clean_error(e)) from None
        if rel.description is None:
            return [], []
        cols = [d[0] for d in rel.description]
        return cols, rel.fetchall()

    def explain(self, sql: str) -> str:
        cols, rows = self.query("EXPLAIN " + sql)
        return "\n".join(r[-1] for r in rows)

    def close(self):
        self.con.close()


class QueryError(Exception):
    pass


def check_writes(sql: str) -> None:
    """Refuse anything but read queries before any source sees it.

    Sources are also attached READ_ONLY (Postgres and MySQL run in read-only
    transactions), so this is a second, earlier line of defence with a clear message.
    DuckDB's own parser decides the statement type, so CTE tricks don't get past it.
    """
    try:
        stmts = duckdb.connect(":memory:").extract_statements(sql)
    except duckdb.Error:
        return  # not parseable: the real run reports the syntax error
    if len(stmts) > 1:
        raise QueryError("crossql runs one statement at a time")
    for st in stmts:
        kind = getattr(st.type, "name", str(st.type)).upper()
        if kind != "SELECT":
            raise QueryError(f"crossql only runs read queries (got {kind}). Sources are attached READ_ONLY.")
