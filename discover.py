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

import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .common import canonical_url, db, fetch, load_sources, log, now


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


METHODS = {"rss": from_rss, "sitemap": from_sitemap, "html": from_html}


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
                conn.execute(
                    """INSERT INTO items
                       (source_id, org, country, region, target_name, url,
                        canonical_url, title, author, published_at, date_source,
                        first_seen, last_seen, language, status, run_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'discovered',?)""",
                    (source["id"], source["org"], source.get("country"),
                     source.get("region"), target.get("name"), cand["url"],
                     canon, cand.get("title"), cand.get("author"),
                     cand.get("published_at"), cand.get("date_source"),
                     now(), now(), source.get("language"), run_id))
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
