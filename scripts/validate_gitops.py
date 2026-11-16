"""Validate rendered objects against pinned Kubernetes OpenAPI and Helm's Rollouts CRDs.

This checks structural schemas offline after download, not admission webhooks or CEL rules.
"""
import argparse
import json
from pathlib import Path

import jsonschema
import yaml


def normalize_schema(value):
    if isinstance(value, list):
        return [normalize_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: normalize_schema(item) for key, item in value.items()}
    # Kubernetes OpenAPI v2 models IntOrString as a string plus format; honor its actual wire types.
    if result.get("format") == "int-or-string" or result.get("x-kubernetes-int-or-string"):
        result.pop("type", None)
        result["anyOf"] = [{"type": "integer"}, {"type": "string"}]
    if result.get("nullable") and isinstance(result.get("type"), str):
        result["type"] = [result["type"], "null"]
    return result


def validate(objects, kubernetes, crds):
    kubernetes = normalize_schema(kubernetes)
    crds = [normalize_schema(crd) for crd in crds]
    schemas = {}
    for schema in kubernetes['definitions'].values():
        for identity in schema.get('x-kubernetes-group-version-kind', []):
            api = (identity['group']+'/' if identity['group'] else '')+identity['version']
            schemas[(api, identity['kind'])] = schema
    for crd in crds:
        if crd and crd.get('kind') == 'CustomResourceDefinition':
            for version in crd['spec']['versions']:
                schemas[(crd['spec']['group']+'/'+version['name'], crd['spec']['names']['kind'])] = version['schema']['openAPIV3Schema']
    count = 0
    for obj in objects:
        if not obj:
            continue
        key=(obj['apiVersion'],obj['kind'])
        if key not in schemas:
            raise ValueError('Missing schema: '+str(key))
        # Kubernetes publishes OpenAPI definitions with internal #/definitions references.
        root={'definitions':kubernetes['definitions'], **schemas[key]}
        jsonschema.Draft4Validator(root).validate(obj)
        count += 1
    return count


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--objects',type=Path,required=True)
    parser.add_argument('--kubernetes-schema',type=Path,required=True)
    parser.add_argument('--rollouts-crds',type=Path,required=True)
    args=parser.parse_args()
    count=validate(yaml.safe_load_all(args.objects.read_text()),json.loads(args.kubernetes_schema.read_text()),yaml.safe_load_all(args.rollouts_crds.read_text()))
    print(f'{count} GitOps objects structurally validated, no missing schemas')


if __name__ == '__main__':
    main()
