"""Post-run recovery verification. Cleanup counts as done only when the final state is read back."""
from __future__ import annotations

from typing import Callable

from demo.runner import preflight, probes
from demo.runner.preflight import FAIL, PASS, SKIP, Check

VERIFIED, NEEDS_ATTENTION = "VERIFIED", "NEEDS_ATTENTION"


def _jobs(kind: str, label: str) -> Callable:
    def check(ctx) -> Check:
        names = probes.active_jobs(ctx.sh)[kind]
        return Check(f"no_{label}_jobs", FAIL if names else PASS, f"active {kind} Jobs: {names}" if names else "none active")
    return check


LIVE_KEYS = ("serving", "main_sha", "gitops_template")


def _fresh(fn: Callable) -> Callable:
    """Run a preflight check against live state, keeping the pre-run snapshot intact for comparisons."""
    def check(ctx) -> Check:
        saved = {key: ctx.snapshot.pop(key) for key in LIVE_KEYS if key in ctx.snapshot}
        try:
            return fn(ctx)
        finally:
            for key in LIVE_KEYS:
                ctx.snapshot.pop(key, None)
            ctx.snapshot.update(saved)
    return check


def _live_matches_git(ctx) -> Check:
    probes.origin_main(ctx.sh)
    template = probes.gitops_template(ctx.sh)
    state = probes.serving_state(ctx.sh)
    ok = template and state["live_image"] == template["image"] and state["live_release_id"] == template["release_id"]
    return Check("live_matches_git", PASS if ok else FAIL,
                 f"live {str(state['live_image'])[-20:]}/{state['live_release_id']} vs Git "
                 f"{str((template or {}).get('image'))[-20:]}/{(template or {}).get('release_id')}")


def _gitops_unchanged(ctx) -> Check:
    probes.origin_main(ctx.sh)
    now, before = probes.gitops_template(ctx.sh), ctx.snapshot.get("gitops_template")
    return Check("gitops_unchanged", PASS if now == before else FAIL,
                 "GitOps serving template unchanged" if now == before else f"template changed: {before} -> {now}")


def _stable_digest_restored(ctx) -> Check:
    before = (ctx.snapshot.get("serving") or {}).get("live_image")
    now = probes.serving_state(ctx.sh)["live_image"]
    return Check("stable_digest_restored", PASS if before and now == before else FAIL,
                 f"live image {'equals' if now == before else 'differs from'} pre-run stable digest")


def _champion_now(ctx) -> str | None:
    with probes.registry(ctx) as registry:
        return (registry.champion() or {}).get("version")


def _champion_unchanged(ctx) -> Check:
    before, now = (ctx.snapshot.get("champion") or {}).get("version"), _champion_now(ctx)
    return Check("champion_unchanged", PASS if before and now == before else FAIL, f"champion {before} -> {now}")


def _champion_consistent(ctx) -> Check:
    """Champion must be the model version that Git/live serving declares."""
    probes.origin_main(ctx.sh)
    serving_version = (probes.gitops_template(ctx.sh) or {}).get("model_version")
    now = _champion_now(ctx)
    return Check("champion_consistent", PASS if now and now == serving_version else FAIL,
                 f"champion {now}; serving template model-version {serving_version}")


def _eks_cidrs_restored(ctx) -> Check:
    before = ctx.snapshot.get("eks_cidrs")
    if before is None:
        return Check("eks_cidrs_restored", SKIP, "no pre-run CIDR snapshot (aws_identity preflight not run)")
    now = probes.eks_cidrs(ctx.sh, ctx.values["cluster"], ctx.values["aws_region"])
    return Check("eks_cidrs_restored", PASS if now == before else FAIL,
                 "EKS publicAccessCidrs equal pre-run list" if now == before else f"CIDRs differ: {before} -> {now}")


RECOVERY_CHECKS: dict[str, Callable] = {
    "no_training_jobs": _jobs("training", "training"), "no_traffic_jobs": _jobs("release_traffic", "traffic"),
    "no_load_jobs": _jobs("k6", "load"), "rollout_stable": _fresh(preflight.CHECKS["rollout_stable"]),
    "argocd_synced_main": _fresh(preflight.CHECKS["argocd_synced_main"]), "live_matches_git": _live_matches_git,
    "gitops_unchanged": _gitops_unchanged, "stable_digest_restored": _stable_digest_restored,
    "champion_unchanged": _champion_unchanged, "champion_consistent": _champion_consistent,
    "eks_cidrs_restored": _eks_cidrs_restored,
}


def run_recovery(ctx) -> tuple[str, list[Check]]:
    results = []
    for check_id in ctx.scenario.recovery.get("checks", []):
        try:
            results.append(RECOVERY_CHECKS[check_id](ctx))
        except Exception as error:  # noqa: BLE001 - an unreadable state is never "recovered"
            results.append(Check(check_id, FAIL, f"probe error: {type(error).__name__}: {error}"))
    status = VERIFIED if all(r.status in (PASS, SKIP) for r in results) else NEEDS_ATTENTION
    return status, results
