"""Cache metadata and respect arXiv throttling, including across local runs."""

import hashlib
import json
import os
import sys
import time
from datetime import timezone
from email.utils import parsedate_to_datetime

import requests

from .state import RunLock


def retry_delay(value, now, fallback):
    try:
        return max(3, int(value))
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(3, date.timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return fallback


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def fetch_arxiv(params, parse_feed, cache_dir):
    retries = int(os.getenv('ARXIV_MAX_RETRIES', '2'))
    ttl = int(os.getenv('ARXIV_CACHE_TTL_SECONDS', '900'))
    if not 0 <= retries <= 3 or ttl < 0:
        raise ValueError('ARXIV_MAX_RETRIES must be 0–3 and ARXIV_CACHE_TTL_SECONDS nonnegative.')
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    cache_path = cache_dir / (key + '.json')
    state_path = cache_dir / 'requests.json'
    with RunLock(cache_dir / 'arxiv.lock'):
        try:
            cached = json.loads(cache_path.read_text(encoding='utf-8'))
            age = time.time() - cached['fetched_at']
            if 0 <= age < ttl and isinstance(cached['papers'], list):
                print('arXiv: using cached metadata (no new API request).', file=sys.stderr)
                return cached['papers']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        for attempt in range(retries + 1):
            wait = max(state.get('blocked_until', 0), state.get('last_request', 0) + 3) - time.time()
            if wait > 60:
                raise ValueError(f'arXiv rate limit cooldown: wait at least {int(wait) + 1} seconds before fetching again. Test email with python -m src.mailer --report <existing-report.json>.')
            if wait > 0:
                print(f'arXiv: waiting {wait:.0f}s before request.', file=sys.stderr)
                time.sleep(wait)
            state['last_request'] = time.time()
            write_json(state_path, state)
            try:
                response = requests.get('https://export.arxiv.org/api/query', params=params,
                                        headers={'User-Agent': 'AgentArticleRecomand/0.2 (https://github.com/thinkercom/paper_tracker)'},
                                        timeout=(10, 60))
            except (requests.Timeout, requests.ConnectionError):
                if attempt == retries:
                    raise ValueError('arXiv connection failed after bounded retries; try later or use an existing report to test email.') from None
                state['blocked_until'] = time.time() + 10 * 2 ** attempt
                write_json(state_path, state)
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                now = time.time()
                delay = retry_delay(response.headers.get('Retry-After'), now, 30 * 2 ** attempt)
                state['blocked_until'] = now + delay
                write_json(state_path, state)
                if attempt == retries or delay > 60:
                    raise ValueError(f'arXiv HTTP {response.status_code}; wait at least {int(delay) + 1} seconds. No paper was scored or email sent. Use src.mailer with an existing report to test email.')
                print(f'arXiv HTTP {response.status_code}: retry {attempt + 1}/{retries} after {delay:.0f}s.', file=sys.stderr)
                continue
            response.raise_for_status()
            papers = parse_feed(response.content)
            state['blocked_until'] = 0
            write_json(state_path, state)
            write_json(cache_path, {'fetched_at': time.time(), 'papers': papers})
            return papers
