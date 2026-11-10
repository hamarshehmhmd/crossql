"""Render the crossql narrated demo: real command output + edge-tts narration.

Usage: python demo/render.py   (needs ffmpeg, edge-tts, Pillow, network)
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(HERE, "build")
OUT = os.path.join(ROOT, "demo.mp4")
SDKBIN = "/tmp/cqdemo/bin" + os.pathsep + "/tmp/cqvenv/bin" + os.pathsep + "/tmp/bin"
SANDBOX = "/tmp/cqdemo"
PG = "postgresql://postgres@/postgres?host=/tmp/pgdata"
MY = "mysql://root:pw@127.0.0.1:3307/billing"
W, H, FPS = 1280, 720, 24
VOICE = "en-US-AndrewNeural"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SANS_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BG, FG = (24, 26, 33), (220, 223, 228)
ANSI = {"31": (255, 95, 95), "32": (95, 215, 120), "33": (240, 200, 80),
        "2": (130, 135, 145), "36": (90, 200, 230), "35": (200, 140, 230), "1": FG}
PROMPT = "dev@laptop analytics $ "

SCENES = [
    {
        "kind": "title",
        "title": "crossql",
        "sub": "one SQL query across Postgres, MySQL,\nSQLite and CSV files. No warehouse.",
        "say": "This is cross Q L, an open source command line tool that joins tables across Postgres, MySQL, SQLite and C S V "
               "or Parquet files in one SQL query, read only, and snapshots the results into a file your B I tool can read.",
    },
    {
        "kind": "bullets",
        "title": "The problem",
        "bullets": [
            "Users in Postgres, invoices in MySQL, leads in a CSV",
            "Metabase #3953 \"Join across multiple databases\":",
            "   open since 2016, 2,110 reactions",
            "\"Join tables in two different databases?\" 191k views",
            "Usual answer: a warehouse + ELT, or run Trino",
        ],
        "say": ("Most small teams have data in more than one database: users in the Postgres app database, invoices in a MySQL "
                "billing database, and a C S V from marketing. B I tools can't join across them. The Metabase issue asking for it "
                "has been open since twenty sixteen with over two thousand reactions. The usual answer is a warehouse, or Trino."),
    },
    {
        "kind": "term",
        "cmds": ["cat crossql.toml", "crossql check"],
        "say": ("With cross Q L you name your sources once in a small config file. Secrets come from environment variables. "
                "Here it's a real Postgres server, a MariaDB server through the MySQL connector, a SQLite file and a C S V. "
                "crossql check connects to each one, and passwords are masked."),
    },
    {
        "kind": "term",
        "cmds": ["crossql tables"],
        "say": ("crossql tables lists everything you can query, with the exact name to use in SQL: source dot table, "
                "or just the source name for a file."),
    },
    {
        "kind": "term",
        "cmds": ["cat revenue.sql", "crossql query -f revenue.sql"],
        "say": ("Now a normal SQL query that joins all four: paid invoices from MySQL, users from Postgres, signup channel "
                "from the C S V and support tickets from SQLite, giving revenue and support load per channel. Each database is "
                "attached read only, and DuckDB does the join locally."),
    },
    {
        "kind": "term",
        "cmds": ["crossql explain \"SELECT email FROM app.users WHERE plan = 'team'\""],
        "say": ("crossql explain shows the plan, so you can see that filters and columns are pushed down to Postgres, "
                "instead of copying whole tables."),
    },
    {
        "kind": "term",
        "cmds": ["crossql query \"WITH x AS (SELECT 1) UPDATE billing.invoices SET amount_cents = 0\"; echo exit=$?"],
        "say": ("It's read only by design. The SQL is parsed with DuckDB's own parser and anything that isn't a single select "
                "is refused, even hidden behind a C T E. And the attach itself is read only, which the tests check against "
                "real Postgres and MySQL servers."),
    },
    {
        "kind": "term",
        "cmds": ["crossql snapshot -o bi/analytics.sqlite --views views.sql",
                 "crossql query --no-config -s bi=bi/analytics.sqlite \"FROM bi._crossql_snapshot\"",
                 "crossql snapshot -o bi/analytics.sqlite --views views.sql --max-age 15m"],
        "say": ("For dashboards, crossql snapshot builds your cross source queries into one SQLite file, or DuckDB, or Parquet. "
                "Add that file to Metabase, Superset or Grafana as a normal SQLite database. Builds are atomic, and max age "
                "skips the rebuild while the file is still fresh."),
    },
    {
        "kind": "term",
        "cmds": ["crossql query -F csv --limit 4 \"SELECT u.email, u.plan, l.campaign FROM app.users u JOIN leads l USING (email)\"",
                 "crossql query \"FROM billing.invoices\" -o invoices.parquet --count"],
        "say": ("Results can be printed as a table, C S V, JSON or markdown, or written straight to Parquet, C S V or JSON files."),
    },
    {
        "kind": "bullets",
        "title": "Limits",
        "bullets": [
            "Not a live server for BI tools yet: snapshot is the bridge",
            "Postgres, MySQL/MariaDB, SQLite, DuckDB, CSV/Parquet/JSON only",
            "Joins run locally: big scans pull rows over the network",
            "Use a read-only DB user and a replica for heavy queries",
        ],
        "say": ("The limits. It isn't a live server that B I tools query directly yet; snapshots are the bridge. It covers the "
                "databases DuckDB can attach. Joins run locally, so a big scan pulls rows over the network: use a replica "
                "and a read only user."),
    },
    {
        "kind": "bullets",
        "title": "Try it",
        "bullets": [
            "curl -fsSL https://raw.githubusercontent.com/",
            "   hamarshehmhmd/crossql/main/install.sh | sh",
            "crossql init  ·  crossql check  ·  crossql query \"...\"",
            "github.com/hamarshehmhmd/crossql",
            "GNU AGPL-3.0-or-later (commercial licenses available)",
        ],
        "say": ("Install it with the one line installer, run crossql init, and query. It's licensed under the GNU Affero "
                "General Public License version 3 or later, with commercial licenses available from the author."),
    },
]


async def tts(text, path):
    import edge_tts
    for attempt in range(8):
        try:
            await edge_tts.Communicate(text, VOICE, rate="+4%").save(path)
            return
        except Exception:
            if os.path.exists(path):
                os.remove(path)
            if attempt == 7:
                raise
            await asyncio.sleep(5)


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        text=True,
    )
    return float(out.strip())


EXPORTS = {}


def run_cmd(cmd, cwd):
    if cmd.startswith("export ") and "=" in cmd:
        k, v = cmd[7:].split("=", 1)
        EXPORTS[k] = v
        return ""
    env = dict(os.environ, FORCE_COLOR="1", APP_DATABASE_URL=PG, BILLING_DATABASE_URL=MY, **EXPORTS)
    env.pop("NO_COLOR", None)
    env["PATH"] = SDKBIN + os.pathsep + env["PATH"]
    env["COLUMNS"] = "120"
    p = subprocess.run(
        ["bash", "-c", "shopt -s nullglob; " + cmd],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    return p.stdout.rstrip("\n")


def parse_ansi(line):
    spans, color, bold = [], FG, False
    for part in re.split(r"(\x1b\[[0-9;]*m)", line):
        m = re.match(r"\x1b\[([0-9;]*)m", part)
        if m:
            for code in (m.group(1) or "0").split(";"):
                if code == "0":
                    color, bold = FG, False
                elif code == "1":
                    bold = True
                elif code in ANSI:
                    color = ANSI[code]
        elif part:
            spans.append((part, color, bold))
    return spans


class Term:
    def __init__(self):
        self.font = ImageFont.truetype(MONO, 14)
        self.bold = ImageFont.truetype(MONO_B, 14)
        self.cw = self.font.getlength("M")
        self.lh = 20
        self.cols = int((W - 80) / self.cw)

    def wrap(self, spans):
        lines, cur, n = [], [], 0
        for text, c, b in spans:
            while text:
                room = self.cols - n
                if len(text) <= room:
                    cur.append((text, c, b)); n += len(text); text = ""
                else:
                    cut = text.rfind(" ", 0, room + 1)
                    cut = cut if cut > 0 else room
                    cur.append((text[:cut], c, b)); lines.append(cur); cur, n = [], 0
                    text = "    " + text[cut:].lstrip(" ")
        lines.append(cur)
        return lines

    def frame(self, lines, cursor=False):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([20, 14, W - 20, H - 14], 12, fill=(30, 33, 41), outline=(60, 64, 76))
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([40 + i * 24, 30, 54 + i * 24, 44], fill=c)
        d.text((W // 2, 37), "~ — bash", font=ImageFont.truetype(SANS, 15), fill=(150, 155, 165), anchor="mm")
        visible = lines[-30:]
        y = 64
        for ln in visible:
            x = 40
            for text, c, b in ln:
                d.text((x, y), text, font=self.bold if b else self.font, fill=c)
                x += self.cw * len(text)
            y += self.lh
        if cursor:
            last = visible[-1] if visible else []
            x = 40 + self.cw * sum(len(t) for t, _, _ in last)
            d.rectangle([x, y - self.lh + 3, x + self.cw - 2, y - 4], fill=FG)
        return img


def card(scene):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    if scene["kind"] == "title":
        d.text((W // 2, 280), scene["title"], font=ImageFont.truetype(MONO_B, 78), fill=(95, 215, 120), anchor="mm")
        d.multiline_text((W // 2, 420), scene["sub"], font=ImageFont.truetype(SANS, 32), fill=FG, anchor="mm",
                         align="center", spacing=12)
        d.text((W // 2, 620), "github.com/hamarshehmhmd/crossql  ·  check · tables · query · explain · snapshot  ·  GNU AGPL-3.0",
               font=ImageFont.truetype(SANS, 20), fill=(150, 155, 165), anchor="mm")
    else:
        d.text((90, 90), scene["title"], font=ImageFont.truetype(SANS_B, 52), fill=(95, 215, 120))
        f = ImageFont.truetype(SANS, 29)
        y = 200
        for b in scene["bullets"]:
            if b and not b.startswith("   "):
                d.text((90, y), "•", font=f, fill=(240, 200, 80))
            d.text((130, y), b.strip(), font=f, fill=FG)
            y += 58 if b.startswith("   ") or not b else 62
    return img


class Writer:
    def __init__(self, path, audio):
        self.p = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", audio,
             "-af", "apad", "-c:v", "libx264", "-preset", "medium", "-crf", "23",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
             "-shortest", path],
            stdin=subprocess.PIPE,
        )
        self.n = 0

    def put(self, img, count=1):
        data = img.tobytes()
        for _ in range(count):
            self.p.stdin.write(data)
        self.n += count

    def close(self):
        self.p.stdin.close()
        self.p.wait()
        if self.p.returncode:
            sys.exit("ffmpeg failed")


def _cap_output(out):
    lines = out.splitlines()
    if len(lines) > 24:
        lines = lines[:22] + ["…", lines[-1]]
    return "\n".join(lines)


def render_scene(i, scene, workdir):
    audio = os.path.join(BUILD, f"s{i}.mp3")
    if not os.path.exists(audio):
        asyncio.run(tts(scene["say"], audio))
    total = int((duration(audio) + 0.7) * FPS)
    path = os.path.join(BUILD, f"s{i}.mp4")
    w = Writer(path, audio)
    if scene["kind"] != "term":
        w.put(card(scene), total)
        w.close()
        return path
    t = Term()
    shown = scene.get("show", scene["cmds"])
    wd = workdir
    outputs = [_cap_output(run_cmd(c, wd)) for c in scene["cmds"]]
    # If show has more lines than cmds (a command displayed as several visual lines
    # that map to one executed command), join show into one typed command per output.
    if len(shown) != len(outputs):
        if len(outputs) == 1:
            shown = [" ".join(s.rstrip("\\").strip() for s in shown)]
        elif len(shown) > len(outputs):
            # first N-1 outputs map 1:1; remaining show lines are the last command
            shown = list(shown[: len(outputs) - 1]) + [
                " ".join(s.rstrip("\\").strip() for s in shown[len(outputs) - 1 :])
            ]
    steps = list(zip(shown, outputs))
    typing = sum(len(s) for s, _ in steps) / 32 * FPS
    pause = max(6, int((total - typing) / max(1, len(steps))) - 4)
    lines = []
    for cmd, out in steps:
        prompt = [(PROMPT, (95, 215, 120), True)]
        for k in range(0, len(cmd) + 1, 2):
            frame = t.frame(lines + t.wrap(prompt + [(cmd[:k], FG, False)]), cursor=True)
            w.put(frame)
        lines += t.wrap(prompt + [(cmd, FG, False)])
        for ol in out.splitlines():
            lines += t.wrap(parse_ansi(ol))
        lines.append([])
        hold = min(pause, max(0, total - w.n))
        w.put(t.frame(lines), hold)
    final = t.frame(lines + t.wrap([(PROMPT, (95, 215, 120), True)]), cursor=True)
    if w.n < total:
        w.put(final, total - w.n)
    w.close()
    return path


REVENUE = """-- revenue and support load by signup channel, across 4 sources
WITH paid AS (
  SELECT user_id, sum(amount_cents) / 100 AS revenue
  FROM billing.invoices WHERE status = 'paid' GROUP BY 1),   -- MySQL
tickets AS (
  SELECT email, count(*) AS n FROM support.tickets GROUP BY 1) -- SQLite
SELECT l.channel, count(*) AS customers, sum(p.revenue) AS revenue,
       coalesce(sum(t.n), 0) AS tickets
FROM app.users u                                               -- Postgres
JOIN paid p ON p.user_id = u.id
JOIN leads l ON l.email = u.email                              -- CSV
LEFT JOIN tickets t ON t.email = u.email
GROUP BY 1 ORDER BY revenue DESC;
"""

TOML = """[sources.app]                       # Postgres
url = "${APP_DATABASE_URL}"
[sources.billing]                   # MySQL / MariaDB
url = "${BILLING_DATABASE_URL}"
[sources.support]
url = "support.sqlite"
[sources.leads]
path = "leads.csv"
"""


def main():
    os.makedirs(BUILD, exist_ok=True)
    if os.path.exists(SANDBOX):
        shutil.rmtree(SANDBOX)
    os.makedirs(os.path.join(SANDBOX, "bin"))
    work = os.path.join(SANDBOX, "analytics")
    subprocess.run([sys.executable, os.path.join(ROOT, "examples", "make_demo.py"), work, "--pg", PG,
                    "--mysql=--no-defaults -h127.0.0.1 -P3307 -uroot -ppw billing"], check=True,
                   env=dict(os.environ, PATH="/tmp/bin:" + os.environ["PATH"]))
    with open(os.path.join(work, "crossql.toml"), "w") as fh:
        fh.write(TOML)
    with open(os.path.join(work, "revenue.sql"), "w") as fh:
        fh.write(REVENUE)
    with open(os.path.join(work, "views.sql"), "w") as fh:
        fh.write("-- name: revenue_by_channel\n" + REVENUE.split("\n", 1)[1] +
                 "\n-- name: open_tickets_by_plan\nSELECT u.plan, count(*) AS open_tickets FROM support.tickets t "
                 "JOIN app.users u USING (email) WHERE t.status = 'open' GROUP BY 1;\n")
    only = [int(x) for x in sys.argv[1:]]
    parts = [render_scene(i, s, work) if (not only or i in only) else os.path.join(BUILD, "s%d.mp4" % i)
             for i, s in enumerate(SCENES)]
    with open(os.path.join(BUILD, "list.txt"), "w") as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i",
         os.path.join(BUILD, "list.txt"), "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", OUT],
        check=True,
    )
    with open(os.path.join(BUILD, "narration.txt"), "w") as fh:
        fh.write("\n\n".join(s["say"] for s in SCENES) + "\n")
    print(OUT, round(duration(OUT), 1), "s")


if __name__ == "__main__":
    main()
