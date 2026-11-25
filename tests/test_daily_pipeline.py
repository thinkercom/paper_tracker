import copy
import json
import os
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.collection import build_query, collect_candidates
from src.daily import assemble_report, historical_review, receipt_key, record_delivery
from src.main import main
from src.reporting import render_report
from src.state import paper_key
from src.notify_failure import main as notify_failure

INTERESTS = 'EEG and brain computer interface'
PAPER = {'paper_title': 'EEG research', 'paper_abstract': 'A real EEG abstract',
         'paper_url': 'https://arxiv.org/abs/2611.10001v1', 'research_interests': INTERESTS}
ANALYSIS = {'score': 85, 'reason': 'EEG relevance', 'summary': 'EEG method', 'recommended': True}


class DailyPipelineTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'RESEARCH_INTERESTS': INTERESTS, 'EMAIL_TO': 'reader@example.com',
                                     'ARXIV_PAGE_SIZE': '2', 'ARXIV_MAX_PAGES': '3'}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def history(self):
        return {'version': 1, 'completed': {paper_key(PAPER): {'paper': dict(PAPER), 'analysis': dict(ANALYSIS)}}}

    def run_project(self, root, argv):
        with patch('src.main.PROJECT_ROOT', root), patch('src.main.load_config'), patch('sys.argv', ['main', *argv]), patch('builtins.print'):
            return main()

    def test_query_uses_research_keywords_and_categories(self):
        query = build_query()
        self.assertIn('ti:"EEG"', query)
        self.assertIn('abs:"motor imagery"', query)
        self.assertIn('cat:eess.SP', query)
        self.assertIn('submittedDate:', query)

    def test_duplicate_first_page_refills_from_second_page(self):
        state = self.history()
        other_old = {**PAPER, 'paper_url': 'https://arxiv.org/abs/2611.10002v1'}
        state['completed'][paper_key(other_old)] = {'paper': other_old, 'analysis': ANALYSIS}
        new1 = {**PAPER, 'paper_url': 'https://arxiv.org/abs/2611.10003v1'}
        new2 = {**PAPER, 'paper_url': 'https://arxiv.org/abs/2611.10004v1'}
        fetch = Mock(side_effect=[[PAPER, other_old], [new1, new2]])
        papers, failures, skipped, fetched = collect_candidates(fetch, state, 2, INTERESTS)
        self.assertEqual([p['paper_url'] for p in papers], [new1['paper_url'], new2['paper_url']])
        self.assertEqual((failures, skipped, fetched), ([], 2, 4))
        self.assertEqual([x.args[0]['start'] for x in fetch.call_args_list], [0, 2])

    def test_later_fetch_failure_preserves_partial_candidates(self):
        new = {**PAPER, 'paper_url': 'https://arxiv.org/abs/2611.10003v1'}
        fetch = Mock(side_effect=[[PAPER, new], ValueError('arXiv HTTP 429')])
        papers, failures, skipped, _ = collect_candidates(fetch, self.history(), 2, INTERESTS)
        self.assertEqual(len(papers), 1)
        self.assertEqual(failures[0]['stage'], 'fetch')
        self.assertEqual(skipped, 1)

    def test_historical_review_is_labelled_and_rotates(self):
        state = self.history()
        second = {**PAPER, 'paper_url': 'https://arxiv.org/abs/2611.10002v1'}
        state['completed'][paper_key(second)] = {'paper': second, 'analysis': {**ANALYSIS, 'score': 58}}
        report = assemble_report([], [{'paper_url': 'arXiv', 'stage': 'fetch', 'error': '429'}], 0, 0, 50, 1, state, INTERESTS)
        self.assertEqual(report['selected'][0]['analysis']['score'], 85)
        self.assertEqual(report['history_count'], 1)
        self.assertEqual(report['new_count'], 0)
        text, html = render_report(report)
        self.assertIn('非今日新论文', text)
        self.assertIn('历史回顾', html)
        self.assertIn('429', text)
        record_delivery(state, report)
        self.assertIn(receipt_key(), state['reports_sent'])
        self.assertEqual(historical_review(state, INTERESTS, 50, 1)[0]['analysis']['score'], 58)

    def test_history_respects_profile_threshold_and_validation(self):
        state = self.history()
        self.assertEqual(historical_review(state, 'unrelated interest', 50, 5), [])
        self.assertEqual(historical_review(state, INTERESTS, 90, 5), [])
        with patch.dict(os.environ, {'ANALYSIS_PROFILE': 'changed'}):
            self.assertEqual(historical_review(state, INTERESTS, 50, 5), [])
        next(iter(state['completed'].values()))['analysis']['score'] = True
        self.assertEqual(historical_review(state, INTERESTS, 50, 5), [])

    @patch('src.main.send_report')
    @patch('src.main.fetch_arxiv', side_effect=ValueError('arXiv HTTP 429'))
    def test_fetch_outage_still_delivers_history_and_receipt(self, fetch, send):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data').mkdir()
            (root / 'data/processed.json').write_text(json.dumps(self.history()), encoding='utf-8')
            self.assertEqual(self.run_project(root, ['--send-email', '--daily']), 0)
            report = send.call_args.args[0]
            self.assertEqual(report['status'], 'degraded')
            self.assertEqual(report['history_count'], 1)
            saved = json.loads((root / 'data/processed.json').read_text(encoding='utf-8'))
            self.assertIn(receipt_key(), saved['reports_sent'])
            # Scheduled catch-up must avoid BOTH API requests and duplicate mail.
            self.assertEqual(self.run_project(root, ['--send-email', '--daily']), 0)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(fetch.call_count, 1)

    @patch('src.main.send_report')
    @patch('src.main.fetch_arxiv', side_effect=ValueError('arXiv HTTP 429'))
    def test_no_history_outage_sends_fault_and_allows_catchup(self, fetch, send):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(self.run_project(root, ['--send-email', '--daily']), 1)
            report = send.call_args.args[0]
            self.assertEqual(report['selected'], [])
            state = json.loads((root / 'data/processed.json').read_text(encoding='utf-8'))
            self.assertNotIn(receipt_key(), state.get('reports_sent', {}))
            self.assertTrue(report['failures'])

    @patch('src.main.send_report', side_effect=smtplib.SMTPConnectError(421, b'unavailable'))
    @patch('src.main.fetch_arxiv', return_value=[])
    def test_smtp_failure_never_marks_daily_success(self, fetch, send):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data').mkdir()
            (root / 'data/processed.json').write_text(json.dumps(self.history()), encoding='utf-8')
            self.assertEqual(self.run_project(root, ['--send-email', '--daily']), 1)
            state = json.loads((root / 'data/processed.json').read_text(encoding='utf-8'))
            self.assertNotIn(receipt_key(), state.get('reports_sent', {}))
            self.assertFalse(json.loads((root / 'reports/delivery.json').read_text())['sent'])

    @patch('src.main.send_report')
    def test_bad_configuration_still_sends_fault_notification(self, send):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'MIN_SCORE': 'invalid'}):
            root = Path(directory)
            self.assertEqual(self.run_project(root, ['--send-email']), 1)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[0]['status'], 'degraded')

    @patch('src.main.send_report')
    def test_independent_workflow_notification_and_no_duplicate_send(self, send):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'SEND_EMAIL': 'true'}):
            root = Path(directory)
            (root / 'data').mkdir()
            (root / 'data/processed.json').write_text(json.dumps(self.history()), encoding='utf-8')
            with patch('src.notify_failure.PROJECT_ROOT', root), patch('src.main.PROJECT_ROOT', root), patch('src.notify_failure.load_config'), patch('builtins.print'):
                self.assertEqual(notify_failure(), 0)
                self.assertEqual(send.call_args.args[0]['history_count'], 1)
                self.assertEqual(notify_failure(), 0)
            self.assertEqual(send.call_count, 1)


if __name__ == '__main__':
    unittest.main()
