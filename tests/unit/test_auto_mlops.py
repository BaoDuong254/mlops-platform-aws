"""Offline orchestration contracts: execute policy, workflow expressions, handoff and traffic boundaries."""
import ast
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


auto = module('auto_mlops')
result = module('training_result')
traffic = module('release_traffic')
verify = module('m32_verify')
guard = module('workflow_guard')


def workflow(name):
    return yaml.safe_load((ROOT / '.github/workflows' / f'{name}.yml').read_text())


def expression(text, **context):
    """Evaluate the actual simple checked-in `if`/`with` expression, not a parallel policy."""
    def obj(value):
        return SimpleNamespace(**{k: obj(v) for k, v in value.items()}) if isinstance(value, dict) else value
    text = text.removeprefix('${{').removesuffix('}}').strip().replace('&&', ' and ').replace('||', ' or ')
    ast.parse(text, mode='eval')
    return eval(text, {'__builtins__': {}, 'always': lambda: True}, {k: obj(v) for k, v in context.items()})


@pytest.fixture
def env():
    return {'AUTO_MLOPS_ENABLED': 'true', 'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_REPOSITORY_ID': '12', 'EXPECTED_REPOSITORY_ID': '12',
            'GITHUB_REPOSITORY_OWNER_ID': '34', 'EXPECTED_OWNER_ID': '34',
            'GITHUB_REPOSITORY': 'owner/project'}


@pytest.fixture
def history(tmp_path, monkeypatch):
    repo = tmp_path / 'git'
    repo.mkdir()
    monkeypatch.chdir(repo)
    subprocess.run(['git', 'init', '-q', '-b', 'main'], check=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.invalid'], check=True)
    subprocess.run(['git', 'config', 'user.name', 'Offline test'], check=True)
    def commit(path, contents):
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(contents)
        subprocess.run(['git', 'add', path], check=True)
        subprocess.run(['git', 'commit', '-qm', 'synthetic source change'], check=True)
        return auto.git('rev-parse', 'HEAD')
    base = commit('src/training/model.py', 'old training\n')
    return commit, base


@pytest.mark.parametrize('path', [
    'src/training/model.py', 'src/training/features.py', 'configs/training.yaml',
    'configs/training-candidate-63.yaml', 'configs/mlflow.yaml', 'uv.lock', 'pyproject.toml',
    'deploy/training/Dockerfile', 'deploy/platform/mlops/training-job.yaml.tpl',
    'scripts/train_workflow.sh', 'scripts/training_result.py', '.dockerignore',
])
def test_training_changes_detected_from_real_git_blobs(history, env, path):
    commit, base = history
    source = commit(path, 'changed training inputs\n')
    assert auto.choose_plan(env, source, base, source)['train'] is True


@pytest.mark.parametrize('path', [
    'README.md', 'docs/architecture.md', 'images/figure.png', 'final-e2e-evidence/notes.md',
    'src/inference/app.py', 'deploy/gitops/inference/deployment.yaml',
    'deploy/gitops/inference/rollout.yaml', '.github/workflows/release.yml',
])
def test_non_training_and_gitops_changes_do_not_train(history, env, path):
    commit, base = history
    source = commit(path, 'unrelated change\n')
    assert auto.choose_plan(env, source, base, source)['train'] is False


@pytest.mark.parametrize('event,ref,ci,enabled,repo,owner,expected', [
    ('push', 'refs/heads/main', 'success', 'true', '12', '34', True),
    ('push', 'refs/heads/main', 'failure', 'true', '12', '34', False),
    ('pull_request', 'refs/pull/1/merge', 'success', 'true', '12', '34', False),
    ('push', 'refs/heads/main', 'success', 'true', '99', '34', False),
    ('push', 'refs/heads/main', 'success', 'true', '12', '99', False),
    ('push', 'refs/heads/feature', 'success', 'true', '12', '34', False),
    ('push', 'refs/heads/main', 'success', 'false', '12', '34', False),
    ('push', 'refs/heads/main', 'success', '', '12', '34', False),
])
def test_actual_ci_gate_before_reusable_cloud_call(event, ref, ci, enabled, repo, owner, expected):
    job = workflow('ci')['jobs']['auto_mlops']
    values = {'github': {'event_name': event, 'ref': ref, 'repository_id': repo, 'repository_owner_id': owner},
              'needs': {'ci': {'result': ci}},
              'vars': {'AUTO_MLOPS_ENABLED': enabled, 'EXPECTED_REPOSITORY_ID': '12', 'EXPECTED_OWNER_ID': '34'}}
    assert bool(expression(job['if'], **values)) is expected
    assert job['needs'] == ['ci']
    ci_stage = workflow('ci')['jobs']['ci']
    assert set(ci_stage['needs']) == {'infrastructure', 'training', 'serving', 'gitops'}


@pytest.mark.parametrize('update', [{'AUTO_MLOPS_ENABLED': 'false'}, {'AUTO_MLOPS_ENABLED': ''},
                                  {'GITHUB_REPOSITORY_ID': '99'}, {'GITHUB_EVENT_NAME': 'pull_request'}])
def test_planner_also_rejects_disabled_or_untrusted_without_reading_git(env, update, monkeypatch):
    monkeypatch.setattr(auto, 'fingerprint', lambda sha: pytest.fail('Must not inspect training or call cloud'))
    assert not auto.choose_plan(env | update, 'a'*40, 'b'*40, 'a'*40)['train']


def test_stale_pending_run_and_cumulative_documentation_followup(history, env):
    commit, base = history
    training = commit('src/training/model.py', 'new training\n')
    latest = commit('README.md', 'documentation arriving during training\n')
    assert auto.choose_plan(env, training, base, latest)['reason'] == 'stale'
    # The later doc commit includes still-unprocessed training changes relative to the retained cursor.
    previous = {'disposition': 'stale', 'base_sha': base}
    assert auto.choose_plan(env, latest, previous['base_sha'], latest, previous)['train']
    # Once processed, docs and duplicate/rerun source do not start another training Job.
    assert not auto.choose_plan(env, latest, latest, latest)['train']


def test_unchanged_failed_attempt_is_blocked_not_automatically_retried(history, env):
    commit, base = history
    failed = commit('src/training/model.py', 'failed attempt\n')
    previous = {'disposition': 'blocked', 'fingerprint': auto.fingerprint(failed)}
    doc = commit('README.md', 'docs\n')
    assert auto.choose_plan(env, doc, failed, doc, previous)['reason'].startswith('blocked-')
    fixed = commit('src/training/model.py', 'fix\n')
    assert auto.choose_plan(env, fixed, failed, fixed, previous)['train']


@pytest.mark.parametrize('head,current,event,enabled,expected', [
    ('a'*40, 'a'*40, 'push', 'true', True),
    ('b'*40, 'a'*40, 'push', 'true', False),
    ('a'*40, 'b'*40, 'push', 'true', False),
    ('a'*40, 'a'*40, 'pull_request', 'true', False),
    ('a'*40, 'a'*40, 'push', 'false', False),
])
def test_exact_ci_checkout_and_main_guard(head, current, event, enabled, expected):
    env = {'SOURCE_SHA': 'a'*40, 'GITHUB_SHA': 'a'*40, 'AUTOMATIC': 'true',
           'AUTO_MLOPS_ENABLED': enabled, 'GITHUB_EVENT_NAME': event}
    if expected:
        guard.assert_source(env, head, current)
    else:
        with pytest.raises(ValueError): guard.assert_source(env, head, current)


@pytest.fixture
def decision():
    return {'validation_status': 'approved', 'model_name': 'taxi-model', 'model_version': '42',
            'registry_status': 'READY', 'mlflow_run_id': 'c'*32, 'source_run_id': '20261117T000000Z',
            'training_git_sha': 'a'*40, 'source_tree_sha256': 'b'*64, 'artifact_checksum': 'd'*64,
            'source_manifest_sha256': 'e'*64, 'validation_dataset_key': 'f'*64,
            'validation_metric_definition': result.METRIC_DEFINITION, 'validation_mae_minutes': 3.1}


@pytest.mark.parametrize('approved,train_state,automatic,expected', [
    (True, 'success', True, True), (False, 'success', True, False),
    (True, 'failure', True, False), (True, 'success', False, False),
])
def test_execute_decision_outputs_and_actual_release_call(tmp_path, decision, approved, train_state, automatic, expected):
    if not approved:
        decision.update(validation_status='rejected', model_version=None, registry_status=None)
    path, out, summary = tmp_path/'decision.json', tmp_path/'outputs', tmp_path/'summary'
    path.write_text(json.dumps(decision))
    run = subprocess.run([str(ROOT/'.venv/bin/python'), str(ROOT/'scripts/training_result.py'), 'outputs',
                          '--path', str(path), '--source-sha', 'a'*40], capture_output=True, text=True,
                         env=dict(os.environ, GITHUB_OUTPUT=str(out), GITHUB_STEP_SUMMARY=str(summary)))
    assert run.returncode == 0, run.stderr
    outputs = dict(row.split('=', 1) for row in out.read_text().splitlines())
    job = workflow('train')['jobs']['release']
    context = {'inputs': {'automatic': automatic, 'source_sha': 'a'*40},
               'needs': {'train': {'result': train_state, 'outputs': outputs}}}
    assert bool(expression(job['if'], **context)) is expected
    if expected:
        inputs = {key: expression(value, **context) if isinstance(value, str) and value.startswith('${{') else value
                  for key, value in job['with'].items()}
        assert (inputs['model_name'], inputs['model_version']) == ('taxi-model', '42')
        assert json.loads(inputs['training_result'])['mlflow_run_id'] == decision['mlflow_run_id']
        assert json.loads(inputs['training_result'])['artifact_checksum'] == decision['artifact_checksum']
    else:
        assert ('rejected' in summary.read_text()) if not approved else True


@pytest.mark.parametrize('change', [
    {'model_version': None}, {'model_version': 42}, {'model_version': 'latest'}, {'registry_status': 'PENDING_REGISTRATION'},
    {'training_git_sha': '9'*40}, {'artifact_checksum': 'bad'}, {'mlflow_run_id': ''},
    {'source_manifest_sha256': None}, {'validation_mae_minutes': float('nan')},
    {'validation_status': 'rejected'}, {'validation_status': 'unknown'},
])
def test_incomplete_or_untrusted_handoff_never_becomes_approved(decision, change):
    with pytest.raises(ValueError): result.validate_decision(decision | change, 'a'*40)


def test_handoff_revalidated_against_registry_and_bundle(decision, monkeypatch, tmp_path):
    monkeypatch.setattr(result, 'verify', lambda *args: decision.copy())
    assert result.verify_handoff(decision, object(), 'a'*40)['model_version'] == '42'
    monkeypatch.setattr(result, 'verify', lambda *args: decision | {'artifact_checksum': '9'*64})
    with pytest.raises(ValueError, match='differs'): result.verify_handoff(decision, object(), 'a'*40)
    bundle = {key: decision[key] for key in ('model_name','model_version','mlflow_run_id','source_run_id','validation_dataset_key')}
    bundle.update(git_sha='a'*40, files={'model.txt': 'd'*64, 'training_manifest.json': 'e'*64})
    path = tmp_path/'release.json'; path.write_text(json.dumps(bundle))
    result.verify_bundle_handoff(tmp_path, decision)
    bundle['files']['model.txt'] = '0'*64; path.write_text(json.dumps(bundle))
    with pytest.raises(ValueError, match='checksum'): result.verify_bundle_handoff(tmp_path, decision)


def archive(payload, filename='auto-checkpoint.json'):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as z: z.writestr(filename, json.dumps(payload))
    return stream.getvalue()


def checkpoint_fixture(history, env, disposition='completed'):
    commit, base = history
    source = commit('src/training/model.py', 'trained\n')
    run = {'id': 10, 'run_attempt': 1, 'head_sha': source, 'head_branch': 'main', 'event': 'push',
           'path': '.github/workflows/ci.yml', 'head_repository': {'id': 12}, 'status': 'completed'}
    data = {'schema_version': 1, 'run_id': '10', 'run_attempt': '1', 'repository_id': '12',
            'source_sha': source, 'base_sha': base, 'fingerprint': auto.fingerprint(source), 'disposition': disposition}
    blob = archive(data)
    artifact = {'id': 20, 'name': 'auto-mlops-checkpoint-10-1', 'digest': 'sha256:'+hashlib.sha256(blob).hexdigest(),
                'expired': False, 'size_in_bytes': len(blob), 'workflow_run': {'id': 10, 'head_sha': source, 'head_branch': 'main',
                'repository_id': 12, 'head_repository_id': 12}}
    return run, data, blob, artifact


def test_checkpoint_archive_identity_digest_and_no_extraction(history, env):
    run, data, blob, artifact = checkpoint_fixture(history, env)
    assert auto.checkpoint_payload(blob, artifact, run, env)['source_sha'] == run['head_sha']
    with pytest.raises(ValueError, match='checksum'): auto.checkpoint_payload(blob+b'x', artifact, run, env)
    with pytest.raises(ValueError, match='trusted'): auto.checkpoint_payload(blob, artifact, run | {'event': 'pull_request'}, env)
    evil = archive(data, '../execute.py')
    with pytest.raises(ValueError, match='contents'):
        auto.checkpoint_payload(evil, artifact | {'digest': 'sha256:'+hashlib.sha256(evil).hexdigest()}, run, env)
    wrong = archive(data | {'fingerprint': '0'*64})
    with pytest.raises(ValueError, match='fingerprint'):
        auto.checkpoint_payload(wrong, artifact | {'digest': 'sha256:'+hashlib.sha256(wrong).hexdigest()}, run, env)


def test_orphan_intent_blocks_duplicates_and_final_checkpoint_wins(history, env, monkeypatch):
    run, data, blob, artifact = checkpoint_fixture(history, env, 'blocked')
    intent = artifact | {'name': 'auto-mlops-intent-10-1'}
    def api(endpoint, binary=False):
        if endpoint.endswith('/zip'): return blob
        if endpoint.endswith('/runs/10'): return run
        return {'artifacts': [intent]}
    monkeypatch.setattr(auto, 'gh', api)
    assert auto.previous_checkpoint(env, run['head_sha'])['disposition'] == 'blocked'
    done = archive(data | {'disposition': 'completed'})
    final = artifact | {'id': 21, 'digest': 'sha256:'+hashlib.sha256(done).hexdigest()}
    def complete(endpoint, binary=False):
        if endpoint.endswith('/21/zip'): return done
        if endpoint.endswith('/runs/10'): return run
        if '/runs/10/artifacts' in endpoint: return {'artifacts': [intent, final]}
        return {'artifacts': [intent]}
    monkeypatch.setattr(auto, 'gh', complete)
    assert auto.previous_checkpoint(env, run['head_sha'])['disposition'] == 'completed'


def test_expired_checkpoint_fails_closed(history, env, monkeypatch):
    run, _, _, artifact = checkpoint_fixture(history, env)
    monkeypatch.setattr(auto, 'gh', lambda endpoint, **kwargs: run if endpoint.endswith('/runs/10') else {'artifacts': [artifact | {'expired': True}]})
    with pytest.raises(ValueError, match='expired'): auto.previous_checkpoint(env, run['head_sha'])


def test_locks_do_not_nest_the_same_cluster_group_and_intent_precedes_aws():
    ci, train, release = workflow('ci'), workflow('train'), workflow('release')
    outer = ci['jobs']['auto_mlops']['concurrency']
    inner = train['jobs']['train']['concurrency']
    assert inner == release['jobs']['release']['concurrency']
    assert outer['group'] != inner['group']
    assert outer['cancel-in-progress'] is inner['cancel-in-progress'] is False
    assert 'concurrency' not in train['jobs']['release'] and 'concurrency' not in train
    assert train['jobs']['train']['needs'] == ['plan']
    intent = train['jobs']['plan']['steps'][-1]
    assert intent['with']['if-no-files-found'] == 'error'
    assert 'auto-mlops-intent-' in intent['with']['name']
    assert 'id-token' not in train['jobs']['plan']['permissions']


def ingress(host='internal-project.ap-southeast-1.elb.amazonaws.com'):
    return {'metadata': {'name': 'inference-api', 'namespace': 'mlops',
                         'annotations': {'alb.ingress.kubernetes.io/scheme': 'internal'}},
            'status': {'loadBalancer': {'ingress': [{'hostname': host}]}}}


@pytest.mark.parametrize('host', ['google.com', 'project.ap-southeast-1.elb.amazonaws.com',
                                 'internal-project.us-east-1.elb.amazonaws.com', '127.0.0.1',
                                 'internal-project.ap-southeast-1.elb.amazonaws.com/evil'])
def test_traffic_refuses_public_arbitrary_and_wrong_region_hosts(host):
    with pytest.raises(ValueError): traffic.internal_url(ingress(host), 'ap-southeast-1')


def test_traffic_budget_is_isolated_nonroot_and_has_no_aws_identity():
    url = traffic.internal_url(ingress(), 'ap-southeast-1')
    job = traffic.job_spec('m32-traffic-10-1', url, (ROOT/'deploy/monitoring/canary-traffic.js').read_text())
    spec = job['spec']['template']['spec']; container = spec['containers'][0]
    assert job['metadata']['namespace'] == 'mlops-release-traffic'
    assert job['spec']['activeDeadlineSeconds'] == 660 and job['spec']['backoffLimit'] == 0
    assert spec['automountServiceAccountToken'] is False and spec['serviceAccountName'] == 'canary-traffic'
    assert spec['nodeSelector'] == {'eks.amazonaws.com/nodegroup': 'system'}
    assert container['resources']['limits']['cpu'] == '500m'
    assert container['securityContext']['readOnlyRootFilesystem'] is True
    assert not any(e['name'].startswith('AWS_') for e in container['env'])
    assert next(e['value'] for e in container['env'] if e['name']=='K6_NO_USAGE_REPORT') == 'true'
    rbac = list(yaml.safe_load_all((ROOT/'deploy/bootstrap/release-traffic-rbac.yaml').read_text()))
    assert next(x for x in rbac if x['kind']=='ResourceQuota')['spec']['hard']['count/jobs.batch'] == '1'
    role = next(x for x in rbac if x['kind']=='Role')
    assert all('secrets' not in rule['resources'] and '*' not in rule['verbs'] for rule in role['rules'])


@pytest.mark.parametrize('state', [{'failed': 1}, {'succeeded': 1}, {}, {'active': 1, 'conditions':[{'type':'FailureTarget','status':'True'}]}])
def test_failed_or_exhausted_generator_cannot_promote_champion(monkeypatch, state):
    monkeypatch.setattr(verify, 'command_json', lambda *args: {'status': state})
    monkeypatch.setattr(verify, 'assert_current_release', lambda *args: None)
    monkeypatch.setattr(verify, 'verify_registry', lambda *args: None)
    client = SimpleNamespace(set_registered_model_alias=lambda *args: pytest.fail('Must not promote'))
    with pytest.raises(RuntimeError): verify.promote(client, {}, 'commit', 'm32-traffic-10-1')


def test_active_generator_is_not_a_substitute_for_valid_canary_metrics(monkeypatch):
    monkeypatch.setattr(verify, 'current_main', lambda: 'commit')
    monkeypatch.setattr(verify, 'live_state', lambda expected: 'progressing')
    monkeypatch.setattr(verify, 'command_json', lambda *args: {'status': {'active': 1}})
    times = iter([0, 0, 1801])
    monkeypatch.setattr(verify.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(verify.time, 'sleep', lambda seconds: None)
    with pytest.raises(TimeoutError): verify.wait_for_outcome({}, 'commit', traffic_job='m32-traffic-10-1')
    # Existing PromQL behavior also rejects empty/NaN measurements instead of vector(0) success.
    gate = list(yaml.safe_load_all((ROOT/'deploy/gitops/inference/analysis-template.yaml').read_text()))[0]
    for metric in gate['spec']['metrics']:
        assert not expression(metric['successCondition'], result=[float('nan')])
        assert not expression(metric['failureCondition'], result=[float('nan')])
        assert metric['inconclusiveLimit'] == 3
        assert 'vector(0) / vector(0)' in metric['provider']['prometheus']['query']


def test_candidate_abort_still_uses_exit3_path_even_if_load_also_stops(monkeypatch):
    monkeypatch.setattr(verify, 'current_main', lambda: 'commit')
    monkeypatch.setattr(verify, 'live_state', lambda expected: 'aborted')
    monkeypatch.setattr(verify, 'require_traffic', lambda job: pytest.fail('Abort must retain guarded revert classification'))
    assert verify.wait_for_outcome({}, 'commit', traffic_job='m32-traffic-10-1') == 'aborted'
    script = (ROOT/'scripts/m32_release.sh').read_text()
    assert 'if [[ "$outcome" -eq 3 ]]' in script and 'git revert --no-edit "$commit"' in script
    assert script.index('main moved after the release commit') < script.index('git revert --no-edit')
    assert 'champion alias was NOT changed.' in script


def test_cleanup_refuses_other_job_ownership(monkeypatch, tmp_path):
    state = tmp_path/'state.json'; state.write_text(json.dumps({'namespace':traffic.NAMESPACE,'name':'m32-traffic-10-1'}))
    monkeypatch.setattr(traffic, 'kube', lambda *args: {'metadata': {'labels': {'app': 'unrelated'}}})
    with pytest.raises(ValueError, match='ownership'): traffic.stop(state)
    assert state.exists()


def test_rerun_reads_immutable_previous_attempt_checkpoint_instead_of_duplicate_training(history, env, monkeypatch):
    run, _, blob, artifact = checkpoint_fixture(history, env)
    rerun = run | {'run_attempt': 2, 'status': 'in_progress'}
    def api(endpoint, binary=False):
        if endpoint.endswith('/zip'): return blob
        if endpoint.endswith('/runs/10'): return rerun
        return {'artifacts': [artifact]}
    monkeypatch.setattr(auto, 'gh', api)
    checkpoint = auto.previous_checkpoint(env | {'GITHUB_RUN_ID':'10','GITHUB_RUN_ATTEMPT':'2'}, run['head_sha'])
    assert checkpoint['run_attempt'] == '1'
    assert not auto.choose_plan(env, run['head_sha'], checkpoint['source_sha'], run['head_sha'], checkpoint)['train']


def test_plan_persists_intent_and_requires_explicit_baseline(history, env, monkeypatch, tmp_path):
    commit, base = history
    source = commit('src/training/model.py', 'unprocessed changes\n')
    event = tmp_path/'event.json'; event.write_text(json.dumps({'before':base}))
    out, summary = tmp_path/'outputs', tmp_path/'summary'
    run_env = env | {'AUTOMATIC':'true','SOURCE_SHA':source,'AUTO_MLOPS_BASELINE_SHA':base,
                     'GITHUB_EVENT_PATH':str(event),'GITHUB_OUTPUT':str(out),'GITHUB_STEP_SUMMARY':str(summary),
                     'GITHUB_RUN_ID':'10','GITHUB_RUN_ATTEMPT':'1','RUNNER_TEMP':str(tmp_path)}
    original = auto.git
    monkeypatch.setattr(auto, 'git', lambda *args: source+'\trefs/heads/main' if args[0]=='ls-remote' else original(*args))
    monkeypatch.setattr(auto, 'previous_checkpoint', lambda *args: None)
    monkeypatch.setattr(auto, 'assert_no_lost_checkpoint', lambda *args: None)
    monkeypatch.setenv('GITHUB_OUTPUT',str(out))
    auto.plan(run_env)
    intent = json.loads((tmp_path/'auto-intent/auto-checkpoint.json').read_text())
    assert intent['disposition'] == 'blocked' and intent['base_sha'] == base
    with pytest.raises(ValueError, match='BASELINE'): auto.plan(run_env | {'AUTO_MLOPS_BASELINE_SHA':''})


@pytest.mark.parametrize('validation,train_state,release_state,expected', [
    ('approved','success','success','completed'), ('rejected','success','skipped','completed'),
    ('approved','success','failure','blocked'), ('','failure','skipped','blocked'),
])
def test_final_checkpoint_preserves_actual_gate_and_release_outcomes(history, env, monkeypatch, tmp_path,
                                                                    validation, train_state, release_state, expected):
    commit, base = history
    source = commit('src/training/model.py', 'training\n')
    original = auto.git
    monkeypatch.setattr(auto,'git',lambda *args: '' if args[0]=='fetch' else source if args==('rev-parse','origin/main') else original(*args))
    auto.checkpoint(env | {'SOURCE_SHA':source,'BASE_SHA':base,'GITHUB_RUN_ID':'10','GITHUB_RUN_ATTEMPT':'1',
                          'RUNNER_TEMP':str(tmp_path),'VALIDATION_STATUS':validation,
                          'TRAIN_RESULT':train_state,'RELEASE_RESULT':release_state})
    record=json.loads((tmp_path/'auto-checkpoint.json').read_text())
    assert record['disposition']==expected


def test_source_tree_matches_image_build_inventory(history):
    commit, base = history
    source = commit('src/training/model.py', 'model contents\n')
    expected = hashlib.sha256((hashlib.sha256(b'model contents\n').hexdigest()+'  src/training/model.py\n').encode()).hexdigest()
    assert result.source_tree_checksum(source) == expected


def test_live_registry_verification_matches_source_and_rejects_checksum_tamper(request, tmp_path, monkeypatch):
    from mlflow import MlflowClient
    from training.tracking import import_run, TrackingConfig
    monkeypatch.setenv('GIT_SHA', 'a'*40)
    monkeypatch.setenv('SOURCE_TREE_SHA256', 'b'*64)
    training_run = request.getfixturevalue('training_run')
    monkeypatch.chdir(tmp_path)
    uri = f'sqlite:///{tmp_path / "handoff.db"}'
    tracked = import_run(training_run, uri, TrackingConfig('handoff', 'taxi-model', 0.01))
    manifest=json.loads((training_run/'manifest.json').read_text())
    schema=json.loads((training_run/'feature_schema.json').read_text())
    metrics=json.loads((training_run/'metrics.json').read_text())
    raw={'tracking':tracked,'source_run_id':manifest['run_id'],
         'artifact_checksum':hashlib.sha256((training_run/'model.txt').read_bytes()).hexdigest(),
         'source_manifest_sha256':hashlib.sha256((training_run/'manifest.json').read_bytes()).hexdigest(),
         'validation_dataset_key':result.validation_dataset_key(manifest,schema),
         'validation_mae_minutes':metrics['validation']['model']['mae_minutes']}
    client=MlflowClient(tracking_uri=uri)
    monkeypatch.setattr(result,'source_tree_checksum',lambda sha: 'b'*64)
    verified=result.verify(raw,client,'taxi-model','a'*40)
    assert result.validate_decision(verified,'a'*40)
    assert result.verify_handoff(verified,client,'a'*40)['model_version']=='1'
    with pytest.raises(ValueError,match='artifacts'):
        result.verify_handoff(verified | {'artifact_checksum':'0'*64},client,'a'*40)
    monkeypatch.setattr(result,'source_tree_checksum',lambda sha: '9'*64)
    with pytest.raises(ValueError,match='source SHA/tree'):
        result.verify(raw,client,'taxi-model','a'*40)
    monkeypatch.setattr(result,'source_tree_checksum',lambda sha: 'b'*64)
    client.set_model_version_tag('taxi-model','1','git_sha','9'*40)
    with pytest.raises(ValueError,match='Registry source SHA'):
        result.verify(raw,client,'taxi-model','a'*40)
    client.set_model_version_tag('taxi-model','1','git_sha','a'*40)
    client.set_model_version_tag('taxi-model','1','validation_mae_minutes','999')
    with pytest.raises(ValueError,match='Registry validation metric'):
        result.verify(raw,client,'taxi-model','a'*40)


def test_explicit_reviewed_baseline_acknowledges_old_blocked_cursor(history, env, monkeypatch):
    run, _, _, artifact = checkpoint_fixture(history, env, 'blocked')
    monkeypatch.setattr(auto, 'gh', lambda endpoint, **kwargs: {'artifacts': [artifact]})
    assert auto.previous_checkpoint(env | {'AUTO_MLOPS_BASELINE_SHA':run['head_sha']}, run['head_sha']) is None


def test_automatic_handoff_validation_precedes_credentials_and_manual_dispatch_remains():
    release = workflow('release')
    steps = release['jobs']['release']['steps']
    check = next(i for i, s in enumerate(steps) if s.get('name')=='Validate exact automatic model handoff before credentials')
    oidc = next(i for i, s in enumerate(steps) if s.get('uses','').startswith('aws-actions/configure-aws-credentials@'))
    assert check < oidc
    assert set(release['on']) == {'workflow_call','workflow_dispatch'}
    assert set(workflow('train')['on']) == {'workflow_call','workflow_dispatch'}


def test_guarded_load_ready_create_and_cleanup_happen_on_exact_job_only(tmp_path, monkeypatch, capsys):
    state = tmp_path/'traffic.json'
    monkeypatch.setenv('GITHUB_RUN_ID','10'); monkeypatch.setenv('GITHUB_RUN_ATTEMPT','1')
    monkeypatch.setenv('AWS_REGION','ap-southeast-1')
    def read(*args):
        if 'ingress' in args: return ingress()
        if 'pods' in args: return {'items':[{'metadata':{'name':'traffic-pod'},'status':{'phase':'Running'}}]}
        return {'metadata':{'labels':{'app':'m32-canary-traffic','mlops.aws/release-job':'m32-traffic-10-1'}},'status':{'active':1}}
    calls=[]
    def command(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout='CANARY_TRAFFIC_READY\n',stderr='')
    monkeypatch.setattr(traffic,'kube',read)
    monkeypatch.setattr(traffic.subprocess,'run',command)
    monkeypatch.chdir(ROOT)
    traffic.start(state)
    assert capsys.readouterr().out.strip()=='m32-traffic-10-1'
    traffic.stop(state)
    assert not state.exists()
    delete=next(args for args in calls if 'delete' in args)
    assert 'm32-traffic-10-1' in delete
    assert traffic.NAMESPACE in delete and '--all' not in delete


def test_missing_namespace_or_job_creation_failure_preserves_cleanup_handle(tmp_path, monkeypatch):
    state=tmp_path/'traffic.json'
    monkeypatch.setenv('GITHUB_RUN_ID','10'); monkeypatch.setenv('GITHUB_RUN_ATTEMPT','1')
    monkeypatch.setenv('AWS_REGION','ap-southeast-1'); monkeypatch.chdir(ROOT)
    monkeypatch.setattr(traffic,'kube',lambda *args: ingress())
    def fail(*args, **kwargs): raise subprocess.CalledProcessError(1,args[0])
    monkeypatch.setattr(traffic.subprocess,'run',fail)
    with pytest.raises(subprocess.CalledProcessError): traffic.start(state)
    assert json.loads(state.read_text())['name']=='m32-traffic-10-1'


def test_missing_checkpoint_after_cloud_attempt_fails_closed(history, env, monkeypatch):
    commit, base = history
    source = commit('src/training/model.py','training\n')
    run={'id':10,'run_attempt':1,'head_sha':source,'event':'push','head_branch':'main','head_repository':{'id':12}}
    def api(endpoint, **kwargs):
        if '/attempts/' in endpoint: return {'jobs':[{'name':'Automatic MLOps / MLOps Cloud Training','conclusion':'success'}]}
        return {'workflow_runs':[run]}
    monkeypatch.setattr(auto,'gh',api)
    with pytest.raises(ValueError,match='Missing automatic checkpoint'):
        auto.assert_no_lost_checkpoint(env | {'GITHUB_RUN_ID':'20','GITHUB_RUN_ATTEMPT':'1'},source,base)


def test_first_automatic_run_with_no_previous_cloud_job_is_allowed(history, env, monkeypatch):
    _, base = history
    monkeypatch.setattr(auto,'gh',lambda *args, **kwargs: {'workflow_runs':[]})
    auto.assert_no_lost_checkpoint(env | {'GITHUB_RUN_ID':'20','GITHUB_RUN_ATTEMPT':'1'},base,base)


def test_reusable_decision_outputs_and_permissions_are_explicit():
    train = workflow('train')
    for key in ['validation_status','model_name','model_version']:
        assert train['on']['workflow_call']['outputs'][key]['value'] == '${{ jobs.train.outputs.'+key+' }}'
    for name, job in train['jobs'].items():
        assert 'permissions' in job
        if name in ['plan','checkpoint']: assert 'id-token' not in job['permissions']
    job = traffic.job_spec('m32-traffic-10-1','http://internal-project.ap-southeast-1.elb.amazonaws.com','script')
    assert job['spec']['template']['spec']['securityContext']['fsGroup'] == 12345


def test_training_fingerprint_handles_unicode_and_deleted_paths(history, env):
    commit, base = history
    added = commit('src/training/đặc trưng.py','training feature\n')
    assert auto.choose_plan(env,added,base,added)['train']
    subprocess.run(['git','rm','src/training/đặc trưng.py'],check=True,capture_output=True)
    subprocess.run(['git','commit','-qm','delete synthetic feature'],check=True)
    deleted=auto.git('rev-parse','HEAD')
    assert auto.choose_plan(env,deleted,added,deleted)['train']
    assert auto.fingerprint(base)==auto.fingerprint(deleted)
