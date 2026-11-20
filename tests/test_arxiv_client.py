import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src.arxiv_client import fetch_arxiv, retry_delay


class ArxivClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.clock = 1000.0
        self.start_patch(patch.dict(os.environ, {}, clear=True))
        self.start_patch(patch('src.arxiv_client.time.time', side_effect=lambda: self.clock))
        self.sleep = self.start_patch(patch('src.arxiv_client.time.sleep', side_effect=self.advance))
        self.get = self.start_patch(patch('src.arxiv_client.requests.get'))
        self.start_patch(patch('builtins.print'))
        self.params = {'search_query': 'cat:cs.LG', 'max_results': 1}
        self.papers = [{'paper_title': 'Example', 'paper_abstract': 'Abstract', 'paper_url': 'https://arxiv.org/abs/1234.56789'}]
        self.parse = Mock(return_value=self.papers)

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def advance(self, seconds):
        self.clock += seconds

    def response(self, status, retry_after=None):
        response = Mock(status_code=status, headers={} if retry_after is None else {'Retry-After': retry_after}, content=b'<feed/>')
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(str(status))
        return response

    def test_429_wait_then_success_and_cache_avoids_request(self):
        self.get.side_effect = [self.response(429, '3'), self.response(200)]
        self.assertEqual(fetch_arxiv(self.params, self.parse, self.directory), self.papers)
        self.sleep.assert_called_once_with(3)
        self.assertEqual(self.get.call_count, 2)
        self.assertEqual(fetch_arxiv(self.params, self.parse, self.directory), self.papers)
        self.assertEqual(self.get.call_count, 2)
        self.parse.assert_called_once()

    def test_long_retry_after_is_preserved_across_queries_and_runs(self):
        self.get.return_value = self.response(429, '600')
        with self.assertRaisesRegex(ValueError, 'HTTP 429'):
            fetch_arxiv(self.params, self.parse, self.directory)
        self.sleep.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'cooldown'):
            fetch_arxiv({**self.params, 'max_results': 5}, self.parse, self.directory)
        self.assertEqual(self.get.call_count, 1)
        state = json.loads((self.directory / 'requests.json').read_text(encoding='utf-8'))
        self.assertEqual(state['blocked_until'], 1600)

    def test_429_attempts_are_bounded(self):
        self.get.return_value = self.response(429)
        with self.assertRaisesRegex(ValueError, 'HTTP 429'):
            fetch_arxiv(self.params, self.parse, self.directory)
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [30, 60])
        self.parse.assert_not_called()

    def test_400_is_not_retried(self):
        self.get.return_value = self.response(400)
        with self.assertRaises(requests.HTTPError):
            fetch_arxiv(self.params, self.parse, self.directory)
        self.assertEqual(self.get.call_count, 1)
        self.sleep.assert_not_called()

    def test_expired_cache_is_refetched(self):
        self.get.return_value = self.response(200)
        fetch_arxiv(self.params, self.parse, self.directory)
        self.advance(901)
        fetch_arxiv(self.params, self.parse, self.directory)
        self.assertEqual(self.get.call_count, 2)

    def test_retry_after_http_date(self):
        self.assertEqual(retry_delay('Thu, 01 Jan 1970 00:20:00 GMT', 1000, 30), 200)
        self.assertEqual(retry_delay('invalid', 1000, 30), 30)


if __name__ == '__main__':
    unittest.main()
