"""Daily paper retrieval, scoring, historical review and independent delivery."""

import argparse
import json
import math
import os
import smtplib
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .dify_client import DifyError, PROJECT_ROOT, analyze_paper, load_config, validate_inputs
from .state import RunLock, load_state, mark_completed, paper_key, save_state
from .reporting import render_report
from .mailer import send_report, email_error_message
from .arxiv_client import fetch_arxiv
from .collection import collect_candidates
from .daily import assemble_report, record_delivery, receipt_key

RUN_ID = os.getenv('GITHUB_RUN_ID') or uuid.uuid4().hex


def parse_feed(content):
    ns = {'a': 'http://www.w3.org/2005/Atom'}
    root = ET.fromstring(content)
    if root.tag != '{http://www.w3.org/2005/Atom}feed':
        raise ValueError('arXiv did not return an Atom feed.')
    papers = []
    for entry in root.findall('a:entry', ns):
        url = entry.findtext('a:id', '', ns).strip()
        if '/api/errors' in url:
            raise ValueError('arXiv returned an API error entry.')
        papers.append({'paper_title': ' '.join(entry.findtext('a:title', '', ns).split()),
                       'paper_abstract': ' '.join(entry.findtext('a:summary', '', ns).split()),
                       'paper_url': url, 'published': entry.findtext('a:published', '', ns)})
    return papers


def save_report(report):
    if os.getenv('GITHUB_RUN_ID'):
        report.setdefault('workflow_url', f"https://github.com/{os.getenv('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    output_dir = PROJECT_ROOT / 'reports'
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = output_dir / f'papers-{stamp}'
    output.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    text, html = render_report(report)
    output.with_suffix('.md').write_text(text, encoding='utf-8')
    output.with_suffix('.html').write_text(html, encoding='utf-8')
    print(f"Report: {output.with_suffix('.md')}")
    print(f"Scored: {len(report['results'])}; duplicates skipped: {report['skipped']}; recommended: {len(report['selected'])}; history: {report['history_count']}; failed: {len(report['failures'])}")
    return output


def deliver_report(report, output, state=None, state_path=None):
    marker = PROJECT_ROOT / 'reports/delivery.json'
    marker.write_text(json.dumps({'run_id': RUN_ID, 'attempted': True, 'sent': False}), encoding='utf-8')
    try:
        send_report(report)
    except (OSError, ValueError, KeyError, smtplib.SMTPException) as error:
        print('Email failed: ' + email_error_message(error), file=sys.stderr)
        print(f"Retry delivery using python -m src.mailer --report {output.with_suffix('.json')}", file=sys.stderr)
        return False
    marker.write_text(json.dumps({'run_id': RUN_ID, 'attempted': True, 'sent': True, 'report': output.name}), encoding='utf-8')
    print('Email sent.')
    if state is not None and state_path is not None:
        record_delivery(state, report)
        save_state(state_path, state)
    return True


def main():
    load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paper-file', type=Path)
    parser.add_argument('--max-papers', type=int, default=None, help='Maximum NEW papers scored, after deduplication.')
    parser.add_argument('--min-score', type=float, default=None)
    parser.add_argument('--top-k', type=int, default=None)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--send-email', action='store_true')
    parser.add_argument('--daily', action='store_true', help='Skip if a paper digest was already sent today to this recipient/profile.')
    args = parser.parse_args()
    state = None
    threshold, top_k = 50, 5
    interests = os.getenv('RESEARCH_INTERESTS', '')
    try:
        limit = args.max_papers if args.max_papers is not None else int(os.getenv('MAX_PAPERS', '10'))
        threshold = args.min_score if args.min_score is not None else float(os.getenv('MIN_SCORE', '50'))
        top_k = args.top_k if args.top_k is not None else int(os.getenv('TOP_K', '5'))
        if not 1 <= limit <= 100 or not math.isfinite(threshold) or not 0 <= threshold <= 100 or top_k < 1:
            raise ValueError('Invalid MAX_PAPERS, MIN_SCORE or TOP_K.')
        state_path = PROJECT_ROOT / 'data/processed.json'
        with RunLock(PROJECT_ROOT / 'data/run.lock'):
            state = load_state(state_path)
            if args.daily and args.send_email and receipt_key() in state.get('reports_sent', {}):
                print('Daily paper digest already sent; skipping retrieval and delivery.')
                (PROJECT_ROOT / 'reports').mkdir(exist_ok=True)
                (PROJECT_ROOT / 'reports/delivery.json').write_text(json.dumps({'attempted': False, 'sent': True, 'already_sent': True}), encoding='utf-8')
                return 0
            results, failures, seen = [], [], set()
            skipped, fetched = 0, 0
            if args.paper_file:
                papers = json.loads(args.paper_file.read_text(encoding='utf-8-sig'))
                papers = papers if isinstance(papers, list) else [papers]
            else:
                papers, failures, skipped, fetched = collect_candidates(
                    lambda params: fetch_arxiv(params, parse_feed, PROJECT_ROOT / '.cache/arxiv'),
                    state, limit, interests, args.force)
            attempted = 0
            for raw in papers:
                if attempted >= limit:
                    break
                if not isinstance(raw, dict):
                    failures.append({'paper_url': 'input', 'stage': 'input', 'error': 'Paper must be an object.'})
                    continue
                paper = dict(raw)
                paper.setdefault('research_interests', interests)
                try:
                    validate_inputs(paper)
                    key = paper_key(paper)
                    if key in seen or (not args.force and key in state['completed']):
                        skipped += 1
                        continue
                    seen.add(key)
                    attempted += 1
                    analysis = analyze_paper(paper['paper_title'], paper['paper_abstract'], paper['paper_url'], paper['research_interests'])
                    item = {'paper': paper, 'analysis': analysis}
                    results.append(item)
                    mark_completed(state_path, state, key, item)
                except DifyError as error:
                    failures.append({'paper_url': paper.get('paper_url', 'input'), 'stage': 'analysis', 'error': str(error)})
            report = assemble_report(results, failures, len(results) + skipped + len([x for x in failures if x.get('stage') != 'fetch']),
                                     skipped, threshold, top_k, state, interests)
            report['fetched_metadata'] = fetched
            output = save_report(report)
            if args.send_email and not deliver_report(report, output, state, state_path):
                return 1
            if failures:
                # Historical reviews recover an unavailable source without hiding
                # the degradation in the report and Actions warning.
                if report['selected'] and all(x.get('stage') == 'fetch' for x in failures):
                    print('::warning::arXiv retrieval degraded; digest produced with explicitly labelled new/history sources.')
                    return 0
                return 1
            return 0
    except (OSError, ValueError, KeyError, RuntimeError, ET.ParseError) as error:
        print(f'FAILED: pipeline initialization/state error ({type(error).__name__}).', file=sys.stderr)
        # Preserve a meaningful daily notification even when setup/state fails.
        if not math.isfinite(threshold) or not 0 <= threshold <= 100:
            threshold = 50
        top_k = max(1, top_k)
        report = assemble_report([], [{'paper_url': 'pipeline', 'stage': 'pipeline',
                                       'error': f'Pipeline setup/state failed ({type(error).__name__}); inspect Actions logs/configuration.'}],
                                 0, 0, threshold, top_k, state or {'completed': {}}, interests)
        try:
            output = save_report(report)
            marker = PROJECT_ROOT / 'reports/delivery.json'
            delivery = json.loads(marker.read_text()) if marker.exists() else {}
            attempted = delivery.get('attempted') and delivery.get('run_id') == RUN_ID
            if args.send_email and not attempted:
                deliver_report(report, output)
        except (OSError, ValueError, RuntimeError):
            print('Could not persist/send pipeline failure report.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
