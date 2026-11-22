#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
case "${1:?infrastructure or gitops}" in
  infrastructure)
    tf_data=''
    trap 'rm -rf "${tf_data:-}"' EXIT
    for layer in bootstrap persistent platform; do
      # Isolated TF_DATA_DIR avoids touching local backend configuration/state during CI validation.
      tf_data="$(mktemp -d)"
      TF_DATA_DIR="$tf_data" terraform -chdir="infra/terraform/$layer" init -backend=false -input=false -lockfile=readonly
      TF_DATA_DIR="$tf_data" terraform -chdir="infra/terraform/$layer" validate
      rm -rf "$tf_data"
      terraform -chdir="infra/terraform/$layer" fmt -check -recursive
    done
    uv run --locked pytest -q tests/unit/test_m23_iac.py tests/unit/test_m23_manifests.py tests/unit/test_m23_backup.py
    ;;
  gitops)
    uv run --locked pytest -q tests/unit/test_m31_manifests.py tests/unit/test_m32_release.py tests/unit/test_m33_config.py tests/unit/test_grafana_history.py tests/unit/test_m4_manifests.py tests/unit/test_m41_addons.py tests/unit/test_m42_canary_tools.py tests/unit/test_m46_release_canary.py tests/unit/test_workflows.py tests/unit/test_auto_mlops.py
    uv run --locked python scripts/m31-validate-manifest.py
    schema_temp="$(mktemp -d)"
    trap 'rm -rf "$schema_temp"' EXIT
    kubectl kustomize deploy/gitops/inference > "$schema_temp/objects.yaml"
    curl -fsSL https://raw.githubusercontent.com/kubernetes/kubernetes/v1.36.0/api/openapi-spec/swagger.json -o "$schema_temp/kubernetes.json"
    helm template argo-rollouts argo-rollouts --repo https://argoproj.github.io/argo-helm --version 2.43.9 --namespace argo-rollouts --include-crds -f deploy/platform/argo-rollouts-values.yaml > "$schema_temp/rollouts.yaml"
    uv run --locked python scripts/validate_gitops.py --objects "$schema_temp/objects.yaml" --kubernetes-schema "$schema_temp/kubernetes.json" --rollouts-crds "$schema_temp/rollouts.yaml"
    uv run --locked python scripts/release_traffic.py render > "$schema_temp/traffic-job.json"
    for objects in deploy/bootstrap/m32-github-release-rbac.yaml deploy/bootstrap/release-traffic-rbac.yaml "$schema_temp/traffic-job.json"; do
      uv run --locked python scripts/validate_gitops.py --objects "$objects" --kubernetes-schema "$schema_temp/kubernetes.json" --rollouts-crds "$schema_temp/rollouts.yaml"
    done
    for script in scripts/*.sh; do bash -n "$script"; done
    python3 -m json.tool deploy/monitoring/inference-load-hpa-dashboard.json >/dev/null
    python3 -m json.tool deploy/monitoring/canary-performance-dashboard.json >/dev/null
    uv run --locked python scripts/check_dashboard_metrics.py
    helm template aws-load-balancer-controller aws-load-balancer-controller --repo https://aws.github.io/eks-charts --version 3.6.2 --namespace kube-system -f deploy/platform/aws-load-balancer-controller-values.yaml --set-string clusterName=mlops-aws-dev --set-string region=ap-southeast-1 --set-string vpcId=vpc-00000000 >/dev/null
    helm template argo-rollouts argo-rollouts --repo https://argoproj.github.io/argo-helm --version 2.43.9 --namespace argo-rollouts -f deploy/platform/argo-rollouts-values.yaml >/dev/null
    helm template m33-prometheus prometheus --repo https://prometheus-community.github.io/helm-charts --version 29.37.1 --namespace monitoring -f deploy/monitoring/prometheus-values.yaml >/dev/null
    helm template m33-grafana grafana --repo https://grafana-community.github.io/helm-charts --version 14.0.1 --namespace monitoring -f deploy/monitoring/grafana-values.yaml >/dev/null
    docker run --rm --network none --read-only --user 12345:12345 --cap-drop ALL --security-opt no-new-privileges \
      --tmpfs /tmp:rw,uid=12345,gid=12345 -e HOME=/tmp -e K6_NO_USAGE_REPORT=true \
      -v "$PWD/deploy/monitoring/canary-traffic.js:/traffic.js:ro" --entrypoint k6 grafana/k6:1.8.2 inspect /traffic.js >/dev/null
    uv run --locked python scripts/check_canary_metrics.py
    bash scripts/workflow_lint.sh
    git diff --check
    ;;
  *) echo 'Unknown check module' >&2; exit 2 ;;
esac
