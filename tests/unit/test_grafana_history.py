"""Historical CSV preservation is separate from the two live dashboards."""
import importlib.util
import json
from pathlib import Path
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('grafana_dashboards', ROOT/'scripts/grafana_dashboards.py')
grafana = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grafana)


def test_detect_history_by_scenario_plugin_or_uid():
    assert grafana.is_historical({'panels':[{'targets':[{'scenarioId':'csv_content','csvContent':'x,y\n1,2'}]}]},set())
    assert grafana.is_historical({'datasource':{'uid':'historical-ds'}},{'historical-ds'})
    assert not grafana.is_historical({'datasource':{'uid':'prometheus'}},{'historical-ds'})


def test_preserve_retains_csv_uid_units_and_datasource_before_grafana_recreation(tmp_path, monkeypatch):
    monkeypatch.setattr(grafana,'ROOT',tmp_path)
    monkeypatch.setattr(grafana,'kubectl',lambda *a,**k: b'deployment.apps/m33-grafana')
    monkeypatch.setattr(grafana,'password',lambda: 'not-a-real-credential')
    @contextmanager
    def tunnel(service): yield 'http://local.invalid'
    monkeypatch.setattr(grafana,'tunnel',tunnel)
    dashboard={'id':12,'uid':'historical-hpa','title':'HPA Historical','panels':[{'fieldConfig':{'defaults':{'unit':'percent'}},'targets':[{'scenarioId':'csv_content','csvContent':'time,cpu\n1,200'}]}]}
    def api(base,endpoint,credential):
        if endpoint=='/api/datasources': return [{'id':2,'name':'History','uid':'history','type':'grafana-testdata-datasource','access':'proxy','jsonData':{}}]
        if endpoint.startswith('/api/search'): return [{'uid':'m33-inference'},{'uid':'historical-hpa'}]
        return {'dashboard':dashboard}
    monkeypatch.setattr(grafana,'api',api)
    applied=[]
    monkeypatch.setattr(grafana.subprocess,'run',lambda *a,**k: applied.append(json.loads(k['input'])))
    grafana.preserve()
    saved=json.loads(applied[0]['data']['historical-hpa.json'])
    assert saved['uid']==dashboard['uid'] and saved['panels']==dashboard['panels']
    assert set(applied[0]['data'])=={'historical-hpa.json'}
    sources=json.loads(applied[1]['data']['historical.yaml'])['datasources']
    assert sources[0]['uid']=='history' and 'id' not in sources[0]
