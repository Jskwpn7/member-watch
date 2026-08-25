"""Stage 2 -- fetch each discovered URL and pull out the actual content.

Two things here are harder than they look:

  1. Publication dates. Most sites do not put a machine-readable date in an
     obvious place, and half of those that do are wrong. We try four sources
     in descending order of trust and always record which one we used, so a
     date you cannot trust is visibly a date you cannot trust.

  2. PDFs. A lot of member research exists only as a PDF, and a pipeline
     that quietly skips them will miss the most valuable content on the site.
"""

from __future__ import annotations

import argparse
import io
import json
import re
from datetime import datetime

import trafilatura
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .common import content_hash, db, fetch, log, now

MIN_WORDS = 40          # below this it is a stub, a menu, or a paywall notice
SUMMARY_CHARS = 400


# ---------------------------------------------------------------- dates

def _sane(value) -> str | None:
    """Reject dates that are in the future or implausibly old."""
    try:
        parsed = dateparser.parse(str(value), fuzzy=False)
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed is None:
        return None
    year = parsed.year
    if year < 1995 or parsed.date() > datetime.now().date():
        return None
    return parsed.date().isoformat()


def find_date(soup: BeautifulSoup, html: str) -> tuple[str | None, str | None]:
    """Return (iso_date, where_we_found_it). Ordered most to least trusted."""

    # 1. JSON-LD structured data -- the most reliable when present.
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            blob = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in (blob if isinstance(blob, list) else [blob]):
            if not isinstance(node, dict):
                continue
            for key in ("datePublished", "dateCreated", "dateModified"):
                found = _sane(node.get(key))
                if found:
                    return found, "jsonld"

    # 2. Meta tags.
    meta_keys = [
        ("property", "article:published_time"),
        ("property", "og:published_time"),
        ("name", "date"),
        ("name", "pubdate"),
        ("name", "DC.date.issued"),
        ("itemprop", "datePublished"),
    ]
    for attr, value in meta_keys:
        tag = soup.find("meta", attrs={attr: value})
        if tag:
            found = _sane(tag.get("content"))
            if found:
                return found, "meta"

    # 3. A <time datetime="..."> element.
    for tag in soup.find_all("time"):
        found = _sane(tag.get("datetime") or tag.get_text(strip=True))
        if found:
            return found, "time_tag"

    # 4. A date printed in the first chunk of visible text.
    pattern = (r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
               r"[a-z]*\s+\d{4}|\d{4}-\d{2}-\d{2})\b")
    match = re.search(pattern, soup.get_text(" ", strip=True)[:1500], re.I)
    if match:
        found = _sane(match.group(1))
        if found:
            return found, "body_text"

    return None, None


# ---------------------------------------------------------------- content

def extract_pdf(raw: bytes) -> tuple[str, str | None]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", None
    try:
        reader = PdfReader(io.BytesIO(raw))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:60])
        title = (reader.metadata or {}).get("/Title") if reader.metadata else None
        return text, (str(title).strip() or None) if title else None
    except Exception as exc:                                # noqa: BLE001
        log.warning("pdf parse failed: %s", exc)
        return "", None


def extract_html(html: str) -> tuple[str, BeautifulSoup]:
    soup = BeautifulSoup(html, "html.parser")
    text = trafilatura.extract(
        html, include_comments=False, include_tables=True,
        favor_precision=True, no_fallback=False) or ""
    if len(text.split()) < MIN_WORDS:
        # Trafilatura is precise but occasionally too aggressive. Fall back
        # to stripped body text before declaring the page empty.
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
    return text, soup


def process_one(conn, row) -> str:
    resp = fetch(row["url"])
    if resp is None:
        conn.execute("UPDATE items SET status='failed', error=? WHERE id=?",
                     ("unreachable", row["id"]))
        return "failed"
    if resp.status_code != 200:
        conn.execute("UPDATE items SET status='failed', error=? WHERE id=?",
                     (f"http {resp.status_code}", row["id"]))
        return "failed"

    ctype = (resp.headers.get("content-type") or "").lower()
    title, author = row["title"], row["author"]
    published, date_source = row["published_at"], row["date_source"]

    if "pdf" in ctype or resp.url.lower().endswith(".pdf"):
        media = "pdf"
        text, pdf_title = extract_pdf(resp.content)
        title = title or pdf_title
    else:
        media = "html"
        text, soup = extract_html(resp.text)
        if not title:
            og = soup.find("meta", property="og:title")
            title = (og.get("content") if og else None) or (
                soup.title.get_text(strip=True) if soup.title else None)
        # A date found on the page always beats one guessed from a sitemap.
        page_date, where = find_date(soup, resp.text)
        if page_date and date_source in (None, "sitemap"):
            published, date_source = page_date, where

    words = len(text.split())
    if words < MIN_WORDS:
        conn.execute("UPDATE items SET status='failed', error=? WHERE id=?",
                     (f"too short ({words} words)", row["id"]))
        return "failed"

    if not published:
        # Never leave a date blank -- an unknown date sorts to the bottom and
        # disappears. Use the harvest date and label it honestly.
        published, date_source = row["first_seen"][:10], "first_seen"

    summary = re.sub(r"\s+", " ", text).strip()[:SUMMARY_CHARS]
    conn.execute(
        """UPDATE items SET title=?, author=?, published_at=?, date_source=?,
           body_text=?, summary=?, word_count=?, content_hash=?, media_type=?,
           last_seen=?, status='extracted', error=NULL WHERE id=?""",
        ((title or "").strip()[:400] or None, author, published, date_source,
         text, summary, words, content_hash(text), media, now(), row["id"]))
    return "extracted"


def extract_all(conn, limit: int | None = None) -> int:
    query = "SELECT * FROM items WHERE status='discovered' ORDER BY id"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query).fetchall()
    log.info("extracting %d items", len(rows))

    done = 0
    for index, row in enumerate(rows, 1):
        result = process_one(conn, row)
        done += result == "extracted"
        if index % 20 == 0:
            conn.commit()
            log.info("  %d/%d", index, len(rows))
    conn.commit()
    log.info("extracted %d, failed %d", done, len(rows) - done)
    return done


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    extract_all(db(), args.limit)
