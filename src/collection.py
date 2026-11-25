"""Search relevant metadata and refill the new-paper budget after deduplication."""

import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import requests

from .state import paper_key
from .dify_client import validate_inputs, DifyError

DEFAULT_KEYWORDS = 'EEG,electroencephalography,brain-computer interface,brain computer interface,motor imagery'
DEFAULT_CATEGORIES = 'cs.LG,q-bio.NC,eess.SP,cs.HC'


def build_query():
    days = int(os.getenv('ARXIV_LOOKBACK_DAYS', '30'))
    categories = [s.strip() for s in os.getenv('ARXIV_CATEGORIES', DEFAULT_CATEGORIES).split(',') if s.strip()]
    if days < 1 or not categories:
        raise ValueError('Use positive ARXIV_LOOKBACK_DAYS and nonempty ARXIV_CATEGORIES.')
    query = '(' + ' OR '.join('cat:' + s for s in categories) + ')'
    keywords = [' '.join(s.replace('"', '').split()) for s in os.getenv('ARXIV_KEYWORDS', DEFAULT_KEYWORDS).split(',') if s.strip()]
    if keywords:
        terms = [f'{field}:"{term}"' for term in keywords for field in ('ti', 'abs')]
        query += ' AND (' + ' OR '.join(terms) + ')'
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return query + f' AND submittedDate:[{start:%Y%m%d}000000 TO {end:%Y%m%d}235959]'


def collect_candidates(fetch_page, state, limit, interests, force=False):
    page_size = int(os.getenv('ARXIV_PAGE_SIZE', '50'))
    max_pages = int(os.getenv('ARXIV_MAX_PAGES', '3'))
    if not 1 <= page_size <= 100 or not 1 <= max_pages <= 10:
        raise ValueError('ARXIV_PAGE_SIZE must be 1–100 and ARXIV_MAX_PAGES 1–10.')
    query = build_query()
    papers, failures, seen = [], [], set()
    skipped, fetched = 0, 0
    for page_index in range(max_pages):
        try:
            page = fetch_page({'search_query': query, 'start': page_index * page_size,
                               'max_results': page_size, 'sortBy': 'submittedDate', 'sortOrder': 'descending'})
        except (OSError, ValueError, ET.ParseError, requests.RequestException) as error:
            failures.append({'paper_url': 'arXiv 查询', 'stage': 'fetch', 'error': str(error)})
            break
        fetched += len(page)
        for paper in page:
            paper = {**paper, 'research_interests': interests}
            try:
                validate_inputs(paper)
            except DifyError as error:
                failures.append({'paper_url': paper.get('paper_url', 'arXiv'), 'stage': 'input', 'error': str(error)})
                continue
            key = paper_key(paper)
            if key in seen or (not force and key in state['completed']):
                skipped += 1
                continue
            seen.add(key)
            papers.append(paper)
            if len(papers) >= limit:
                break
        if len(papers) >= limit or len(page) < page_size:
            break
    return papers, failures, skipped, fetched
