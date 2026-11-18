"""Bounded internal-ALB traffic Job; no AWS credentials, ConfigMap or public endpoint."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

NAMESPACE = "mlops-release-traffic"


def kube(*args):
    result = subprocess.run(["kubectl", "--request-timeout=20s", *args], check=True,
                            capture_output=True, text=True)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def internal_url(ingress, region):
    annotations = ingress.get("metadata", {}).get("annotations", {})
    if (ingress["metadata"]["name"] != "inference-api" or ingress["metadata"]["namespace"] != "mlops" or
        annotations.get("alb.ingress.kubernetes.io/scheme") != "internal"):
        raise ValueError("Not the project's internal inference Ingress")
    entries = ingress.get("status", {}).get("loadBalancer", {}).get("ingress", [])
    if len(entries) != 1 or not re.fullmatch(
        r"internal-[a-z0-9-]+\." + re.escape(region) + r"\.elb\.amazonaws\.com", entries[0].get("hostname", "")
    ):
        raise ValueError("Expected one internal ALB hostname in the configured region")
    return "http://" + entries[0]["hostname"]


def job_spec(name, url, script):
    if not re.fullmatch(r"m32-traffic-[0-9]+-[0-9]+", name):
        raise ValueError("Unsafe traffic Job name")
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"app": "m32-canary-traffic", "mlops.aws/release-job": name}},
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 660, "ttlSecondsAfterFinished": 300,
                     "template": {"metadata": {"labels": {"app": "m32-canary-traffic"}}, "spec": {
                         "serviceAccountName": "canary-traffic", "automountServiceAccountToken": False,
                         "restartPolicy": "Never", "nodeSelector": {"eks.amazonaws.com/nodegroup": "system"},
                         "securityContext": {"runAsNonRoot": True, "runAsUser": 12345, "runAsGroup": 12345, "fsGroup": 12345,
                                             "seccompProfile": {"type": "RuntimeDefault"}},
                         "containers": [{"name": "k6", "image": "grafana/k6:1.8.2",
                             "command": ["/bin/sh", "-ec"],
                             "args": ['printf \'%s\\n\' "$TRAFFIC_SCRIPT" | k6 run -'],
                             "env": [{"name": "TRAFFIC_SCRIPT", "value": script},
                                     {"name": "LOAD_BASE_URL", "value": url},
                                     {"name": "K6_NO_USAGE_REPORT", "value": "true"},
                                     {"name": "HOME", "value": "/tmp"}],
                             "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                 "capabilities": {"drop": ["ALL"]}},
                             "resources": {"requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "64Mi"},
                                           "limits": {"cpu": "500m", "memory": "512Mi", "ephemeral-storage": "256Mi"}},
                             "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}],
                         "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]}}}}


def start(state_path):
    name = f"m32-traffic-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    ingress = kube("-n", "mlops", "get", "ingress", "inference-api", "-o", "json")
    url = internal_url(ingress, os.environ["AWS_REGION"])
    spec = job_spec(name, url, Path("deploy/monitoring/canary-traffic.js").read_text())
    # Save a recovery handle before create; cleanup after interrupted/ambiguous responses uses exact name+labels.
    if state_path.exists():
        raise FileExistsError("Traffic recovery state already exists")
    state_path.write_text(json.dumps({"namespace": NAMESPACE, "name": name}) + "\n")
    subprocess.run(["kubectl", "--request-timeout=30s", "create", "-f", "-"],
                   input=json.dumps(spec), text=True, check=True, capture_output=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = kube("-n", NAMESPACE, "get", "job", name, "-o", "json")
        if job.get("status", {}).get("failed", 0):
            raise RuntimeError("Traffic generator failed before GitOps release")
        pods = kube("-n", NAMESPACE, "get", "pods", "-l", "job-name=" + name, "-o", "json")["items"]
        for pod in pods:
            if pod.get("status", {}).get("phase") == "Running":
                logs = subprocess.run(["kubectl", "--request-timeout=15s", "-n", NAMESPACE, "logs",
                                       pod["metadata"]["name"], "--tail=30"], capture_output=True, text=True)
                if logs.returncode == 0 and "CANARY_TRAFFIC_READY" in logs.stdout:
                    print(name)
                    return
        time.sleep(2)
    raise TimeoutError("Traffic generator not ready; refusing GitOps release")


def stop(state_path):
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text())
    if state.get("namespace") != NAMESPACE or not re.fullmatch(r"m32-traffic-[0-9]+-[0-9]+", state.get("name", "")):
        raise ValueError("Unsafe cleanup handle")
    job = kube("-n", NAMESPACE, "get", "job", state["name"], "--ignore-not-found", "-o", "json")
    if not job:
        state_path.unlink()
        return
    labels = job.get("metadata", {}).get("labels", {})
    if labels.get("app") != "m32-canary-traffic" or labels.get("mlops.aws/release-job") != state["name"]:
        raise ValueError("Refusing to delete a Job without the exact project/run ownership labels")
    # Logs may be unavailable if the API/runner failed. Deletion failure must still fail the workflow.
    logs = subprocess.run(["kubectl", "--request-timeout=20s", "-n", NAMESPACE, "logs",
                           "job/" + state["name"], "--tail=200"], capture_output=True, text=True)
    (state_path.parent / "canary-traffic.log").write_text(logs.stdout + logs.stderr)
    subprocess.run(["kubectl", "--request-timeout=30s", "-n", NAMESPACE, "delete", "job", state["name"],
                    "--ignore-not-found", "--wait=true", "--timeout=90s"], check=True)
    state_path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["start", "stop", "render"])
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    if args.mode == "render":
        print(json.dumps(job_spec("m32-traffic-1-1", "http://internal-schema.ap-southeast-1.elb.amazonaws.com",
                                  Path("deploy/monitoring/canary-traffic.js").read_text())))
    else:
        if args.state is None: parser.error("--state required for start/stop")
        {"start": start, "stop": stop}[args.mode](args.state)


if __name__ == "__main__":
    main()
