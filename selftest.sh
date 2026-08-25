#!/usr/bin/env bash
# Builds a fake member site with known traps, runs the pipeline against it,
# and asserts the results. Run this before pointing the tool at real sites,
# and again any time you change discover.py, extract.py or dedupe.py.
set -euo pipefail
cd "$(dirname "$0")/.."

FIX=$(mktemp -d); DB_DIR=$(mktemp -d); PORT=8765
trap 'kill %1 2>/dev/null || true; rm -rf "$FIX" "$DB_DIR"' EXIT

page () { # path title date body
  mkdir -p "$FIX/$(dirname "$1")"
  cat > "$FIX/$1" <<EOF
<!doctype html><html><head><title>$2</title>
<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"$3"}</script>
</head><body><article><h1>$2</h1><p>$4</p></article></body></html>
EOF
}

A="The consumer group published analysis of instant payment systems warning that scam protections lag behind card protections, and called on the regulator to mandate reimbursement for authorised push payment fraud and require banks to publish scam data covering two thousand surveyed consumers."
B="Regulators fined an online retailer for misleading discount pricing after finding reference prices were not genuine, covering eighteen months of promotional activity, with a corrective notice and pricing changes required within ninety days or further penalties will follow."

echo "User-agent: *" > "$FIX/robots.txt"; echo "Allow: /" >> "$FIX/robots.txt"
page news/a.html    "Instant payment scam protections lag behind cards" 2026-08-19 "$A"
page news/dup.html  "Instant payment scam protections lag behind cards" 2026-08-19 "$A"   # trap 1: same text, different URL
page news/b.html    "Retailer fined over misleading pricing"            2026-08-21 "$B"
mkdir -p "$FIX/reports"
cat > "$FIX/reports/nodate.html" <<EOF                                                    # trap 2: no date anywhere
<!doctype html><html><head><title>Annual redress review</title></head>
<body><article><h1>Annual redress review</h1><p>$B Our review considers ombudsman performance, small claims access and collective action across four jurisdictions.</p></article></body></html>
EOF
cat > "$FIX/reports/index.html" <<EOF
<div class="listing">
<a href="/reports/nodate.html">Annual redress review</a>
<a href="https://elsewhere.example.com/x">Offsite link</a>
<a href="#top">Anchor</a>
</div>
EOF
cat > "$FIX/news/feed.xml" <<EOF
<?xml version="1.0"?><rss version="2.0"><channel><title>Fixture</title>
<item><title>Instant payment scam protections lag behind cards</title>
<link>http://127.0.0.1:$PORT/news/a.html?utm_source=x</link>
<pubDate>Wed, 19 Aug 2026 09:00:00 GMT</pubDate></item>
<item><title>Retailer fined over misleading pricing</title>
<link>http://127.0.0.1:$PORT/news/b.html</link><pubDate>Fri, 21 Aug 2026 09:00:00 GMT</pubDate></item>
<item><title>Instant payment scam protections lag behind cards</title>
<link>http://127.0.0.1:$PORT/news/dup.html</link><pubDate>Wed, 19 Aug 2026 10:00:00 GMT</pubDate></item>
</channel></rss>
EOF

SRC=sources/_selftest.yml
cat > "$SRC" <<EOF
id: _selftest
org: Fixture Consumer Body
country: GB
region: Europe
language: en
homepage: http://127.0.0.1:$PORT
targets:
  - {name: news, method: rss, url: "http://127.0.0.1:$PORT/news/feed.xml"}
  - {name: research, method: html, url: "http://127.0.0.1:$PORT/reports/index.html", item_selector: ".listing a[href]"}
EOF
trap 'kill %1 2>/dev/null || true; rm -rf "$FIX" "$DB_DIR"; rm -f "$SRC"' EXIT

( cd "$FIX" && python -m http.server $PORT --bind 127.0.0.1 >/dev/null 2>&1 ) &
sleep 1.5

mv data/corpus.db "$DB_DIR/real.db" 2>/dev/null || true
PER_HOST_DELAY=0 no_proxy=127.0.0.1 CONTACT_EMAIL=selftest@example.org \
  python -m pipeline.run --offline >/dev/null 2>&1

python - <<'EOF'
import sqlite3, sys
c = sqlite3.connect("data/corpus.db"); c.row_factory = sqlite3.Row
def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    return ok

live = c.execute("SELECT COUNT(*) n FROM items WHERE status='classified'").fetchone()["n"]
dups = c.execute("SELECT COUNT(*) n FROM items WHERE status='duplicate'").fetchone()["n"]
utm  = c.execute("SELECT canonical_url u FROM items WHERE url LIKE '%utm_source%'").fetchone()
nodate = c.execute("SELECT date_source d FROM items WHERE title LIKE 'Annual redress%'").fetchone()
offsite = c.execute("SELECT COUNT(*) n FROM items WHERE url LIKE '%elsewhere%'").fetchone()["n"]
jsonld = c.execute("SELECT published_at p FROM items WHERE title LIKE 'Retailer fined%'").fetchone()

results = [
  check("live items after dedup",        live, 3),
  check("duplicate caught by hash",      dups, 1),
  check("utm stripped from canonical",   "utm" in (utm["u"] if utm else ""), False),
  check("missing date falls back",       nodate["d"] if nodate else None, "first_seen"),
  check("offsite link ignored",          offsite, 0),
  check("json-ld date used",             jsonld["p"] if jsonld else None, "2026-08-21"),
]
print("\nself-test", "PASSED" if all(results) else "FAILED")
sys.exit(0 if all(results) else 1)
EOF
STATUS=$?
rm -f data/corpus.db data/corpus.db-wal data/corpus.db-shm
mv "$DB_DIR/real.db" data/corpus.db 2>/dev/null || true
exit $STATUS
