"""Fetch papers, deduplicate, score via Dify, and build optional email reports."""

import argparse
import json
import math
import os
import smtplib
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .dify_client import DifyError, PROJECT_ROOT, analyze_paper, load_config, validate_inputs
from .state import RunLock, load_state, mark_completed, paper_key
from .reporting import render_report, select_results
from .mailer import send_report, email_error_message
from .arxiv_client import fetch_arxiv


def parse_feed(content):
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(content)
    if root.tag != "{http://www.w3.org/2005/Atom}feed":
        raise ValueError("arXiv did not return an Atom feed.")
    papers = []
    for entry in root.findall("a:entry", ns):
        url = entry.findtext("a:id", "", ns).strip()
        if "/api/errors" in url:
            raise ValueError("arXiv returned an API error entry.")
        papers.append({
            "paper_title": " ".join(entry.findtext("a:title", "", ns).split()),
            "paper_abstract": " ".join(entry.findtext("a:summary", "", ns).split()),
            "paper_url": url,
        })
    return papers


def fetch_papers(limit):
    days = int(os.getenv("ARXIV_LOOKBACK_DAYS", "5"))
    categories = [x.strip() for x in os.getenv("ARXIV_CATEGORIES", "cs.LG,q-bio.NC").split(",") if x.strip()]
    if days < 1 or not categories:
        raise ValueError("Use positive ARXIV_LOOKBACK_DAYS and nonempty ARXIV_CATEGORIES.")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    query = "(" + " OR ".join(f"cat:{x}" for x in categories) + ")"
    query += f" AND submittedDate:[{start:%Y%m%d}000000 TO {end:%Y%m%d}235959]"
    return fetch_arxiv({
        "search_query": query, "max_results": limit, "sortBy": "submittedDate", "sortOrder": "descending",
    }, parse_feed, PROJECT_ROOT / ".cache/arxiv")


def main():
    load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-file", type=Path, help="One paper object or an array of paper objects.")
    parser.add_argument("--max-papers", type=int, default=None)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="Re-score previously processed IDs; still deduplicate within this batch.")
    parser.add_argument("--send-email", action="store_true", help="Explicitly send the generated report via configured SMTP.")
    args = parser.parse_args()
    try:
        if not os.getenv("DIFY_API_KEY", "").strip():
            raise DifyError("Set DIFY_API_KEY in the project .env before running.")
        limit = args.max_papers if args.max_papers is not None else int(os.getenv("MAX_PAPERS", "3"))
        if not 1 <= limit <= 100:
            raise ValueError("max-papers must be from 1 to 100.")
        threshold = args.min_score if args.min_score is not None else float(os.getenv("MIN_SCORE", "70"))
        top_k = args.top_k if args.top_k is not None else int(os.getenv("TOP_K", "5"))
        if not math.isfinite(threshold) or not 0 <= threshold <= 100 or top_k < 1:
            raise ValueError("MIN_SCORE must be 0–100 and TOP_K must be positive.")
        if args.paper_file:
            papers = json.loads(args.paper_file.read_text(encoding="utf-8-sig"))
            papers = papers if isinstance(papers, list) else [papers]
            papers = papers[:limit]
        else:
            papers = fetch_papers(limit)
        # Validate every paper before spending tokens on the batch.
        prepared = []
        for paper in papers:
            if not isinstance(paper, dict):
                raise ValueError("Each paper must be a JSON object.")
            inputs = dict(paper)
            inputs.setdefault("research_interests", os.getenv("RESEARCH_INTERESTS", ""))
            validate_inputs(inputs)
            prepared.append(inputs)
        with RunLock(PROJECT_ROOT / "data/run.lock"):
            state_path = PROJECT_ROOT / "data/processed.json"
            state = load_state(state_path)
            results, failures, seen = [], [], set()
            skipped = 0
            for paper in prepared:
                key = paper_key(paper)
                if key in seen or (not args.force and key in state["completed"]):
                    skipped += 1
                    continue
                seen.add(key)
                try:
                    result = analyze_paper(paper["paper_title"], paper["paper_abstract"],
                                           paper["paper_url"], paper["research_interests"])
                    item = {"paper": paper, "analysis": result}
                    results.append(item)
                    mark_completed(state_path, state, key, item)
                except DifyError as exc:
                    failures.append({"paper_url": paper["paper_url"], "error": str(exc)})
            report = {"results": results, "failures": failures, "count": len(prepared), "skipped": skipped,
                      "min_score": threshold, "top_k": top_k,
                      "selected": select_results(results, threshold, top_k)}
            output_dir = PROJECT_ROOT / "reports"
            output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            output = output_dir / f"papers-{stamp}"
            output.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            text, html = render_report(report)
            output.with_suffix(".md").write_text(text, encoding="utf-8")
            output.with_suffix(".html").write_text(html, encoding="utf-8")
        print(f"Report: {output.with_suffix('.md')}")
        print(f"Scored: {len(results)}; duplicates skipped: {skipped}; recommended: {len(report['selected'])}; failed: {len(failures)}")
        if args.send_email:
            try:
                send_report(report)
                print("Email sent.")
            except (OSError, ValueError, KeyError, smtplib.SMTPException) as exc:
                print("Email failed: " + email_error_message(exc), file=sys.stderr)
                print(f"Email failed; retry delivery using python -m src.mailer --report {output.with_suffix('.json')}", file=sys.stderr)
                return 1
        return 1 if failures else 0
    except (DifyError, OSError, ValueError, ET.ParseError, requests.RequestException) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
