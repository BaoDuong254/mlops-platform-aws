"""Independent, locale-neutral build input identities and explicit ECR references.

No AWS calls: safe for CI, tag inspection, and recovery on a fresh checkout.
Fingerprints describe local inputs, not reproducible upstream image layers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PURPOSES = ('training', 'mlflow', 'backup')
ECR = re.compile(r'(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/(?P<name>[a-z0-9]+(?:[._/-][a-z0-9]+)*)(?:(?P<sep>@)sha256:(?P<digest>[a-f0-9]{64})|:(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))')


def inputs(purpose, root=ROOT):
    files = {'.dockerignore', f'deploy/{purpose}/Dockerfile'}
    if purpose == 'training':
        files |= {'pyproject.toml', 'uv.lock'}
        for directory in ('src', 'configs'):
            for path in (root / directory).rglob('*'):
                relative = path.relative_to(root)
                # These are the exclusions in the checked-in .dockerignore.
                if '__pycache__' not in relative.parts and path.is_file():
                    files.add(relative.as_posix())
    elif purpose == 'mlflow':
        files |= {'pyproject.toml', 'uv.lock', 'deploy/mlflow/server.py'}
    elif purpose == 'backup':
        files.add('deploy/backup/backup.py')
    else:
        raise ValueError(f'Unknown image purpose: {purpose}')
    return sorted(files, key=lambda path: path.encode('utf-8'))


def fingerprint(purpose, root=ROOT, paths=None):
    result = hashlib.sha256(b'm23-build-inputs-v1\0' + purpose.encode() + b'\0')
    for name in sorted(set(inputs(purpose, root) if paths is None else paths), key=lambda p: p.encode('utf-8')):
        path = root / name
        if path.is_symlink():
            raise ValueError(f'Symlink build input is unsupported: {name}')
        executable = b'1' if path.stat().st_mode & 0o111 else b'0'
        result.update(name.encode() + b'\0' + executable + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    return result.hexdigest()


def parse_reference(reference):
    match = ECR.fullmatch(reference)
    if not match or match['tag'] == 'latest':
        raise ValueError('Use an explicit private ECR repository:tag or repository@sha256:<64 hex>; latest is not allowed')
    return match


def lock_reference(path, purpose):
    doc = json.loads(Path(path).read_text())
    if doc.get('schema_version') != 1:
        raise ValueError('Unsupported image lock schema')
    value = doc['images'][purpose]['reference']
    if not parse_reference(value)['digest']:
        raise ValueError('Image lock entries must be digest-pinned')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['fingerprint', 'tag', 'inputs', 'parse', 'lock-ref'])
    parser.add_argument('value')
    parser.add_argument('purpose', nargs='?', choices=PURPOSES)
    args = parser.parse_args()
    if args.command == 'parse':
        match = parse_reference(args.value)
        image_id = 'imageDigest=sha256:' + match['digest'] if match['digest'] else 'imageTag=' + match['tag']
        print('\t'.join([match['account'], match['region'], match['name'], image_id]))
    elif args.command == 'lock-ref':
        print(lock_reference(args.value, args.purpose))
    elif args.command == 'inputs':
        print('\n'.join(inputs(args.value)))
    else:
        value = fingerprint(args.value)
        print(value if args.command == 'fingerprint' else f'm23-{args.value}-{value}')


if __name__ == '__main__':
    main()
