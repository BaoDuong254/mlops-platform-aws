#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/m23-common.sh"
mode="${1:?Use build, push, tags or resolve}"
purpose="${2:-all}"
case "$purpose" in
  all) purposes=(training mlflow backup) ;;
  training|mlflow|backup) purposes=("$purpose") ;;
  *) echo 'IMAGE_PURPOSE must be all, training, mlflow or backup' >&2; exit 2 ;;
esac
case "$mode" in build|push|tags|resolve) ;; *) echo 'Use build, push, tags or resolve' >&2; exit 2 ;; esac
if [[ "$mode" == tags ]]; then
  for purpose in "${purposes[@]}"; do
    tag="$(image_tag "$purpose")"
    printf '%s %s\n' "$purpose" "$tag"
  done
  exit 0
fi
if [[ "$mode" == resolve ]]; then
  for purpose in "${purposes[@]}"; do
    ref="$(resolve_pushed_image "$(image_url "$purpose")")"
    printf '%s %s\n' "$purpose" "$ref"
  done
  exit 0
fi
require_command docker
if [[ "$mode" == push ]]; then
  require_command aws
  account="$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text)"
  aws ecr get-login-password --profile "$AWS_PROFILE" --region "$AWS_REGION" | \
    docker login --username AWS --password-stdin "$account.dkr.ecr.$AWS_REGION.amazonaws.com"
fi
for purpose in "${purposes[@]}"; do
  tag="$(image_tag "$purpose")"
  local_image="mlops-m23-$purpose:$tag"
  if [[ "$mode" == build ]]; then
    if [[ "$purpose" == training ]]; then
      revision="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || printf unknown)"
      source_tree="$(python3 "$ROOT_DIR/scripts/image_identity.py" fingerprint training)"
      docker build -f "$ROOT_DIR/deploy/training/Dockerfile" --build-arg "GIT_SHA=$revision" \
        --build-arg "SOURCE_TREE_SHA256=$source_tree" -t "$local_image" "$ROOT_DIR"
    else
      docker build -f "$ROOT_DIR/deploy/$purpose/Dockerfile" -t "$local_image" "$ROOT_DIR"
    fi
    printf 'Built %s\n' "$local_image"
    continue
  fi
  # Publishing is distinct from selecting an existing recovery artifact.
  reference_var="${purpose^^}_IMAGE_REF"
  [[ -z "${!reference_var:-}${IMAGE_LOCK_FILE:-}" ]] || {
    echo 'Unset *_IMAGE_REF and IMAGE_LOCK_FILE when publishing; they select existing artifacts.' >&2; exit 2;
  }
  remote_image="$(image_url "$purpose")"
  repository="${remote_image%:*}"
  image_name="${repository#*/}"
  remote_tag="${remote_image##*:}"
  if ! aws ecr describe-images --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --registry-id "$account" --repository-name "$image_name" --image-ids "imageTag=$remote_tag" >/dev/null 2>&1; then
    docker image inspect "$local_image" >/dev/null
    docker tag "$local_image" "$remote_image"
    docker push "$remote_image"
  else
    printf 'Already present (immutable tag): %s\n' "$remote_image"
  fi
  reference="$(resolve_pushed_image "$remote_image")"
  # Preserve digest refs for fresh-cluster recovery; no credentials in the lock.
  lock="${IMAGE_LOCK_OUTPUT:-$ROOT_DIR/artifacts/images/m23-image-lock.json}"
  python3 - "$lock" "$purpose" "$reference" <<'PY'
import json, os, pathlib, sys, tempfile
path=pathlib.Path(sys.argv[1]); path.parent.mkdir(parents=True, exist_ok=True)
doc=json.loads(path.read_text()) if path.exists() else {'schema_version':1,'images':{}}
assert doc['schema_version']==1
doc['images'][sys.argv[2]]={'reference':sys.argv[3]}
with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
    json.dump(doc,stream,indent=2); stream.write('\n'); name=stream.name
os.replace(name,path)
PY
  printf '%s\nImage lock: %s\n' "$reference" "$lock"
done
