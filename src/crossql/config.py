"""Source definitions: crossql.toml, --source flags and type detection."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

DB_TYPES = ("postgres", "mysql", "sqlite", "duckdb")
FILE_TYPES = ("csv", "parquet", "json")
TYPES = DB_TYPES + FILE_TYPES
CONFIG_NAMES = ("crossql.toml", ".crossql.toml")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
RESERVED = {"memory", "system", "temp", "main", "snapshot"}


class ConfigError(Exception):
    pass


@dataclass
class Source:
    name: str
    type: str
    url: str  # connection string or file path / glob (env vars already expanded)
    raw: str = ""  # as written, before env expansion (safe to print if it used ${VARS})
    options: dict = field(default_factory=dict)

    @property
    def is_file(self) -> bool:
        return self.type in FILE_TYPES

    def display(self) -> str:
        return redact(self.raw or self.url)


def expand_env(value: str, where: str) -> str:
    def sub(m):
        name, default = m.group(1), m.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ConfigError(f"{where}: environment variable ${{{name}}} is not set")

    return ENV_RE.sub(sub, value)


def redact(url: str) -> str:
    """Hide passwords in URLs and key=value connection strings."""
    if "://" in url:
        try:
            parts = urlsplit(url)
        except ValueError:
            return url
        if parts.password:
            netloc = parts.netloc.replace(":" + parts.password + "@", ":***@", 1)
            url = urlunsplit(parts._replace(netloc=netloc))
        url = re.sub(r"(?i)(password=)[^&\s]+", r"\1***", url)
        return url
    return re.sub(r"(?i)\b(password|passwd|pwd)\s*=\s*('[^']*'|\S+)", r"\1=***", url)


def detect_type(url: str) -> str:
    low = url.lower()
    scheme = low.split("://", 1)[0] if "://" in low else ""
    if scheme in ("postgres", "postgresql", "pg"):
        return "postgres"
    if scheme in ("mysql", "mariadb"):
        return "mysql"
    if scheme == "sqlite":
        return "sqlite"
    if scheme == "duckdb":
        return "duckdb"
    if re.search(r"\b(host|dbname)=", low) and "port=3306" not in low:
        return "postgres"
    path = low.split("?", 1)[0]
    for ext, t in ((".csv", "csv"), (".tsv", "csv"), (".csv.gz", "csv"), (".parquet", "parquet"),
                   (".json", "json"), (".jsonl", "json"), (".ndjson", "json"),
                   (".sqlite", "sqlite"), (".sqlite3", "sqlite"), (".db", "sqlite"), (".duckdb", "duckdb")):
        if path.endswith(ext):
            return t
    raise ConfigError(f"cannot tell the source type of {redact(url)!r}; set type = \"postgres|mysql|sqlite|duckdb|csv|parquet|json\"")


def _strip_scheme(url: str, t: str) -> str:
    """sqlite:///abs/path, sqlite://rel/path and duckdb:/// map to a plain path."""
    if t in ("sqlite", "duckdb") and "://" in url:
        rest = url.split("://", 1)[1]
        return rest
    if t == "mysql" and url.lower().startswith("mariadb://"):
        return "mysql://" + url.split("://", 1)[1]
    if t == "postgres" and url.lower().startswith("pg://"):
        return "postgresql://" + url.split("://", 1)[1]
    return url


def make_source(name: str, url: str, type_: str | None = None, base_dir: str = ".", options=None, where="") -> Source:
    where = where or f"source {name!r}"
    if not NAME_RE.match(name):
        raise ConfigError(f"{where}: name must be a SQL identifier (letters, digits, _), got {name!r}")
    if name.lower() in RESERVED:
        raise ConfigError(f"{where}: {name!r} is reserved, pick another name")
    raw = url
    url = expand_env(url, where)
    t = (type_ or detect_type(url)).lower()
    if t in ("postgresql", "pg"):
        t = "postgres"
    if t == "mariadb":
        t = "mysql"
    if t not in TYPES:
        raise ConfigError(f"{where}: unknown type {t!r} (use one of {', '.join(TYPES)})")
    url = _strip_scheme(url, t)
    if t in ("sqlite", "duckdb") or t in FILE_TYPES:
        url = os.path.expanduser(url)
        if not os.path.isabs(url):
            url = os.path.normpath(os.path.join(base_dir, url))
    return Source(name=name, type=t, url=url, raw=raw if raw != url else "", options=dict(options or {}))


def parse_flag(spec: str) -> Source:
    """--source name=url  or  --source name:type=url"""
    if "=" not in spec:
        raise ConfigError(f"--source {spec!r}: expected NAME=URL (e.g. app=postgres://localhost/app)")
    name, url = spec.split("=", 1)
    type_ = None
    if ":" in name:
        name, type_ = name.split(":", 1)
    return make_source(name.strip(), url.strip(), type_, os.getcwd(), where=f"--source {name.strip()}")


@dataclass
class Config:
    sources: dict
    views: dict  # name -> sql
    path: str = ""
    snapshot: dict = field(default_factory=dict)


def find_config(start: str = ".") -> str | None:
    d = os.path.abspath(start)
    while True:
        for n in CONFIG_NAMES:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def load(path: str | None = None, flags=(), use_file: bool = True) -> Config:
    sources, views, snap = {}, {}, {}
    if path is None and use_file:
        path = find_config()
    if path:
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {path}")
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}")
        base = os.path.dirname(os.path.abspath(path))
        for name, spec in (data.get("sources") or {}).items():
            where = f"{os.path.basename(path)} [sources.{name}]"
            if isinstance(spec, str):
                spec = {"url": spec}
            if not isinstance(spec, dict):
                raise ConfigError(f"{where}: expected a table or a URL string")
            url = spec.get("url") or spec.get("path")
            if not url:
                raise ConfigError(f"{where}: needs url = \"...\" (or path = \"...\" for files)")
            opts = {k: v for k, v in spec.items() if k not in ("url", "path", "type")}
            sources[name] = make_source(name, str(url), spec.get("type"), base, opts, where)
        for name, spec in (data.get("views") or {}).items():
            sql = spec.get("sql") if isinstance(spec, dict) else spec
            if not isinstance(sql, str) or not sql.strip():
                raise ConfigError(f"{os.path.basename(path)} [views.{name}]: needs sql = \"...\"")
            if not NAME_RE.match(name):
                raise ConfigError(f"view name {name!r} must be a SQL identifier")
            views[name] = sql.strip().rstrip(";")
        snap = data.get("snapshot") or {}
        if snap.get("output"):
            out = os.path.expanduser(str(snap["output"]))
            snap["output"] = out if os.path.isabs(out) else os.path.join(base, out)
    for spec in flags:
        s = parse_flag(spec)
        sources[s.name] = s
    return Config(sources=sources, views=views, path=path or "", snapshot=snap)


def parse_views_sql(text: str, where: str = "views file") -> dict:
    """Split a .sql file into named views using '-- name: <view>' markers."""
    views, cur, buf = {}, None, []
    for line in text.splitlines():
        m = re.match(r"^\s*--\s*name:\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", line)
        if m:
            if cur:
                views[cur] = "\n".join(buf).strip().rstrip(";")
            cur, buf = m.group(1), []
        elif cur:
            buf.append(line)
    if cur:
        views[cur] = "\n".join(buf).strip().rstrip(";")
    if not views:
        raise ConfigError(f"{where}: no views found; start each query with a line '-- name: my_view'")
    empty = [k for k, v in views.items() if not v]
    if empty:
        raise ConfigError(f"{where}: view {empty[0]!r} has no SQL")
    return views


def parse_duration(s: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(s))
    if not m:
        raise ConfigError(f"bad duration {s!r} (use e.g. 90s, 15m, 6h, 1d)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
