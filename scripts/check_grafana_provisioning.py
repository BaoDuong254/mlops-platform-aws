"""Local Docker smoke check of real Grafana provisioning with an empty Prometheus.

No Kubernetes, AWS, load generators, or synthetic inference metrics. An empty
query is expected here; this check does NOT verify HPA/Canary scraping on EKS.
Uses exactly the images selected by the pinned monitoring Helm chart.
"""
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def main():
    suffix = uuid.uuid4().hex[:10]
    network = 'mlops-dashboard-check-' + suffix
    grafana = 'mlops-grafana-check-' + suffix
    prometheus = 'mlops-prom-check-' + suffix
    credential = secrets.token_urlsafe(24)
    headers = {'Authorization': 'Basic ' + base64.b64encode(('admin:' + credential).encode()).decode()}
    def request(base, endpoint):
        with urlopen(Request(base + endpoint, headers=headers), timeout=10) as response:
            return json.load(response)
    with tempfile.TemporaryDirectory(prefix='mlops-grafana-check-') as directory:
        path = Path(directory); path.chmod(0o755)
        provisioning = path / 'provisioning'
        dashboards = path / 'dashboards'
        for folder in [provisioning/'dashboards', provisioning/'datasources', dashboards/'inference', dashboards/'historical']:
            folder.mkdir(parents=True, exist_ok=True)
        values = yaml.safe_load((ROOT/'deploy/monitoring/grafana-values.yaml').read_text())
        (provisioning/'dashboards/providers.yaml').write_text(yaml.safe_dump(values['dashboardProviders']['dashboardproviders.yaml']))
        sources = values['datasources']['datasources.yaml']
        sources['datasources'][0]['url'] = f'http://{prometheus}:9090'
        (provisioning/'datasources/prometheus.yaml').write_text(yaml.safe_dump(sources))
        for source in (ROOT/'deploy/monitoring').glob('*-dashboard.json'):
            shutil.copyfile(source, dashboards/'inference'/source.name)
        config = path/'prometheus.yml'
        config.write_text('global:\n  scrape_interval: 15s\nscrape_configs: []\n')
        try:
            docker('network', 'create', network)
            docker('run', '-d', '--name', prometheus, '--network', network,
                   '-v', f'{config}:/etc/prometheus/prometheus.yml:ro', 'prom/prometheus:v3.15.1')
            docker('run', '-d', '--name', grafana, '--network', network, '-p', '127.0.0.1::3000',
                   '-e', 'GF_SECURITY_ADMIN_PASSWORD=' + credential,
                   '-e', 'GF_ANALYTICS_REPORTING_ENABLED=false',
                   '-e', 'GF_ANALYTICS_CHECK_FOR_UPDATES=false',
                   '-e', 'GF_PLUGINS_PREINSTALL_DISABLED=true',
                   '-v', f'{provisioning}:/etc/grafana/provisioning:ro',
                   '-v', f'{dashboards}:/var/lib/grafana/dashboards:ro', 'grafana/grafana:13.2.4-distroless')
            port = json.loads(docker('inspect', grafana))[0]['NetworkSettings']['Ports']['3000/tcp'][0]['HostPort']
            base = 'http://127.0.0.1:' + port
            for _ in range(100):
                try:
                    if request(base, '/api/health')['database'] == 'ok': break
                except Exception: time.sleep(.3)
            else: raise RuntimeError('Grafana did not become ready')
            health=request(base, '/api/datasources/uid/prometheus/health')
            assert health['status'].upper()=='OK',health
            entries=request(base,'/api/search?type=dash-db')
            assert {entry['uid'] for entry in entries}=={'m33-load-hpa','m33-canary'},entries
            checked=0
            for uid in ['m33-load-hpa','m33-canary']:
                model=request(base,'/api/dashboards/uid/'+uid)
                assert model['meta']['provisioned']
                for panel in model['dashboard']['panels']:
                    for target in panel.get('targets',[]):
                        result=request(base,'/api/datasources/proxy/uid/prometheus/api/v1/query?'+urlencode({'query':target['expr']}))
                        assert result['status']=='success',result
                        assert not result['data']['result'], 'Unexpected inference data in empty local Prometheus'
                        checked+=1
            print(f'PASS: local Grafana 13.2.4, two provisioned UIDs, healthy datasource, {checked} queries accepted; all workload panels correctly empty. Not EKS runtime evidence.')
        finally:
            for name in [grafana,prometheus]:
                subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            subprocess.run(['docker','network','rm',network],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__': main()
