"""Plan rendering and execution per engine. Generic steps come from YAML; only engine-specific
waiting/collection lives here."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from demo.runner import collect, probes, training, verdict
from demo.runner.context import utc_now
from demo.runner.spec import ROOT, render_argv, render_text

WORKFLOW = {"training": "train.yml", "release": "release.yml"}


@dataclass
class Step:
    id: str
    effect: str
    argv: list[str] | None = None
    instruction: str | None = None
    profile: dict | None = None

    @property
    def display(self) -> str:
        return " ".join(_quote(a) for a in self.argv) if self.argv else str(self.instruction)

    def as_dict(self) -> dict:
        return asdict(self) | {"display": self.display}


def _quote(arg: str) -> str:
    return arg if re.fullmatch(r"[A-Za-z0-9_./:=@,+-]+", arg) else "'" + arg.replace("'", "'\\''") + "'"


def run_values(values: dict, stamp: str | None = None) -> dict:
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return {**values, "stamp": stamp, "branch": f"demo/s2-{stamp}",
            "worktree": f"artifacts/demo/worktrees/s2-{stamp}"}


def build_plan(scenario, values: dict) -> list[Step]:
    steps = []
    for raw in scenario.trigger:
        if raw.get("automatic_only") and values.get("mode") != "automatic":
            continue
        if raw["effect"] == "manual":
            steps.append(Step(raw["id"], "manual", instruction=render_text(raw["instruction"], values)))
        elif raw.get("per_profile"):
            for profile in values["load_profiles"]:
                steps.append(Step(f"{raw['id']}:{profile['name']}", raw["effect"],
                                  render_argv(raw["run"], {**values, **profile}), profile=profile))
        else:
            steps.append(Step(raw["id"], raw["effect"], render_argv(raw["run"], values)))
    return steps


def github_ui(scenario, values: dict) -> dict:
    ui = scenario.raw.get("github_ui") or {}
    return {"workflow": ui.get("workflow"), "branch": render_text(str(ui.get("branch", "main")), values),
            "fields": {k: render_text(str(v), values) for k, v in (ui.get("fields") or {}).items()}}


# --------------------------------------------------------------------------- execution


def _run_step(ctx, step: Step, log: list[dict]) -> Any:
    started = utc_now()
    result = ctx.sh.run(step.argv, timeout=900)
    log.append({"step": step.id, "effect": step.effect, "command": step.display, "started": started,
                "finished": utc_now(), "returncode": result.returncode,
                "stdout_tail": result.stdout[-2000:], "stderr_tail": result.stderr[-2000:]})
    if not result.ok:
        raise RuntimeError(f"step {step.id} failed (rc={result.returncode}): {result.stderr.strip()[-400:]}")
    return result


def execute_dispatch(ctx, steps: list[Step], log: list[dict]) -> tuple[dict, verdict.Verdict]:
    engine, expect = ctx.scenario.engine, ctx.scenario.verify["expect"]
    since = utc_now()
    for step in steps:
        _run_step(ctx, step, log)
    run = collect.find_run(ctx, WORKFLOW[engine], since, event="workflow_dispatch")
    if run is None:
        obs = {"workflow": {}, "timed_out": False}
        return obs, verdict.Verdict(verdict.INCONCLUSIVE, "run_not_found", expect,
                                    ["dispatch accepted but no new run appeared within 3 minutes"])
    log.append({"step": "run-located", "run_id": run["databaseId"], "url": run.get("url"), "at": utc_now()})
    return observe_run(ctx, str(run["databaseId"]))


def observe_run(ctx, run_id: str) -> tuple[dict, verdict.Verdict]:
    """Shared by `run --execute` and `verify --run-id` (UI-triggered runs)."""
    engine, expect = ctx.scenario.engine, ctx.scenario.verify["expect"]
    detail, timed_out = collect.wait_run(ctx, run_id, ctx.scenario.verify["timeout_minutes"])
    if engine == "training":
        obs = collect.training_observation(ctx, detail, timed_out)
        return obs, verdict.classify_training(obs, expect)
    if engine == "release":
        obs = collect.release_observation(ctx, detail, timed_out)
        return obs, verdict.classify_release(obs, expect)
    if engine == "pipeline":
        obs = collect.training_observation(ctx, detail, timed_out)
        obs["preview"] = ctx.snapshot.get("preview")
        result = obs.get("result") or {}
        if result.get("validation_status") == "approved":
            obs["release"] = collect.release_observation(ctx, detail, timed_out, result.get("model_version"))
        return obs, verdict.classify_pipeline(obs)
    raise ValueError(f"engine {engine} has no workflow run")


def execute_pipeline(ctx, steps: list[Step], log: list[dict], confirm) -> tuple[dict, verdict.Verdict]:
    expect = ctx.scenario.verify["expect"]
    pr_url = None
    for step in steps:
        if step.id == "local-preview":
            continue  # preview already ran in-process before confirmation
        if step.effect == "manual":
            print(f"\n>>> {step.instruction}\n    PR: {pr_url}")
            merge_sha = wait_for_merge(ctx, pr_url)
            log.append({"step": step.id, "merged_commit": merge_sha, "at": utc_now()})
            if not merge_sha:
                return {"pr": pr_url}, verdict.Verdict(verdict.TIMEOUT, "pr_not_merged", expect,
                                                       ["PR was not merged within 60 minutes"])
            run = collect.find_run(ctx, "ci.yml", "", event="push", head_sha=merge_sha, timeout=600)
            if run is None:
                return {"pr": pr_url}, verdict.Verdict(verdict.INCONCLUSIVE, "ci_run_not_found", expect,
                                                       [f"no Source CI push run for {merge_sha}"])
            log.append({"step": "run-located", "run_id": run["databaseId"], "url": run.get("url"), "at": utc_now()})
            obs, result = observe_run(ctx, str(run["databaseId"]))
            obs["pr"] = pr_url
            return obs, result
        if step.effect in {"git-remote", "aws"} and not confirm(step):
            raise RuntimeError(f"operator declined step {step.id}")
        output = _run_step(ctx, step, log)
        if step.id == "pull-request":
            pr_url = output.stdout.strip().splitlines()[-1]
    return {}, verdict.Verdict(verdict.INCONCLUSIVE, "no_merge_step", expect, ["plan has no merge step"])


def wait_for_merge(ctx, pr_url: str | None, timeout_minutes: int = 60) -> str | None:
    deadline = ctx.clock() + timeout_minutes * 60
    while pr_url and ctx.clock() < deadline:
        state = probes.gh_json(ctx.sh, "pr", "view", pr_url, "--repo", ctx.values["github_repo"],
                               "--json", "state,mergeCommit")
        if state and state.get("state") == "MERGED":
            return (state.get("mergeCommit") or {}).get("oid")
        if state and state.get("state") == "CLOSED":
            return None
        ctx.sleep(15)
    return None


# --------------------------------------------------------------------------- local preview (S2)


def local_preview(ctx, train: bool, reference_mae: float | None = None) -> dict:
    """Train the candidate locally (same pipeline/data/seed) and apply the real gate function."""
    config = ROOT / ctx.values["candidate_config"]
    training.validate_identity(config)
    output = ROOT / "artifacts/demo-preview" / config.stem
    if train:
        result = ctx.sh.run(["uv", "run", "--locked", "mlops-train", "train", "--config", str(config),
                             "--output-dir", str(output)], timeout=3600)
        if not result.ok:
            raise RuntimeError("local candidate training failed: " + result.stderr[-400:])
    run_dir = training.latest_run(output)
    if run_dir is None:
        raise RuntimeError(f"no local run under {output}; re-run with --train")
    run = training.read_run(run_dir)
    reference, source = reference_mae, "operator-supplied"
    if reference is None:
        champion = ctx.snapshot.get("champion") or ctx.open_registry().champion()
        if champion is None:
            raise RuntimeError("no champion: pass --reference-mae for an offline preview")
        reference, source = champion["validation_mae_minutes"], f"cloud champion v{champion['version']}"
        if champion.get("validation_dataset_key") and champion["validation_dataset_key"] != run["validation_dataset_key"]:
            raise RuntimeError("local validation dataset key differs from the champion's; preview not comparable")
    decision = training.preview_decision(run["validation_mae"], reference)
    return {**decision, "reference_source": source, "local_run": run["run_dir"], "test_mae": run["test_mae"],
            "validation_dataset_key": run["validation_dataset_key"], "model": run["model"],
            "note": "Preview only: the cloud gate decides. Same data, seed and gate function as the Spot Job."}


# --------------------------------------------------------------------------- load / HPA (S5)

K6_JOB = re.compile(r"Load test: (?:job\.batch/)?(m33-k6-[a-z0-9]+)")
K6_SUMMARY = re.compile(r"^\s*(iterations|dropped_iterations|http_req_failed|http_req_duration|http_reqs)\b[^:]*:\s*(.+)$",
                        re.M)
RPS_QUERY = 'sum(rate(inference_http_requests_total{path="/predict"}[1m]))'


def _sample(ctx, t0: float, phase: str) -> dict:
    sample = probes.hpa_sample(ctx.sh)
    try:
        sample["rps"] = ctx.query(RPS_QUERY)
    except Exception:  # noqa: BLE001 - RPS is supplementary; HPA metrics drive the verdict
        sample["rps"] = None
        ctx.prometheus = lambda query: None  # do not stall every sample re-opening a dead tunnel
    sample.update(t=round(ctx.clock() - t0, 1), phase=phase, at=utc_now())
    return sample


def execute_load(ctx, steps: list[Step], log: list[dict]) -> tuple[dict, verdict.Verdict]:
    interval, t0 = ctx.values["sample_interval_seconds"], ctx.clock()
    samples, loads = [_sample(ctx, t0, "baseline")], []
    try:
        for step in steps:
            started = utc_now()
            handle = ctx.sh.spawn(step.argv)
            while handle.poll() is None:
                samples.append(_sample(ctx, t0, "load:" + step.profile["name"]))
                ctx.sleep(interval)
            output = handle.output()
            job = K6_JOB.search(output)
            loads.append({"name": step.profile["name"], "ok": handle.returncode == 0, "job": job.group(1) if job else None,
                          "started": started, "finished": utc_now(),
                          "k6_summary": {m.group(1): m.group(2).strip() for m in K6_SUMMARY.finditer(output)}})
            log.append({"step": step.id, "command": step.display, "started": started, "finished": utc_now(),
                        "returncode": handle.returncode, "stdout_tail": output[-3000:]})
            if handle.returncode != 0:
                break
        deadline, settled = ctx.clock() + ctx.values["observe_after_seconds"], 0
        while ctx.clock() < deadline:
            sample = _sample(ctx, t0, "cooldown")
            samples.append(sample)
            settled = settled + 1 if sample.get("desired_replicas") == sample.get("min_replicas") == sample.get("current_replicas") else 0
            if settled >= 3:
                break
            ctx.sleep(interval)
    finally:
        for load in loads:  # delete only Jobs this run created
            if load.get("job"):
                ctx.sh.run(["kubectl", "-n", "mlops", "delete", "job", load["job"], "--ignore-not-found"], timeout=60)
    obs = {"samples": samples, "loads": loads}
    return obs, verdict.analyze_hpa(samples, loads)
