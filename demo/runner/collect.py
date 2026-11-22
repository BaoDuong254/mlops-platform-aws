"""Live evidence collection after a trigger: locate the run, wait, read artifacts and cluster state."""
from __future__ import annotations

import json
from pathlib import Path

from demo.runner import probes
from demo.runner.verdict import CANARY_REJECTED_MARKER, REVERT_DONE_MARKER


def find_run(ctx, workflow: str, since: str, event: str | None = None, head_sha: str | None = None,
             timeout: int = 180) -> dict | None:
    """Locate the run created by our trigger (gh 2.46 does not print the dispatched run ID)."""
    deadline = ctx.clock() + timeout
    while ctx.clock() < deadline:
        for run in probes.workflow_runs(ctx.sh, ctx.values["github_repo"], workflow, 15):
            if run.get("createdAt", "") >= since and (event is None or run.get("event") == event) and (
                    head_sha is None or run.get("headSha") == head_sha):
                return run
        ctx.sleep(10)
    return None


def wait_run(ctx, run_id: str, timeout_minutes: int, poll: int = 30) -> tuple[dict | None, bool]:
    deadline = ctx.clock() + timeout_minutes * 60
    detail = None
    while ctx.clock() < deadline:
        detail = probes.run_detail(ctx.sh, ctx.values["github_repo"], run_id)
        if detail and detail.get("status") == "completed":
            return detail, False
        ctx.sleep(poll)
    return detail, True


def job_conclusions(detail: dict) -> dict[str, str | None]:
    return {job["name"]: job.get("conclusion") for job in (detail or {}).get("jobs", [])}


def release_job_conclusion(detail: dict) -> str | None:
    """Reusable release appears as `release / release` (or nested under the CI caller)."""
    found = [c for name, c in job_conclusions(detail).items() if "release" in name.split(" / ")[-2:]]
    if not found:
        return None
    return "success" if all(c == "success" for c in found) else next(c for c in found if c != "success")


def _artifact(ctx, run_id: str, name: str, filename: str) -> dict | None:
    target = ctx.evidence_dir / "artifacts" / name
    if not probes.download_artifact(ctx.sh, ctx.values["github_repo"], run_id, name, str(target)):
        return None
    hits = sorted(Path(target).rglob(filename))
    return json.loads(hits[0].read_text()) if hits else None


def training_observation(ctx, detail: dict | None, timed_out: bool) -> dict:
    pre = ctx.snapshot
    obs = {"workflow": _summary(detail), "timed_out": timed_out,
           "champion_before": (pre.get("champion") or {}).get("version"), "release_job": release_job_conclusion(detail)}
    if not detail or timed_out:
        return obs
    attempt = detail.get("attempt", 1)
    obs["result"] = _artifact(ctx, detail["databaseId"], f"train-result-{detail['databaseId']}-{attempt}",
                              "training-result.json")
    with probes.registry(ctx) as registry:
        champion = registry.champion()
        obs["champion_after"] = (champion or {}).get("version")
        run_id = (obs["result"] or {}).get("mlflow_run_id")
        if run_id:
            obs["mlflow_run"] = registry.run(run_id)
            obs["versions_for_run"] = [v["version"] for v in registry.versions() if v.get("run_id") == run_id]
    return obs


def _summary(detail: dict | None) -> dict:
    if not detail:
        return {}
    return {k: detail.get(k) for k in ("databaseId", "status", "conclusion", "url", "createdAt", "updatedAt",
                                       "headSha", "event", "workflowName", "attempt")} | {"jobs": job_conclusions(detail)}


def _commits(ctx, release_id: str) -> tuple[str | None, str | None]:
    probes.origin_main(ctx.sh)
    log = probes.git(ctx.sh, "log", "origin/main", "-n", "60", "--format=%H%x09%s") or ""
    subject = f"m32: release {release_id} by digest"
    release = revert = None
    for line in log.splitlines():
        sha, _, text = line.partition("\t")
        if text == subject:
            release = sha
        elif text == f'Revert "{subject}"':
            revert = sha
    return release, revert


def release_observation(ctx, detail: dict | None, timed_out: bool, model_version: str | None = None) -> dict:
    pre = ctx.snapshot
    obs = {"workflow": _summary(detail), "timed_out": timed_out,
           "model_version": model_version or ctx.values.get("model_version"),
           "champion_before": (pre.get("champion") or {}).get("version"),
           "stable_image_before": (pre.get("serving") or {}).get("live_image"),
           "stable_cpu": (pre.get("gitops_template") or {}).get("cpu_limit")}
    if not detail or timed_out:
        return obs
    run_id, attempt = detail["databaseId"], detail.get("attempt", 1)
    obs["release_id"] = f"m32-{run_id}-{attempt}"
    name = f"release-result-{run_id}-{attempt}"
    obs["expected"] = _artifact(ctx, run_id, name, "m32-expected.json")
    obs["result"] = _artifact(ctx, run_id, name, "m32-result.json")
    obs["markers"] = probes.run_log_markers(ctx.sh, ctx.values["github_repo"], run_id,
                                            (CANARY_REJECTED_MARKER, REVERT_DONE_MARKER))
    obs["release_commit"], obs["revert_commit"] = _commits(ctx, obs["release_id"])
    if obs["release_commit"]:
        template = probes.gitops_template(ctx.sh, obs["release_commit"])
        obs["candidate_cpu"] = (template or {}).get("cpu_limit")
    runs = [r for r in probes.analysis_runs(ctx.sh) if (r["created"] or "") >= detail.get("createdAt", "")]
    background = [r for r in runs if "p95-latency-seconds" in r["metrics"]]
    smoke = [r for r in runs if "canary-smoke" in r["metrics"]]
    obs["analysis"] = background[-1] if background else None
    obs["smoke"] = smoke[-1] if smoke else None
    obs["smoke_failed"] = bool(smoke and smoke[-1]["phase"] in {"Failed", "Error"})
    obs["serving_after"] = probes.serving_state(ctx.sh)
    with probes.registry(ctx) as registry:
        obs["champion_after"] = (registry.champion() or {}).get("version")
    return obs
