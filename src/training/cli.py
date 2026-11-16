"""Command-line entry point for download, local training and inference smoke checks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from training.config import load_config
from training.data import download_month
from training.model import load_model, train


def main() -> None:
    parser = argparse.ArgumentParser(description="NYC TLC LightGBM training pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("download", "train"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--config", type=Path, default=Path("configs/training.yaml"))
        sub.add_argument("--input-dir", type=Path, help="Override dataset directory")
        if command == "train":
            sub.add_argument("--result-json", type=Path, help="Write and log a structured cloud-training result")
            sub.add_argument("--output-dir", type=Path, help="Override run artifact parent directory")
            sub.add_argument("--track", action="store_true", help="Import the completed run into MLflow")
            sub.add_argument("--mlflow-config", type=Path, default=Path("configs/mlflow.yaml"))
            sub.add_argument("--tracking-uri", default=os.getenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000"))
    predict_parser = subparsers.add_parser("predict")
    predict_parser.add_argument("--model-dir", type=Path, required=True)
    predict_parser.add_argument("--request", required=True, help="JSON object with pickup time and two location IDs")
    for command in ("import-run", "validate-run"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--run-dir", type=Path, required=True)
        sub.add_argument("--mlflow-config", type=Path, default=Path("configs/mlflow.yaml"))
        sub.add_argument("--tracking-uri", default=os.getenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000"))
    verify_parser = subparsers.add_parser("tracking-verify")
    verify_parser.add_argument("--tracking-uri", default=os.getenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000"))
    args = parser.parse_args()

    if args.command == "predict":
        request = json.loads(args.request)
        print(json.dumps({"predicted_duration_minutes": load_model(args.model_dir).predict(request)}))
        return
    if args.command in ("import-run", "validate-run", "tracking-verify"):
        from training.tracking import import_run, load_tracking_config, preview_gate, verify_tracking

        if args.command == "tracking-verify":
            result = verify_tracking(args.tracking_uri)
        else:
            tracking_config = load_tracking_config(args.mlflow_config)
            result = (
                import_run(args.run_dir, args.tracking_uri, tracking_config)
                if args.command == "import-run"
                else preview_gate(args.run_dir, args.tracking_uri, tracking_config)
            )
        print(json.dumps(result, sort_keys=True))
        return

    config = load_config(args.config)
    config = config.with_paths(
        data_dir=args.input_dir,
        output_dir=getattr(args, "output_dir", None),
    )
    if args.command == "download":
        for month in config.all_months:
            metadata = download_month(config, month)
            print(json.dumps({"period": month, "path": metadata["path"], "sha256": metadata["sha256"]}))
    else:
        run_dir = train(config)
        result = {"run_dir": str(run_dir)}
        if args.track:
            from training.tracking import import_run, load_tracking_config

            result["tracking"] = import_run(run_dir, args.tracking_uri, load_tracking_config(args.mlflow_config))
        if args.result_json:
            import hashlib
            from training.gate import validation_dataset_key
            manifest = json.loads((run_dir / "manifest.json").read_text())
            metrics = json.loads((run_dir / "metrics.json").read_text())
            schema = json.loads((run_dir / "feature_schema.json").read_text())
            result.update(source_run_id=manifest["run_id"],
                          artifact_checksum=hashlib.sha256((run_dir / "model.txt").read_bytes()).hexdigest(),
                          source_manifest_sha256=hashlib.sha256((run_dir / "manifest.json").read_bytes()).hexdigest(),
                          validation_dataset_key=validation_dataset_key(manifest, schema),
                          validation_mae_minutes=metrics["validation"]["model"]["mae_minutes"])
            args.result_json.parent.mkdir(parents=True, exist_ok=True)
            args.result_json.write_text(json.dumps(result, sort_keys=True, allow_nan=False) + "\n")
            print("MLOPS_TRAINING_RESULT=" + json.dumps(result, sort_keys=True, allow_nan=False))
        else:
            print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
