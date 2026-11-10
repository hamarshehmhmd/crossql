"""Render query results as a table, CSV, TSV, JSON, JSON lines or Markdown."""
from __future__ import annotations

import csv
import datetime
import decimal
import io
import json
import os
import sys
import uuid

FORMATS = ("table", "csv", "tsv", "json", "jsonl", "md")


def use_color(stream=None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream or sys.stdout
    return hasattr(stream, "isatty") and stream.isatty()


def paint(text: str, code: str, stream=None) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if use_color(stream) else text


def _plain(v):
    if v is None:
        return None
    if isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v) if v == v.to_integral_value() or abs(v) < 1e15 else str(v)
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, datetime.timedelta):
        return str(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return str(v)


def _cell(v) -> str:
    if v is None:
        return "NULL"
    v = _plain(v)
    if isinstance(v, float):
        return f"{v:.6g}" if abs(v) < 1e15 else str(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _is_num(v) -> bool:
    return isinstance(v, (int, float, decimal.Decimal)) and not isinstance(v, bool)


def table(cols, rows, max_width=40, max_rows=200) -> str:
    if not cols:
        return "(no result)"
    shown = rows[:max_rows]
    cells = [[_cell(v) for v in r] for r in shown]
    cells = [[c if len(c) <= max_width else c[: max_width - 1] + "…" for c in r] for r in cells]
    widths = [len(c) for c in cols]
    for r in cells:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    widths = [min(w, max_width) for w in widths]
    sep = "  "
    head = sep.join(paint(c.ljust(widths[i])[: widths[i]], "1") for i, c in enumerate(cols))
    rule = sep.join("─" * w for w in widths)
    lines = [head, paint(rule, "2")]
    for raw, r in zip(shown, cells):
        lines.append(sep.join(
            (c.rjust(widths[i]) if _is_num(raw[i]) else c.ljust(widths[i])) for i, c in enumerate(r)
        ).rstrip())
    n = len(rows)
    foot = f"({n} row{'s' if n != 1 else ''}"
    if n > len(shown):
        foot += f", first {len(shown)} shown; use -F csv or -o FILE for all"
    lines.append(paint(foot + ")", "2"))
    return "\n".join(lines)


def render(cols, rows, fmt="table", max_rows=200) -> str:
    if fmt == "table":
        return table(cols, rows, max_rows=max_rows)
    if fmt in ("csv", "tsv"):
        buf = io.StringIO()
        w = csv.writer(buf, delimiter="," if fmt == "csv" else "\t", lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if v is None else _cell(v) for v in r])
        return buf.getvalue().rstrip("\n")
    if fmt == "json":
        return json.dumps([dict(zip(cols, map(_plain, r))) for r in rows], indent=2, ensure_ascii=False)
    if fmt == "jsonl":
        return "\n".join(json.dumps(dict(zip(cols, map(_plain, r))), ensure_ascii=False) for r in rows)
    if fmt == "md":
        esc = lambda s: s.replace("|", "\\|")  # noqa: E731
        out = ["| " + " | ".join(esc(c) for c in cols) + " |", "|" + "|".join(
            "---:" if rows and _is_num(rows[0][i]) else "---" for i in range(len(cols))) + "|"]
        out += ["| " + " | ".join(esc(_cell(v)) for v in r) + " |" for r in rows]
        return "\n".join(out)
    raise ValueError(f"unknown format {fmt!r}")
