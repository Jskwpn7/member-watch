"""Stage 3 -- collapse duplicates and record revisions.

URL-level deduplication already happened in discover.py. This stage catches
what canonical URLs cannot:

  exact duplicates   the same document published at two URLs, typically a
                     report appearing under both /news/ and /publications/
  near duplicates    a press release syndicated with small edits, or the same
                     story covered by two different members
  revisions          a page we already hold whose content has changed. That
                     is one item with a new revision, not a new item -- which
                     is what "one version of each" in the brief requires.

Near-duplicate detection uses shingled MinHash-style Jaccard similarity on
word trigrams. No extra dependencies, and at POC volume the cost is trivial.
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict

from .common import db, log, now

NEAR_DUP_THRESHOLD = 0.82   # tuned by hand; raise if you see false merges
SHINGLE_SIZE = 3
COMPARE_WORDS = 300         # only the opening of each document


def shingles(text: str) -> set[str]:
    words = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower()).split()[:COMPARE_WORDS]
    if len(words) < SHINGLE_SIZE:
        return set()
    return {" ".join(words[i:i + SHINGLE_SIZE])
            for i in range(len(words) - SHINGLE_SIZE + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def mark_duplicate(conn, dup_id: int, original_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE items SET status='duplicate', duplicate_of=?, error=? WHERE id=?",
        (original_id, reason, dup_id))


def dedupe(conn) -> int:
    fresh = conn.execute(
        "SELECT * FROM items WHERE status='extracted' ORDER BY id").fetchall()
    if not fresh:
        log.info("nothing to dedupe")
        return 0

    # --- exact duplicates, against everything we have ever held -----------
    by_hash: dict[str, int] = {}
    for row in conn.execute(
            "SELECT id, content_hash FROM items "
            "WHERE content_hash IS NOT NULL AND status != 'duplicate' "
            "ORDER BY id"):
        by_hash.setdefault(row["content_hash"], row["id"])

    duplicates = 0
    survivors = []
    for row in fresh:
        original = by_hash.get(row["content_hash"])
        if original is not None and original != row["id"]:
            mark_duplicate(conn, row["id"], original, "exact content match")
            duplicates += 1
            continue

        # --- revision check: same document, changed text ------------------
        prior = conn.execute(
            "SELECT content_hash FROM revisions WHERE item_id=? "
            "ORDER BY seen_at DESC LIMIT 1", (row["id"],)).fetchone()
        if prior and prior["content_hash"] != row["content_hash"]:
            conn.execute("UPDATE items SET updated_at=? WHERE id=?",
                         (now(), row["id"]))
            log.info("updated: %s", (row["title"] or row["url"])[:70])
        conn.execute(
            "INSERT OR IGNORE INTO revisions (item_id, content_hash, seen_at, "
            "word_count) VALUES (?,?,?,?)",
            (row["id"], row["content_hash"], now(), row["word_count"]))

        survivors.append(row)

    # --- near duplicates --------------------------------------------------
    # Compare new items against each other and against the last 500 held
    # items. Bucketed by rough length so we are not comparing a tweet-length
    # note to a 40-page report.
    recent = conn.execute(
        "SELECT id, title, body_text, word_count FROM items "
        "WHERE status='classified' ORDER BY id DESC LIMIT 500").fetchall()

    buckets: dict[int, list[tuple[int, set[str]]]] = defaultdict(list)

    def bucket_key(word_count: int) -> int:
        return int((word_count or 0) ** 0.5) // 2

    for row in recent:
        buckets[bucket_key(row["word_count"])].append(
            (row["id"], shingles(row["body_text"])))

    for row in survivors:
        key = bucket_key(row["word_count"])
        mine = shingles(row["body_text"])
        matched = None
        for neighbour in (key - 1, key, key + 1):
            for other_id, theirs in buckets.get(neighbour, []):
                if other_id == row["id"]:
                    continue
                if jaccard(mine, theirs) >= NEAR_DUP_THRESHOLD:
                    matched = other_id
                    break
            if matched:
                break

        if matched:
            mark_duplicate(conn, row["id"], matched, "near-duplicate text")
            duplicates += 1
        else:
            buckets[key].append((row["id"], mine))

    conn.commit()
    log.info("deduped: %d duplicates from %d extracted", duplicates, len(fresh))
    return duplicates


if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()
    dedupe(db())
