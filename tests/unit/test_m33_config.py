"""Guard the HPA ownership and limited monitoring scope across future releases."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def read_yaml(path: str):
    return yaml.safe_load((ROOT / path).read_text())


def test_hpa_and_argocd_agree_on_replica_ownership():
    hpa = read_yaml("deploy/gitops/inference/hpa.yaml")
    app = read_yaml("deploy/argocd/inference-application-automated.yaml")
    kustomization = read_yaml("deploy/gitops/inference/kustomization.yaml")
    assert hpa["apiVersion"] == "autoscaling/v2"
    assert hpa["spec"]["scaleTargetRef"] == {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Rollout", "name": "inference-api"
    }
    assert (hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]) == (1, 3)
    assert hpa["spec"]["metrics"][0]["resource"] == {
        "name": "cpu", "target": {"type": "Utilization", "averageUtilization": 60}
    }
    assert "hpa.yaml" in kustomization["resources"]
    assert any("/spec/replicas" in item.get("jsonPointers", []) for item in app["spec"]["ignoreDifferences"])
    ignored = {item["kind"] for item in app["spec"]["ignoreDifferences"]
               if "/spec/replicas" in item.get("jsonPointers", [])}
    assert {"Deployment", "Rollout"} <= ignored
    assert "RespectIgnoreDifferences=true" in app["spec"]["syncPolicy"]["syncOptions"]
    assert app["spec"]["syncPolicy"]["automated"] == {"prune": False, "selfHeal": True}


def test_monitoring_is_private_ephemeral_with_minimal_hpa_state_collection():
    prom = read_yaml("deploy/monitoring/prometheus-values.yaml")
    grafana = read_yaml("deploy/monitoring/grafana-values.yaml")
    assert prom["server"]["persistentVolume"]["enabled"] is False
    assert prom["server"]["retention"] == "24h"
    assert prom["server"]["service"]["type"] == grafana["service"]["type"] == "ClusterIP"
    assert not prom["rbac"]["create"] and not grafana["rbac"]["create"]
    assert all(not prom[name]["enabled"] for name in (
        "alertmanager", "prometheus-node-exporter", "prometheus-pushgateway"))
    state = prom["kube-state-metrics"]
    assert state["enabled"] and state["namespaces"] == ["mlops"]
    assert state["collectors"] == ["horizontalpodautoscalers", "pods"]
    assert state["rbac"] == {"create": True, "useClusterRole": False}
    assert 'kube_horizontalpodautoscaler_status_target_metric' in state['metricAllowlist']
    jobs = prom["serverFiles"]["prometheus.yml"]["scrape_configs"]
    assert [job['job_name'] for job in jobs] == ['inference-api', 'mlops-state']
    assert jobs[0]["kubernetes_sd_configs"][0]["namespaces"]["names"] == ["mlops"]
    assert jobs[0]['scrape_interval'] == jobs[1]['scrape_interval'] == '15s'
    assert grafana["admin"]["existingSecret"] == "m33-grafana-admin"
    assert grafana["deploymentStrategy"]["type"] == "Recreate"
    providers = grafana["dashboardProviders"]["dashboardproviders.yaml"]["providers"]
    assert not providers[0]['disableDeletion']  # prune only old file-provisioned realtime UID
    assert providers[1]['disableDeletion'] and providers[1]['name'] == 'historical'
    assert grafana['dashboardsConfigMaps']['historical'] == 'm33-historical-dashboards'


def test_two_live_dashboards_keep_units_gates_and_no_guessed_roles_or_fake_idle_data():
    paths = list((ROOT / 'deploy/monitoring').glob('*-dashboard.json'))
    assert len(paths) == 2
    dashboards = [json.loads(path.read_text()) for path in paths]
    assert {doc['uid'] for doc in dashboards} == {'m33-load-hpa', 'm33-canary'}
    for doc in dashboards:
        assert doc['refresh'] == '15s' and doc['time'] == {'from': 'now-15m', 'to': 'now'}
        for panel in doc['panels']:
            for target in panel.get('targets', []):
                assert target['datasource'] == {'type': 'prometheus', 'uid': 'prometheus'}
                assert 'or vector(0)' not in target['expr']
                assert 'testdata' not in json.dumps(target).lower()
        # No overlapping panel rectangles, including explanatory text.
        for i, first in enumerate(doc['panels']):
            a=first['gridPos']
            for second in doc['panels'][i+1:]:
                b=second['gridPos']
                assert a['x']+a['w']<=b['x'] or b['x']+b['w']<=a['x'] or a['y']+a['h']<=b['y'] or b['y']+b['h']<=a['y']
    canary = next(doc for doc in dashboards if doc['uid']=='m33-canary')
    for panel in canary['panels']:
        assert 'rollouts_pod_template_hash' in panel['targets'][0]['expr']
        assert panel['targets'][0]['legendFormat'] == 'RS {{rollouts_pod_template_hash}}'
    assert [panel['fieldConfig']['defaults']['thresholds']['steps'][1]['value'] for panel in canary['panels'][:3]] == [.2, 500, 1]
    gate = next(yaml.safe_load_all((ROOT/'deploy/gitops/inference/analysis-template.yaml').read_text()))
    conditions = {metric['name']:metric['successCondition'] for metric in gate['spec']['metrics']}
    assert conditions['p95-latency-seconds']=='result[0] < 0.5'
    assert conditions['error-rate']=='result[0] < 0.01'
    assert conditions['canary-request-rate']=='result[0] >= 0.2'


def test_discovery_rbac_and_load_job_stay_in_scope():
    rbac = list(yaml.safe_load_all((ROOT / "deploy/monitoring/prometheus-rbac.yaml").read_text()))
    role = next(item for item in rbac if item["kind"] == "Role")
    binding = next(item for item in rbac if item["kind"] == "RoleBinding")
    assert role["metadata"]["namespace"] == binding["metadata"]["namespace"] == "mlops"
    assert role["rules"] == [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]}]
    assert binding["subjects"][0]["namespace"] == "monitoring"

    template = (ROOT / "deploy/monitoring/k6-job.yaml.tpl").read_text()
    for key, value in {"LOAD_VUS": "10", "LOAD_RATE": "20", "LOAD_DURATION": "300s", "LOAD_PAYLOAD": "valid"}.items():
        template = template.replace("${" + key + "}", value)
    job = yaml.safe_load(template)
    assert job["metadata"]["namespace"] == "mlops"
    assert job["metadata"]["labels"]["app"] == "m33-k6"
    assert job["spec"]["template"]["spec"]["nodeSelector"] == {"eks.amazonaws.com/nodegroup": "system"}
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["limits"]["cpu"] == "500m"
    assert container["resources"]["limits"]["memory"] == "512Mi"
