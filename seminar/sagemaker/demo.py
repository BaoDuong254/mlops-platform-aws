"""Usage: 
    uv run --locked python seminar/sagemaker/demo.py selftest --run-dir <training run dir>
    uv run --locked python seminar/sagemaker/demo.py --profile <p> role|package|deploy|invoke|cleanup
    
Every step saves a JSON file seminar/sagemaker/results.json with the run's parameters and results.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import statistics
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RESULTS = HERE / "results.json"
NAME = "seminar-taxi-duration"
ROLE = "seminar-sagemaker-exec"
INSTANCE = "ml.m5.large"
# Resolved once with sagemaker SDK v2: image_uris.retrieve("sklearn", "ap-southeast-1", "1.4-2").
IMAGE = "121021644041.dkr.ecr.ap-southeast-1.amazonaws.com/sagemaker-scikit-learn:1.4-2-cpu-py3"
SAMPLE = {
    "tpep_pickup_datetime": "2025-04-07T12:00:00",
    "PULocationID": 161,
    "DOLocationID": 237,
}


def save(section: str, values: dict) -> None:
    data = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}
    data.setdefault(section, {}).update(values)
    RESULTS.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({section: values}, indent=2, ensure_ascii=False))


def local_prediction(run_dir: Path) -> tuple[float, float]:
    """Handler prediction vs the project's own LoadedModel on the same request."""
    handler = load_handler()
    from training.model import load_model

    prediction = handler.predict_fn(
        handler.input_fn(json.dumps(SAMPLE), "application/json"),
        handler.model_fn(str(run_dir)),
    )
    return prediction, load_model(run_dir).predict(SAMPLE)


def load_handler() -> ModuleType:
    """Load seminar/sagemaker/inference.py by path: `import inference` would hit src/inference."""
    sys.path.insert(0, str(ROOT / "src"))
    import training.features

    sys.modules["features"] = training.features
    spec = importlib.util.spec_from_file_location(
        "sagemaker_handler", HERE / "inference.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def selftest(args) -> None:
    handler, project = local_prediction(args.run_dir)
    assert abs(handler - project) < 1e-9, (handler, project)
    out, content_type = load_handler().output_fn(handler, "application/json")
    assert (
        content_type == "application/json"
        and json.loads(out)["predicted_duration_minutes"] == handler
    )
    save(
        "selftest",
        {"run_dir": str(args.run_dir), "sample": SAMPLE, "local_prediction": handler},
    )


class Aws:
    def __init__(self, profile: str | None, region: str):
        import boto3

        self.session = boto3.Session(profile_name=profile, region_name=region)
        self.region = region
        self.account = self.session.client("sts").get_caller_identity()["Account"]
        self.bucket = f"seminar-sagemaker-{self.account}-{region}"
        self.sm = self.session.client("sagemaker")

    def role_arn(self) -> str:
        return self.session.client("iam").get_role(RoleName=ROLE)["Role"]["Arn"]


def role(aws: Aws, _args) -> None:
    iam = aws.session.client("iam")
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "sagemaker.amazonaws.com"},
            }
        ],
    }
    try:
        iam.create_role(
            RoleName=ROLE,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Seminar demo SageMaker execution role",
        )
    except iam.exceptions.EntityAlreadyExistsException:
        pass
    iam.attach_role_policy(
        RoleName=ROLE, PolicyArn="arn:aws:iam::aws:policy/AmazonSageMakerFullAccess"
    )
    time.sleep(10)  # IAM propagation before SageMaker assumes the role
    save("steps", {"1_iam_role": ROLE, "1_iam_role_api_calls": 2})


def package(aws: Aws, args) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "model"
        (stage / "code").mkdir(parents=True)
        for name in ("model.txt", "feature_schema.json"):
            shutil.copy2(args.run_dir / name, stage / name)
        shutil.copy2(ROOT / "src/training/features.py", stage / "code/features.py")
        for name in ("inference.py", "requirements.txt"):
            shutil.copy2(HERE / name, stage / "code" / name)
        archive = Path(tmp) / "model.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for path in sorted(stage.rglob("*")):
                tar.add(path, arcname=str(path.relative_to(stage)), recursive=False)
        s3 = aws.session.client("s3")
        try:
            s3.create_bucket(
                Bucket=aws.bucket,
                CreateBucketConfiguration={"LocationConstraint": aws.region},
            )
        except (
            s3.exceptions.BucketAlreadyOwnedByYou,
            s3.exceptions.BucketAlreadyExists,
        ):
            pass
        key = f"{NAME}/model.tar.gz"
        s3.upload_file(str(archive), aws.bucket, key)
        size = archive.stat().st_size
    save(
        "steps",
        {
            "2_s3_bucket": aws.bucket,
            "3_model_artifact": f"s3://{aws.bucket}/{key}",
            "model_tar_gz_bytes": size,
        },
    )


def deploy(aws: Aws, _args) -> None:
    started = time.time()
    aws.sm.create_model(
        ModelName=NAME,
        ExecutionRoleArn=aws.role_arn(),
        PrimaryContainer={
            "Image": IMAGE,
            "ModelDataUrl": f"s3://{aws.bucket}/{NAME}/model.tar.gz",
            "Environment": {
                "SAGEMAKER_PROGRAM": "inference.py",
                "SAGEMAKER_SUBMIT_DIRECTORY": "/opt/ml/model/code",
            },
        },
    )
    aws.sm.create_endpoint_config(
        EndpointConfigName=NAME,
        ProductionVariants=[
            {
                "VariantName": "AllTraffic",
                "ModelName": NAME,
                "InstanceType": INSTANCE,
                "InitialInstanceCount": 1,
            }
        ],
    )
    endpoint_started = time.time()
    aws.sm.create_endpoint(EndpointName=NAME, EndpointConfigName=NAME)
    print(f"Creating endpoint {NAME}; screenshot the console when it is InService...")
    try:
        aws.sm.get_waiter("endpoint_in_service").wait(
            EndpointName=NAME, WaiterConfig={"Delay": 15, "MaxAttempts": 80}
        )
    finally:
        status = aws.sm.describe_endpoint(EndpointName=NAME)
        save(
            "deploy",
            {
                "instance_type": INSTANCE,
                "image": IMAGE,
                "status": status["EndpointStatus"],
                "failure_reason": status.get("FailureReason"),
                "endpoint_created_at": status["CreationTime"].isoformat(),
                "endpoint_create_seconds": round(time.time() - endpoint_started),
                "total_deploy_seconds": round(time.time() - started),
                "deploy_api_calls": 3,
            },
        )


def invoke(aws: Aws, args) -> None:
    runtime = aws.session.client("sagemaker-runtime")
    body = json.dumps(SAMPLE)
    first = json.loads(
        runtime.invoke_endpoint(
            EndpointName=NAME, ContentType="application/json", Body=body
        )["Body"].read()
    )
    latencies = []
    for _ in range(args.requests):
        tick = time.perf_counter()
        runtime.invoke_endpoint(
            EndpointName=NAME, ContentType="application/json", Body=body
        )["Body"].read()
        latencies.append((time.perf_counter() - tick) * 1000)
    latencies.sort()
    result = {
        "sample": SAMPLE,
        "endpoint_prediction": first["predicted_duration_minutes"],
        "requests": args.requests,
        "client_p50_ms": round(statistics.median(latencies), 1),
        "client_p95_ms": round(latencies[int(0.95 * len(latencies)) - 1], 1),
    }
    if args.run_dir:
        local, _ = local_prediction(args.run_dir)
        result["local_prediction"] = local
        result["parity_ok"] = abs(local - result["endpoint_prediction"]) < 1e-6
    
    now = datetime.now(UTC)
    points = aws.session.client("cloudwatch").get_metric_statistics(
        Namespace="AWS/SageMaker",
        MetricName="ModelLatency",
        StartTime=now - timedelta(minutes=30),
        EndTime=now,
        Period=1800,
        ExtendedStatistics=["p95"],
        Dimensions=[
            {"Name": "EndpointName", "Value": NAME},
            {"Name": "VariantName", "Value": "AllTraffic"},
        ],
    )["Datapoints"]
    result["server_model_latency_p95_ms"] = (
        round(points[0]["ExtendedStatistics"]["p95"] / 1000, 2)
        if points
        else "not yet in CloudWatch; rerun invoke in a few minutes"
    )
    save("invoke", result)


def cleanup(aws: Aws, args) -> None:
    try:
        created = aws.sm.describe_endpoint(EndpointName=NAME)["CreationTime"]
    except aws.sm.exceptions.ClientError:
        created = None
    for call, kwargs in (
        (aws.sm.delete_endpoint, {"EndpointName": NAME}),
        (aws.sm.delete_endpoint_config, {"EndpointConfigName": NAME}),
        (aws.sm.delete_model, {"ModelName": NAME}),
    ):
        try:
            call(**kwargs)
        except aws.sm.exceptions.ClientError as error:
            print(f"skip: {error}")
    if created:  # DeleteEndpoint is async; wait so the check below is meaningful
        aws.sm.get_waiter("endpoint_deleted").wait(
            EndpointName=NAME, WaiterConfig={"Delay": 10, "MaxAttempts": 60}
        )
    left = [e["EndpointName"] for e in aws.sm.list_endpoints()["Endpoints"]]
    result: dict[str, object] = {"endpoints_left_in_region": left}
    if created:
        result["endpoint_billed_hours_approx"] = round(
            (datetime.now(UTC) - created).total_seconds() / 3600, 3
        )
    if args.all:
        s3 = aws.session.client("s3")
        for obj in s3.list_objects_v2(Bucket=aws.bucket).get("Contents", []):
            s3.delete_object(Bucket=aws.bucket, Key=obj["Key"])
        s3.delete_bucket(Bucket=aws.bucket)
        iam = aws.session.client("iam")
        iam.detach_role_policy(
            RoleName=ROLE, PolicyArn="arn:aws:iam::aws:policy/AmazonSageMakerFullAccess"
        )
        iam.delete_role(RoleName=ROLE)
        result["bucket_and_role_deleted"] = True
    save("cleanup", result)
    assert not left, f"Endpoints still running (billable): {left}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile")
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument(
        "command",
        choices=("selftest", "role", "package", "deploy", "invoke", "cleanup"),
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="Training run dir with model.txt and feature_schema.json",
    )
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument(
        "--all", action="store_true", help="cleanup: also delete S3 bucket and IAM role"
    )
    args = parser.parse_args()
    if args.command in ("selftest", "package") and not args.run_dir:
        parser.error(f"{args.command} needs --run-dir")
    if args.command == "selftest":
        return selftest(args)
    aws = Aws(args.profile, args.region)
    print(f"Account {aws.account}, region {aws.region}")
    {
        "role": role,
        "package": package,
        "deploy": deploy,
        "invoke": invoke,
        "cleanup": cleanup,
    }[args.command](aws, args)


if __name__ == "__main__":
    main()
