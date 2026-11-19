"""Validate training configuration and independently verify the Job result in MLflow."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

from mlflow import MlflowClient
from training.config import load_config
from training.gate import METRIC_DEFINITION, validation_dataset_key


def validate_config(path: Path):
    resolved = path.resolve()
    if resolved.parent != Path("configs").resolve() or resolved.suffix != ".yaml":
        raise ValueError("Training configuration must be a committed configs/*.yaml file")
    candidate, baseline = load_config(path), load_config(Path("configs/training.yaml"))
    fields = ("train_months", "validation_months", "test_months", "seed", "sample_limit_per_month",
              "duration_min_minutes", "duration_max_minutes", "source_base_url")
    if any(getattr(candidate, key) != getattr(baseline, key) for key in fields):
        raise ValueError("Keep dataset/evaluation identity unchanged; tune only model parameters")
    return candidate


def extract(logs: str):
    rows = [json.loads(line.split("=", 1)[1]) for line in logs.splitlines()
            if line.startswith("MLOPS_TRAINING_RESULT=")]
    if len(rows) != 1:
        raise ValueError("Expected exactly one structured result from the successful training Pod")
    return rows[0]


def source_tree_checksum(sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected exact training source SHA")
    paths = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "-z", sha, "--",
                                     "src", "configs", "pyproject.toml", "uv.lock", "deploy/training"]).split(b"\0")
    records = []
    for raw in sorted(p for p in paths if p):
        path = raw.decode()
        if "\n" in path or "\\" in path:
            raise ValueError("Unsupported source path in checksum inventory")
        content = subprocess.check_output(["git", "show", f"{sha}:{path}"])
        records.append(hashlib.sha256(content).hexdigest() + "  " + path + "\n")
    return hashlib.sha256("".join(records).encode()).hexdigest()


def verify(result: dict, client: MlflowClient, name: str, expected_source_sha: str | None = None):
    tracking = result["tracking"]
    status, run_id = tracking["validation_status"], tracking["mlflow_run_id"]
    if status not in {"approved", "rejected"}:
        raise ValueError("Unexpected validation decision")
    run = client.get_run(run_id)
    if run.info.status != "FINISHED" or run.data.tags.get("validation_status") != status:
        raise ValueError("Training run did not finish with the reported approval")
    with tempfile.TemporaryDirectory(prefix="training-verify-") as tmp:
        paths = {key: Path(client.download_artifacts(run_id, "training/"+key, tmp))
                 for key in ("manifest.json", "metrics.json", "feature_schema.json", "model.txt")}
        manifest = json.loads(paths["manifest.json"].read_text())
        metrics = json.loads(paths["metrics.json"].read_text())
        schema = json.loads(paths["feature_schema.json"].read_text())
        expected = {"source_run_id": manifest["run_id"],
                    "source_manifest_sha256": hashlib.sha256(paths["manifest.json"].read_bytes()).hexdigest(),
                    "artifact_checksum": hashlib.sha256(paths["model.txt"].read_bytes()).hexdigest(),
                    "validation_dataset_key": validation_dataset_key(manifest, schema),
                    "validation_mae_minutes": metrics["validation"]["model"]["mae_minutes"]}
        if any(result.get(k) != v for k, v in expected.items()):
            raise ValueError("Job result differs from persisted training artifacts")
        for key in ("source_run_id", "source_manifest_sha256", "validation_dataset_key"):
            if run.data.tags.get(key) != expected[key]:
                raise ValueError("MLflow provenance differs: "+key)
        training_sha = manifest.get("git_sha") or "unknown"
        tree_sha = manifest.get("source_tree_sha256")
        if expected_source_sha is not None and (
            training_sha != expected_source_sha or not re.fullmatch(r"[0-9a-f]{40}", training_sha) or
            not isinstance(tree_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", tree_sha) or
            tree_sha != source_tree_checksum(expected_source_sha)
        ):
            raise ValueError("Training source SHA/tree differs from the admitted CI commit")
        if run.data.tags.get("git_sha") != training_sha:
            raise ValueError("MLflow source SHA differs from manifest")
        mae = expected["validation_mae_minutes"]
        if isinstance(mae, bool) or not isinstance(mae, (int,float)) or not math.isfinite(mae) or mae <= 0:
            raise ValueError("Invalid validation metric")
        if run.data.metrics.get("validation.model.mae_minutes") != mae:
            raise ValueError("MLflow metric differs from artifacts")
        if status == "approved":
            version = client.get_model_version(name, tracking["model_version"])
            if version.status != "READY" or version.run_id != run_id or version.tags.get("validation_status") != "approved":
                raise ValueError("Registry candidate not READY/approved for this run")
            for key in ("source_run_id", "source_manifest_sha256", "validation_dataset_key"):
                if version.tags.get(key) != expected[key]:
                    raise ValueError("Registry provenance differs: "+key)
            if version.tags.get("git_sha") != training_sha:
                raise ValueError("Registry source SHA differs from training manifest")
            if float(version.tags.get("validation_mae_minutes", "nan")) != mae:
                raise ValueError("Registry validation metric differs from persisted artifacts")
            if version.tags.get("validation_metric_definition") != METRIC_DEFINITION:
                raise ValueError("Registry metric definition differs")
        elif tracking.get("model_version") is not None:
            raise ValueError("Rejected candidate must not have a Registry version")
    return {**expected, **tracking, "model_name": name,
            "model_version": str(tracking["model_version"]) if status == "approved" else None,
            "registry_status": "READY" if status == "approved" else None,
            "training_git_sha": training_sha, "source_tree_sha256": tree_sha,
            "validation_metric_definition": METRIC_DEFINITION}


def validate_decision(result, source_sha):
    """Validate a verified, bounded JSON handoff; no `latest` or inferred version."""
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha) or result.get("training_git_sha") != source_sha:
        raise ValueError("Decision training source differs")
    for key in ("artifact_checksum", "source_manifest_sha256", "validation_dataset_key", "source_tree_sha256"):
        if not isinstance(result.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", result[key]):
            raise ValueError("Missing or invalid checksum: " + key)
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", str(result.get("model_name", ""))):
        raise ValueError("Invalid model name")
    if not isinstance(result.get("mlflow_run_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", result["mlflow_run_id"]):
        raise ValueError("Missing MLflow run ID")
    if not isinstance(result.get("source_run_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", result["source_run_id"]):
        raise ValueError("Missing source run identity")
    mae = result.get("validation_mae_minutes")
    if isinstance(mae, bool) or not isinstance(mae, (int, float)) or not math.isfinite(mae) or mae <= 0:
        raise ValueError("Invalid validation metric")
    if result.get("validation_metric_definition") != METRIC_DEFINITION:
        raise ValueError("Invalid metric definition")
    approved = result.get("validation_status") == "approved"
    if approved:
        if (result.get("registry_status") != "READY" or not isinstance(result.get("model_version"), str) or
            not re.fullmatch(r"[1-9][0-9]{0,19}", result["model_version"])):
            raise ValueError("Approved decision lacks exact READY version")
    elif result.get("validation_status") != "rejected" or result.get("model_version") is not None or result.get("registry_status") is not None:
        raise ValueError("Invalid rejected decision")
    return approved


def verify_handoff(result, client, source_sha):
    if not validate_decision(result, source_sha):
        raise ValueError("Cannot release a rejected decision")
    raw = {**result, "tracking": {key: result[key] for key in ("validation_status", "mlflow_run_id", "model_version")}}
    observed = verify(raw, client, result["model_name"], source_sha)
    for key in observed:
        if key in result and observed[key] != result[key]:
            raise ValueError("Release handoff differs from live Registry/artifacts: " + key)
    return observed


def verify_bundle_handoff(bundle, result):
    metadata = json.loads((bundle / "release.json").read_text())
    for key in ("model_name", "model_version", "mlflow_run_id", "source_run_id", "validation_dataset_key"):
        if metadata.get(key) != result.get(key):
            raise ValueError("Exported bundle differs from exact training decision: " + key)
    if (metadata.get("git_sha") != result["training_git_sha"] or
        metadata.get("files", {}).get("model.txt") != result["artifact_checksum"] or
        metadata.get("files", {}).get("training_manifest.json") != result["source_manifest_sha256"]):
        raise ValueError("Exported model/manifest checksum or source differs from training")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="mode", required=True)
    config=sub.add_parser("validate-config"); config.add_argument("--config",type=Path,required=True)
    check=sub.add_parser("verify"); check.add_argument("--log",type=Path,required=True)
    check.add_argument("--tracking-uri",required=True); check.add_argument("--output",type=Path,required=True)
    check.add_argument("--model-name",default="nyc-taxi-trip-duration")
    check.add_argument("--expected-source-sha")
    for mode in ("outputs", "verify-handoff", "verify-bundle"):
        cmd=sub.add_parser(mode); cmd.add_argument("--path", type=Path, required=True)
        cmd.add_argument("--source-sha", required=True)
        if mode == "verify-handoff": cmd.add_argument("--tracking-uri", required=True)
        if mode == "verify-bundle": cmd.add_argument("--bundle", type=Path, required=True)
    admission=sub.add_parser("validate-input"); admission.add_argument("--source-sha", required=True)
    args=parser.parse_args()
    if args.mode == "validate-input":
        result=json.loads(os.environ["TRAINING_RESULT"])
        if not validate_decision(result,args.source_sha):
            raise ValueError("Rejected training cannot release")
        if (result["model_name"],result["model_version"]) != (os.environ["MODEL_NAME"],os.environ["MODEL_VERSION"]):
            raise ValueError("Release model input differs from exact verified training result")
    elif args.mode == "validate-config":
        validate_config(args.config); print("Training configuration validated without changing evaluation identity")
    elif args.mode == "verify":
        result=verify(extract(args.log.read_text()), MlflowClient(tracking_uri=args.tracking_uri), args.model_name, args.expected_source_sha)
        args.output.write_text(json.dumps(result,sort_keys=True,indent=2,allow_nan=False)+"\n")
        print(json.dumps(result,sort_keys=True))
    else:
        result=json.loads(args.path.read_text())
        approved=validate_decision(result,args.source_sha)
        if args.mode == "verify-handoff":
            verify_handoff(result,MlflowClient(tracking_uri=args.tracking_uri),args.source_sha)
        elif args.mode == "verify-bundle":
            verify_bundle_handoff(args.bundle,result)
        else:
            with open(os.environ["GITHUB_OUTPUT"],"a") as out:
                for key in ("validation_status","model_name","model_version"):
                    out.write(f"{key}={result[key] if result[key] is not None else ''}\n")
                out.write("handoff=" + json.dumps(result,sort_keys=True,separators=(",",":"),allow_nan=False) + "\n")
            with open(os.environ["GITHUB_STEP_SUMMARY"],"a") as out:
                out.write(f"\nValidation decision: **{result['validation_status']}**; exact version: `{result['model_version']}`. "
                          + ("Eligible for automatic release; not yet champion.\n" if approved else "No release or champion change.\n"))


if __name__ == "__main__":
    main()
