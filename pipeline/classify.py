"""Stage 4 -- tag each item with a content type and topics.

Two layers, in order of cost:

  1. The target it came from. An item scraped from a page you configured as
     /publications/ is a report. This is free and more reliable than any
     model, so it sets a strong prior.

  2. An LLM call against taxonomy.yml. Returns labels plus a confidence.
     Anything below REVIEW_THRESHOLD gets flagged for human review rather
     than silently mislabelled.

Runs offline with --offline (or with no API key set), using keyword matching.
That is not good enough to ship, but it means the rest of the pipeline is
testable without spending anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re

import requests

from .common import db, load_taxonomy, log

MODEL = "claude-sonnet-4-6"
BATCH = 10                  # items per API call
REVIEW_THRESHOLD = 0.7
MAX_CHARS = 2500            # opening of the document is enough to classify

KEYWORDS = {
    "digital": ["privacy", "data protection", "algorithm", "platform", " ai ",
                "artificial intelligence", "online", "digital", "cyber"],
    "finance": ["bank", "credit", "loan", "insurance", "payment", "fintech",
                "mortgage", "interest rate", "overdraft"],
    "food":    ["food", "nutrition", "labelling", "obesity", "hfss", "diet"],
    "energy":  ["energy", "electricity", "gas bill", "tariff", "utility"],
    "safety":  ["recall", "product safety", "hazard", "unsafe", "standard"],
    "sustainability": ["sustainab", "repair", "greenwash", "waste",
                       "circular", "emissions"],
    "scams":   ["scam", "fraud", "misleading", "rip-off", "phishing"],
    "law":     ["consumer law", "redress", "compensation", "regulation",
                "competition", "rights"],
}


def build_prompt(taxonomy: dict, items: list) -> str:
    types = "\n".join(f"  {k}: {v}" for k, v in taxonomy["content_type"].items())
    topics = "\n".join(f"  {k}: {v}" for k, v in taxonomy["topic"].items())
    evidence = "\n".join(f"  {k}: {v}" for k, v in taxonomy["evidence"].items())

    blocks = []
    for row in items:
        body = re.sub(r"\s+", " ", row["body_text"] or "")[:MAX_CHARS]
        # Items whose publisher already told us the form still need the
        # evidence axis, so say which fields we are actually asking about.
        known = (f"<publisher_label>{row['pub_content_type']}</publisher_label>\n"
                 if row["pub_content_type"] else "")
        blocks.append(
            f"<item id=\"{row['id']}\">\n"
            f"<org>{row['org']}</org>\n"
            f"<section>{row['target_name'] or 'unknown'}</section>\n"
            f"{known}"
            f"<title>{row['title'] or ''}</title>\n"
            f"<text>{body}</text>\n</item>")

    return f"""You are tagging content harvested from consumer-advocacy organisations.

CONTENT TYPE -- the FORM of the item. Choose exactly one:
{types}

Apply the first rule that fits, in this order:
  1. Addressed to a decision-maker rather than the public -> policy
  2. The item itself is the study, report or dataset      -> research
  3. Rates or compares specific named products            -> review
  4. Has a news hook -- something happened, this reports
     or reacts to it                                      -> news
  5. Teaches the reader how to do or decide something      -> advice

EVIDENCE -- who produced the evidence the item rests on. Choose exactly one:
{evidence}

Content type and evidence are independent. A press release announcing the
organisation's own survey findings is content_type "news" (rule 4) with
evidence "original". An explainer built on official statistics is content_type
"advice" (rule 5) with evidence "cited". Judge evidence by who did the work,
not by how much data appears in the text.

TOPICS -- choose one to three, most relevant first:
{topics}

The <section> is the part of the site the item came from and is strong evidence
for the content type. Trust it unless the text clearly contradicts it. Where a
<publisher_label> is given, the organisation has already told us the form --
keep your content_type consistent with it and concentrate on the evidence axis.

Return ONLY a JSON array, no prose and no markdown fences. One object per item:
{{"id": <int>, "content_type": "<key>", "topics": ["<key>"],
  "evidence": "<key>", "confidence": <0-1>}}

Set confidence below 0.7 when the item is ambiguous, is mostly navigation text,
or does not fit the taxonomy. Do not invent keys outside the lists above.

{chr(10).join(blocks)}"""


def classify_api(taxonomy: dict, items: list, api_key: str) -> dict[int, dict]:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 1500,
              "messages": [{"role": "user",
                            "content": build_prompt(taxonomy, items)}]},
        timeout=120)
    resp.raise_for_status()

    text = "".join(block.get("text", "")
                   for block in resp.json().get("content", [])
                   if block.get("type") == "text")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()

    valid_types = set(taxonomy["content_type"])
    valid_topics = set(taxonomy["topic"])
    valid_evidence = set(taxonomy["evidence"])
    out = {}
    for entry in json.loads(text):
        ctype = entry.get("content_type")
        topics = [t for t in (entry.get("topics") or []) if t in valid_topics]
        evid = entry.get("evidence")
        if ctype not in valid_types:
            continue
        out[int(entry["id"])] = {
            "content_type": ctype,
            # No fallback topic. Defaulting to a real key turned 'law' into
            # the corpus junk drawer; an empty list plus a low confidence
            # says "we don't know", which is the truth.
            "topics": topics,
            "evidence": evid if evid in valid_evidence else None,
            "confidence": float(entry.get("confidence", 0.5)),
        }
    return out


def classify_offline(taxonomy: dict, items: list) -> dict[int, dict]:
    out = {}
    for row in items:
        haystack = f"{row['title'] or ''} {(row['body_text'] or '')[:2000]}".lower()
        scored = sorted(
            ((sum(haystack.count(word) for word in words), topic)
             for topic, words in KEYWORDS.items()),
            reverse=True)
        topics = [topic for score, topic in scored[:2] if score > 0]
        out[row["id"]] = {
            "content_type": row["target_name"] if row["target_name"] in
                            taxonomy["content_type"] else "news",
            "topics": topics,
            # Keyword matching cannot tell who ran a survey. Leave it unset
            # rather than guess -- a null evidence field is picked up by the
            # next real classify run.
            "evidence": None,
            # Deliberately low: everything from the offline path should be
            # reviewed, because keyword matching is a placeholder.
            "confidence": 0.4,
        }
    return out


def classify_all(conn, offline: bool = False, reclassify: bool = False) -> int:
    taxonomy = load_taxonomy()
    # Items whose publisher told us the form need no model call for
    # content_type or topics -- that is ground truth from the source's own
    # metadata, and cheaper and more accurate than re-inferring it.
    #
    # They DO still need the evidence axis. No publisher supplies it, and it
    # is the field that distinguishes "our members' own research" from "our
    # members commenting on someone else's". So a field_map no longer means
    # "skip the classifier"; it means "skip two of its three questions".
    pre_tagged = conn.execute(
        "UPDATE items SET status='classified', needs_review=0 "
        "WHERE status='extracted' AND content_type IS NOT NULL").rowcount
    conn.commit()
    if pre_tagged:
        log.info("%d items pre-tagged by their publisher "
                 "(model still asked for the evidence axis)", pre_tagged)

    if reclassify:
        # Re-tag everything the model has ever labelled. This is what makes
        # "edit taxonomy.yml, rerun classify" true -- without it the normal
        # query skips every item that already has a content_type, so an
        # edited taxonomy or a run that fell back to the offline keyword
        # matcher can never be corrected.
        #
        # Publisher-labelled items (pub_content_type set) are left alone:
        # those came from the source's own metadata via field_map, are
        # ground truth, and re-deriving them from the text would be strictly
        # worse. Duplicates are left alone too.
        rows = conn.execute(
            "SELECT * FROM items WHERE status IN ('extracted','classified') "
            "ORDER BY id").fetchall()
        pub = sum(1 for r in rows if r["pub_content_type"])
        log.info("reclassifying %d items (%d keep their publisher's "
                 "content_type and topics; evidence re-asked for all)",
                 len(rows), pub)
    else:
        # Anything missing a form, or missing the evidence axis. The second
        # clause is what pulls publisher-labelled items in.
        rows = conn.execute(
            "SELECT * FROM items WHERE status IN ('extracted','classified') "
            "AND ((status='extracted' AND content_type IS NULL) "
            "     OR evidence IS NULL) ORDER BY id").fetchall()
    if not rows:
        log.info("nothing left to classify")
        return pre_tagged

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if reclassify and not offline and not api_key:
        # Refuse rather than quietly overwrite good labels with keyword
        # guesses. A reclassify is destructive; falling back by accident
        # would replace real classifications with 0.4 placeholders.
        raise SystemExit(
            "--reclassify needs ANTHROPIC_API_KEY. Set it, or pass --offline "
            "as well if you really do want keyword labels.")
    if offline or not api_key:
        log.warning("classifying offline (keyword fallback) -- "
                    "set ANTHROPIC_API_KEY for real classification")

    done = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        try:
            if offline or not api_key:
                results = classify_offline(taxonomy, batch)
            else:
                results = classify_api(taxonomy, batch, api_key)
        except Exception as exc:                            # noqa: BLE001
            log.error("classify batch failed, falling back: %s", exc)
            results = classify_offline(taxonomy, batch)

        for row in batch:
            result = results.get(row["id"])
            if result is None:
                # Never drop an item because the model ignored it.
                result = {"content_type": "news", "topics": [],
                          "evidence": None, "confidence": 0.0}

            # An item with no topic is as much a review case as a low score.
            review = int(result["confidence"] < REVIEW_THRESHOLD
                         or not result["topics"]
                         or result["evidence"] is None)

            if row["pub_content_type"]:
                # The publisher owns the form outright. It owns topics only
                # where its own labels actually mapped onto ours -- a
                # publisher's vocabulary is built for its site, not for this
                # corpus, and its broadest labels are programme names that
                # map onto nothing. Where the map came back empty the model
                # fills the gap, rather than leaving the item untagged.
                pub_topics = json.loads(row["topics"] or "[]")
                topics = pub_topics or result["topics"]
                conn.execute(
                    "UPDATE items SET topics=?, evidence=?, "
                    "status='classified', needs_review=? WHERE id=?",
                    (json.dumps(topics), result["evidence"],
                     int(result["evidence"] is None or not topics),
                     row["id"]))
            else:
                conn.execute(
                    """UPDATE items SET content_type=?, topics=?, evidence=?,
                       confidence=?, needs_review=?, status='classified'
                       WHERE id=?""",
                    (result["content_type"], json.dumps(result["topics"]),
                     result["evidence"], result["confidence"], review,
                     row["id"]))
            done += 1
        conn.commit()
        log.info("  classified %d/%d", min(start + BATCH, len(rows)), len(rows))

    flagged = conn.execute(
        "SELECT COUNT(*) c FROM items WHERE needs_review=1 AND reviewed=0"
    ).fetchone()["c"]
    log.info("classified %d by model (+%d from publisher), %d awaiting review",
             done, pre_tagged, flagged)
    return done + pre_tagged


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="use keyword matching instead of the API")
    ap.add_argument("--reclassify", action="store_true",
                    help="re-tag items the model has already labelled; use "
                         "after editing taxonomy.yml or after a run that "
                         "fell back to offline matching")
    args = ap.parse_args()
    classify_all(db(), offline=args.offline, reclassify=args.reclassify)
