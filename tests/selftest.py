"""Cross-platform self-test. Works in PowerShell, cmd, macOS and Linux.

Builds a fake member site containing the three traps that quietly break this
kind of tool, runs the real pipeline against it, and asserts the results:

  1. the same article published at two different URLs
  2. a page with no publication date anywhere in the markup
  3. a list page whose links point off-site

Run it before pointing the tool at real websites, and after any change to
discover.py, extract.py or dedupe.py.

    python -m tests.selftest
"""

from __future__ import annotations

import functools
import http.server
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "corpus.db"
SOURCE_FILE = ROOT / "sources" / "_selftest.yml"

BODY_A = ("The consumer group published analysis of instant payment systems, "
          "warning that scam protections lag behind card protections, and "
          "called on the regulator to mandate reimbursement for authorised "
          "push payment fraud and require banks to publish scam data, drawing "
          "on a survey of two thousand consumers about redress.")
BODY_B = ("Regulators fined an online retailer for misleading discount pricing "
          "after finding reference prices were not genuine, covering eighteen "
          "months of promotional activity, with a corrective notice and pricing "
          "changes required within ninety days or further penalties follow.")


def article(title: str, date: str | None, body: str) -> str:
    jsonld = (f'<script type="application/ld+json">'
              f'{{"@type":"NewsArticle","datePublished":"{date}"}}</script>'
              if date else "")
    return (f"<!doctype html><html><head><title>{title}</title>{jsonld}</head>"
            f"<body><article><h1>{title}</h1><p>{body}</p></article>"
            f"</body></html>")


def build_fixture(root: Path, port: int) -> None:
    (root / "news").mkdir(parents=True)
    (root / "reports").mkdir(parents=True)

    (root / "robots.txt").write_text("User-agent: *\nAllow: /\n")

    title_a = "Instant payment scam protections lag behind cards"
    title_b = "Retailer fined over misleading discount pricing"

    (root / "news/a.html").write_text(article(title_a, "2026-08-19", BODY_A))
    # Trap 1: identical text at a second URL.
    (root / "news/dup.html").write_text(article(title_a, "2026-08-19", BODY_A))
    (root / "news/b.html").write_text(article(title_b, "2026-08-21", BODY_B))
    # Trap 2: no date in JSON-LD, meta, or anywhere else.
    (root / "reports/nodate.html").write_text(article(
        "Annual redress review", None,
        BODY_B + " Our review considers ombudsman performance, small claims "
                 "access and collective action across four jurisdictions."))

    # Trap 3: a list page linking off-site and to an anchor.
    (root / "reports/index.html").write_text(
        '<div class="listing">'
        '<a href="/reports/nodate.html">Annual redress review</a>'
        '<a href="https://elsewhere.example.com/x">Offsite link</a>'
        '<a href="#top">Anchor</a></div>')

    base = f"http://127.0.0.1:{port}"
    (root / "news/feed.xml").write_text(f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Fixture</title>
<item><title>{title_a}</title><link>{base}/news/a.html?utm_source=x</link>
<pubDate>Wed, 19 Aug 2026 09:00:00 GMT</pubDate></item>
<item><title>{title_b}</title><link>{base}/news/b.html</link>
<pubDate>Fri, 21 Aug 2026 09:00:00 GMT</pubDate></item>
<item><title>{title_a}</title><link>{base}/news/dup.html</link>
<pubDate>Wed, 19 Aug 2026 10:00:00 GMT</pubDate></item>
</channel></rss>""")

    SOURCE_FILE.write_text(f"""id: _selftest
org: Fixture Consumer Body
country: GB
region: Europe
language: en
homepage: {base}
targets:
  - {{name: news, method: rss, url: "{base}/news/feed.xml"}}
  - {{name: research, method: html, url: "{base}/reports/index.html",
      item_selector: ".listing a[href]"}}
""")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(root: Path, port: int) -> http.server.ThreadingHTTPServer:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):       # keep the test output readable
            pass

    handler = functools.partial(Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def assertions() -> list[bool]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    one = lambda sql: conn.execute(sql).fetchone()                    # noqa: E731

    checks = [
        ("live items after dedup",
         one("SELECT COUNT(*) n FROM items WHERE status='classified'")["n"], 3),
        ("duplicate caught by content hash",
         one("SELECT COUNT(*) n FROM items WHERE status='duplicate'")["n"], 1),
        ("utm parameter stripped from canonical url",
         "utm" in (one("SELECT canonical_url u FROM items "
                       "WHERE url LIKE '%utm_source%'") or {"u": ""})["u"], False),
        ("missing date falls back to harvest date",
         (one("SELECT date_source d FROM items "
              "WHERE title LIKE 'Annual redress%'") or {"d": None})["d"],
         "first_seen"),
        ("offsite link ignored",
         one("SELECT COUNT(*) n FROM items WHERE url LIKE '%elsewhere%'")["n"], 0),
        ("json-ld date preferred over feed date",
         (one("SELECT published_at p FROM items "
              "WHERE title LIKE 'Retailer fined%'") or {"p": None})["p"],
         "2026-08-21"),
    ]
    conn.close()

    results = []
    for label, got, want in checks:
        ok = got == want
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {label}"
              f"{'' if ok else f'  (got {got!r}, want {want!r})'}")
    return results


def main() -> int:
    fixture = Path(tempfile.mkdtemp(prefix="memberwatch-fixture-"))
    backup = Path(tempfile.mkdtemp(prefix="memberwatch-db-")) / "corpus.db"
    port = free_port()
    server = None

    try:
        build_fixture(fixture, port)
        server = serve(fixture, port)

        # Keep the real corpus out of harm's way while we test.
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(DB) + suffix)
            if candidate.exists() and suffix == "":
                shutil.move(str(candidate), str(backup))
            elif candidate.exists():
                candidate.unlink()

        env = {**os.environ,
               "PER_HOST_DELAY": "0",
               "CONTACT_EMAIL": "selftest@example.org",
               "no_proxy": "127.0.0.1,localhost",
               "NO_PROXY": "127.0.0.1,localhost"}
        run = subprocess.run(
            [sys.executable, "-m", "pipeline.run", "--offline"],
            cwd=ROOT, env=env, capture_output=True, text=True)

        if run.returncode != 0:
            print("pipeline failed:\n", run.stdout[-2000:], run.stderr[-2000:])
            return 1

        print("\nself-test results")
        results = assertions()
        passed = all(results)
        print(f"\nself-test {'PASSED' if passed else 'FAILED'} "
              f"({sum(results)}/{len(results)} checks)")
        return 0 if passed else 1

    finally:
        if server:
            server.shutdown()
        SOURCE_FILE.unlink(missing_ok=True)
        for suffix in ("", "-wal", "-shm"):
            Path(str(DB) + suffix).unlink(missing_ok=True)
        if backup.exists():
            shutil.move(str(backup), str(DB))
        shutil.rmtree(fixture, ignore_errors=True)
        shutil.rmtree(backup.parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
