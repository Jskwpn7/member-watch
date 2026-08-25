"""Stage 1 -- find candidate URLs.

Three methods, chosen per target in the source YAML:

  rss      the site publishes a feed. Gives title and date for free.
  sitemap  no feed, but sitemap.xml with <lastmod>. Filter by URL pattern.
  html     neither. Parse a list page with a CSS selector. Brittle by nature.

Discovery does URL-level deduplication only: if we already hold this
canonical URL we skip it, unless --refresh is passed. Content-level
deduplication happens later, in dedupe.py, once we have the text.
"""

from __future__ import annotations

import argparse
import re
from urllib.parse import urljoin, urlparse

import copy
import json as jsonlib
from datetime import datetime, timedelta, timezone

import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .common import (USER_AGENT, canonical_url, db, fetch, load_sources, log,
                     now, robots_allows, _session, _throttle)


# ---------------------------------------------------------------- methods

def from_rss(target: dict) -> list[dict]:
    resp = fetch(target["url"])
    if resp is None:
        raise RuntimeError("feed unreachable")
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"feed did not parse: {feed.bozo_exception}")

    out = []
    for entry in feed.entries:
        link = entry.get("link")
        if not link:
            continue
        published = None
        for key in ("published", "updated", "created"):
            if entry.get(key):
                try:
                    published = dateparser.parse(entry[key]).date().isoformat()
                    break
                except (ValueError, TypeError, OverflowError):
                    pass
        out.append({
            "url": link,
            "title": entry.get("title"),
            "published_at": published,
            "date_source": "feed" if published else None,
            "author": entry.get("author"),
        })
    return out


def from_sitemap(target: dict, depth: int = 0) -> list[dict]:
    resp = fetch(target["url"])
    if resp is None:
        raise RuntimeError("sitemap unreachable")
    body = resp.text

    # A sitemap index points at more sitemaps. Follow one level.
    if "<sitemapindex" in body[:3000].lower() and depth == 0:
        children = re.findall(r"<loc>\s*(.*?)\s*</loc>", body, re.I)
        found: list[dict] = []
        for child in children[:20]:
            try:
                found += from_sitemap({**target, "url": child}, depth + 1)
            except RuntimeError as exc:
                log.warning("child sitemap %s: %s", child, exc)
        return found

    include = target.get("include_patterns") or []
    exclude = target.get("exclude_patterns") or []

    out = []
    for block in re.findall(r"<url>(.*?)</url>", body, re.S | re.I):
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.I)
        if not loc:
            continue
        url = loc.group(1)
        if include and not any(p in url for p in include):
            continue
        if any(p in url for p in exclude):
            continue

        lastmod = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block, re.I)
        published = None
        if lastmod:
            try:
                published = dateparser.parse(lastmod.group(1)).date().isoformat()
            except (ValueError, TypeError, OverflowError):
                pass
        out.append({
            "url": url,
            "title": None,
            "published_at": published,
            # lastmod is when the page changed, not when it was published.
            # Treat it as weak evidence and let extract.py overwrite it.
            "date_source": "sitemap" if published else None,
            "author": None,
        })
    return out


def from_html(target: dict) -> list[dict]:
    resp = fetch(target["url"])
    if resp is None:
        raise RuntimeError("list page unreachable")
    soup = BeautifulSoup(resp.text, "html.parser")

    selector = target.get("item_selector")
    if not selector:
        raise RuntimeError("html target needs an item_selector")

    base_host = urlparse(target["url"]).netloc
    include = target.get("include_patterns") or []
    exclude = target.get("exclude_patterns") or []

    seen, out = set(), []
    for node in soup.select(selector):
        href = node.get("href")
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(resp.url, href)
        # Stay on the member's own site; list pages link out constantly.
        if urlparse(url).netloc != base_host:
            continue
        if include and not any(p in url for p in include):
            continue
        if any(p in url for p in exclude):
            continue
        if url in seen:
            continue
        seen.add(url)

        title = node.get_text(" ", strip=True) or None
        out.append({
            "url": url,
            "title": title if title and len(title) > 8 else None,
            "published_at": None,
            "date_source": None,
            "author": None,
        })
    return out


def _dig(obj, path: str):
    """Walk a dotted path through nested dicts and lists: 'data.items.0.url'."""
    if not path:
        return obj
    for key in path.split("."):
        if obj is None:
            return None
        if isinstance(obj, list):
            try:
                obj = obj[int(key)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return obj


def _bury(obj: dict, path: str, value) -> None:
    """Set a value at a dotted path, creating dicts as needed."""
    keys = path.split(".")
    for key in keys[:-1]:
        obj = obj.setdefault(key, {})
    obj[keys[-1]] = value


def json_request(target: dict, page: int = 0) -> dict:
    """One request to a JSON endpoint. Shared by from_json and try_target."""
    method = target.get("http_method", "GET").upper()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json",
               **(target.get("headers") or {})}

    if not robots_allows(target["url"]):
        raise RuntimeError("robots.txt disallows this endpoint")

    params = copy.deepcopy(target.get("params") or {})
    body = copy.deepcopy(target.get("body") or {})
    if target.get("page_param"):
        _bury(body if method == "POST" else params, target["page_param"],
              page + int(target.get("first_page", 1)))

    _throttle(target["url"].split("/")[2])
    try:
        if method == "POST":
            resp = _session.post(target["url"], json=body,
                                 params=params or None, headers=headers,
                                 timeout=30)
        else:
            resp = _session.get(target["url"], params=params or None,
                                headers=headers, timeout=30)
    except Exception as exc:                                # noqa: BLE001
        raise RuntimeError(f"request failed: {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(f"http {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except (ValueError, jsonlib.JSONDecodeError) as exc:
        raise RuntimeError(f"response was not JSON: {resp.text[:300]}") from exc


def _labels(row: dict, spec: dict | None) -> tuple[list[str], list[str]]:
    """
    Pull the publisher's own labels off an API item and map them to our
    taxonomy. Returns (raw_labels, mapped_labels).

    A publisher's own classification is ground truth -- Which? knows whether
    something is a policy submission or a press statement better than any
    model inferring it from the text. Where it exists, use it.

    spec:
      field     dotted path to the value ("content_type_data", "taxonomy")
      item_key  for lists of objects, which key holds the label ("name")
      map       publisher label -> our taxonomy key
    """
    if not spec:
        return [], []
    value = _dig(row, spec["field"])
    if value is None:
        return [], []

    raw = []
    for entry in (value if isinstance(value, list) else [value]):
        if isinstance(entry, dict):
            label = entry.get(spec.get("item_key", "name"))
        else:
            label = entry
        if label:
            raw.append(str(label).strip())

    lookup = {k.strip().lower(): v for k, v in (spec.get("map") or {}).items()}
    mapped, seen = [], set()
    for label in raw:
        target = lookup.get(label.lower())
        if target and target not in seen:
            seen.add(target)
            mapped.append(target)
    return raw, mapped


def _parse_api_date(value) -> str | None:
    """
    Parse a date from an API field.

    APIs return epoch seconds, epoch milliseconds, or a string, and the first
    two look identical to a naive parser -- 1755680400 parsed as text is not
    a date at all. Getting this wrong loses every date on the source silently.
    """
    if value in (None, "", 0):
        return None
    text = str(value).strip()

    if text.isdigit() and len(text) in (10, 13):
        seconds = int(text) / (1000 if len(text) == 13 else 1)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    try:
        parsed = dateparser.parse(text)
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed is None or not 1995 <= parsed.year <= datetime.now().year + 1:
        return None
    return parsed.date().isoformat()


def from_json(target: dict) -> list[dict]:
    """
    Read a JSON API directly.

    Many modern sites render their listing pages client-side, so the HTML
    contains no articles at all. The page calls an API; so do we. This is
    more stable than scraping would have been -- an API contract changes far
    less often than a CSS class name.

    Config:
      url            the endpoint
      http_method    GET (default) or POST
      params         query string, for GET
      body           JSON payload, for POST
      headers        any extra headers the site requires
      items_path     dotted path to the array of results, e.g. "data.results"
      url_field      field on each item holding the link or slug
      url_prefix     prepended when url_field is a slug, not a full URL
      title_field    optional
      date_field     optional
      page_param     dotted path (in params or body) to the page number
      pages          how many pages to walk (default 1)
    """
    items_path = target.get("items_path", "")
    url_field = target.get("url_field", "url")
    prefix = target.get("url_prefix", "")
    title_field = target.get("title_field")
    date_field = target.get("date_field")
    page_param = target.get("page_param")
    pages = int(target.get("pages", 1)) if page_param else 1
    field_map = target.get("field_map") or {}

    cutoff = None
    if target.get("days_back"):
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=int(target["days_back"]))).date().isoformat()

    out, seen = [], set()
    for page in range(pages):
        payload = json_request(target, page)
        rows = _dig(payload, items_path)
        if rows is None:
            raise RuntimeError(
                f"items_path {items_path!r} not found. Top-level keys: "
                f"{list(payload)[:12] if isinstance(payload, dict) else type(payload).__name__}")
        if not isinstance(rows, list):
            raise RuntimeError(f"items_path {items_path!r} is not a list")
        if not rows:
            break                                    # ran past the last page

        for row in rows:
            raw = _dig(row, url_field)
            if not raw:
                continue
            url = raw if str(raw).startswith("http") else prefix + str(raw)
            if url in seen:
                continue
            seen.add(url)

            published = None
            if date_field:
                published = _parse_api_date(_dig(row, date_field))
            raw_type, mapped_type = _labels(row, field_map.get("content_type"))
            raw_topics, mapped_topics = _labels(row, field_map.get("topics"))

            out.append({
                "url": url,
                "title": _dig(row, title_field) if title_field else None,
                "published_at": published,
                "date_source": "api" if published else None,
                "author": None,
                "pub_content_type": raw_type[0] if raw_type else None,
                "pub_topics": raw_topics,
                "content_type": mapped_type[0] if mapped_type else None,
                "topics": mapped_topics,
            })

        # Results are newest-first, so once we cross the cutoff every later
        # page is older still. Without this a backfill walks all 143 pages.
        if cutoff and published and published < cutoff:
            log.info("  reached %s, stopping pagination", cutoff)
            break
    return out


METHODS = {"rss": from_rss, "sitemap": from_sitemap, "html": from_html,
           "json": from_json}


# ---------------------------------------------------------------- driver

def discover(conn, run_id: int, refresh: bool = False) -> int:
    known = {row["canonical_url"] for row in conn.execute(
        "SELECT canonical_url FROM items")}
    inserted = 0

    for source in load_sources():
        for target in source["targets"]:
            method = METHODS.get(target.get("method"))
            label = f"{source['id']}/{target.get('name', '?')}"
            if method is None:
                log.error("%s: unknown method %r", label, target.get("method"))
                continue

            try:
                candidates = method(target)
                ok, error = 1, None
            except Exception as exc:                      # noqa: BLE001
                candidates, ok, error = [], 0, str(exc)[:400]
                log.error("%s failed: %s", label, error)

            # A target that fetches fine but yields nothing is a FAILURE, not
            # a quiet success. This is how a wrong selector or a site redesign
            # hides: the run completes, the dashboard just stops growing.
            if ok and not candidates:
                ok = 0
                error = ("target returned 0 candidates -- selector matched "
                         "nothing, or patterns filtered everything out")
                log.error("%s: %s", label, error)

            new_here = 0
            for cand in candidates:
                canon = canonical_url(cand["url"])
                if canon in known and not refresh:
                    continue
                if canon in known:
                    conn.execute(
                        "UPDATE items SET last_seen=?, status='discovered' "
                        "WHERE canonical_url=?", (now(), canon))
                    continue
                topics = cand.get("topics") or []
                conn.execute(
                    """INSERT INTO items
                       (source_id, org, country, region, target_name, url,
                        canonical_url, title, author, published_at, date_source,
                        first_seen, last_seen, language, pub_content_type,
                        pub_topics, content_type, topics, confidence,
                        status, run_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                               'discovered',?)""",
                    (source["id"], source["org"], source.get("country"),
                     source.get("region"), target.get("name"), cand["url"],
                     canon, cand.get("title"), cand.get("author"),
                     cand.get("published_at"), cand.get("date_source"),
                     now(), now(), source.get("language"),
                     cand.get("pub_content_type"),
                     jsonlib.dumps(cand["pub_topics"]) if cand.get("pub_topics") else None,
                     cand.get("content_type"),
                     jsonlib.dumps(topics) if topics else None,
                     1.0 if cand.get("content_type") else None,
                     run_id))
                known.add(canon)
                new_here += 1
                inserted += 1

            conn.execute(
                """INSERT INTO source_runs
                   (run_id, source_id, target, found, new_items, ok, error)
                   VALUES (?,?,?,?,?,?,?)""",
                (run_id, source["id"], target.get("name"),
                 len(candidates), new_here, ok, error))
            log.info("%-28s %3d found, %3d new%s",
                     label, len(candidates), new_here,
                     "" if ok else "  [FAILED]")

    conn.commit()
    return inserted


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="re-check URLs we already hold, to detect updates")
    args = ap.parse_args()

    conn = db()
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now(),))
    total = discover(conn, cur.lastrowid, refresh=args.refresh)
    conn.execute("UPDATE runs SET finished_at=?, discovered=? WHERE id=?",
                 (now(), total, cur.lastrowid))
    conn.commit()
    log.info("discovered %d new URLs", total)
