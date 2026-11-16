"""Prepare one offline bundle and one digest-pinned serving manifest update."""

from __future__ import annotations

import argparse
import json
import re
import os
from pathlib import Path

import yaml

from inference.bundle import validate_release

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deploy/gitops/inference/deployment.yaml"
ECR = os.getenv("INFERENCE_ECR_REPOSITORY") or yaml.safe_load(DEPLOYMENT.read_text())["spec"]["template"]["spec"]["containers"][0]["image"].split("@", 1)[0]


def prepare_bundle(bundle: Path, release_id: str, serving_git_sha: str) -> dict:
    if not re.fullmatch(r"m32-[0-9]+-[0-9]+", release_id):
        raise ValueError("Release ID must include the GitHub run ID and attempt")
    if not re.fullmatch(r"[0-9a-f]{40}", serving_git_sha):
        raise ValueError("Serving Git SHA must be a full commit hash")
    metadata, _ = validate_release(bundle)
    if metadata.get("release_id") not in (None, release_id):
        raise ValueError("Bundle belongs to another release")
    metadata["release_id"] = release_id
    metadata["serving_git_sha"] = serving_git_sha
    (bundle / "release.json").write_text(json.dumps(metadata, indent=2, sort_keys=True,
                                                   allow_nan=False) + "\n", encoding="utf-8")
    validate_release(bundle)
    return metadata


def update_manifest(bundle: Path, image: str, deployment_path: Path = DEPLOYMENT, candidate_cpu: str = "off") -> dict:
    if not re.fullmatch(re.escape(ECR) + r"@sha256:[0-9a-f]{64}", image):
        raise ValueError("Serving image must use the inference ECR digest")
    metadata, _ = validate_release(bundle)
    if not re.fullmatch(r"m32-[0-9]+-[0-9]+", metadata.get("release_id", "")) or not re.fullmatch(
            r"[0-9a-f]{40}", metadata.get("serving_git_sha", "")):
        raise ValueError("Bundle lacks M3.2 release identity")
    deployment = yaml.safe_load(deployment_path.read_text(encoding="utf-8"))
    if deployment["metadata"]["name"] != "inference-api" or deployment["metadata"]["namespace"] != "mlops":
        raise ValueError("Unexpected serving Deployment")
    template = deployment["spec"]["template"]
    deployment["metadata"]["labels"]["mlops.aws/model-version"] = metadata["model_version"]
    template["metadata"]["labels"]["mlops.aws/model-version"] = metadata["model_version"]
    annotations = template["metadata"].setdefault("annotations", {})
    annotations["mlops.aws/release-id"] = metadata["release_id"]
    annotations["mlops.aws/artifact-checksum"] = metadata["artifact_checksum"]
    annotations["mlops.aws/mlflow-run-id"] = metadata["mlflow_run_id"]
    annotations["mlops.aws/serving-git-sha"] = metadata["serving_git_sha"]
    template["spec"]["containers"][0]["image"] = image
    if candidate_cpu not in {"off", "100m"}:
        raise ValueError("Candidate CPU test must be off or 100m")
    if candidate_cpu != "off":
        # Only the new workloadRef template changes; stable ReplicaSet resources stay intact.
        resources = template["spec"]["containers"][0]["resources"]
        annotations["mlops.aws/test-original-cpu-request"] = str(resources["requests"]["cpu"])
        annotations["mlops.aws/test-original-cpu-limit"] = str(resources["limits"]["cpu"])
        resources["requests"]["cpu"] = candidate_cpu
        resources["limits"]["cpu"] = candidate_cpu
    elif "mlops.aws/test-original-cpu-request" in annotations:
        resources = template["spec"]["containers"][0]["resources"]
        resources["requests"]["cpu"] = annotations.pop("mlops.aws/test-original-cpu-request")
        resources["limits"]["cpu"] = annotations.pop("mlops.aws/test-original-cpu-limit")

    deployment_path.write_text(yaml.safe_dump(deployment, sort_keys=False), encoding="utf-8")
    if deployment_path == DEPLOYMENT:
        rollout_path = deployment_path.with_name("rollout.yaml")
        rollout = yaml.safe_load(rollout_path.read_text())
        annotations = rollout["metadata"].setdefault("annotations", {})
        for key in ("model_name", "model_version", "release_id", "artifact_checksum", "serving_git_sha", "mlflow_run_id"):
            annotations["mlops.aws/" + key.replace("_", "-")] = metadata[key]
        rollout_path.write_text(yaml.safe_dump(rollout, sort_keys=False), encoding="utf-8")

    return {key: metadata[key] for key in ("release_id", "model_name", "model_version",
                                           "mlflow_run_id", "source_run_id", "git_sha",
                                           "serving_git_sha", "artifact_checksum")} | {"image": image}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--bundle", type=Path, required=True)
    prepare.add_argument("--release-id", required=True)
    prepare.add_argument("--serving-git-sha", required=True)
    render = sub.add_parser("render")
    render.add_argument("--bundle", type=Path, required=True)
    render.add_argument("--image", required=True)
    render.add_argument("--candidate-cpu", choices=("off", "100m"), default="off")
    render.add_argument("--expected-output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "prepare":
        result = prepare_bundle(args.bundle, args.release_id, args.serving_git_sha)
    else:
        result = update_manifest(args.bundle, args.image, candidate_cpu=args.candidate_cpu)
        args.expected_output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                                        encoding="utf-8")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
