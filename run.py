"""The weekly run. This is what GitHub Actions calls.

    python -m pipeline.run                # normal weekly harvest
    python -m pipeline.run --offline      # no API key needed, for testing
    python -m pipeline.run --refresh      # also re-check pages we already hold

Each stage is independently runnable (python -m pipeline.extract, etc.) so you
can rerun one without redoing the others. Classification in particular is worth
rerunning on its own after you edit taxonomy.yml.
"""

from __future__ import annotations

import argparse
import traceback

from .classify import classify_all
from .common import db, log, now
from .dedupe import dedupe
from .discover import discover
from .export import export
from .extract import extract_all


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="skip the classification API, use keyword fallback")
    ap.add_argument("--refresh", action="store_true",
                    help="re-check known URLs to detect updated pages")
    ap.add_argument("--limit", type=int,
                    help="cap items extracted this run, for testing")
    args = ap.parse_args()

    conn = db()
    cursor = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now(),))
    run_id = cursor.lastrowid
    conn.commit()
    log.info("=== run %d starting ===", run_id)

    stats = {"discovered": 0, "extracted": 0, "duplicates": 0, "classified": 0}
    ok, notes = 1, None

    try:
        stats["discovered"] = discover(conn, run_id, refresh=args.refresh)
        stats["extracted"] = extract_all(conn, limit=args.limit)
        stats["duplicates"] = dedupe(conn)
        stats["classified"] = classify_all(conn, offline=args.offline)
        export(conn)
    except Exception:                                       # noqa: BLE001
        ok, notes = 0, traceback.format_exc()[-1500:]
        log.error("run failed:\n%s", notes)

    conn.execute(
        """UPDATE runs SET finished_at=?, discovered=?, extracted=?,
           duplicates=?, classified=?, ok=?, notes=? WHERE id=?""",
        (now(), stats["discovered"], stats["extracted"], stats["duplicates"],
         stats["classified"], ok, notes, run_id))
    conn.commit()

    failed = conn.execute(
        "SELECT source_id, target, error FROM source_runs "
        "WHERE run_id=? AND ok=0", (run_id,)).fetchall()
    if failed:
        # Loud failure is the point. A scraper that silently returns nothing
        # after a site redesign is the main way this kind of tool rots.
        log.warning("--- %d source targets failed this run ---", len(failed))
        for row in failed:
            log.warning("  %s/%s: %s", row["source_id"], row["target"],
                        row["error"])

    log.info("=== run %d done: %s ===", run_id, stats)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
