"""Execute the dashboard's actual PromQL with promtool, offline synthetic unit fixtures.

These fixtures verify query semantics; they are never runtime evidence or Grafana data.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]


def rules():
    output = []
    for name in ('inference-load-hpa-dashboard.json', 'canary-performance-dashboard.json'):
        doc = json.loads((ROOT / 'deploy/monitoring' / name).read_text())
        for panel in doc['panels']:
            for target in panel.get('targets', []):
                output.append({'record': doc['uid'].replace('-', '_') + f'_p{panel["id"]}_' + target['refId'].lower(),
                               'expr': target['expr']})
    return {'groups': [{'name': 'dashboards', 'interval': '15s', 'rules': output}]}


def main():
    with tempfile.TemporaryDirectory(prefix='mlops-dashboard-promql-') as directory:
        path = Path(directory)
        (path / 'rules.yaml').write_text(yaml.safe_dump(rules()))
        (path / 'tests.yaml').write_text((ROOT / 'tests/prometheus/dashboard-metrics.test.yaml').read_text())
        subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--user', f'{os.getuid()}:{os.getgid()}',
                        '-v', f'{path}:/checks:ro', '--entrypoint', '/bin/promtool', 'prom/prometheus:v3.15.1',
                        'test', 'rules', '/checks/tests.yaml'], check=True)


if __name__ == '__main__':
    main()
