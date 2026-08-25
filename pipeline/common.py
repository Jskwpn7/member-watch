"""Shared plumbing: config, database, polite fetching, URL canonicalisation.

Every other pipeline module imports from here. If something is behaving
oddly across the whole pipeline, it is probably in this file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import time
import urllib.robotparser as robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "corpus.db"
SOURCES_DIR = ROOT / "sources"
TAXONOMY_PATH = ROOT / "taxonomy.yml"

CONTACT = os.environ.get("CONTACT_EMAIL", "set-CONTACT_EMAIL-in-env")
USER_AGENT = f"CI-Member-Monitor/0.1 (research prototype; contact: {CONTACT})"

TIMEOUT = 30
PER_HOST_DELAY = float(os.environ.get("PER_HOST_DELAY", 2.0))          # seconds between requests to the same host
MAX_BYTES = 8 * 1024 * 1024   # skip anything larger; usually a video or dataset

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname).1s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("harvest")


def now() -> str:
    """UTC timestamp, ISO 8601, second precision. Used everywhere."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- config

def load_sources() -> list[dict]:
    """One dict per member org, read from sources/*.yml."""
    sources = []
    for path in sorted(SOURCES_DIR.glob("*.yml")):
        with open(path) as fh:
            src = yaml.safe_load(fh)
        missing = {"id", "org", "targets"} - set(src)
        if missing:
            raise ValueError(f"{path.name} is missing: {', '.join(sorted(missing))}")
        src["_path"] = str(path)
        sources.append(src)
    if not sources:
        raise SystemExit(f"No source files found in {SOURCES_DIR}")
    return sources


def load_taxonomy() -> dict:
    with open(TAXONOMY_PATH) as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------- database

def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript((ROOT / "pipeline" / "schema.sql").read_text())

    # Add columns introduced after a database was first created. CREATE TABLE
    # IF NOT EXISTS will not do this, and a silent missing column shows up
    # much later as a confusing insert failure.
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    for column, decl in (("pub_content_type", "TEXT"), ("pub_topics", "TEXT")):
        if column not in existing:
            conn.execute(f"ALTER TABLE items ADD COLUMN {column} {decl}")
            log.info("migrated: added items.%s", column)
    conn.commit()
    return conn


# ---------------------------------------------------------------- urls

# Query params that identify a campaign, not a document.
JUNK_PARAMS = re.compile(
    r"^(utm_|fbclid|gclid|mc_cid|mc_eid|ref|source|_ga|igshid|s_cid)", re.I
)


def canonical_url(url: str) -> str:
    """
    Normalise a URL so the same document always produces the same key.

    Handles the ~90% case of duplicate detection: tracking params, http vs
    https, www, trailing slashes, fragments, and index.html.
    """
    parts = urlparse(url.strip())
    scheme = "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if netloc.endswith(":443"):
        netloc = netloc[:-4]

    path = re.sub(r"/index\.(html?|php|aspx)$", "/", parts.path)
    if len(path) > 1:
        path = path.rstrip("/")
    path = path or "/"

    kept = [(k, v) for k, v in parse_qsl(parts.query) if not JUNK_PARAMS.match(k)]
    query = urlencode(sorted(kept))

    return urlunparse((scheme, netloc, path, "", query, ""))


def content_hash(text: str) -> str:
    """
    Hash of the normalised body text. Catches the same document published at
    two URLs, and tells us when a page's content has genuinely changed rather
    than just its ads or timestamps.
    """
    normalised = re.sub(r"\s+", " ", (text or "")).strip().lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- http

_last_hit: dict[str, float] = {}
_robots: dict[str, robotparser.RobotFileParser | None] = {}
_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


def _throttle(host: str) -> None:
    elapsed = time.monotonic() - _last_hit.get(host, 0.0)
    if elapsed < PER_HOST_DELAY:
        time.sleep(PER_HOST_DELAY - elapsed)
    _last_hit[host] = time.monotonic()


def robots_allows(url: str) -> bool:
    """
    Check robots.txt before fetching. Cached per host. If robots.txt is
    unreachable we allow the fetch -- a missing file is not a disallow.
    """
    parts = urlparse(url)
    host = f"{parts.scheme}://{parts.netloc}"
    if host not in _robots:
        parser = robotparser.RobotFileParser()
        parser.set_url(urljoin(host, "/robots.txt"))
        try:
            _throttle(parts.netloc)
            resp = _session.get(urljoin(host, "/robots.txt"), timeout=TIMEOUT)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            else:
                parser = None
        except requests.RequestException:
            parser = None
        _robots[host] = parser
    parser = _robots[host]
    return True if parser is None else parser.can_fetch(USER_AGENT, url)


def fetch(url: str, *, respect_robots: bool = True) -> requests.Response | None:
    """
    Polite GET. Returns None rather than raising, so one bad URL never kills
    a run. Rate-limited per host and robots-aware.
    """
    if respect_robots and not robots_allows(url):
        log.warning("robots.txt disallows %s", url)
        return None
    try:
        _throttle(urlparse(url).netloc)
        resp = _session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
        length = int(resp.headers.get("content-length") or 0)
        if length > MAX_BYTES:
            log.warning("skipping %s (%.1f MB)", url, length / 1e6)
            resp.close()
            return None
        resp.content  # force download now that we know the size is sane
        return resp
    except requests.RequestException as exc:
        log.warning("fetch failed %s: %s", url, exc)
        return None
