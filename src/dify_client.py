"""Blocking Workflow API client with text parsing and bounded safe retries."""

import json
import math
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DifyError(RuntimeError):
    """An actionable error without credentials or raw server output."""


def load_config():
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def validate_inputs(inputs):
    if not isinstance(inputs, dict):
        raise DifyError("Workflow inputs must be a JSON object.")
    for key in ("paper_title", "paper_abstract", "paper_url", "research_interests"):
        if not isinstance(inputs.get(key), str) or not inputs[key].strip():
            raise DifyError(f"Missing or empty string input: {key}")


def extract_result(response):
    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
        raise DifyError("Dify response must contain a data object.")
    data = response["data"]
    if data.get("status") != "succeeded":
        raise DifyError("Workflow did not succeed; inspect the run in Dify Logs.")
    outputs = data.get("outputs")
    if not isinstance(outputs, dict) or "result" not in outputs:
        raise DifyError("Missing data.outputs.result; check the Output node mapping.")
    result = outputs["result"]
    if isinstance(result, str):
        result = result.strip()
        # DeepSeek may prepend reasoning to text. Only strip a complete leading
        # block; never search reasoning/prose for a plausible scoring object.
        if result.startswith("<think>"):
            closing = result.find("</think>", len("<think>"))
            if closing == -1:
                raise DifyError("Incomplete <think> block; check model output/token limit in Dify Logs.")
            result = result[closing + len("</think>"):].strip()
        fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", result, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            result = fenced.group(1).strip()
        try:
            result = json.loads(result)
        except ValueError:
            raise DifyError("result must contain complete JSON after optional <think> and JSON fences; check truncation and Output mapping.") from None
    if not isinstance(result, dict):
        raise DifyError("result must be a JSON object.")
    score = result.get("score")
    if (type(score) not in (int, float) or not math.isfinite(score)
            or not 0 <= score <= 100):
        raise DifyError("score must be a finite number from 0 to 100.")
    for key in ("reason", "summary"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise DifyError(f"{key} must be a nonempty string.")
    if type(result.get("recommended")) is not bool:
        raise DifyError("recommended must be a JSON boolean, not a string.")
    return {key: result[key] for key in ("score", "reason", "summary", "recommended")}


def run_workflow(inputs):
    """Call once and return the validated, raw Dify response envelope."""
    load_config()
    validate_inputs(inputs)
    api_key = os.getenv("DIFY_API_KEY", "").strip()
    if not api_key:
        raise DifyError("Set DIFY_API_KEY in the project .env before calling Dify.")
    base_url = os.getenv("DIFY_API_URL", "https://api.dify.ai/v1").strip().rstrip("/")
    parsed = urlsplit(base_url)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise DifyError("DIFY_API_URL must be an API base URL such as https://api.dify.ai/v1.")
    if base_url.endswith("/workflows/run"):
        raise DifyError("DIFY_API_URL must omit /workflows/run; keep the API base including /v1.")
    try:
        timeout = float(os.getenv("DIFY_TIMEOUT_SECONDS", "120"))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except ValueError:
        raise DifyError("DIFY_TIMEOUT_SECONDS must be a positive finite number.") from None
    user = os.getenv("DIFY_USER", "paper-tracker-local").strip()
    if not user:
        raise DifyError("DIFY_USER must be nonempty.")
    try:
        retries = int(os.getenv("DIFY_MAX_RETRIES", "2"))
        if not 0 <= retries <= 5:
            raise ValueError
    except ValueError:
        raise DifyError("DIFY_MAX_RETRIES must be an integer from 0 to 5.") from None
    # Only retry explicit rate rejection or connection establishment timeout.
    # Read timeouts/5xx can follow a billable run: leave these for manual review.
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                f"{base_url}/workflows/run",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"inputs": inputs, "response_mode": "blocking", "user": user},
                timeout=(10, timeout),
                allow_redirects=False,
            )
        except requests.ConnectTimeout:
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise DifyError("Dify connection timed out after bounded retries.") from None
        except requests.Timeout:
            raise DifyError("Dify timed out. Check Dify Logs before rerunning; the run may still finish.") from None
        except requests.RequestException:
            raise DifyError("Cannot connect to Dify; check API URL, network and TLS configuration.") from None
        if response.status_code == 429 and attempt < retries:
            delay = 2 ** (attempt + 1)
            retry_after = response.headers.get("Retry-After", "")
            if isinstance(retry_after, str) and retry_after.isdigit():
                delay = min(60, max(delay, int(retry_after)))
            time.sleep(delay)
            continue
        break
    if response.status_code != 200:
        hints = {400: "Check input names and ensure the Workflow is published.",
                 401: "Check the Workflow application API key.",
                 403: "Check app access permissions.",
                 404: "Check API base URL and app type (Workflow).",
                 429: "Rate limited; wait before manually retrying."}
        raise DifyError(f"Dify HTTP {response.status_code}. " + hints.get(response.status_code, "Inspect Dify Logs."))
    try:
        envelope = response.json()
    except ValueError:
        raise DifyError("Dify returned non-JSON data; check API URL and reverse proxy.") from None
    extract_result(envelope)
    return envelope


def analyze_paper(title, abstract, url, research_interests=None):
    """Return {score, reason, summary, recommended} from a real Workflow run."""
    load_config()
    interests = research_interests if research_interests is not None else os.getenv("RESEARCH_INTERESTS", "")
    return extract_result(run_workflow({
        "paper_title": title,
        "paper_abstract": abstract,
        "paper_url": url,
        "research_interests": interests,
    }))
