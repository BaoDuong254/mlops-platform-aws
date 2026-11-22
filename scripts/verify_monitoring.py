"""Read-only runtime verification of both provisioned dashboards and their queries."""
import argparse
import json
from pathlib import Path
from urllib.parse import quote, urlencode

from grafana_dashboards import api, password

ROOT = Path(__file__).resolve().parents[1]
FILES = ('inference-load-hpa-dashboard.json', 'canary-performance-dashboard.json')


def verify(grafana, prometheus):
    credential = password()
    health = api(grafana, '/api/datasources/uid/prometheus/health', credential)
    assert health['status'].upper() == 'OK', health
    checked = 0
    for name in FILES:
        source = json.loads((ROOT / 'deploy/monitoring' / name).read_text())
        live = api(grafana, '/api/dashboards/uid/' + quote(source['uid']), credential)
        assert live['meta']['provisioned'], 'Dashboard is not file-provisioned'
        assert live['dashboard']['title'] == source['title']
        queries = [target['expr'] for panel in source['panels'] for target in panel.get('targets', [])]
        live_queries = [target['expr'] for panel in live['dashboard']['panels'] for target in panel.get('targets', [])]
        assert live_queries == queries, 'Live queries differ from source'
        for query in queries:
            result = api(grafana, '/api/datasources/proxy/uid/prometheus/api/v1/query?' + urlencode({'query': query}), credential)
            assert result['status'] == 'success', result
            checked += 1
            if not result['data']['result']:
                print(f'No data (not fabricated): {source["uid"]}: {query}')
        print(f'Verified dashboard {source["uid"]}: {len(queries)} PromQL targets')
    for query in ['up{job="mlops-state"}',
                  'kube_horizontalpodautoscaler_status_current_replicas{job="mlops-state",namespace="mlops",horizontalpodautoscaler="inference-api"}',
                  'kube_horizontalpodautoscaler_status_desired_replicas{job="mlops-state",namespace="mlops",horizontalpodautoscaler="inference-api"}',
                  'kube_horizontalpodautoscaler_status_target_metric{job="mlops-state",namespace="mlops",horizontalpodautoscaler="inference-api",metric_name="cpu",metric_target_type="utilization"}',
                  'kube_horizontalpodautoscaler_spec_target_metric{job="mlops-state",namespace="mlops",horizontalpodautoscaler="inference-api",metric_name="cpu",metric_target_type="utilization"}']:
        result = api(grafana, '/api/datasources/proxy/uid/prometheus/api/v1/query?' + urlencode({'query': query}), credential)
        assert result['status'] == 'success' and result['data']['result'], f'Missing state metric: {query}'
        if query.startswith('up{'):
            assert all(float(item['value'][1]) == 1 for item in result['data']['result']), 'State exporter is down'
    try:
        api(grafana, '/api/dashboards/uid/m33-inference', credential)
    except Exception as error:
        from urllib.error import HTTPError
        if not isinstance(error, HTTPError) or error.code != 404:
            raise
    else:
        raise AssertionError('Legacy realtime dashboard is still provisioned')
    print(f'PASS: datasource health, two dashboards, {checked} queries, HPA replicas/CPU and legacy dashboard migration')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--grafana', default='http://127.0.0.1:13000')
    parser.add_argument('--prometheus', default='http://127.0.0.1:19090')
    args = parser.parse_args()
    verify(args.grafana, args.prometheus)
