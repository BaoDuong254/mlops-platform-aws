"""Preserve historical TestData dashboards before ephemeral Grafana recreation.

Never edits metrics or historical queries. Secret stays in memory. Failure to read
an existing Grafana stops deployment before its Helm upgrade.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
REALTIME_UIDS = {'m33-inference', 'm33-load-hpa', 'm33-canary'}


def kubectl(*args, **kwargs):
    return subprocess.check_output(['kubectl', '--request-timeout=15s', '-n', 'monitoring', *args], **kwargs)


def password():
    value = kubectl('get', 'secret', 'm33-grafana-admin', '-o', 'jsonpath={.data.admin-password}')
    return base64.b64decode(value).decode()


def api(base, endpoint, credential=None):
    headers = {}
    if credential:
        headers['Authorization'] = 'Basic ' + base64.b64encode(('admin:' + credential).encode()).decode()
    with urlopen(Request(base + endpoint, headers=headers), timeout=10) as response:
        return json.load(response)


@contextmanager
def tunnel(service):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(['kubectl', '-n', 'monitoring', 'port-forward', '--address', '127.0.0.1', f'service/{service}', f'{port}:80'], stdout=log, stderr=log)
        try:
            base = f'http://127.0.0.1:{port}'
            for _ in range(50):
                if process.poll() is not None:
                    raise RuntimeError(f'Port forward failed for {service}')
                try:
                    with urlopen(base + '/api/health', timeout=1):
                        break
                except Exception:
                    time.sleep(.2)
            else:
                raise RuntimeError(f'Service unavailable: {service}')
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()


def is_historical(dashboard, testdata_uids):
    content = json.dumps(dashboard)
    return ('csv_content' in content or 'csvContent' in content or
            'grafana-testdata-datasource' in content or
            any(('"' + uid + '"') in content for uid in testdata_uids))


def preserve():
    folder = ROOT / 'artifacts/monitoring/historical'
    folder.mkdir(parents=True, exist_ok=True)
    # --ignore-not-found distinguishes a fresh install from an unavailable API.
    existing = kubectl('get', 'deployment', 'm33-grafana', '--ignore-not-found', '-o', 'name').strip()
    datasources = []
    if existing:
        credential = password()
        with tunnel('m33-grafana') as base:
            sources = api(base, '/api/datasources', credential)
            datasources = [{key: source[key] for key in ('name', 'uid', 'type', 'access', 'jsonData') if key in source}
                           for source in sources if source['type'] == 'grafana-testdata-datasource']
            testdata_uids = {source['uid'] for source in datasources}
            dashboards = api(base, '/api/search?type=dash-db&limit=5000', credential)
            if len(dashboards) == 5000:
                raise RuntimeError('Dashboard preservation inventory limit reached')
            for item in dashboards:
                if item['uid'] in REALTIME_UIDS:
                    continue
                dashboard = api(base, '/api/dashboards/uid/' + item['uid'], credential)['dashboard']
                if is_historical(dashboard, testdata_uids):
                    # Preserve UID, queries, CSV, titles and panel configuration exactly.
                    dashboard['id'] = None
                    name = item['uid']
                    if not name or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for char in name):
                        raise ValueError('Unsupported historical dashboard UID')
                    (folder / f'{name}.json').write_text(json.dumps(dashboard, indent=2) + '\n')
            archive = folder / 'datasources.json'
            previous = json.loads(archive.read_text())['datasources'] if archive.exists() else []
            combined = {source['uid']: source for source in previous + datasources}
            archive.write_text(json.dumps({'apiVersion': 1, 'datasources': list(combined.values())}, indent=2) + '\n')
    ds_path = folder / 'datasources.json'
    data = {path.name: path.read_text() for path in folder.glob('*.json') if path.name != 'datasources.json'}
    datasource_doc = json.loads(ds_path.read_text()) if ds_path.exists() else {'apiVersion': 1, 'datasources': []}
    for name, content in [('m33-historical-dashboards', data),
                          ('m33-historical-datasources', {'historical.yaml': json.dumps(datasource_doc)})]:
        doc = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': name, 'namespace': 'monitoring'}, 'data': content}
        # Kubernetes ConfigMaps have a 1 MiB ceiling. Fail before replacing Grafana.
        if len(json.dumps(doc).encode()) > 900_000:
            raise ValueError('Historical export exceeds ConfigMap budget; archive/provision it separately before upgrade')
        subprocess.run(['kubectl', '-n', 'monitoring', 'apply', '-f', '-'], input=json.dumps(doc), text=True, check=True)
    print(f'Preserved {len(data)} historical dashboards; local archive: {folder}')


if __name__ == '__main__':
    preserve()
