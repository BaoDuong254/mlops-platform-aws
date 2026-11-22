"""Build input boundaries and actual shell/ECR selection without cloud mutation."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('image_identity', ROOT / 'scripts/image_identity.py')
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)
REPO = '123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/mlflow'
DIGEST = 'sha256:' + 'a' * 64


@pytest.fixture
def tree(tmp_path):
    for purpose in identity.PURPOSES:
        for name in identity.inputs(purpose):
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
    return tmp_path


def hashes(tree):
    return {purpose: identity.fingerprint(purpose, tree) for purpose in identity.PURPOSES}


def test_build_boundaries_match_docker_copy_and_training_does_not_retag_auxiliary(tree):
    before = hashes(tree)
    (tree / 'configs/training.yaml').write_text('changed training config')
    after = hashes(tree)
    assert before['training'] != after['training']
    assert before['mlflow'] == after['mlflow'] and before['backup'] == after['backup']
    # Training Dockerfile COPYs all src, including inference modules, not just training/*.py.
    (tree / 'src/inference/metrics.py').write_text('changed packaged source')
    assert identity.fingerprint('training', tree) != after['training']


def test_backup_code_only_changes_backup_and_shared_dependencies_affect_consumers(tree):
    before = hashes(tree)
    (tree / 'deploy/backup/backup.py').write_text('changed backup')
    after = hashes(tree)
    assert before['training'] == after['training'] and before['mlflow'] == after['mlflow']
    assert before['backup'] != after['backup']
    (tree / 'uv.lock').write_text('new dependencies')
    deps = hashes(tree)
    assert deps['training'] != after['training'] and deps['mlflow'] != after['mlflow']
    assert deps['backup'] == after['backup']


def test_fingerprint_ignores_order_cache_and_unrelated_files_but_tracks_modes(tree):
    paths = identity.inputs('training', tree)
    before = identity.fingerprint('training', tree, paths)
    assert before == identity.fingerprint('training', tree, list(reversed(paths)) + paths)
    (tree / 'README.md').write_text('unrelated')
    (tree / 'src/training/__pycache__').mkdir()
    (tree / 'src/training/__pycache__/model.cpython.pyc').write_bytes(b'cache')
    assert before == identity.fingerprint('training', tree)
    target = tree / 'src/training/model.py'
    target.chmod(target.stat().st_mode ^ 0o100)
    assert before != identity.fingerprint('training', tree)


def shell(command, env=None):
    clean = {key: value for key, value in os.environ.items()
             if key not in ('IMAGE_TAG', 'IMAGE_LOCK_FILE', 'MLFLOW_IMAGE_REF', 'BACKUP_IMAGE_REF', 'TRAINING_IMAGE_REF')}
    clean.update(env or {})
    return subprocess.run(['bash', '-c', 'source "$1/scripts/m23-common.sh"; ' + command, '_', str(ROOT)],
                          text=True, capture_output=True, env=clean)


def test_tags_are_locale_independent():
    first = shell('image_tag mlflow', {'LC_ALL': 'C'})
    second = shell('image_tag mlflow', {'LC_ALL': 'en_US.UTF-8'})
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout


def test_legacy_tags_and_explicit_digest_override_without_terraform():
    legacy = shell('persistent_value() { echo "$ECR_TEST_REPO"; }; image_url backup',
                   {'IMAGE_TAG': 'm23-legacy', 'ECR_TEST_REPO': REPO})
    assert legacy.returncode == 0 and legacy.stdout.strip() == REPO + ':m23-legacy-backup'
    explicit = shell('persistent_value() { exit 99; }; image_url mlflow',
                     {'MLFLOW_IMAGE_REF': REPO + '@' + DIGEST, 'IMAGE_TAG': 'm23-legacy'})
    assert explicit.returncode == 0 and explicit.stdout.strip() == REPO + '@' + DIGEST


def test_fresh_checkout_lock_selects_independent_digest_refs_without_local_build(tmp_path):
    path = tmp_path / 'lock.json'
    path.write_text(json.dumps({'schema_version': 1, 'images': {
        'mlflow': {'reference': REPO + '@' + DIGEST},
        'backup': {'reference': REPO + '@sha256:' + 'b' * 64}}}))
    for purpose, digest in [('mlflow', DIGEST), ('backup', 'sha256:' + 'b' * 64)]:
        result = shell(f'persistent_value() {{ exit 99; }}; image_url {purpose}', {'IMAGE_LOCK_FILE': str(path)})
        assert result.returncode == 0 and result.stdout.strip() == REPO + '@' + digest


def test_ecr_resolver_checks_exact_tag_or_digest_and_pins_output():
    for reference, image_id in [(REPO + ':m23-legacy', 'imageTag=m23-legacy'),
                                (REPO + '@' + DIGEST, 'imageDigest=' + DIGEST)]:
        result = shell('aws() { printf "%s\\n" "$*" >&2; echo "$ECR_TEST_DIGEST"; }; resolve_pushed_image "$ECR_TEST_REF"',
                       {'ECR_TEST_REF': reference, 'ECR_TEST_DIGEST': DIGEST})
        assert result.returncode == 0 and result.stdout.strip() == REPO + '@' + DIGEST
        assert image_id in result.stderr and '--registry-id 123456789012' in result.stderr
    missing = shell('aws() { return 1; }; resolve_pushed_image "$ECR_TEST_REF"', {'ECR_TEST_REF': REPO + ':missing'})
    assert missing.returncode != 0 and 'IMAGE_LOCK_FILE' in missing.stderr


@pytest.mark.parametrize('reference', [REPO + ':latest', REPO, REPO + '@sha256:bad', 'https://' + REPO + ':v1'])
def test_no_floating_or_invalid_references(reference):
    with pytest.raises(ValueError):
        identity.parse_reference(reference)


def test_build_and_push_use_distinct_tags_and_write_recovery_lock(tmp_path):
    """Execute the real publishing script, replacing only Docker/AWS/Terraform."""
    commands = tmp_path / 'bin'; commands.mkdir()
    events = tmp_path / 'events.jsonl'; lock = tmp_path / 'images.json'
    fake = '''#!/usr/bin/env python3
import json,os,pathlib,sys
name=pathlib.Path(sys.argv[0]).name; args=sys.argv[1:]
with open(os.environ['EVENTS'],'a') as stream: stream.write(json.dumps([name,*args])+'\\n')
if name=='terraform': print(json.dumps({'training':os.environ['TEST_REPO']+'/training','mlflow':os.environ['TEST_REPO']+'/mlflow'}))
elif name=='aws':
    if args[0:2]==['sts','get-caller-identity']: print('123456789012')
    elif args[0:2]==['ecr','get-login-password']: print('offline-test-token')
    elif '--query' in args: print('sha256:'+'a'*64)
    else: sys.exit(1)
elif args and args[0]=='login': sys.stdin.read()
'''
    for command in ('aws', 'docker', 'terraform'):
        path=commands/command; path.write_text(fake); path.chmod(0o755)
    env={k:v for k,v in os.environ.items() if k not in ('IMAGE_TAG','IMAGE_LOCK_FILE','TRAINING_IMAGE_REF','MLFLOW_IMAGE_REF','BACKUP_IMAGE_REF')}
    env.update(PATH=str(commands)+os.pathsep+env['PATH'],EVENTS=str(events),TEST_REPO=REPO,IMAGE_LOCK_OUTPUT=str(lock))
    for mode in ('build','push'):
        subprocess.run(['bash',str(ROOT/'scripts/m23-images.sh'),mode],env=env,text=True,capture_output=True,check=True)
    log=[json.loads(line) for line in events.read_text().splitlines()]
    builds=[line for line in log if line[:2]==['docker','build']]
    assert len(builds)==3
    tags=[line[line.index('-t')+1] for line in builds]
    assert len(set(tag.split(':')[1] for tag in tags))==3
    assert len([line for line in log if line[:2]==['docker','push']])==3
    doc=json.loads(lock.read_text())
    assert doc['schema_version']==1 and set(doc['images'])==set(identity.PURPOSES)
    assert all(entry['reference'].endswith('@'+DIGEST) for entry in doc['images'].values())
    # Publishing backup alone never builds the training image.
    events.unlink()
    subprocess.run(['bash',str(ROOT/'scripts/m23-images.sh'),'build','backup'],env=env,text=True,capture_output=True,check=True)
    log=[json.loads(line) for line in events.read_text().splitlines()]
    assert len(log)==1 and '/deploy/backup/Dockerfile' in log[0][log[0].index('-f')+1]


def test_legacy_latest_rejected_in_tag_inspection():
    result=shell('image_tag backup',{'IMAGE_TAG':'latest'})
    assert result.returncode!=0 and 'not allowed' in result.stderr
