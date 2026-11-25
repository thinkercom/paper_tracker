"""Independent Actions failure notification when the main process never sent."""

import json
import os

from .daily import assemble_report
from .dify_client import PROJECT_ROOT, load_config
from .main import deliver_report, save_report
from .state import RunLock, load_state


def main():
    load_config()
    marker = PROJECT_ROOT / 'reports/delivery.json'
    if marker.exists():
        receipt = json.loads(marker.read_text(encoding='utf-8'))
        if receipt.get('sent') or receipt.get('attempted'):
            print('Delivery already completed/attempted; not automatically resending SMTP DATA.')
            return 0
    if os.getenv('SEND_EMAIL', 'false').lower() != 'true':
        print('Email disabled; failure remains visible in Actions.')
        return 0
    path = PROJECT_ROOT / 'data/processed.json'
    with RunLock(PROJECT_ROOT / 'data/run.lock'):
        try:
            state = load_state(path)
        except (OSError, ValueError):
            state = {'completed': {}}
        try:
            threshold, top_k = float(os.getenv('MIN_SCORE', '50')), int(os.getenv('TOP_K', '5'))
            if not 0 <= threshold <= 100 or top_k < 1:
                raise ValueError
        except ValueError:
            threshold, top_k = 50, 5
        report = assemble_report([], [{'paper_url': 'GitHub Actions', 'stage': 'workflow',
                                       'error': 'Workflow failed before regular delivery; inspect this run in GitHub Actions.'}],
                                 0, 0, threshold, top_k, state, os.getenv('RESEARCH_INTERESTS', ''))
        report['workflow_url'] = (os.getenv('GITHUB_SERVER_URL', 'https://github.com') + '/' +
                                  os.getenv('GITHUB_REPOSITORY', '') + '/actions/runs/' + os.getenv('GITHUB_RUN_ID', ''))
        return 0 if deliver_report(report, save_report(report), state, path) else 1


if __name__ == '__main__':
    raise SystemExit(main())
