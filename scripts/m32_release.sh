#!/usr/bin/env bash
set -Eeuo pipefail

: "${GITHUB_RUN_ID:?}"
: "${GITHUB_RUN_ATTEMPT:?}"
: "${MODEL_NAME:?}"
: "${MODEL_VERSION:?}"
: "${AWS_REGION:?}"
: "${RUNNER_TEMP:?}"

mlflow_pid=''
inference_pid=''
traffic_state="$RUNNER_TEMP/canary-traffic.json"
traffic_job=''
cleanup() {
  local result=$?
  trap - EXIT
  if ! python3 scripts/release_traffic.py stop --state "$traffic_state"; then result=1; fi
  if [[ -n "$inference_pid" ]]; then kill "$inference_pid" 2>/dev/null || true; wait "$inference_pid" 2>/dev/null || true; fi
  if [[ -n "$mlflow_pid" ]]; then kill "$mlflow_pid" 2>/dev/null || true; wait "$mlflow_pid" 2>/dev/null || true; fi
  exit "$result"
}
trap cleanup EXIT

wait_tunnel() {
  local pid="$1" url="$2" logfile="$3"
  for _ in $(seq 1 60); do
    if ! kill -0 "$pid" 2>/dev/null; then
      cat "$logfile" >&2
      return 1
    fi
    if curl --silent --show-error --fail --max-time 2 "$url" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  cat "$logfile" >&2
  return 1
}

python3 scripts/workflow_guard.py release
release_id="m32-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
serving_sha="$(git rev-parse HEAD)"
bundle="artifacts/releases/${MODEL_NAME}-v${MODEL_VERSION}"
expected="artifacts/m32-expected.json"
account="$(aws sts get-caller-identity --query Account --output text)"
repo="${INFERENCE_ECR_REPOSITORY:-$account.dkr.ecr.${AWS_REGION}.amazonaws.com/mlops-aws-inference-dev}"
export INFERENCE_ECR_REPOSITORY="$repo"
previous="$RUNNER_TEMP/previous-serving.json"

aws eks update-kubeconfig --name "${EKS_CLUSTER_NAME:-mlops-aws-dev}" --region "$AWS_REGION"
uv run --locked python scripts/release_snapshot.py > "$previous"
kubectl -n mlops port-forward --address 127.0.0.1 service/mlflow 15000:5000 >"$RUNNER_TEMP/m32-mlflow-tunnel.log" 2>&1 &
mlflow_pid=$!
wait_tunnel "$mlflow_pid" http://127.0.0.1:15000/health "$RUNNER_TEMP/m32-mlflow-tunnel.log"
export MLFLOW_TRACKING_URI=http://127.0.0.1:15000

if [[ "${AUTOMATIC:-false}" == true ]]; then
  python3 - <<'CHECK'
import json,os
from pathlib import Path
result=json.loads(os.environ["TRAINING_RESULT"])
if (result.get("model_name"),result.get("model_version")) != (os.environ["MODEL_NAME"],os.environ["MODEL_VERSION"]):
    raise ValueError("Release input differs from exact training decision")
Path(os.environ["RUNNER_TEMP"],"approved-training-result.json").write_text(json.dumps(result))
CHECK
  uv run --locked python scripts/training_result.py verify-handoff \
    --path "$RUNNER_TEMP/approved-training-result.json" --source-sha "$SOURCE_SHA" --tracking-uri "$MLFLOW_TRACKING_URI"
fi

uv run --locked python -m inference.export --tracking-uri "$MLFLOW_TRACKING_URI" \
  --model-name "$MODEL_NAME" --model-version "$MODEL_VERSION" --output "$bundle"
if [[ "${AUTOMATIC:-false}" == true ]]; then
  uv run --locked python scripts/training_result.py verify-bundle \
    --path "$RUNNER_TEMP/approved-training-result.json" --source-sha "$SOURCE_SHA" --bundle "$bundle"
fi
uv run --locked python scripts/m32_gitops.py prepare --bundle "$bundle" \
  --release-id "$release_id" --serving-git-sha "$serving_sha" >/dev/null
checksum="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["artifact_checksum"])' "$bundle/release.json")"
tag="m32-v${MODEL_VERSION}-${checksum:0:16}-${serving_sha:0:12}-${release_id}"
docker build -f deploy/inference/Dockerfile \
  --build-arg "RELEASE_DIR=$bundle" --build-arg "SERVING_GIT_SHA=$serving_sha" \
  -t "$repo:$tag" .
docker run --rm --network none "$repo:$tag" python -c \
  'from inference.bundle import validate_release; from pathlib import Path; validate_release(Path("/app/release"))'

aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "${repo%/*}"
docker push "$repo:$tag"
digest=''
for _ in $(seq 1 12); do
  digest="$(aws ecr describe-images --repository-name "${repo##*/}" \
    --image-ids "imageTag=$tag" --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || true)"
  if [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then break; fi
  sleep 5
done
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'ECR did not return an immutable digest' >&2; exit 1; }
image="$repo@$digest"

uv run --locked python scripts/m32_gitops.py render --bundle "$bundle" \
  --image "$image" --candidate-cpu "${CANARY_TEST_CPU:-off}" --expected-output "$expected" >/dev/null
uv run --locked python scripts/m31-validate-manifest.py
python3 scripts/workflow_guard.py release
traffic_job="$(python3 scripts/release_traffic.py start --state "$traffic_state")"
# Main can move while the internal ALB/load Pod starts. Never push a release from stale source.
python3 scripts/workflow_guard.py release
git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
git add deploy/gitops/inference/deployment.yaml deploy/gitops/inference/rollout.yaml
if git diff --cached --quiet; then
  echo 'Manifest unchanged; expected a distinct release-id annotation for this attempt' >&2
  exit 1
fi
git commit -m "m32: release $release_id by digest"
commit="$(git rev-parse HEAD)"
if ! git push origin HEAD:main; then
  echo "Release image exists at $image, but main rejected the single GitOps commit $commit." >&2
  echo 'Review branch protection or concurrent main changes; no champion promotion was attempted.' >&2
  exit 1
fi
{
  echo "Release: $release_id; source: $serving_sha; GitOps commit: $commit"
  echo "Model: $MODEL_NAME version $MODEL_VERSION; checksum: $checksum"
  echo "Serving image: $image"
} >> "$GITHUB_STEP_SUMMARY"

# Argo Rollouts chạy canary (smoke, 10%, 50%) rồi tự promote hoặc abort. Exit 3 = canary bị từ chối.
set +e
uv run --locked python scripts/m32_verify.py --expected "$expected" --commit "$commit" \
  --mlflow-uri "$MLFLOW_TRACKING_URI" --inference-uri http://127.0.0.1:18000 --traffic-job "$traffic_job" --wait-outcome
outcome=$?
set -e
if [[ "$outcome" -eq 3 ]]; then
  echo "Canary for $release_id was rejected; reverting GitOps commit $commit so main matches the stable release." >&2
  git fetch origin main
  if [[ "$(git rev-parse origin/main)" != "$commit" ]]; then
    echo "main moved after the release commit; refusing automatic revert. Stable still serves traffic." >&2
    exit 1
  fi
  git revert --no-edit "$commit"
  revert_commit="$(git rev-parse HEAD)"
  if ! git push origin HEAD:main; then
    echo "Revert $revert_commit was rejected by main. Stable still serves, but Git still names the rejected digest." >&2
    exit 1
  fi
  {
    echo "Canary rejected: $release_id; GitOps revert commit: $revert_commit"
    echo "champion alias was NOT changed."
  } >> "$GITHUB_STEP_SUMMARY"
  uv run --locked python scripts/m32_verify.py --expected "$expected" --commit "$revert_commit" \
    --mlflow-uri "$MLFLOW_TRACKING_URI" --inference-uri http://127.0.0.1:18000 --wait-reverted --previous "$previous"
  echo "Release $release_id failed its canary gates; stable restored and recorded in Git." >&2
  exit 1
fi
[[ "$outcome" -eq 0 ]] || exit "$outcome"
kubectl -n mlops port-forward --address 127.0.0.1 service/inference-api 18000:8000 >"$RUNNER_TEMP/m32-inference-tunnel.log" 2>&1 &
inference_pid=$!
wait_tunnel "$inference_pid" http://127.0.0.1:18000/healthz "$RUNNER_TEMP/m32-inference-tunnel.log"
uv run --locked python scripts/m32_verify.py --expected "$expected" --commit "$commit" \
  --mlflow-uri "$MLFLOW_TRACKING_URI" --inference-uri http://127.0.0.1:18000 --traffic-job "$traffic_job" \
  | tee artifacts/m32-result.json
cat artifacts/m32-result.json >> "$GITHUB_STEP_SUMMARY"
