"""Step 2: execute one real paper scoring request, then persist evidence."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .dify_client import DifyError, PROJECT_ROOT, extract_result, run_workflow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "examples/test_paper.json")
    args = parser.parse_args()
    try:
        inputs = json.loads(args.input.read_text(encoding="utf-8-sig"))
        envelope = run_workflow(inputs)
        result = extract_result(envelope)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output = PROJECT_ROOT / "reports" / f"smoke-{timestamp}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"verified_via": "real_dify_api", "inputs": inputs,
                                      "response": envelope, "result": result},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
    except (DifyError, OSError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"PASS: real API response and schema validated. Evidence: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
