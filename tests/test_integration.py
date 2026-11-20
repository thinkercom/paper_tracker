import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src.dify_client import DifyError, analyze_paper, extract_result, run_workflow
from src.main import main, parse_feed
from src.state import paper_key, load_state, RunLock
from src.reporting import select_results, render_report
from src.mailer import build_message, send_report, email_error_message, MailConfigurationError
import smtplib

ROOT = Path(__file__).resolve().parents[1]
INPUTS = json.loads((ROOT / "examples/test_paper.json").read_text(encoding="utf-8"))
RESULT = {"score": 91, "reason": "EEG and BCI relevance", "summary": "Compact EEG CNN", "recommended": True}
ENVELOPE = {"workflow_run_id": "test-run", "data": {"status": "succeeded", "outputs": {"result": RESULT}}}


class ContractTests(unittest.TestCase):
    def test_object_and_string_outputs(self):
        self.assertEqual(extract_result(ENVELOPE), RESULT)
        envelope = copy.deepcopy(ENVELOPE)
        envelope["data"]["outputs"]["result"] = json.dumps(RESULT)
        self.assertEqual(extract_result(envelope), RESULT)

    def test_invalid_outputs_are_rejected(self):
        for updates in ({"score": True}, {"score": "91"}, {"score": -1}, {"score": 101},
                        {"score": float("nan")}, {"recommended": "true"}, {"reason": ""}, {"summary": None}):
            with self.subTest(updates=updates):
                envelope = copy.deepcopy(ENVELOPE)
                envelope["data"]["outputs"]["result"].update(updates)
                with self.assertRaises(DifyError):
                    extract_result(envelope)
        for envelope in ({}, {"data": {"status": "failed"}},
                         {"data": {"status": "succeeded", "outputs": {}}}):
            with self.assertRaises(DifyError):
                extract_result(envelope)

    def test_deepseek_reasoning_and_fences(self):
        for text in (
            '<think><!--dify-deepseek-reasoning-->Internal reasoning {"score": 0}</think>' + json.dumps(RESULT),
            '```json\n' + json.dumps(RESULT) + '\n```',
            '<think>Reasoning</think>\n```json\n' + json.dumps(RESULT) + '\n```',
        ):
            with self.subTest(text=text):
                envelope = copy.deepcopy(ENVELOPE)
                envelope["data"]["outputs"]["result"] = text
                self.assertEqual(extract_result(envelope), RESULT)

    def test_truncation_and_prose_are_not_repaired(self):
        for text in ('<think>' + json.dumps(RESULT), json.dumps(RESULT)[:-1],
                     'Here is the result: ' + json.dumps(RESULT),
                     json.dumps(RESULT) + json.dumps(RESULT)):
            with self.subTest(text=text):
                envelope = copy.deepcopy(ENVELOPE)
                envelope["data"]["outputs"]["result"] = text
                with self.assertRaises(DifyError):
                    extract_result(envelope)

    @patch("src.dify_client.load_config")
    @patch.dict(os.environ, {"DIFY_API_KEY": "test-only-key", "DIFY_API_URL": "https://dify.example/v1/",
                             "DIFY_USER": "test", "DIFY_TIMEOUT_SECONDS": "12"}, clear=True)
    @patch("src.dify_client.requests.post")
    def test_analyze_paper_payload(self, post, _load):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = ENVELOPE
        result = analyze_paper(INPUTS["paper_title"], INPUTS["paper_abstract"], INPUTS["paper_url"], INPUTS["research_interests"])
        self.assertEqual(result, RESULT)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://dify.example/v1/workflows/run")
        self.assertEqual(kwargs["json"], {"inputs": INPUTS, "response_mode": "blocking", "user": "test"})
        self.assertEqual(kwargs["timeout"], (10, 12))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-only-key")
        self.assertFalse(kwargs["allow_redirects"])

    @patch("src.dify_client.load_config")
    @patch.dict(os.environ, {"DIFY_API_KEY": "test-only-key", "DIFY_MAX_RETRIES": "0"}, clear=True)
    @patch("src.dify_client.requests.post")
    def test_http_errors_and_timeout_do_not_retry_or_expose_key(self, post, _load):
        for status in (400, 401, 429, 500):
            post.reset_mock()
            post.return_value = Mock(status_code=status)
            with self.assertRaises(DifyError) as error:
                run_workflow(INPUTS)
            self.assertNotIn("test-only-key", str(error.exception))
            self.assertEqual(post.call_count, 1)
        post.side_effect = requests.Timeout("sensitive request data")
        with self.assertRaisesRegex(DifyError, "may still finish"):
            run_workflow(INPUTS)

    @patch("src.dify_client.load_config")
    @patch.dict(os.environ, {}, clear=True)
    @patch("src.dify_client.requests.post")
    def test_missing_key_fails_before_request(self, post, _load):
        with self.assertRaisesRegex(DifyError, "DIFY_API_KEY"):
            run_workflow(INPUTS)
        post.assert_not_called()

    @patch("src.dify_client.load_config")
    @patch.dict(os.environ, {"DIFY_API_KEY": "test-only-key", "DIFY_MAX_RETRIES": "2"}, clear=True)
    @patch("src.dify_client.time.sleep")
    @patch("src.dify_client.requests.post")
    def test_rate_limit_retry_is_bounded(self, post, sleep, _load):
        rejected = Mock(status_code=429, headers={"Retry-After": "3"})
        success = Mock(status_code=200)
        success.json.return_value = ENVELOPE
        post.side_effect = [rejected, success]
        self.assertEqual(run_workflow(INPUTS), ENVELOPE)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(3)
        post.reset_mock()
        sleep.reset_mock()
        post.side_effect = None
        post.return_value = rejected
        with self.assertRaises(DifyError):
            run_workflow(INPUTS)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


class PipelineTests(unittest.TestCase):
    def test_atom_summary_reaches_paper(self):
        papers = parse_feed(b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <id>https://arxiv.org/abs/1234.56789v1</id><title>EEG\n Network</title>
        <summary>Actual abstract from API</summary></entry></feed>''')
        self.assertEqual(papers[0]["paper_abstract"], "Actual abstract from API")
        self.assertEqual(papers[0]["paper_title"], "EEG Network")
        with self.assertRaises(ValueError):
            parse_feed(b"<html/>")
        with self.assertRaises(ValueError):
            parse_feed(b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#1</id></entry></feed>')

    @patch("src.main.load_config")
    @patch.dict(os.environ, {"DIFY_API_KEY": "test-only-key"}, clear=True)
    @patch("src.main.analyze_paper")
    def test_local_report_and_partial_failure(self, analyze, _load):
        # Exercise file loading, scoring, report generation and failure exit status.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "papers.json"
            other = {**INPUTS, "paper_url": "https://arxiv.org/abs/1234.56789"}
            fixture.write_text(json.dumps([INPUTS, other]), encoding="utf-8")
            analyze.side_effect = [RESULT, DifyError("Workflow failed")]
            with patch("src.main.PROJECT_ROOT", root), patch("sys.argv", ["main", "--paper-file", str(fixture)]), patch("builtins.print"):
                self.assertEqual(main(), 1)
            report_path = next((root / "reports").glob("*.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["results"][0]["analysis"], RESULT)
            self.assertEqual(len(report["failures"]), 1)
            self.assertTrue(report_path.with_suffix(".md").is_file())
            self.assertEqual(analyze.call_args_list[0].args[1], INPUTS["paper_abstract"])
            state = load_state(root / "data/processed.json")
            self.assertEqual(len(state["completed"]), 1)
            # A second run only retries the failed paper, not the successful one.
            analyze.reset_mock()
            analyze.side_effect = [RESULT]
            with patch("src.main.PROJECT_ROOT", root), patch("sys.argv", ["main", "--paper-file", str(fixture)]), patch("builtins.print"):
                self.assertEqual(main(), 0)
            self.assertEqual(analyze.call_count, 1)
            self.assertEqual(analyze.call_args.args[2], other["paper_url"])

    def test_arxiv_versions_and_interest_profiles(self):
        a = {**INPUTS, "paper_url": "http://arxiv.org/abs/1611.08024v1"}
        b = {**INPUTS, "paper_url": "https://arxiv.org/pdf/1611.08024v4.pdf"}
        self.assertEqual(paper_key(a), paper_key(b))
        b["research_interests"] = "different interest"
        self.assertNotEqual(paper_key(a), paper_key(b))

    def test_corrupt_state_and_parallel_run_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "processed.json"
            state_path.write_text('[]', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_state(state_path)
            lock = Path(directory) / "run.lock"
            with RunLock(lock):
                with self.assertRaises(ValueError):
                    with RunLock(lock):
                        pass
            self.assertFalse(lock.exists())


class ReportingTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    @patch("src.mailer.smtplib.SMTP")
    def test_missing_settings_are_identified_without_connecting(self, smtp):
        with self.assertRaises(MailConfigurationError) as caught:
            send_report(self.make_report())
        self.assertIn("SMTP_PASSWORD", str(caught.exception))
        self.assertIn("EMAIL_TO", str(caught.exception))
        smtp.assert_not_called()

    def test_smtp_error_diagnostics_do_not_expose_server_text(self):
        error = smtplib.SMTPAuthenticationError(535, b"server text including sensitive-password")
        message = email_error_message(error)
        self.assertIn("535", message)
        self.assertNotIn("sensitive-password", message)

    def make_report(self):
        item = {"paper": {**INPUTS, "paper_title": "<script>alert(1)</script>"}, "analysis": RESULT}
        low = {"paper": INPUTS, "analysis": {**RESULT, "score": 69}}
        selected = select_results([low, item], 70, 1)
        return {"count": 2, "results": [low, item], "selected": selected, "failures": [],
                "skipped": 0, "min_score": 70, "top_k": 1}

    def test_threshold_topk_and_html_escaping(self):
        report = self.make_report()
        self.assertEqual(len(report["selected"]), 1)
        text, html = render_report(report)
        self.assertIn("91", text)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        report["selected"] = []
        self.assertIn("没有达到条件", render_report(report)[0])

    @patch.dict(os.environ, {"SMTP_SERVER": "smtp.example", "SMTP_USERNAME": "user", "SMTP_PASSWORD": "test-only",
                             "EMAIL_FROM": "sender@example.com", "EMAIL_TO": "receiver@example.com"}, clear=True)
    @patch("src.mailer.ssl.create_default_context")
    @patch("src.mailer.smtplib.SMTP")
    def test_email_multipart_and_tls(self, smtp, _context):
        report = self.make_report()
        message = build_message(report)
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        self.assertIsNotNone(message.get_body(preferencelist=("plain",)))
        self.assertIsNotNone(message.get_body(preferencelist=("html",)))
        server = smtp.return_value.__enter__.return_value
        server.send_message.return_value = {}
        send_report(report)
        server.starttls.assert_called_once()
        server.login.assert_called_once()
        server.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
