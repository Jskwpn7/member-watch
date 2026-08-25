# Member Watch

A weekly harvest of what consumer-advocacy organisations publish, deduplicated,
tagged and made searchable in one place.

Built to run on a schedule with no server: a Python pipeline writes to SQLite,
exports JSON, and a single static HTML page reads it.

---

## Quick start

```bash
pip install -r requirements.txt
export CONTACT_EMAIL="you@yourorg.org"      # goes in the crawler user-agent
export ANTHROPIC_API_KEY="sk-ant-..."       # optional; omit to run offline

python -m pipeline.run --offline            # first harvest, no API needed
cd site && python -m http.server 8000       # then open localhost:8000
```

Check it works before pointing it at real websites:

```bash
bash tests/selftest.sh
```

That builds a fake member site containing a duplicate article, a page with no
publication date, and links that should be ignored, then asserts the pipeline
handles all three correctly.

---

## How a run works

```
discover  ->  extract  ->  dedupe  ->  classify  ->  export
```

Each stage reads and writes the same SQLite database and can be run on its own:

```bash
python -m pipeline.discover          # find candidate URLs
python -m pipeline.extract           # fetch and pull out text + dates
python -m pipeline.dedupe            # collapse duplicates, record revisions
python -m pipeline.classify          # tag against taxonomy.yml
python -m pipeline.export            # write site/data/corpus.json
```

That separation matters in practice. When you change `taxonomy.yml` you rerun
`classify` alone — no refetching, no extra requests to anyone's website.

| Flag | What it does |
|---|---|
| `--offline` | Skip the classification API and use keyword matching. Fine for testing, not for real use. |
| `--refresh` | Re-check URLs already held, to catch pages that have been edited. Slower; run monthly rather than weekly. |
| `--limit N` | Stop after N extractions. Useful when adding a new source. |

## Adding a member

One YAML file per organisation in `sources/`. Run `audit_sources.py` first to
find out which method the site supports.

```yaml
id: which-uk
org: Which?
country: GB
region: Europe
language: en
homepage: https://www.which.co.uk
targets:
  - name: news                  # also used as a hint for the classifier
    method: rss                 # rss | sitemap | html
    url: https://www.which.co.uk/news/feed/
```

**`rss`** needs only a URL. **`sitemap`** takes `include_patterns` and
`exclude_patterns` matched against the URL, because a site's sitemap contains
everything including the shopping basket. **`html`** needs an `item_selector`
CSS selector for the links on a list page, and only follows links on the same
host.

Nothing site-specific lives in the Python. That is deliberate: it is the only
way this reaches 200 organisations, and it means members could eventually
register their own sources without anyone touching the code.

## How duplicates are handled

Three layers, because "have I seen this URL" is not enough:

1. **Canonical URL** — tracking parameters stripped, `http`/`https` and `www`
   normalised, trailing slashes and `index.html` removed. Catches most of it.
2. **Content hash** — SHA-256 of the extracted text. Catches the same report
   published at two URLs, which happens constantly when something appears under
   both `/news/` and `/publications/`.
3. **Near-duplicate** — Jaccard similarity on word trigrams, threshold 0.82.
   Catches lightly-edited syndication. Raise the threshold in `dedupe.py` if
   you see distinct items being merged.

A page whose content changes is stored as a **new revision of the same item**,
not as a new item, and is marked "Updated" in the dashboard.

## Dates

Publication dates are the least reliable field on most sites. `extract.py`
tries JSON-LD, then meta tags, then `<time>` elements, then a date printed in
the opening text — and records which one it used. Where nothing is found it
falls back to the harvest date and labels it "(harvested)" in the interface,
so a date you cannot trust always looks like a date you cannot trust.

## Classification

Two axes defined in `taxonomy.yml`: a single **content type** and one to three
**topics**. The topic list mirrors the Consumers International programme areas
so the vocabulary matches what the network already uses.

The section of the site an item came from is strong evidence and is passed to
the model as a prior. Anything classified below 0.7 confidence is flagged
`needs review` and appears under the "Needs review" lens. Spending five minutes
a week there is what keeps the taxonomy honest — and those corrections are the
raw material for few-shot examples later.

Cost is negligible at this volume: batches of 10 items, roughly 100 items a
week across five organisations.

## Deploying

The dashboard is a static directory. Any host works.

1. Push to a **private** GitHub repo.
2. Add `ANTHROPIC_API_KEY` and `CONTACT_EMAIL` as repository secrets.
3. `.github/workflows/weekly.yml` runs every Monday at 06:00 UTC and commits
   the updated database and JSON. There is a manual "Run workflow" button too.
4. Deploy `site/` to Cloudflare Pages, then put **Cloudflare Access** in front
   of it — free for up to 50 users, email or Google login, no code.

The SQLite file lives in the repo. At this volume it stays small, git gives you
free versioned backups, and you can open it in any SQLite browser. When it
outgrows that, moving to Postgres is mechanical.

## Being a good citizen

The crawler identifies itself with your contact email, honours `robots.txt`,
waits two seconds between requests to the same host, and skips anything over
8 MB. Leave all of that alone. These are peer organisations, and the cost of
being noticed for the wrong reason is much higher than the cost of a slow run.

The dashboard stores full text for search but displays only a short extract and
a link out. If it ever becomes visible beyond your own team, keep it that way —
it is both the safe answer on copyright and the one that sends traffic to
members rather than away from them.

## Known limits

- `html` sources break when a site is redesigned. Failures are logged loudly
  and surfaced in the dashboard header; a source silently returning zero is the
  main way this kind of tool rots.
- Language is recorded but nothing is translated yet. The schema is ready for
  it: translate title and summary only, keep the original.
- Near-duplicate detection compares against the most recent 500 items, not the
  whole corpus. Fine at POC scale, needs proper indexing beyond a few thousand.
- No write-back from the dashboard. Corrections go in the database.

---

`site/data/corpus.json` ships with a few fixture rows so the dashboard renders
something the first time you open it. The organisation is called "Test Consumer
Body" and the links point at localhost. Your first real run overwrites it.
