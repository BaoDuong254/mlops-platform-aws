"""No-cloud migration proofs: refuse mismatched/ambiguous artifacts and preserve digests."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import image_identity as identity
import migrate_image_tags as migration

REPO='123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/models'


@pytest.fixture
def history(tmp_path):
    for purpose in identity.PURPOSES:
        for name in identity.inputs(purpose):
            target=tmp_path/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,target)
    def git(*args): return subprocess.check_output(['git','-C',str(tmp_path),*args],text=True).strip()
    git('init','-q','-b','main');git('config','user.name','Offline test');git('config','user.email','test@example.invalid')
    def commit():
        git('add','.');git('commit','-qm','snapshot');return git('rev-parse','HEAD')
    return tmp_path,migration.Git(tmp_path),commit,commit()


def legacy_image(rev,git,digest,suffix=''):
    value=sorted(migration.legacy_hashes(rev,git))[0]
    return {'imageTags':['m23-'+value+suffix],'imageDigest':digest}


def test_config_change_cannot_relabel_old_training_but_auxiliary_images_match(history):
    root,git,commit,base=history
    (root/'configs/training.yaml').write_text('new training config')
    with (root/'.dockerignore').open('a') as stream:stream.write('\nunrelated-handoff/\n')
    current=commit()
    assert migration.matches_source('mlflow',base,git,root)
    assert migration.matches_source('backup',base,git,root)
    assert not migration.matches_source('training',base,git,root)
    images=[legacy_image(base,git,'sha256:'+'a'*64)]
    assert not migration.candidates('training',images,git,root)
    images.append({'imageTags':['train-12-1-'+current[:12]],'imageDigest':'sha256:'+'b'*64})
    assert {row['digest'] for row in migration.candidates('training',images,git,root)}=={'sha256:'+'b'*64}


def test_copy_policy_and_changed_backup_code_refuse_migration(history):
    root,git,commit,base=history
    with (root/'.dockerignore').open('a') as stream:stream.write('\ndeploy/backup/backup.py\n')
    assert not migration.matches_source('backup',base,git,root)
    (root/'.dockerignore').write_bytes(git.content(git.tree(base),'.dockerignore'))
    (root/'deploy/backup/backup.py').write_text('changed backup')
    assert not migration.matches_source('backup',base,git,root)
    assert migration.matches_source('mlflow',base,git,root)


def test_different_digests_with_equivalent_auxiliary_sources_are_not_chosen_by_time(history):
    root,git,commit,base=history
    first=legacy_image(base,git,'sha256:'+'a'*64)
    (root/'configs/training.yaml').write_text('another config');current=commit()
    second=legacy_image(current,git,'sha256:'+'b'*64)
    class ECR:
        region='ap-southeast-1'
        def call(self,*args,**kwargs):return {'imageDetails':[first,second]}
    with pytest.raises(ValueError,match='found 2'):
        migration.plan(['mlflow'],{'mlflow':REPO},ECR(),git,root)


def test_manifest_retag_preserves_digest_and_never_deletes_or_rebuilds():
    manifest=json.dumps({'schemaVersion':2,'mediaType':'application/vnd.oci.image.manifest.v1+json','config':{},'layers':[]})
    digest='sha256:'+hashlib.sha256(manifest.encode()).hexdigest()
    calls=[]
    class ECR:
        def call(self,op,**kwargs):
            calls.append((op,kwargs))
            if op=='batch-get-image':return {'images':[{'imageManifest':manifest}]}
            if op=='describe-images':return {'imageDetails':[{'imageDigest':digest}]}
            return {}
    report={'images':[{'action':'add-tag','registry':'123456789012','repository':'models','url':REPO,'tag':'m23-backup-test','digest':digest}]}
    migration.apply_migration(report,ECR())
    assert [op for op,_ in calls]==['batch-get-image','put-image','describe-images']
    assert calls[1][1]['image_digest']==digest and calls[1][1]['image_manifest']==manifest
    assert report['images'][0]['verified_reference']==REPO+'@'+digest
    calls.clear();report['images'][0]['action']='already-present'
    migration.apply_migration(report,ECR())
    assert not calls


def test_manifest_corruption_is_rejected_before_put():
    class ECR:
        def call(self,op,**kwargs):
            assert op=='batch-get-image'
            return {'images':[{'imageManifest':'{}'}]}
    report={'images':[{'action':'add-tag','registry':'123456789012','repository':'models','digest':'sha256:'+'0'*64}]}
    with pytest.raises(ValueError,match='digest differs'):
        migration.apply_migration(report,ECR())
