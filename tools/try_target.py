#!/usr/bin/env python3
"""Try one target against a live site and print what it would capture.

Writes nothing. Touches no database. Use it to get a selector right in
seconds instead of running the whole pipeline and inspecting SQLite after.

    python tools/try_target.py sources/which-uk.yml news
    python tools/try_target.py sources/which-uk.yml policy --fetch 3

    # try a selector without editing the YAML first
    python tools/try_target.py --url https://example.org/news \\
                              --method html --selector "article h2 a"

--fetch N also downloads the first N results and shows the title, date and
word count that extraction would produce, which is how you catch a selector
that finds the right number of links but points at the wrong pages.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml                                                    # noqa: E402

from pipeline.common import canonical_url                      # noqa: E402
from pipeline.discover import METHODS, json_request             # noqa: E402


DATE_HINTS = ("date", "published", "time", "created", "updated")
LINK_HINTS = ("url", "path", "slug", "link", "href", "permalink")


def outline(node, path: str = "", depth: int = 0, out=None) -> list:
    """Sketch the shape of a JSON response: paths, types, sizes."""
    out = [] if out is None else out
    pad = "  " * depth
    if isinstance(node, dict):
        for key, value in list(node.items())[:14]:
            here = f"{path}.{key}" if path else key
            if isinstance(value, (dict, list)):
                kind = "object" if isinstance(value, dict) else f"array[{len(value)}]"
                out.append(f"{pad}{key}: {kind}")
                if depth < 3:
                    outline(value, here, depth + 1, out)
            else:
                shown = str(value)[:52]
                out.append(f"{pad}{key} = {shown}")
    elif isinstance(node, list) and node:
        out.append(f"{pad}[0] of {len(node)}:")
        outline(node[0], f"{path}.0", depth + 1, out)
    return out


def find_arrays(node, path: str = "", found=None) -> list:
    """Every path holding a list of objects -- candidates for items_path."""
    found = [] if found is None else found
    if isinstance(node, list):
        if node and isinstance(node[0], dict):
            found.append((path, len(node), node[0]))
    elif isinstance(node, dict):
        for key, value in node.items():
            find_arrays(value, f"{path}.{key}" if path else key, found)
    return found


def inspect(target: dict) -> int:
    """Fetch once and report the shape, so items_path is read not guessed."""
    payload = json_request(target, 0)
    print("response shape")
    for line in outline(payload)[:60]:
        print("  " + line)

    candidates = sorted(find_arrays(payload), key=lambda c: -c[1])
    if not candidates:
        print("\nNo arrays of objects found. Check the request body — the "
              "filters may be excluding everything.")
        return 1

    print("\ncandidate items_path values")
    for path, count, sample in candidates[:5]:
        keys = list(sample)
        print(f"\n  items_path: {path or '(root)'}   [{count} items]")
        print(f"    keys: {', '.join(keys[:16])}")
        links = [k for k in keys if any(h in k.lower() for h in LINK_HINTS)]
        dates = [k for k in keys if any(h in k.lower() for h in DATE_HINTS)]
        if links:
            print(f"    url_field candidates:  {', '.join(links)}")
            print(f"      e.g. {links[0]} = {str(sample[links[0]])[:70]}")
        if dates:
            print(f"    date_field candidates: {', '.join(dates)}")
    return 0

# Titles that mean the selector has found site furniture, not content.
FURNITURE = {"read", "read more", "more", "about us", "about", "home",
             "contact", "contact us", "skip to main content", "menu",
             "search", "log in", "login", "join", "subscribe", "next",
             "previous", "view all", "sign up", "cookies", "privacy"}


def load_target(path: str, name: str) -> dict:
    source = yaml.safe_load(Path(path).read_text())
    for target in source["targets"]:
        if target.get("name") == name:
            return target
    names = ", ".join(t.get("name", "?") for t in source["targets"])
    raise SystemExit(f"No target named {name!r} in {path}. Found: {names}")


def warn(results: list[dict]) -> list[str]:
    """Cheap heuristics for the ways a target goes wrong without erroring."""
    notes = []
    if not results:
        return ["0 results -- selector matched nothing, or patterns "
                "filtered everything out"]
    if len(results) < 3:
        notes.append(f"only {len(results)} results -- suspiciously few for a "
                     "list page")
    if len(results) > 250:
        notes.append(f"{len(results)} results -- patterns are probably too "
                     "loose; you may be pulling in the whole site")

    titles = [(r.get("title") or "").strip().lower() for r in results]
    junk = sum(1 for t in titles if t in FURNITURE or len(t) < 9)
    if junk > len(results) / 3:
        notes.append(f"{junk}/{len(results)} titles look like navigation "
                     "('Read', 'About us') -- the selector is matching site "
                     "furniture, not articles")

    if len(set(titles)) < len(titles) / 2:
        notes.append("many duplicate titles -- selector may be matching "
                     "several links per article")

    dated = sum(1 for r in results if r.get("published_at"))
    if not dated:
        notes.append("no dates from discovery -- extraction will have to find "
                     "them on each page, and may fall back to harvest date")

    depths = {len([p for p in canonical_url(r["url"]).split("/")[3:] if p])
              for r in results}
    if len(depths) > 3:
        notes.append("results sit at many different URL depths -- often a "
                     "sign of mixing articles with section pages")
    return notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", help="path to a sources/*.yml file")
    ap.add_argument("target", nargs="?", help="target name inside that file")
    ap.add_argument("--url")
    ap.add_argument("--method", choices=sorted(METHODS))
    ap.add_argument("--selector", help="CSS selector, for --method html")
    ap.add_argument("--include", nargs="*", default=None)
    ap.add_argument("--exclude", nargs="*", default=None)
    ap.add_argument("--fetch", type=int, default=0,
                    help="also extract the first N pages")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--inspect", action="store_true",
                    help="for method json: print the response shape and "
                         "suggest items_path, url_field and date_field")
    args = ap.parse_args()

    if args.source and args.target:
        target = load_target(args.source, args.target)
    elif args.url and args.method:
        target = {"name": "adhoc", "method": args.method, "url": args.url,
                  "item_selector": args.selector}
    else:
        ap.error("give either SOURCE TARGET, or --url with --method")

    if args.include is not None:
        target["include_patterns"] = args.include
    if args.exclude is not None:
        target["exclude_patterns"] = args.exclude

    if args.inspect:
        if target["method"] != "json":
            ap.error("--inspect only applies to method: json")
        try:
            return inspect(target)
        except Exception as exc:                               # noqa: BLE001
            print(f"FAILED: {exc}")
            return 1

    print(f"method   {target['method']}")
    print(f"url      {target['url']}")
    if target.get("item_selector"):
        print(f"selector {target['item_selector']}")
    print()

    try:
        results = METHODS[target["method"]](target)
    except Exception as exc:                                   # noqa: BLE001
        print(f"FAILED: {exc}")
        return 1

    print(f"{len(results)} candidates\n")
    for row in results[:args.limit]:
        date = row.get("published_at") or "—"
        title = (row.get("title") or "(no title from listing)")[:64]
        print(f"  {date:<12}{title}")
        print(f"  {'':<12}{canonical_url(row['url'])}")
    if len(results) > args.limit:
        print(f"  … {len(results) - args.limit} more")

    notes = warn(results)
    if notes:
        print("\nwarnings")
        for note in notes:
            print(f"  ! {note}")
    else:
        print("\nlooks healthy")

    if args.fetch and results:
        # Import here so the common case does not pay for trafilatura.
        from pipeline.extract import extract_html, extract_pdf, find_date
        from pipeline.common import fetch as http_get
        from bs4 import BeautifulSoup

        print(f"\nextracting first {min(args.fetch, len(results))}:")
        for row in results[:args.fetch]:
            resp = http_get(row["url"])
            if resp is None or resp.status_code != 200:
                print(f"  FAIL  {row['url']}")
                continue
            if "pdf" in (resp.headers.get("content-type") or "").lower():
                text, title = extract_pdf(resp.content)
                date, where = None, None
            else:
                text, soup = extract_html(resp.text)
                title = row.get("title") or (
                    soup.title.get_text(strip=True) if soup.title else None)
                date, where = find_date(soup, resp.text)
            words = len(text.split())
            flag = "  <- too short, will be dropped" if words < 40 else ""
            print(f"  {words:>6}w  {date or 'no date':<12}"
                  f"{(title or '(none)')[:52]}{flag}")
            if words >= 40:
                print(f"          {' ':<12}{text.strip()[:100]}…")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
