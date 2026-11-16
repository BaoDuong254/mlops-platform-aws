"""Validate training configuration and independently verify the Job result in MLflow."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
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


def verify(result: dict, client: MlflowClient, name: str):
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
        mae = expected["validation_mae_minutes"]
        if not isinstance(mae, (int,float)) or not math.isfinite(mae) or mae <= 0:
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
            if version.tags.get("validation_metric_definition") != METRIC_DEFINITION:
                raise ValueError("Registry metric definition differs")
        elif tracking.get("model_version") is not None:
            raise ValueError("Rejected candidate must not have a Registry version")
    return {**expected, **tracking, "model_name": name}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="mode", required=True)
    config=sub.add_parser("validate-config"); config.add_argument("--config",type=Path,required=True)
    check=sub.add_parser("verify"); check.add_argument("--log",type=Path,required=True)
    check.add_argument("--tracking-uri",required=True); check.add_argument("--output",type=Path,required=True)
    check.add_argument("--model-name",default="nyc-taxi-trip-duration")
    args=parser.parse_args()
    if args.mode == "validate-config":
        validate_config(args.config); print("Training configuration validated without changing evaluation identity")
    else:
        result=verify(extract(args.log.read_text()), MlflowClient(tracking_uri=args.tracking_uri), args.model_name)
        args.output.write_text(json.dumps(result,sort_keys=True,indent=2,allow_nan=False)+"\n")
        print(json.dumps(result,sort_keys=True))


if __name__ == "__main__":
    main()
