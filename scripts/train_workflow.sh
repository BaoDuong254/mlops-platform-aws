#!/usr/bin/env bash
# Main-only workflow: build one immutable image, execute one Spot Job, verify without release.
set -Eeuo pipefail
: "${GITHUB_RUN_ID:?}" "${GITHUB_RUN_ATTEMPT:?}" "${RUNNER_TEMP:?}" "${AWS_REGION:?}" "${DATA_BUCKET:?}"
python3 scripts/workflow_guard.py train
export TRAINING_CONFIG="${TRAINING_CONFIG:-configs/training.yaml}"
uv run --locked python scripts/training_result.py validate-config --config "$TRAINING_CONFIG"
account="$(aws sts get-caller-identity --query Account --output text)"
repo="${TRAINING_ECR_REPOSITORY:-$account.dkr.ecr.$AWS_REGION.amazonaws.com/mlops-aws-training-dev}"
sha="$(git rev-parse HEAD)"
tag="train-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${sha:0:12}"
export RUN_SUFFIX="gh-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
job="mlops-training-$RUN_SUFFIX"
tunnel_pid=''
cleanup() {
  local result=$?
  trap - EXIT
  if [[ -n "$tunnel_pid" ]]; then kill "$tunnel_pid" 2>/dev/null || true; wait "$tunnel_pid" 2>/dev/null || true; fi
  # Delete only this run's Job and its Pods; no namespace-wide cleanup.
  if ! kubectl --request-timeout=20s -n mlops delete "job/$job" --ignore-not-found --wait=true --timeout=120s; then
    echo "Training Job cleanup failed: $job" >&2; result=1
  fi
  exit "$result"
}
aws eks update-kubeconfig --name "${EKS_CLUSTER_NAME:-mlops-aws-dev}" --region "$AWS_REGION"
source_tree="$(git ls-files -z src configs pyproject.toml uv.lock deploy/training | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"
docker build -f deploy/training/Dockerfile --build-arg "GIT_SHA=$sha" --build-arg "SOURCE_TREE_SHA256=$source_tree" -t "$repo:$tag" .
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${repo%/*}"
docker push "$repo:$tag"
digest="$(aws ecr describe-images --repository-name "${repo##*/}" --image-ids "imageTag=$tag" --query 'imageDetails[0].imageDigest' --output text)"
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Missing ECR digest' >&2; exit 1; }
export TRAINING_IMAGE="$repo@$digest"
trap cleanup EXIT
python3 scripts/workflow_guard.py train
python3 scripts/m23-render.py deploy/platform/mlops/training-job.yaml.tpl | kubectl --request-timeout=30s create -f -
# Reuse the existing bounded Job status/Spot retry monitor, not a second controller.
source scripts/m23-common.sh
wait_m23_job "$job" 7800
pod="$(kubectl --request-timeout=20s -n mlops get pods -l "job-name=$job" -o json | python3 -c 'import json,sys; p=[x for x in json.load(sys.stdin)["items"] if x.get("status",{}).get("phase")=="Succeeded"]; assert len(p)==1; print(p[0]["metadata"]["name"])')"
kubectl --request-timeout=60s -n mlops logs "$pod" > "$RUNNER_TEMP/training.log"
kubectl -n mlops port-forward --address 127.0.0.1 service/mlflow 15000:5000 > "$RUNNER_TEMP/training-mlflow.log" 2>&1 &
tunnel_pid=$!
ready=false
for _ in $(seq 1 60); do
  kill -0 "$tunnel_pid" 2>/dev/null || break
  if curl -fsS --max-time 2 http://127.0.0.1:15000/health >/dev/null; then ready=true; break; fi
  sleep 2
done
[[ "$ready" == true ]] || { echo 'MLflow tunnel unavailable' >&2; exit 1; }
model_name="$(uv run --locked python -c 'from training.tracking import load_tracking_config; from pathlib import Path; print(load_tracking_config(Path("configs/mlflow.yaml")).registered_model_name)')"
uv run --locked python scripts/training_result.py verify --model-name "$model_name" --log "$RUNNER_TEMP/training.log" \
  --tracking-uri http://127.0.0.1:15000 --output "$RUNNER_TEMP/training-result.json" --expected-source-sha "$SOURCE_SHA"
python3 scripts/workflow_guard.py train
{
  printf 'Training image: `%s`; Job: `%s`\n' "$TRAINING_IMAGE" "$job"
  cat "$RUNNER_TEMP/training-result.json"
} >> "$GITHUB_STEP_SUMMARY"
# Approved and rejected are both valid completed training decisions. Neither promotes/releases.
