"""Local orchestration policy. GitHub reads only; never calls AWS or promotes a model."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

SHA = re.compile(r"[0-9a-f]{40}")
# Dependencies are shared by training/serving; a lock change can change training behavior.
TRAINING_FILES = {
    "pyproject.toml", "uv.lock", ".dockerignore", "configs/mlflow.yaml",
    "deploy/training/Dockerfile", "deploy/platform/mlops/training-job.yaml.tpl",
    "deploy/platform/mlops/serviceaccounts.yaml",
    "scripts/train_workflow.sh", "scripts/training_result.py", "scripts/workflow_guard.py",
    "scripts/auto_mlops.py", "scripts/m23-render.py", "scripts/m23-common.sh", "scripts/image_identity.py",
    ".github/workflows/train.yml", ".github/workflows/ci.yml",
}
GITOPS_FILES = {"deploy/gitops/inference/deployment.yaml", "deploy/gitops/inference/rollout.yaml"}
MAX_ARCHIVE = 262144


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def training_path(path):
    return (path in TRAINING_FILES or path.startswith("src/training/") or
            re.fullmatch(r"configs/training[^/]*\.yaml", path) is not None)


def fingerprint(sha):
    if not SHA.fullmatch(sha):
        raise ValueError("Expected exact source commit SHA")
    # Git tree records include file mode, path and blob hash; no worktree/artifact dependency.
    records = subprocess.check_output(["git", "ls-tree", "-r", "-z", sha]).split(b"\0")
    selected = [row for row in records if row and training_path(row.split(b"\t", 1)[1].decode("utf-8"))]
    return hashlib.sha256(b"\0".join(sorted(selected))).hexdigest()


def trusted(env):
    return (env.get("GITHUB_EVENT_NAME") == "push" and env.get("GITHUB_REF") == "refs/heads/main"
            and env.get("EXPECTED_REPOSITORY_ID", "").isdigit()
            and env.get("GITHUB_REPOSITORY_ID") == env["EXPECTED_REPOSITORY_ID"]
            and env.get("EXPECTED_OWNER_ID", "").isdigit()
            and env.get("GITHUB_REPOSITORY_OWNER_ID") == env["EXPECTED_OWNER_ID"])


def choose_plan(env, source, base, current, previous=None):
    if env.get("AUTO_MLOPS_ENABLED") != "true" or not trusted(env):
        return {"train": False, "reason": "disabled-or-untrusted", "base_sha": base}
    if source != current:
        return {"train": False, "reason": "stale", "base_sha": base}
    if previous and previous["disposition"] == "blocked" and previous["fingerprint"] == fingerprint(source):
        return {"train": False, "reason": "blocked-previous-attempt-needs-review", "base_sha": base}
    changed = fingerprint(base) != fingerprint(source)
    return {"train": changed, "reason": "training-inputs-changed" if changed else "no-training-change",
            "base_sha": base}


def gh(endpoint, binary=False):
    value = subprocess.check_output(["gh", "api", endpoint])
    return value if binary else json.loads(value)


def checkpoint_payload(archive, artifact, run, env):
    """Untrusted archive is parsed as one bounded JSON file, never extracted/executed."""
    if len(archive) > MAX_ARCHIVE or artifact.get("digest") != "sha256:" + hashlib.sha256(archive).hexdigest():
        raise ValueError("Checkpoint archive checksum/size differs")
    if (run.get("event") != "push" or run.get("head_branch") != "main" or
        str(run.get("head_repository", {}).get("id")) != env["EXPECTED_REPOSITORY_ID"] or
        run.get("path", "").split("@")[0] != ".github/workflows/ci.yml"):
        raise ValueError("Checkpoint not produced by trusted main Source CI")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        files = bundle.infolist()
        if len(files) != 1 or files[0].filename != "auto-checkpoint.json" or files[0].file_size > MAX_ARCHIVE:
            raise ValueError("Unexpected checkpoint archive contents")
        result = json.loads(bundle.read(files[0]))
    if (result.get("schema_version") != 1 or result.get("run_id") != str(run["id"]) or
        not str(result.get("run_attempt", "")).isdigit() or
        not 1 <= int(result["run_attempt"]) <= int(run["run_attempt"]) or
        artifact["name"] not in {f"auto-mlops-checkpoint-{run['id']}-{result['run_attempt']}",
                                  f"auto-mlops-intent-{run['id']}-{result['run_attempt']}"} or
        result.get("source_sha") != run["head_sha"] or
        result.get("repository_id") != env["EXPECTED_REPOSITORY_ID"] or
        result.get("disposition") not in {"completed", "stale", "blocked"}):
        raise ValueError("Checkpoint run/repository identity differs")
    for key in ("source_sha", "base_sha"):
        if not SHA.fullmatch(result.get(key, "")):
            raise ValueError("Invalid checkpoint commit")
    if result.get("fingerprint") != fingerprint(result["source_sha"]):
        raise ValueError("Checkpoint training fingerprint differs from Git")
    return result


def previous_checkpoint(env, source):
    repo = env["GITHUB_REPOSITORY"]
    # Never assume API ordering. Read a bounded inventory, then inspect newest records first.
    artifacts = []
    for page in range(1, 11):
        doc = gh(f"repos/{repo}/actions/artifacts?per_page=100&page={page}")
        artifacts.extend(doc["artifacts"])
        if len(doc["artifacts"]) < 100:
            break
    else:
        raise ValueError("Checkpoint lookup limit reached; manual inspection required")
    for artifact in sorted(artifacts, key=lambda item: (item.get("created_at", ""), item["id"]), reverse=True):
        wr = artifact.get("workflow_run", {})
        names = (f"auto-mlops-checkpoint-{wr.get('id')}-", f"auto-mlops-intent-{wr.get('id')}-")
        if not artifact["name"].startswith(names):
            continue
        if (str(wr.get("repository_id")) != env["EXPECTED_REPOSITORY_ID"] or
            str(wr.get("head_repository_id")) != env["EXPECTED_REPOSITORY_ID"] or wr.get("head_branch") != "main"):
            continue
        if subprocess.run(["git", "merge-base", "--is-ancestor", wr["head_sha"], source],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            continue
        baseline = env.get("AUTO_MLOPS_BASELINE_SHA", "")
        if SHA.fullmatch(baseline) and subprocess.run(
            ["git", "merge-base", "--is-ancestor", wr["head_sha"], baseline],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0:
            # Operator may acknowledge a reconciled baseline after manual recovery/retention expiry.
            continue
        run = gh(f"repos/{repo}/actions/runs/{wr['id']}")
        suffix = artifact["name"].rsplit("-", 1)[1]
        if not suffix.isdigit() or not 1 <= int(suffix) <= int(run["run_attempt"]):
            raise ValueError("Invalid checkpoint attempt")
        expected = f"auto-mlops-checkpoint-{run['id']}-{suffix}"
        intent = f"auto-mlops-intent-{run['id']}-{suffix}"
        if artifact["name"] == intent:
            siblings = gh(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100")["artifacts"]
            if len(siblings) == 100:
                raise ValueError("Run artifact limit reached")
            artifact = next((a for a in siblings if a['name'] == expected), artifact)
        if artifact.get("expired"):
            raise ValueError("Automatic checkpoint expired; inspect history before enabling again")
        if run.get("event") != "push" or run.get("path", "").split("@")[0] != ".github/workflows/ci.yml":
            continue
        historical_attempt = (str(run["id"]) == env.get("GITHUB_RUN_ID") and
                              int(suffix) < int(env.get("GITHUB_RUN_ATTEMPT", "0")))
        if run.get("status") != "completed" and not historical_attempt:
            raise ValueError("Previous automatic pipeline has not finished")
        if not isinstance(artifact.get("size_in_bytes"), int) or not 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE:
            raise ValueError("Checkpoint too large or missing archive size")
        blob = gh(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", binary=True)
        return checkpoint_payload(blob, artifact, run, env)
    return None


def assert_no_lost_checkpoint(env, source, baseline):
    """A deleted/expired artifact must not masquerade as a first automatic run."""
    repo = env["GITHUB_REPOSITORY"]
    def used_cloud(jobs):
        return any((job["name"] == "MLOps Cloud Training" or job["name"].endswith(" / MLOps Cloud Training"))
                   and job.get("conclusion") != "skipped" for job in jobs)
    for page in range(1, 11):
        runs = gh(f"repos/{repo}/actions/workflows/ci.yml/runs?branch=main&event=push&per_page=100&page={page}")["workflow_runs"]
        for run in runs:
            sha = run["head_sha"]
            if (run.get("event") != "push" or run.get("head_branch") != "main" or
                str(run.get("head_repository", {}).get("id")) != env["EXPECTED_REPOSITORY_ID"]):
                continue
            if subprocess.run(["git", "merge-base", "--is-ancestor", sha, baseline],
                              capture_output=True).returncode == 0:
                continue
            if subprocess.run(["git", "merge-base", "--is-ancestor", sha, source],
                              capture_output=True).returncode != 0:
                continue
            attempt = int(run["run_attempt"])
            if str(run["id"]) == env["GITHUB_RUN_ID"]:
                attempt = int(env["GITHUB_RUN_ATTEMPT"]) - 1
            if attempt > 20:
                raise ValueError("Historical attempt lookup limit reached")
            for number in range(1, attempt + 1):
                jobs = gh(f"repos/{repo}/actions/runs/{run['id']}/attempts/{number}/jobs?per_page=100")["jobs"]
                if len(jobs) == 100 or used_cloud(jobs):
                    raise ValueError("Missing automatic checkpoint after a cloud attempt; reconcile before retry")
        if len(runs) < 100:
            return
    raise ValueError("CI history lookup limit reached; review baseline before enabling")


def output(values):
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        for key, value in values.items():
            stream.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")


def plan(env):
    source = env["SOURCE_SHA"]
    if env.get("AUTOMATIC") != "true":
        output({"train": True, "base_sha": source, "reason": "manual"})
        return
    if not trusted(env) or env.get("AUTO_MLOPS_ENABLED") != "true":
        output({"train": False, "base_sha": source, "reason": "disabled-or-untrusted"})
        return
    current = git("ls-remote", "origin", "refs/heads/main").split()[0]
    before = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())["before"]
    # All-zero before (new branch) is not a safe baseline for this existing project.
    if not SHA.fullmatch(before) or before == "0" * 40:
        raise ValueError("No trustworthy previous main commit; run maintenance training explicitly")
    baseline = env.get("AUTO_MLOPS_BASELINE_SHA", "")
    if not SHA.fullmatch(baseline):
        raise ValueError("Set AUTO_MLOPS_BASELINE_SHA to the reviewed main training baseline before enabling")
    previous = previous_checkpoint(env, source)
    assert_no_lost_checkpoint(env, source, previous["source_sha"] if previous else baseline)
    base = baseline
    if previous:
        base = previous["source_sha"] if previous["disposition"] in {"completed", "blocked"} else previous["base_sha"]
    if not SHA.fullmatch(base):
        raise ValueError("Set AUTO_MLOPS_BASELINE_SHA to the reviewed main training baseline before enabling")
    if subprocess.run(["git", "merge-base", "--is-ancestor", base, source]).returncode != 0:
        raise ValueError("Training baseline is not an ancestor of current source")
    result = choose_plan(env, source, base, current, previous)
    output(result)
    if result["train"] or result["reason"] == "stale":
        record = {"schema_version": 1, "run_id": env["GITHUB_RUN_ID"],
                  "run_attempt": env["GITHUB_RUN_ATTEMPT"], "repository_id": env["EXPECTED_REPOSITORY_ID"],
                  "source_sha": source, "base_sha": base, "fingerprint": fingerprint(source),
                  "disposition": "stale" if result["reason"] == "stale" else "blocked"}
        path = Path(env["RUNNER_TEMP"], "auto-intent", "auto-checkpoint.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, sort_keys=True) + "\n")
    with open(env["GITHUB_STEP_SUMMARY"], "a") as stream:
        stream.write("\nAutomatic MLOps plan: `" + json.dumps(result, sort_keys=True) + "`\n")
    if result["reason"] == "blocked-previous-attempt-needs-review":
        raise ValueError("Previous attempt blocked; inspect it before retrying unchanged training inputs")


def checkpoint(env):
    source, base = env["SOURCE_SHA"], env["BASE_SHA"]
    if not trusted(env) or env.get("AUTO_MLOPS_ENABLED") != "true":
        raise ValueError("Untrusted checkpoint producer")
    git("fetch", "origin", "main")
    current = git("rev-parse", "origin/main")
    changed = set(git("diff", "--name-only", source, current).splitlines())
    stale = current != source and bool(changed - GITOPS_FILES)
    completed = env.get("TRAIN_RESULT") == "success" and (
        env.get("VALIDATION_STATUS") == "rejected" or
        (env.get("VALIDATION_STATUS") == "approved" and env.get("RELEASE_RESULT") == "success"))
    disposition = "completed" if completed else "stale" if stale or env.get("PLAN_REASON") == "stale" else "blocked"
    result = {"schema_version": 1, "run_id": env["GITHUB_RUN_ID"],
              "run_attempt": env["GITHUB_RUN_ATTEMPT"], "repository_id": env["EXPECTED_REPOSITORY_ID"],
              "source_sha": source, "base_sha": base, "fingerprint": fingerprint(source),
              "disposition": disposition, "validation_status": env.get("VALIDATION_STATUS", ""),
              "train_result": env.get("TRAIN_RESULT"), "release_result": env.get("RELEASE_RESULT")}
    Path(env["RUNNER_TEMP"], "auto-checkpoint.json").write_text(json.dumps(result, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["plan", "checkpoint", "assert-terminal"])
    args = parser.parse_args()
    if args.mode == "assert-terminal":
        record = json.loads(Path(os.environ["RUNNER_TEMP"], "auto-checkpoint.json").read_text())
        print(json.dumps(record, sort_keys=True))
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write("\nAutomatic pipeline terminal state: `" + record["disposition"] + "`.\n")
        if record["disposition"] == "blocked":
            raise ValueError("Automatic pipeline blocked; inspect training/release/cleanup before maintenance retry")
    else:
        {"plan": plan, "checkpoint": checkpoint}[args.mode](os.environ)


if __name__ == "__main__":
    main()
