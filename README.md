# Member Watch

A weekly harvest of what consumer-advocacy organisations publish, deduplicated,
tagged and made searchable in one place.

Built to run on a schedule with no server: a Python pipeline writes to SQLite,
exports JSON, and a single static HTML page reads it.

---

## Quick start

**Windows (PowerShell)**

```powershell
pip install -r requirements.txt
$env:CONTACT_EMAIL   = "you@yourorg.org"    # goes in the crawler user-agent
$env:ANTHROPIC_API_KEY = "sk-ant-..."       # optional; omit to run offline

python -m tests.selftest                    # verify before touching real sites
python -m pipeline.run --offline            # first harvest, no API needed
cd site; python -m http.server 8000         # then open localhost:8000
```

To make the environment variables stick across sessions, use
`setx CONTACT_EMAIL "you@yourorg.org"` once and reopen the terminal.

**macOS / Linux**

```bash
pip install -r requirements.txt
export CONTACT_EMAIL="you@yourorg.org"
export ANTHROPIC_API_KEY="sk-ant-..."

python -m tests.selftest
python -m pipeline.run --offline
cd site && python -m http.server 8000
```

`tests/selftest.py` builds a fake member site containing a duplicate article, a
page with no publication date, and links that should be ignored, then asserts
the pipeline handles all three. It backs up and restores your real database, so
it is safe to run at any time.

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

Add `--reclassify` to make that rerun actually re-tag things:

```bash
python -m pipeline.classify --reclassify
```

Without it, `classify` only looks at items that have never been tagged, so an
edited taxonomy would silently apply to new items only. `--reclassify` re-tags
everything the model labelled and leaves publisher-labelled items alone — those
came from the source's own metadata and are ground truth. It refuses to run
without `ANTHROPIC_API_KEY` unless you also pass `--offline`, because otherwise
a missing key would quietly overwrite real labels with keyword guesses.

| Flag | What it does |
|---|---|
| `--offline` | Skip the classification API and use keyword matching. Fine for testing, not for real use. |
| `--refresh` | Re-check URLs already held, to catch pages that have been edited. Slower; run monthly rather than weekly. |
| `--limit N` | Stop after N extractions. Useful when adding a new source. |

## Getting a selector right

Never guess a selector and run the whole pipeline to find out. Use:

```powershell
python tools\try_target.py sources\cag-in.yml news --fetch 3
```

It fetches the listing, prints what would be captured, and warns about the
failure modes that do not raise errors — zero results, suspiciously few
results, titles that are navigation text rather than headlines, patterns so
loose they pull in the whole site. `--fetch N` also extracts the first N pages
so you can see the title, date and word count the pipeline would store.

Nothing is written to the database, so iterate freely.

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

## Using a publisher's own labels

Some APIs return the organisation's own content type and topics. That is
ground truth — Which? knows whether something is a policy submission or a
press statement better than any model inferring it from the text. Map those
labels onto our taxonomy with `field_map` and the classifier is skipped
entirely for that source: cheaper, faster, and more accurate.

```yaml
    field_map:
      content_type:
        field: content_type_data       # dotted path; a string or a list
        map:
          Press statement: news
          Policy submission: policy
      topics:
        field: taxonomy                # a list of objects
        item_key: name                 # which key on each object holds the label
        map:
          Digital Markets: digital
          Financial Services: finance
```

Unmapped labels are dropped, so an unfamiliar one costs you a topic rather
than corrupting the taxonomy. The publisher's exact wording is kept in
`pub_content_type` and shown in the dashboard as a dashed tag beside our
coarser label, so "Policy submission" is not flattened away into "policy".

Check the aggregates in the API response before writing the map — they list
every label in use with a count, which tells you what you are covering.

## Controlling backfill

`days_back` stops pagination once results go past a cutoff. Because these
APIs return newest-first, that is safe: every later page is older still.
Which?'s library holds 2,142 items over 143 pages; `days_back: 100` fetches
roughly the last three months and stops, rather than walking the lot. Raise it
once the pipeline is proven — a deeper backfill costs nothing but time, and
re-running discovery later will pick up the older items without duplicating
anything already held.

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

## Where to keep this folder

**Do not keep the repository inside OneDrive, Dropbox or Google Drive.**

SQLite in WAL mode writes three files that must stay consistent with each other
(`corpus.db`, `-wal`, `-shm`). Sync clients upload them independently and may
lock a file mid-write, which produces a corrupted or silently truncated
database. Syncing a `.git` directory causes similar trouble.

Keep the project on local disk — `C:\\Users\\<you>\\dev\\member-watch` or
similar — and use a private GitHub repository as the backup. Git already gives
you versioned history, which is what you actually wanted from OneDrive.

If you must keep it in a synced folder, at minimum change `journal_mode=WAL` to
`journal_mode=DELETE` in `pipeline/common.py` and pause syncing while a harvest
runs. This is a mitigation, not a fix.

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

## When a site needs JavaScript

Some sites render their listing pages client-side. Fetch one with `requests`
and you get an empty shell — Which?'s policy library at
`/policy-and-insight/search` returns literally "Loading content". No CSS
selector can fix this, because the articles are not in the HTML.

**Use the API the page calls.** Open the page in Chrome, DevTools → Network →
Fetch/XHR, reload, and find the request that returns the listing. Right-click
it → Copy → Copy as cURL, which gives you the method, headers and body. Then
use `method: json`:

```yaml
  - name: policy
    method: json
    url: https://example.execute-api.eu-west-1.amazonaws.com/prod/search
    http_method: POST              # GET is the default
    body:                          # the payload the page sends
      filters:
        contentTypes: policy-paper,policy-submission
    items_path: data.results       # dotted path to the array
    url_field: slug                # field holding the link
    url_prefix: https://www.example.org/policy-and-insight/
    title_field: title
    date_field: publishedDate
    page_param: filters.page       # dotted path to the page number
    pages: 5                       # stops early when a page comes back empty
```

Do not guess `items_path`. Run this once and it tells you:

```powershell
python tools\try_target.py sources\which-uk.yml policy --inspect
```

It prints the response structure and lists every array of objects it found,
with the likely `url_field` and `date_field` on each.

This is usually *better* than scraping, not a workaround: an API contract
changes far less often than a CSS class name, and the dates come back already
structured. If `items_path` is wrong the error tells you the keys that were
actually present, so `try_target.py` gets you there in a couple of guesses.

Two fallbacks if there is no usable API: look for a sitemap covering that
section, since individual articles are often indexed even when the listing
page is not scrapeable; or render the page with `playwright`, which works on
anything but is a heavy dependency and a slow run.

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
