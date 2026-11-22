"""Preflight checks. Read-only; they also record the pre-run snapshot used by verification/recovery."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable

from demo.runner import probes, training

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class Check:
    id: str
    status: str
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


def _tools(ctx) -> Check:
    needed = {"training": ["git", "gh", "kubectl", "aws"], "pipeline": ["git", "gh", "kubectl", "aws"],
              "release": ["git", "gh", "kubectl", "aws"], "load": ["kubectl", "make"]}[ctx.scenario.engine]
    missing = [tool for tool in needed if not ctx.sh.which(tool)]
    return Check("tools", FAIL if missing else PASS, f"missing: {missing}" if missing else ", ".join(needed))


def _aws_identity(ctx) -> Check:
    result = ctx.sh.run(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"], timeout=40)
    account = result.stdout.strip() if result.ok else None
    expected = str(ctx.values["aws_account_id"])
    ctx.snapshot["eks_cidrs"] = probes.eks_cidrs(ctx.sh, ctx.values["cluster"], ctx.values["aws_region"])
    if account != expected:
        return Check("aws_identity", FAIL, f"authenticated account {account!r} != expected {expected}")
    return Check("aws_identity", PASS, f"account {account}; EKS public CIDRs recorded: {len(ctx.snapshot['eks_cidrs'] or [])}")


def _kube_context(ctx) -> Check:
    result = ctx.sh.run(["kubectl", "config", "current-context"], timeout=20)
    current = result.stdout.strip()
    if not result.ok or ctx.values["cluster"] not in current:
        return Check("kube_context", FAIL, f"kubectl context {current!r} is not {ctx.values['cluster']}")
    if probes.kube_json(ctx.sh, "get", "namespace", "mlops") is None:
        return Check("kube_context", FAIL, "cannot read namespace mlops (EKS CIDR allow-list / credentials?)")
    return Check("kube_context", PASS, current)


def _gh_access(ctx) -> Check:
    if not ctx.sh.run(["gh", "auth", "status"], timeout=20).ok:
        return Check("gh_access", FAIL, "gh is not authenticated")
    result = ctx.sh.run(["gh", "api", f"repos/{ctx.values['github_repo']}", "--jq", ".permissions.push"], timeout=30)
    if result.stdout.strip() != "true":
        return Check("gh_access", FAIL, "account cannot dispatch workflows (needs write/push permission)")
    return Check("gh_access", PASS, f"write access to {ctx.values['github_repo']}")


def _git_main_synced(ctx) -> Check:
    sha = probes.origin_main(ctx.sh)
    ctx.snapshot["main_sha"] = sha
    if not sha:
        return Check("git_main_synced", FAIL, "cannot fetch origin/main")
    ctx.snapshot["gitops_template"] = probes.gitops_template(ctx.sh)
    return Check("git_main_synced", PASS, f"origin/main {sha[:12]}")


def _demo_config_on_main(ctx) -> Check:
    path = ctx.values["training_config"]
    text = probes.git(ctx.sh, "show", f"origin/main:{path}")
    if text is None:
        return Check("demo_config_on_main", FAIL, f"{path} is not on origin/main; merge the demo configs first")
    base = probes.git(ctx.sh, "show", "origin/main:configs/training.yaml")
    problems = training.semantic_match(text, base, ctx.values["model_overrides"]) if base else ["no base config"]
    if problems:
        return Check("demo_config_on_main", FAIL, f"{path} on main differs from preset: {problems}")
    return Check("demo_config_on_main", PASS, f"{path} on main = main identity + preset overrides")


def _auto_mode_enabled(ctx) -> Check:
    if ctx.values.get("mode") != "automatic":
        return Check("auto_mode_enabled", SKIP, "mode=preview: no automatic pipeline")
    repo = ctx.values["github_repo"]
    enabled = probes.gh_variable(ctx.sh, repo, "AUTO_MLOPS_ENABLED")
    baseline = probes.gh_variable(ctx.sh, repo, "AUTO_MLOPS_BASELINE_SHA")
    ctx.snapshot["auto"] = {"enabled": enabled, "baseline_sha": baseline}
    ctx.snapshot["training_fingerprint"] = training.fingerprint_at(ctx.sh, "origin/main")
    ctx.snapshot["training_config_text"] = probes.git(ctx.sh, "show", "origin/main:configs/training.yaml")
    if enabled != "true":
        return Check("auto_mode_enabled", FAIL, f"AUTO_MLOPS_ENABLED={enabled!r}; automatic mode will not train")
    return Check("auto_mode_enabled", PASS, f"auto enabled; baseline {str(baseline)[:12]}")


def _no_active_workflows(ctx) -> Check:
    runs = probes.active_runs(ctx.sh, ctx.values["github_repo"])
    if runs:
        listed = ", ".join(f"{r.get('workflowName')}#{r.get('databaseId')}({r.get('status')})" for r in runs)
        return Check("no_active_workflows", FAIL, "conflicting runs in progress: " + listed)
    return Check("no_active_workflows", PASS, "no queued/in-progress CI/Training/Release runs")


def _no_active_jobs(ctx) -> Check:
    jobs = probes.active_jobs(ctx.sh)
    ctx.snapshot["active_jobs"] = jobs
    busy = {key: names for key, names in jobs.items() if names}
    return Check("no_active_jobs", FAIL if busy else PASS, f"active: {busy}" if busy else "no training/k6/traffic Jobs")


def _rollout_stable(ctx) -> Check:
    state = probes.serving_state(ctx.sh)
    ctx.snapshot["serving"] = state
    ok = (state["rollout_found"] and state["phase"] == "Healthy" and not state["abort"] and not state["paused"]
          and state["stable_hash"] and state["stable_hash"] == state["current_hash"])
    detail = f"phase={state['phase']} stable={state['stable_hash']} current={state['current_hash']} abort={state['abort']}"
    return Check("rollout_stable", PASS if ok else FAIL, detail)


def _argocd_synced_main(ctx) -> Check:
    state = ctx.snapshot.get("serving") or probes.serving_state(ctx.sh)
    main = ctx.snapshot.get("main_sha") or probes.origin_main(ctx.sh)
    if state["app_sync"] != "Synced" or state["app_health"] != "Healthy":
        return Check("argocd_synced_main", FAIL, f"Argo CD {state['app_sync']}/{state['app_health']}")
    template = ctx.snapshot.get("gitops_template") or probes.gitops_template(ctx.sh)
    if template and state["live_image"] != template["image"]:
        return Check("argocd_synced_main", FAIL, "live Deployment image differs from origin/main template")
    if state["app_revision"] != main:
        return Check("argocd_synced_main", WARN, f"Argo revision {str(state['app_revision'])[:12]} != main {str(main)[:12]} (refresh pending?)")
    return Check("argocd_synced_main", PASS, f"Synced/Healthy at {main[:12]}; live image = Git")


def _champion(ctx) -> Check:
    try:
        registry = ctx.open_registry()
        champion = registry.champion()
    except Exception as error:  # noqa: BLE001 - report any tunnel/MLflow failure as a failed check
        return Check("champion", FAIL, f"MLflow unavailable: {error}")
    ctx.snapshot["champion"] = champion
    if not champion:
        return Check("champion", FAIL, "no champion alias")
    if ctx.values.get("model_version") == "champion":
        ctx.values["model_version"] = champion["version"]
        ctx.values["model_version_source"] = "champion"
    return Check("champion", PASS, f"champion v{champion['version']} MAE={champion.get('validation_mae_minutes')}")


def _model_version_approved(ctx) -> Check:
    number = ctx.values["model_version"]
    if number == "champion":
        return Check("model_version_approved", FAIL, "champion could not be resolved")
    version = ctx.open_registry().version(number)
    if not version:
        return Check("model_version_approved", FAIL, f"version {number} not found")
    if version["status"] != "READY" or version["validation_status"] != "approved":
        return Check("model_version_approved", FAIL, f"version {number} is {version['status']}/{version['validation_status']}")
    ctx.snapshot["release_version"] = version
    return Check("model_version_approved", PASS, f"version {number} READY/approved run {version['run_id']}")


def _monitoring(ctx) -> Check:
    try:
        targets = ctx.query('count(up{job="inference-api"} == 1)')
    except Exception as error:  # noqa: BLE001
        return Check("monitoring", FAIL, f"Prometheus unavailable: {error}")
    if not targets:
        return Check("monitoring", FAIL, "no inference-api target UP in Prometheus (canary gates need metrics)")
    return Check("monitoring", PASS, f"{int(targets)} inference-api target(s) UP")


def _hpa_metrics(ctx) -> Check:
    sample = probes.hpa_sample(ctx.sh)
    ctx.snapshot["hpa"] = sample
    if sample["cpu_utilization"] is None:
        return Check("hpa_metrics", FAIL, "HPA has no CPU metric (metrics-server?)")
    status = PASS if sample["current_replicas"] == sample["min_replicas"] else WARN  # scale-in baseline
    return Check("hpa_metrics", status, f"CPU {sample['cpu_utilization']}%/{sample['cpu_target']}%, "
                                      f"replicas {sample['current_replicas']} (min {sample['min_replicas']}, max {sample['max_replicas']})")


CHECKS: dict[str, Callable] = {
    "tools": _tools, "aws_identity": _aws_identity, "kube_context": _kube_context, "gh_access": _gh_access,
    "git_main_synced": _git_main_synced, "demo_config_on_main": _demo_config_on_main,
    "auto_mode_enabled": _auto_mode_enabled, "no_active_workflows": _no_active_workflows,
    "no_active_jobs": _no_active_jobs, "rollout_stable": _rollout_stable, "argocd_synced_main": _argocd_synced_main,
    "champion": _champion, "model_version_approved": _model_version_approved, "monitoring": _monitoring,
    "hpa_metrics": _hpa_metrics,
}


def run_preflight(ctx, stop_on_fail: bool = False) -> list[Check]:
    results = []
    for check_id in ctx.scenario.preflight:
        try:
            result = CHECKS[check_id](ctx)
        except Exception as error:  # noqa: BLE001 - a crashing probe is a failed check, never a pass
            result = Check(check_id, FAIL, f"probe error: {type(error).__name__}: {error}")
        results.append(result)
        if stop_on_fail and result.status == FAIL:
            break
    return results


def passed(results: list[Check]) -> bool:
    return all(result.status != FAIL for result in results)
