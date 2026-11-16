#!/usr/bin/env bash
# Ordered, project-scoped cleanup; called only after platform-down confirmation.
set -Eeuo pipefail
source "$(dirname "$0")/k8s-common.sh"
check_cluster
# Capture only this project's ALB before deleting the Ingress. Never require all account ALBs to vanish.
arn=''
if kubectl -n mlops get ingress inference-api >/dev/null 2>&1; then
  host="$(kubectl -n mlops get ingress inference-api -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
  if [[ -n "$host" ]]; then
    arn="$(aws elbv2 describe-load-balancers --profile "$AWS_PROFILE" --region "$AWS_REGION" --query "LoadBalancers[?DNSName=='$host'].LoadBalancerArn | [0]" --output text)"
    [[ "$arn" == arn:aws:elasticloadbalancing:* ]] || { echo 'Cannot identify the Ingress ALB; inspect before cleanup.' >&2; exit 1; }
    tags="$(aws elbv2 describe-tags --profile "$AWS_PROFILE" --region "$AWS_REGION" --resource-arns "$arn")"
    python3 -c 'import json,sys; tags={x["Key"]:x["Value"] for x in json.load(sys.stdin)["TagDescriptions"][0]["Tags"]}; assert tags.get("Project")=="mlops-platform-aws" and tags.get("elbv2.k8s.aws/cluster")==sys.argv[1], "ALB not owned by project cluster"' "$CLUSTER_NAME" <<< "$tags"
  fi
fi
if kubectl -n argocd get application inference-api >/dev/null 2>&1; then
  kubectl -n argocd get application inference-api -o json | python3 -c 'import json,sys; x=json.load(sys.stdin); assert x["spec"]["source"]["path"]=="deploy/gitops/inference" and x["spec"]["destination"]["namespace"]=="mlops", "Unexpected Application ownership"'
  kubectl -n argocd delete application inference-api --wait=true --timeout=120s
fi
kubectl -n mlops delete ingress inference-api --ignore-not-found --wait=true --timeout=300s
if [[ -n "$arn" ]]; then
  aws elbv2 wait load-balancers-deleted --profile "$AWS_PROFILE" --region "$AWS_REGION" --load-balancer-arns "$arn"
fi
if kubectl get crd rollouts.argoproj.io >/dev/null 2>&1; then
  kubectl -n mlops delete rollout.argoproj.io inference-api --ignore-not-found --wait=true --timeout=300s
fi
bash "$ROOT_DIR/scripts/m33.sh" cleanup
bash "$ROOT_DIR/scripts/m41.sh" cleanup
