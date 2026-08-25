#!/usr/bin/env python3
"""
audit_sources.py — Step zero of the CI member monitoring POC.

For each candidate member site, works out HOW hard it will be to monitor:

  Tier 1 (easy)   — a real RSS/Atom feed exists
  Tier 2 (ok)     — no feed, but a sitemap with <lastmod> dates
  Tier 3 (hard)   — neither; needs per-site HTML scraping

Run this against 8-10 candidates, then pick your 5 to span the tiers.

Usage:
    pip install requests beautifulsoup4
    python audit_sources.py                 # audit the default candidate list
    python audit_sources.py --json out.json # also write machine-readable output

Be polite: this makes ~10 requests per site with a delay between them.
"""

import argparse
import json
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- config

UA = (
    "CI-Member-Monitor-Audit/0.1 "
    "(research prototype; contact: YOUR_EMAIL_HERE)"
)
TIMEOUT = 20
DELAY = 1.5  # seconds between requests to the same host

# Edit this list. Homepages only — the script works out the rest.
CANDIDATES = [
    ("Which?",                    "https://www.which.co.uk"),
    ("CHOICE",                    "https://www.choice.com.au"),
    ("Consumer NZ",               "https://www.consumer.org.nz"),
    ("Hong Kong Consumer Council","https://www.consumer.org.hk"),
    ("Consumer Reports",          "https://www.consumerreports.org"),
    ("Consumer Federation of America", "https://consumerfed.org"),
    ("CAG India",                 "https://www.cag.org.in"),
    ("FOMCA",                     "https://www.fomca.org.my"),
    ("Consumer Council of Fiji",  "https://consumercouncil.com.fj"),
    ("Consumentenbond",           "https://www.consumentenbond.nl"),
]

FEED_PATHS = [
    "/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml",
    "/atom.xml", "/index.xml", "/news/feed", "/news/rss",
    "/blog/feed", "/feeds/posts/default",
]

SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"]

FEED_CONTENT_TYPES = ("xml", "rss", "atom")

session = requests.Session()
session.headers.update({"User-Agent": UA})


# ---------------------------------------------------------------- helpers

def get(url, method="get"):
    """Fetch a URL, returning the response or None. Never raises."""
    try:
        fn = session.head if method == "head" else session.get
        r = fn(url, timeout=TIMEOUT, allow_redirects=True)
        return r
    except requests.RequestException:
        return None
    finally:
        time.sleep(DELAY)


def looks_like_feed(resp):
    """Is this response actually a feed, not an HTML page or soft 404?"""
    if resp is None or resp.status_code != 200:
        return False
    ctype = resp.headers.get("content-type", "").lower()
    body = resp.text[:2000].lower()
    if not any(t in ctype for t in FEED_CONTENT_TYPES) and "<?xml" not in body:
        return False
    return any(tag in body for tag in ("<rss", "<feed", "<rdf:rdf"))


def declared_feeds(homepage_url):
    """Feeds the site advertises in its HTML <head>. The reliable way."""
    resp = get(homepage_url)
    if resp is None or resp.status_code != 200:
        return [], resp
    soup = BeautifulSoup(resp.text, "html.parser")
    found = []
    for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
        ctype = (link.get("type") or "").lower()
        if "rss" in ctype or "atom" in ctype:
            href = link.get("href")
            if href:
                found.append(urljoin(homepage_url, href))
    return found, resp


def probe_feed_paths(base):
    """Brute-force the usual suspects. Stops at the first real feed."""
    for path in FEED_PATHS:
        resp = get(urljoin(base, path))
        if looks_like_feed(resp):
            return resp.url
    return None


def sitemaps_from_robots(base):
    resp = get(urljoin(base, "/robots.txt"))
    if resp is None or resp.status_code != 200:
        return []
    return re.findall(r"(?im)^\s*sitemap:\s*(\S+)", resp.text)


def inspect_sitemap(url, depth=0):
    """
    Returns (url, url_count, has_lastmod) for the first real URL set found.
    Follows one level of sitemap index.
    """
    resp = get(url)
    if resp is None or resp.status_code != 200:
        return None
    body = resp.text
    if "<sitemapindex" in body[:3000].lower() and depth == 0:
        children = re.findall(r"<loc>\s*(.*?)\s*</loc>", body, re.I)[:1]
        return inspect_sitemap(children[0], depth + 1) if children else None
    count = body.lower().count("<url>")
    if count == 0:
        return None
    return (resp.url, count, "<lastmod" in body.lower())


# ---------------------------------------------------------------- audit

def audit(org, base):
    result = {
        "org": org, "url": base, "reachable": False, "tier": None,
        "feed": None, "feed_source": None,
        "sitemap": None, "sitemap_urls": None, "sitemap_lastmod": False,
        "notes": [],
    }

    feeds, home = declared_feeds(base)
    if home is None:
        result["notes"].append("unreachable — check URL, TLS, or blocking")
        result["tier"] = "?"
        return result

    result["reachable"] = True
    if home.status_code != 200:
        result["notes"].append(f"homepage returned {home.status_code}")

    # Tier 1: a feed?
    for candidate in feeds:
        if looks_like_feed(get(candidate)):
            result["feed"], result["feed_source"] = candidate, "declared"
            break
    if not result["feed"]:
        found = probe_feed_paths(base)
        if found:
            result["feed"], result["feed_source"] = found, "probed"

    # Tier 2: a sitemap with dates?
    sitemap_candidates = sitemaps_from_robots(base) or [
        urljoin(base, p) for p in SITEMAP_PATHS
    ]
    for candidate in sitemap_candidates[:3]:
        info = inspect_sitemap(candidate)
        if info:
            result["sitemap"], result["sitemap_urls"], result["sitemap_lastmod"] = info
            break

    if result["feed"]:
        result["tier"] = 1
    elif result["sitemap"] and result["sitemap_lastmod"]:
        result["tier"] = 2
    elif result["sitemap"]:
        result["tier"] = 3
        result["notes"].append("sitemap has no <lastmod> — can't detect updates from it")
    else:
        result["tier"] = 3
        result["notes"].append("no feed, no sitemap — bespoke HTML scraping needed")

    return result


TIER_LABEL = {
    1: "1 EASY  feed",
    2: "2 OK    sitemap+dates",
    3: "3 HARD  scrape HTML",
    "?": "?  UNREACHABLE",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="write results as JSON")
    args = ap.parse_args()

    if "YOUR_EMAIL_HERE" in UA:
        print("!! Set a contact email in the UA string before running.\n",
              file=sys.stderr)

    results = []
    for org, base in CANDIDATES:
        print(f"auditing {org} ...", file=sys.stderr)
        results.append(audit(org, base))

    results.sort(key=lambda r: (str(r["tier"]), r["org"]))

    print(f"\n{'ORG':<34} {'TIER':<22} DETAIL")
    print("-" * 100)
    for r in results:
        if r["feed"]:
            detail = f"{r['feed']}  ({r['feed_source']})"
        elif r["sitemap"]:
            detail = f"{r['sitemap']}  ({r['sitemap_urls']} urls)"
        else:
            detail = "—"
        print(f"{r['org']:<34} {TIER_LABEL[r['tier']]:<22} {detail}")
        for note in r["notes"]:
            print(f"{'':<34} {'':<22} note: {note}")

    counts = {}
    for r in results:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    print("\nTier counts:", ", ".join(f"tier {k}: {v}" for k, v in sorted(
        counts.items(), key=lambda kv: str(kv[0]))))
    print("Pick your 5 to span tiers — include at least one tier 3.")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
