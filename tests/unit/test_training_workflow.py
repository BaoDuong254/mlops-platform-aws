"""Cloud training contracts without AWS credentials or developer artifacts."""
import importlib.util
import json
from pathlib import Path

import pytest
from mlflow import MlflowClient
from training.tracking import TrackingConfig, import_run

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('training_result', ROOT/'scripts/training_result.py')
results = importlib.util.module_from_spec(spec)
spec.loader.exec_module(results)


@pytest.mark.parametrize("threshold,status", [(0.01,"approved"),(0.999999,"rejected")])
def test_result_verifies_persisted_artifacts_and_approval(training_run, tmp_path, monkeypatch, threshold, status):
    import hashlib
    from training.gate import validation_dataset_key
    monkeypatch.chdir(tmp_path)
    uri=f'sqlite:///{tmp_path / "tracking.db"}'
    tracking=import_run(training_run,uri,TrackingConfig('quality','quality-model',threshold))
    manifest=json.loads((training_run/'manifest.json').read_text())
    schema=json.loads((training_run/'feature_schema.json').read_text())
    metrics=json.loads((training_run/'metrics.json').read_text())
    payload={'tracking':tracking,'source_run_id':manifest['run_id'],
             'artifact_checksum':hashlib.sha256((training_run/'model.txt').read_bytes()).hexdigest(),
             'source_manifest_sha256':hashlib.sha256((training_run/'manifest.json').read_bytes()).hexdigest(),
             'validation_dataset_key':validation_dataset_key(manifest,schema),
             'validation_mae_minutes':metrics['validation']['model']['mae_minutes']}
    client=MlflowClient(tracking_uri=uri)
    assert results.verify(payload,client,'quality-model')['validation_status']==status
    payload['artifact_checksum']='0'*64
    with pytest.raises(ValueError,match='differs'):
        results.verify(payload,client,'quality-model')
    # An existing version whose approval was changed must not be reported approved by idempotent import.
    if status == 'approved':
        client.set_model_version_tag('quality-model',tracking['model_version'],'validation_status','rejected')
        with pytest.raises(ValueError,match='approved'):
            import_run(training_run,uri,TrackingConfig('quality','quality-model',threshold))


def test_result_extraction_is_exact_and_does_not_accept_partial_logs():
    assert results.extract('log\nMLOPS_TRAINING_RESULT={"tracking":{}}\n')=={'tracking':{}}
    for logs in ('', 'MLOPS_TRAINING_RESULT={}\nMLOPS_TRAINING_RESULT={}\n'):
        with pytest.raises(ValueError,match='exactly one'):
            results.extract(logs)


def test_config_keeps_evaluation_identity():
    results.validate_config(ROOT/'configs/training.yaml')
    with pytest.raises(ValueError,match='committed'):
        results.validate_config(ROOT/'README.md')


def test_job_is_spot_and_workflow_is_digest_pinned_and_cleanup_scoped():
    script=(ROOT/'scripts/train_workflow.sh').read_text()
    assert 'TRAINING_IMAGE="$repo@$digest"' in script
    assert 'wait_m23_job "$job" 7800' in script
    assert 'delete "job/$job"' in script
    assert 'service/mlflow' in script and 'training_result.py verify' in script
    assert 'gh workflow run' not in script and 'set_registered_model_alias' not in script
