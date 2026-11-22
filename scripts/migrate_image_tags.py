"""Migrate verified legacy/CI artifacts to independent fingerprint tags on ECR.

Default is a read-only plan. --apply adds missing tags to the same manifest and
never removes/overwrites a tag, rebuilds layers, or selects the newest image.
Source provenance is checked against this repository's original M23 and CI tag
contracts. Requires trusted Git history and the project's own ECR repositories.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import locale
import os
from pathlib import Path
import re
import subprocess

import image_identity as identity

ROOT = identity.ROOT


class Git:
    def __init__(self, root=ROOT):
        self.root, self.trees, self.blobs = root, {}, {}

    def command(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args])

    def tree(self, revision):
        if revision not in self.trees:
            result = {}
            for row in self.command('ls-tree', '-r', '-z', revision).split(b'\0'):
                if row:
                    metadata, path = row.split(b'\t', 1)
                    mode, kind, oid = metadata.decode().split()
                    if kind == 'blob': result[path.decode()] = (int(mode, 8), oid)
            self.trees[revision] = result
        return self.trees[revision]

    def content(self, tree, path):
        oid = tree[path][1]
        if oid not in self.blobs:
            self.blobs[oid] = self.command('cat-file', 'blob', oid)
        return self.blobs[oid]


def revision_paths(purpose, tree):
    paths = {'.dockerignore', f'deploy/{purpose}/Dockerfile'}
    if purpose == 'backup': paths.add('deploy/backup/backup.py')
    if purpose == 'mlflow': paths.update(['deploy/mlflow/server.py', 'pyproject.toml', 'uv.lock'])
    if purpose == 'training':
        paths.update(['pyproject.toml', 'uv.lock'])
        paths.update(path for path in tree if path.startswith(('src/', 'configs/')) and '__pycache__' not in Path(path).parts)
    return paths


def relevant_ignore_rules(content, paths):
    """Conservative check: policy changes unrelated to COPY inputs are harmless.

fnmatch is broader than Docker's single-star slash matching, so uncertain policy
changes cause a refusal, not an unsafe migration. Also compare ancestor paths.
"""
    candidates = set(paths)
    for path in paths:
        candidates.update(str(parent) for parent in Path(path).parents if str(parent) != '.')
    relevant = []
    for line in content.decode().splitlines():
        rule = line.strip()
        if not rule or rule.startswith('#') or rule == '.': continue
        pattern = rule.removeprefix('!').strip('/')
        variants = [pattern, pattern.removeprefix('**/')]
        if any(fnmatch.fnmatchcase(path, variant) for path in candidates for variant in variants):
            relevant.append(rule)
    return relevant


def matches_source(purpose, revision, git, root=ROOT):
    tree = git.tree(revision)
    paths = set(identity.inputs(purpose, root))
    if paths != revision_paths(purpose, tree) or not paths <= tree.keys(): return False
    for path in paths - {'.dockerignore'}:
        local = root / path
        if local.is_symlink() or (tree[path][0] & 0o170000) != 0o100000: return False
        if bool(local.stat().st_mode & 0o111) != bool(tree[path][0] & 0o111): return False
        if local.read_bytes() != git.content(tree, path): return False
    copied = paths - {'.dockerignore', f'deploy/{purpose}/Dockerfile'}
    return relevant_ignore_rules((root/'.dockerignore').read_bytes(), copied) == relevant_ignore_rules(git.content(tree, '.dockerignore'), copied)


def legacy_hashes(revision, git):
    tree = git.tree(revision)
    fixed = ['pyproject.toml', 'uv.lock']
    suffix = ['deploy/training/Dockerfile', 'deploy/mlflow/Dockerfile', 'deploy/mlflow/server.py', 'deploy/backup/Dockerfile', 'deploy/backup/backup.py']
    groups = [[p for p in tree if re.fullmatch(r'configs/[^/]+\.yaml', p)],
              [p for p in tree if re.fullmatch(r'src/training/[^/]+\.py', p)]]
    if not set(fixed+suffix) <= tree.keys() or not all(groups): return set()
    hashes = set(); previous = locale.setlocale(locale.LC_COLLATE)
    try:
        for collate in ('C', 'C.UTF-8', 'en_US.UTF-8'):
            try: locale.setlocale(locale.LC_COLLATE, collate)
            except locale.Error: continue
            paths = fixed + [path for group in groups for path in sorted(group, key=locale.strxfrm)] + suffix
            if any('\n' in path or '\\' in path for path in paths): continue
            records = ''.join(hashlib.sha256(git.content(tree, path)).hexdigest()+'  '+path+'\n' for path in paths)
            hashes.add(hashlib.sha256(records.encode()).hexdigest())
    finally: locale.setlocale(locale.LC_COLLATE, previous)
    return hashes


def candidates(purpose, images, git, root=ROOT):
    # All history is bounded and inspected without choosing by push time.
    revisions = git.command('rev-list', '--all', '--max-count=512').decode().splitlines()
    found = []
    for image in images:
        for tag in image.get('imageTags', []):
            legacy = re.fullmatch(r'm23-([a-f0-9]{64})' + ('-backup' if purpose=='backup' else ''), tag)
            ci = re.fullmatch(r'train-\d+-\d+-([a-f0-9]{12})', tag) if purpose=='training' else None
            if not legacy and not ci: continue
            for revision in revisions:
                if ci and not revision.startswith(ci[1]): continue
                if not matches_source(purpose, revision, git, root): continue
                if legacy and legacy[1] not in legacy_hashes(revision, git): continue
                found.append({'digest':image['imageDigest'], 'source_tag':tag, 'source_revision':revision,
                              'provenance':'CI source SHA' if ci else 'recomputed legacy source hash',
                              'inputs_verified':True, 'copy_ignore_policy_verified':True})
                break
    return found


class ECR:
    def __init__(self, profile, region): self.profile, self.region = profile, region

    def call(self, operation, **values):
        args = ['aws', 'ecr', operation, '--profile', self.profile, '--region', self.region,
                '--cli-connect-timeout', '5', '--cli-read-timeout', '20', '--output', 'json']
        for key,value in values.items(): args += ['--'+key.replace('_','-'), value]
        return json.loads(subprocess.check_output(args, text=True))


def plan(purposes, repositories, ecr, git, root=ROOT):
    result=[]; inventories={}
    for purpose in purposes:
        repository = repositories['mlflow' if purpose=='backup' else purpose]
        match=identity.parse_reference(repository+':placeholder')
        if match['region'] != ecr.region: raise ValueError('Repository region differs from AWS region')
        name=match['name']; registry=match['account']
        if name not in inventories:
            inventories[name]=ecr.call('describe-images',registry_id=registry,repository_name=name,filter='tagStatus=TAGGED')['imageDetails']
        tag='m23-'+purpose+'-'+identity.fingerprint(purpose,root)
        existing=next((image for image in inventories[name] if tag in image.get('imageTags',[])),None)
        if existing:
            proof={'digest':existing['imageDigest'],'provenance':'existing exact fingerprint tag'}
            action='already-present'
        else:
            verified=candidates(purpose,inventories[name],git,root)
            digests={item['digest'] for item in verified}
            if len(digests)!=1:
                raise ValueError(f'{purpose}: expected exactly one proven source-equivalent image, found {len(digests)}. Build/push this purpose; no arbitrary fallback is allowed.')
            proof=sorted(verified,key=lambda item:(item['source_tag'],item['source_revision']))[0]
            action='add-tag'
        result.append({'purpose':purpose,'registry':registry,'repository':name,'url':repository,
                       'tag':tag,'action':action,**proof})
    return {'schema_version':1,'images':result}


def apply_migration(report,ecr):
    for item in report['images']:
        if item['action']=='already-present': continue
        source=ecr.call('batch-get-image', registry_id=item['registry'],repository_name=item['repository'],image_ids='imageDigest='+item['digest'])
        if len(source.get('images',[]))!=1 or source.get('failures'): raise ValueError('Cannot retrieve exact source manifest')
        image=source['images'][0]; manifest=image['imageManifest']
        if 'sha256:'+hashlib.sha256(manifest.encode()).hexdigest()!=item['digest']: raise ValueError('Source manifest digest differs; refusing migration')
        media=image.get('imageManifestMediaType') or json.loads(manifest)['mediaType']
        ecr.call('put-image',registry_id=item['registry'],repository_name=item['repository'],
                 image_manifest=manifest,image_manifest_media_type=media,image_digest=item['digest'],image_tag=item['tag'])
        saved=ecr.call('describe-images',registry_id=item['registry'],repository_name=item['repository'],image_ids='imageTag='+item['tag'])['imageDetails'][0]
        if saved['imageDigest']!=item['digest']: raise ValueError('Migrated tag digest verification failed')
        item['verified_reference']=item['url']+'@'+item['digest']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--purpose',nargs='+',choices=identity.PURPOSES,default=list(identity.PURPOSES))
    parser.add_argument('--profile',default=os.getenv('AWS_PROFILE','mlops-dev'))
    parser.add_argument('--region',default=os.getenv('AWS_REGION','ap-southeast-1'))
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--report',default='artifacts/images/migration-report.json')
    args=parser.parse_args()
    repos=json.loads(subprocess.check_output(['terraform','-chdir='+str(ROOT/'infra/terraform/persistent'),'output','-json','ecr_repository_urls'],text=True))
    ecr=ECR(args.profile,args.region)
    report=plan(args.purpose,repos,ecr,Git())
    if args.apply: apply_migration(report,ecr)
    report['applied']=args.apply
    path=ROOT/args.report;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__': main()
