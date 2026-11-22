#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/k8s-common.sh"
PERSISTENT_DIR="$ROOT_DIR/infra/terraform/persistent"
M23_DIR="$ROOT_DIR/deploy/platform/mlops"

persistent_value() {
  local output="$1" key="$2"
  terraform -chdir="$PERSISTENT_DIR" output -json "$output" | python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$key"
}

# Resolution priority: explicit per-image ref > legacy tag > explicit lock > fingerprint.
image_tag() {
  local purpose="${1:-training}"
  case "$purpose" in training|mlflow|backup) ;; *) echo "Unknown image purpose: $purpose" >&2; return 2 ;; esac
  if [[ -n "${IMAGE_TAG:-}" ]]; then
    [[ "$IMAGE_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ && "$IMAGE_TAG" != latest ]] || { echo 'Use an explicit valid IMAGE_TAG; latest is not allowed' >&2; return 1; }
    printf '%s\n' "$IMAGE_TAG"
  else
    python3 "$ROOT_DIR/scripts/image_identity.py" tag "$purpose"
  fi
}

image_url() {
  local purpose="$1" reference_var repository tag suffix=''
  reference_var="${purpose^^}_IMAGE_REF"
  if [[ -n "${!reference_var:-}" ]]; then
    python3 "$ROOT_DIR/scripts/image_identity.py" parse "${!reference_var}" >/dev/null || return
    printf '%s\n' "${!reference_var}"
    return
  fi
  if [[ -z "${IMAGE_TAG:-}" && -n "${IMAGE_LOCK_FILE:-}" ]]; then
    python3 "$ROOT_DIR/scripts/image_identity.py" lock-ref "$IMAGE_LOCK_FILE" "$purpose"
    return
  fi
  repository="$purpose"
  if [[ "$purpose" == backup ]]; then
    repository=mlflow
    # The historical M23 bundle stored backup under <shared-tag>-backup.
    if [[ -n "${IMAGE_TAG:-}" ]]; then suffix=-backup; fi
  fi
  repository="$(persistent_value ecr_repository_urls "$repository")" || return
  tag="$(image_tag "$purpose")" || return
  printf '%s:%s%s\n' "$repository" "$tag" "$suffix"
}

resolve_pushed_image() {
  local image="$1" account region name image_id digest
  local fields
  fields="$(python3 "$ROOT_DIR/scripts/image_identity.py" parse "$image")" || return
  IFS=$'\t' read -r account region name image_id <<< "$fields"
  if ! digest="$(aws ecr describe-images --profile "$AWS_PROFILE" --region "$region" \
    --registry-id "$account" --repository-name "$name" --image-ids "$image_id" \
    --query 'imageDetails[0].imageDigest' --output text)"; then
    printf 'Cannot resolve ECR image: %s\nIf this is a legacy image, run make cloud-images-migrate-plan then make cloud-images-migrate. Otherwise build/push the changed image purpose. Explicit *_IMAGE_REF, IMAGE_LOCK_FILE and legacy IMAGE_TAG remain optional recovery overrides.\n' "$image" >&2
    return 1
  fi
  [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo 'ECR returned no valid digest' >&2; return 1; }
  printf '%s@%s\n' "${image%%[:@]*}" "$digest"
}

require_pushed_image() {
  resolve_pushed_image "$1" >/dev/null
}

render() {
  python3 "$ROOT_DIR/scripts/m23-render.py" "$1"
}

require_mlops_ready() {
  check_cluster
  kubectl -n mlops get statefulset postgres >/dev/null
  kubectl -n mlops rollout status statefulset/postgres --timeout=10m
}

wait_m23_job() {
  local name="$1" timeout_seconds="$2" deadline status next_report=0 connection_failures=0
  deadline=$((SECONDS + timeout_seconds))
  while (( SECONDS < deadline )); do
    if ! status="$(kubectl -n mlops --request-timeout=15s get job "$name" \
      -o jsonpath='{range .status.conditions[*]}{.type}={.status}{" "}{end}' 2>&1)"; then
      if [[ "$status" == *NotFound* ]]; then
        echo "Job $name no longer exists: $status" >&2
        return 1
      fi
      ((connection_failures += 1))
      echo "Cannot query Job $name ($connection_failures/6); retrying: $status" >&2
      if (( connection_failures >= 6 )); then
        echo "Kubernetes API remains unreachable; Job may still be running. Inspect it before retrying the command." >&2
        return 1
      fi
      sleep 10
      continue
    fi
    connection_failures=0
    if [[ "$status" == *'Complete=True'* ]]; then return 0; fi
    if [[ "$status" == *'Failed=True'* ]]; then break; fi
    if (( SECONDS >= next_report )); then
      kubectl -n mlops --request-timeout=15s get job "$name" \
        -o jsonpath='Waiting for Job {.metadata.name}: active={.status.active}, failed={.status.failed}, succeeded={.status.succeeded}{"\n"}' >&2 || true
      next_report=$((SECONDS + 30))
    fi
    sleep 10
  done
  echo "Job $name failed or timed out (conditions: $status)" >&2
  kubectl -n mlops describe "job/$name" >&2 || true
  kubectl -n mlops logs "job/$name" --all-containers=true >&2 || true
  return 1
}

m23_job_logs() {
  local name="$1" attempt
  for attempt in {1..6}; do
    if kubectl -n mlops --request-timeout=20s logs "job/$name" --all-containers=true; then
      return 0
    fi
    echo "Cannot read logs for Job $name ($attempt/6); retrying." >&2
    sleep 5
  done
  echo "Job $name completed, but logs are unavailable; inspect it before retrying the command." >&2
  return 1
}
