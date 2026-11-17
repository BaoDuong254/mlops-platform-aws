"""Workflow separation, fail-closed trust, serialization and pre-traffic identity."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT=Path(__file__).resolve().parents[2]


def test_exactly_three_workflows_and_no_pr_credentials():
    files={p.name for p in (ROOT/'.github/workflows').glob('*.yml')}
    assert files=={'ci.yml','train.yml','release.yml'}
    ci=yaml.safe_load((ROOT/'.github/workflows/ci.yml').read_text())
    assert set(ci['jobs'])=={'infrastructure','training','serving','gitops','ci'}
    assert ci['permissions']=={'contents':'read'}
    assert set(ci['on'])=={'push','pull_request'}
    assert 'always()' in ci['jobs']['ci']['if']
    for job in ci['jobs'].values():
        assert not any('configure-aws-credentials' in x.get('uses','') for x in job['steps'])


def test_cloud_workflows_serialize_and_use_distinct_roles_main_only():
    w={key:yaml.safe_load((ROOT/f'.github/workflows/{key}.yml').read_text()) for key in ('train','release')}
    assert w['train']['concurrency']==w['release']['concurrency']
    assert w['train']['concurrency']['cancel-in-progress'] is False
    for name,workflow in w.items():
        assert set(workflow['on'])=={'workflow_dispatch'}
        job=workflow['jobs'][name]
        assert 'refs/heads/main' in job['if']
        auth=next(x for x in job['steps'] if 'configure-aws-credentials' in x.get('uses',''))
        assert auth['with']['role-to-assume']==('${{ vars.AWS_ROLE_TRAIN }}' if name=='train' else '${{ vars.AWS_ROLE_RELEASE }}')
        close=next(x for x in job['steps'] if 'close --state-file' in x.get('run',''))
        assert 'always()' in close['if']
        assert workflow['permissions']['contents']==('write' if name=='release' else 'read')


def smoke_script():
    smoke=list(yaml.safe_load_all((ROOT/'deploy/gitops/inference/analysis-template.yaml').read_text()))[1]
    return smoke['spec']['metrics'][0]['provider']['job']['spec']['template']['spec']['containers'][0]['command'][-1]


def test_smoke_fails_wrong_candidate_before_prediction(monkeypatch):
    # Execute the checked-in Python smoke itself, with HTTP replaced at its boundary.
    from unittest.mock import patch
    import io
    expected={'model_name':'taxi','model_version':'2','release_id':'m32-4-1','artifact_checksum':'a'*64,'serving_git_sha':'b'*40,'mlflow_run_id':'run'}
    for k,v in expected.items():monkeypatch.setenv('EXPECTED_'+k.upper(),v)
    def response(info):
        def fetch(request,timeout=10):
            url=request if isinstance(request,str) else request.full_url
            body=({'status':'ready'} if url.endswith('/readyz') else info if url.endswith('/model-info') else {'predicted_duration_minutes':5.0,'model_name':'taxi','model_version':'2'})
            return io.BytesIO(json.dumps(body).encode())
        return fetch
    with patch('urllib.request.urlopen',response(expected|{'approval_status':'approved'})):
        exec(compile(smoke_script(),'canary-smoke','exec'),{})
    with patch('urllib.request.urlopen',response(expected|{'approval_status':'approved','release_id':'wrong'})):
        with pytest.raises(AssertionError,match='wrong candidate identity'):
            exec(compile(smoke_script(),'canary-smoke','exec'),{})


def test_client_errors_are_observable_but_do_not_reject_candidate():
    gate=list(yaml.safe_load_all((ROOT/'deploy/gitops/inference/analysis-template.yaml').read_text()))[0]
    query=next(x for x in gate['spec']['metrics'] if x['name']=='error-rate')['provider']['prometheus']['query']
    assert 'status=~"5.."' in query and 'or vector(0)' in query


def test_rollout_expected_identity_matches_the_release_template():
    folder=ROOT/'deploy/gitops/inference'
    d=yaml.safe_load((folder/'deployment.yaml').read_text())['spec']['template']
    r=yaml.safe_load((folder/'rollout.yaml').read_text())
    for key in ('release-id','artifact-checksum','serving-git-sha','mlflow-run-id'):
        assert r['metadata']['annotations']['mlops.aws/'+key]==d['metadata']['annotations']['mlops.aws/'+key]
    args=r['spec']['strategy']['canary']['steps'][2]['analysis']['args']
    assert len(args)==6 and all('metadata.annotations' in x['valueFrom']['fieldRef']['fieldPath'] for x in args)


def test_ordered_cleanup_has_project_guard_before_deletion():
    s=(ROOT/'scripts/canary_cleanup.sh').read_text()
    assert s.index('ALB not owned') < s.index('delete application') < s.index('delete ingress') < s.index('load-balancers-deleted') < s.index('m41.sh')
    assert 'ResourceNotFound' not in s


def test_workflow_guard_rejects_untrusted_repo_and_missing_version():
    spec=importlib.util.spec_from_file_location('guard',ROOT/'scripts/workflow_guard.py')
    guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
    env={'AWS_ACCOUNT_ID':'123456789012','AWS_REGION':'ap-southeast-1','EXPECTED_REPOSITORY_ID':'12','EXPECTED_OWNER_ID':'34','GITHUB_REPOSITORY_ID':'12','GITHUB_REPOSITORY_OWNER_ID':'34','GITHUB_REF':'refs/heads/main','MODEL_NAME':'taxi','MODEL_VERSION':'2','INFERENCE_ECR_REPOSITORY':'123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/mlops-aws-inference-dev'}
    guard.validate('release',env)
    with pytest.raises(ValueError,match='identity'):guard.validate('release',env|{'GITHUB_REPOSITORY_ID':'98'})
    with pytest.raises(ValueError,match='version'):guard.validate('release',env|{'MODEL_VERSION':''})


def test_adoption_accepts_zero_replica_template_only_with_healthy_rollout(monkeypatch):
    spec=importlib.util.spec_from_file_location('adoption',ROOT/'scripts/m32_adoption.py')
    adoption=importlib.util.module_from_spec(spec);spec.loader.exec_module(adoption)
    folder=ROOT/'deploy/gitops/inference'
    deployment=yaml.safe_load((folder/'deployment.yaml').read_text());deployment['spec']['replicas']=0
    service=yaml.safe_load((folder/'service.yaml').read_text())
    rollout={'status':{'phase':'Healthy','stableRS':'s','currentPodHash':'s','readyReplicas':1}}
    monkeypatch.setattr(adoption,'_live',lambda kind,name: {'deployment':deployment,'service':service,'rollout.argoproj.io':rollout}[kind])
    adoption.check()
    rollout['status']['phase']='Degraded'
    with pytest.raises(ValueError,match='Rollout'):adoption.check()


@pytest.mark.parametrize("workflow,job", [("train", "train"), ("release", "release")])
@pytest.mark.parametrize("identity,sts_exit,expected_exit", [
    ("111111111111", 0, 0),
    ("222222222222", 0, 1),
    ("", 42, 42),
])
def test_account_guard_fails_closed_before_eks_mutation(tmp_path, workflow, job,
                                                     identity, sts_exit, expected_exit):
    document = yaml.safe_load((ROOT / ".github/workflows" / f"{workflow}.yml").read_text())
    steps = document["jobs"][job]["steps"]
    auth_index = next(i for i, step in enumerate(steps)
                      if step.get("uses", "").startswith("aws-actions/configure-aws-credentials@"))
    guard_index = next(i for i, step in enumerate(steps)
                       if step.get("name") == "Verify authenticated AWS account")
    open_index = next(i for i, step in enumerate(steps)
                      if step.get("name") == "Admit runner IPv4 only")
    assert auth_index < guard_index < open_index
    assert "allowed-account-ids" not in steps[auth_index].get("with", {})
    aws = tmp_path / "aws"
    aws.write_text(f"#!/bin/sh\nprintf '%s\\n' '{identity}'\nexit {sts_exit}\n")
    aws.chmod(0o700)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", AWS_ACCOUNT_ID="111111111111")
    result = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
                             steps[guard_index]["run"]], env=env, capture_output=True, text=True)
    assert result.returncode == expected_exit
