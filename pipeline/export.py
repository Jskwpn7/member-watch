"""Stage 5 -- write the JSON the dashboard reads.

The dashboard is a static file with no backend, so everything it needs has to
be in these files. We ship a trimmed search blob rather than full body text:
full articles would balloon the payload, and the dashboard links out to the
source anyway.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .common import ROOT, db, load_taxonomy, log, now

OUT = ROOT / "site" / "data"
SEARCH_CHARS = 1200     # how much body text goes into the client-side index


def export(conn) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    taxonomy = load_taxonomy()

    rows = conn.execute(
        """SELECT id, source_id, org, country, region, url, title, summary,
                  published_at, date_source, first_seen, updated_at, word_count,
                  media_type, language, content_type, topics, confidence,
                  pub_content_type,
                  needs_review, body_text
           FROM items WHERE status='classified'
           ORDER BY published_at DESC, id DESC""").fetchall()

    items = []
    for row in rows:
        item = {k: row[k] for k in row.keys() if k != "body_text"}
        item["topics"] = json.loads(row["topics"] or "[]")
        item["search"] = re.sub(
            r"\s+", " ", f"{row['title'] or ''} {row['body_text'] or ''}"
        ).strip()[:SEARCH_CHARS].lower()
        items.append(item)

    runs = [dict(r) for r in conn.execute(
        """SELECT r.*, (SELECT COUNT(*) FROM source_runs s
                        WHERE s.run_id = r.id AND s.ok = 0) AS failed_sources
           FROM runs r ORDER BY r.id DESC LIMIT 26""")]

    sources = [dict(r) for r in conn.execute(
        """SELECT source_id, org, country, region, COUNT(*) AS items,
                  MAX(published_at) AS latest
           FROM items WHERE status='classified'
           GROUP BY source_id ORDER BY org""")]

    payload = {
        "generated_at": now(),
        "taxonomy": {"content_type": taxonomy["content_type"],
                     "topic": taxonomy["topic"],
                     "lenses": taxonomy["lenses"]},
        "sources": sources,
        "runs": runs,
        "items": items,
    }

    path = OUT / "corpus.json"
    path.write_text(json.dumps(payload, separators=(",", ":")))
    log.info("exported %d items -> %s (%.1f MB)",
             len(items), path, path.stat().st_size / 1e6)
    return len(items)


if __name__ == "__main__":
    export(db())
