"""Historical review and delivery receipts; never present history as new papers."""

import hashlib
import os
from datetime import datetime, timezone, timedelta

from .dify_client import extract_result
from .state import paper_key


def local_day():
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def scope():
    text = '\n'.join(os.getenv(k, '').strip() for k in
                     ('EMAIL_TO', 'RESEARCH_INTERESTS', 'ANALYSIS_PROFILE'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def receipt_key():
    return scope() + ':' + local_day()


def historical_review(state, interests, threshold, top_k):
    candidates = []
    deliveries = state.get('deliveries', {})
    for key, record in state.get('completed', {}).items():
        try:
            paper = record['paper']
            if paper['research_interests'].strip() != interests.strip() or paper_key(paper) != key:
                continue
            analysis = extract_result({'data': {'status': 'succeeded', 'outputs': {'result': record['analysis']}}})
            if analysis['score'] < threshold:
                continue
            candidates.append({'paper': paper, 'analysis': analysis, 'source': 'history',
                               'scored_at': record.get('scored_at', ''),
                               '_last_sent': deliveries.get(scope() + ':' + key, '')})
        except (KeyError, TypeError, ValueError, RuntimeError):
            continue
    candidates.sort(key=lambda x: (x['_last_sent'], -x['analysis']['score']))
    return [{k: v for k, v in x.items() if not k.startswith('_')} for x in candidates[:top_k]]


def record_delivery(state, report):
    now = datetime.now(timezone.utc).isoformat()
    for item in report['selected']:
        state.setdefault('deliveries', {})[scope() + ':' + paper_key(item['paper'])] = now
    # A fault-only message does not prevent a later catch-up from sending papers.
    if report['selected']:
        state.setdefault('reports_sent', {})[receipt_key()] = now


def assemble_report(results, failures, count, skipped, threshold, top_k, state, interests):
    from .reporting import select_results
    selected = select_results(results, threshold, top_k)
    for item in selected:
        item['source'] = 'new'
    if not selected:
        selected = historical_review(state, interests, threshold, top_k)
    return {'results': results, 'failures': failures, 'count': count, 'skipped': skipped,
            'min_score': threshold, 'top_k': top_k, 'selected': selected,
            'report_date': local_day(), 'status': 'degraded' if failures else 'ok',
            'new_count': sum(x['source'] == 'new' for x in selected),
            'history_count': sum(x['source'] == 'history' for x in selected)}
