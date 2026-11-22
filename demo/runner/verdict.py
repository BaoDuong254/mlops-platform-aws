"""Verification engine: pure classifiers from observed state to a verdict.

Statuses:
  PREVIEW       local gate preview only (S2 preview mode); never a cloud verdict.
  PASSED        observed outcome matches the scenario expectation and all invariants hold.
  FAILED        the system produced a real outcome that contradicts the expectation, or an invariant broke.
  INCONCLUSIVE  no system verdict could be established (workflow/infra/network failure, missing evidence).
  TIMEOUT       the flow did not finish within the scenario deadline.
A workflow failure is never interpreted as a canary/gate rejection, and vice versa.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PASSED, FAILED, INCONCLUSIVE, TIMEOUT = "PASSED", "FAILED", "INCONCLUSIVE", "TIMEOUT"
FINISHED_OK = {"success"}
TIMEOUT_CONCLUSIONS = {"timed_out", "cancelled"}
CANARY_REJECTED_MARKER = "Canary rejected"
REVERT_DONE_MARKER = "stable restored and recorded in Git"


@dataclass
class Verdict:
    status: str
    outcome: str
    expected: str
    reasons: list[str] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    source: str = "live"

    def as_dict(self) -> dict:
        return asdict(self)


def _workflow_state(obs: dict) -> tuple[str | None, str | None]:
    workflow = obs.get("workflow") or {}
    if obs.get("timed_out") or workflow.get("status") not in (None, "completed"):
        return TIMEOUT, "workflow did not complete before the scenario deadline"
    if workflow.get("conclusion") in TIMEOUT_CONCLUSIONS:
        return TIMEOUT, f"workflow {workflow['conclusion']} (not a gate/canary decision)"
    if not workflow:
        return INCONCLUSIVE, "workflow run not found"
    return None, None


# --------------------------------------------------------------------------- training


def training_outcome(obs: dict) -> tuple[str, list[str], list[str]]:
    """Return (outcome, reasons, broken_invariants) for one training decision."""
    result, reasons, broken = obs.get("result"), [], []
    if (obs.get("workflow") or {}).get("conclusion") not in FINISHED_OK:
        return "workflow_failure", [f"training workflow conclusion={obs['workflow'].get('conclusion')}; "
                                    "infra/workflow failure, not a gate decision"], []
    if not result:
        return "missing_training_result", ["training-result artifact missing; cannot read the decision"], []
    decision = result.get("validation_status")
    run = obs.get("mlflow_run") or {}
    reasons.append(f"validation_status={decision}, candidate MAE={result.get('validation_mae_minutes')}, "
                   f"MLflow run {result.get('mlflow_run_id')}")
    if run and (run.get("status") != "FINISHED" or run.get("validation_status") != decision):
        broken.append(f"MLflow run {run} does not confirm the decision")
    linked = obs.get("versions_for_run") or []
    if decision == "rejected":
        if result.get("model_version") is not None or linked:
            broken.append(f"rejected candidate has Registry version(s) {linked or result.get('model_version')}")
        if obs.get("champion_after") != obs.get("champion_before"):
            broken.append(f"champion changed {obs.get('champion_before')} -> {obs.get('champion_after')}")
        return "rejected", reasons, broken
    if decision == "approved":
        if not result.get("model_version") or result.get("registry_status") != "READY":
            broken.append("approved decision lacks a READY Registry version")
        return "approved", reasons, broken
    return "unknown_decision", reasons + [f"unexpected decision {decision!r}"], []


def classify_training(obs: dict, expected: str = "rejected") -> Verdict:
    state, why = _workflow_state(obs)
    measurements = {k: obs.get(k) for k in ("champion_before", "champion_after", "preview")}
    if obs.get("result"):
        measurements.update({k: obs["result"].get(k) for k in
                             ("validation_status", "validation_mae_minutes", "model_version", "mlflow_run_id")})
    if state:
        return Verdict(state, "workflow_incomplete", expected, [why], measurements)
    outcome, reasons, broken = training_outcome(obs)
    if outcome in {"workflow_failure", "missing_training_result", "unknown_decision"}:
        return Verdict(INCONCLUSIVE, outcome, expected, reasons, measurements)
    if broken:
        return Verdict(FAILED, outcome, expected, reasons + broken, measurements)
    if expected == "rejected" and outcome == "approved":
        return Verdict(FAILED, outcome, expected, reasons + ["candidate was approved; S1 expects a rejection"], measurements)
    if obs.get("release_job") not in (None, "skipped"):
        return Verdict(FAILED, outcome, expected, reasons + [f"release job ran ({obs['release_job']}) for a training-only scenario"], measurements)
    return Verdict(PASSED, outcome, expected, reasons, measurements)


# --------------------------------------------------------------------------- release / canary


def release_outcome(obs: dict) -> tuple[str, str, list[str]]:
    """Return (outcome, status_if_not_expected_mismatch, reasons)."""
    workflow = obs.get("workflow") or {}
    markers = obs.get("markers") or {}
    analysis = (obs.get("analysis") or {}).get("phase")
    serving = obs.get("serving_after") or {}
    reasons = [f"workflow conclusion={workflow.get('conclusion')}; analysis={analysis}; "
               f"release commit={str(obs.get('release_commit'))[:12]}; revert commit={str(obs.get('revert_commit'))[:12]}"]
    if not obs.get("release_commit"):
        reasons.append("no GitOps release commit: failure happened before deployment (build/ECR/guard/network)")
        return "failed_before_gitops", INCONCLUSIVE, reasons
    if obs.get("revert_commit") and markers.get(CANARY_REJECTED_MARKER):
        return "aborted_reverted", PASSED, reasons
    if workflow.get("conclusion") in FINISHED_OK:
        champion = (obs.get("result") or {}).get("champion")
        if champion in {"promoted", "already-promoted"}:
            return "promoted", PASSED, reasons
        return "success_without_promotion_record", INCONCLUSIVE, reasons + ["m32-result.json missing champion field"]
    if serving.get("abort"):
        return "aborted_not_reverted", FAILED, reasons + ["Rollout aborted but Git still names the rejected release"]
    if analysis == "Inconclusive" or serving.get("paused"):
        return "analysis_inconclusive", INCONCLUSIVE, reasons + ["AnalysisRun Inconclusive / Rollout paused"]
    return "workflow_failure_after_gitops", INCONCLUSIVE, reasons + [
        "release failed after the GitOps commit without a canary verdict (timeout/network/verification); "
        "do not assume rollback — recovery checks decide"]


def release_invariants(obs: dict, outcome: str) -> list[str]:
    broken, serving = [], obs.get("serving_after") or {}
    expected = obs.get("expected") or {}
    if outcome == "promoted":
        if serving.get("stable_hash") != serving.get("current_hash") or serving.get("phase") != "Healthy":
            broken.append(f"Rollout not stable after promotion: {serving}")
        if expected.get("image") and serving.get("live_image") != expected["image"]:
            broken.append("live image differs from released digest")
        if str(obs.get("champion_after")) != str(obs.get("model_version")):
            broken.append(f"champion {obs.get('champion_after')} != released version {obs.get('model_version')}")
        if (obs.get("analysis") or {}).get("phase") not in (None, "Successful"):
            broken.append(f"background AnalysisRun {obs['analysis']['phase']} on a promoted release")
    if outcome == "aborted_reverted":
        if obs.get("stable_image_before") and serving.get("live_image") != obs["stable_image_before"]:
            broken.append("live image after revert differs from the pre-run stable digest")
        if obs.get("champion_after") != obs.get("champion_before"):
            broken.append(f"champion changed {obs.get('champion_before')} -> {obs.get('champion_after')} on a rejected canary")
        if (obs.get("analysis") or {}).get("phase") not in (None, "Failed", "Error") and not obs.get("smoke_failed"):
            broken.append(f"canary aborted but AnalysisRun phase is {obs['analysis']['phase']}")
    return broken


def classify_release(obs: dict, expected: str) -> Verdict:
    measurements = {k: obs.get(k) for k in ("release_id", "release_commit", "revert_commit", "model_version",
                                            "champion_before", "champion_after", "candidate_cpu", "stable_cpu")}
    measurements["analysis"] = obs.get("analysis")
    state, why = _workflow_state(obs)
    if state:
        return Verdict(state, "workflow_incomplete", expected, [why], measurements)
    outcome, status, reasons = release_outcome(obs)
    if status != PASSED:
        return Verdict(status, outcome, expected, reasons, measurements)
    broken = release_invariants(obs, outcome)
    if broken:
        return Verdict(FAILED, outcome, expected, reasons + broken, measurements)
    if outcome != expected:
        note = {"promoted": "canary was promoted although a rejection was expected (fault did not trip the gates)",
                "aborted_reverted": "canary was rejected by real gates although promotion was expected"}[outcome]
        return Verdict(FAILED, outcome, expected, reasons + [note], measurements)
    return Verdict(PASSED, outcome, expected, reasons, measurements)


def classify_pipeline(obs: dict) -> Verdict:
    """S2: the real decision is reported; consistency of the downstream path is what passes/fails."""
    verdict = classify_training({**obs, "release_job": None}, expected="approved_or_rejected")
    verdict.measurements["preview"] = obs.get("preview")
    if verdict.status != PASSED:
        return verdict
    preview = (obs.get("preview") or {}).get("decision")
    if preview and preview != verdict.outcome:
        verdict.reasons.append(f"cloud decision {verdict.outcome} differs from local preview {preview}")
    if verdict.outcome == "rejected":
        if obs.get("release_job") not in (None, "skipped"):
            return Verdict(FAILED, "rejected", verdict.expected, verdict.reasons + ["release ran after a rejection"], verdict.measurements)
        verdict.reasons.append("release skipped; champion unchanged")
        return verdict
    release = obs.get("release") or {}
    if not release:
        return Verdict(INCONCLUSIVE, "approved", verdict.expected, verdict.reasons + ["release evidence missing"], verdict.measurements)
    sub = classify_release(release, expected="promoted")
    outcome = "approved_" + sub.outcome
    # Approved then promoted, or approved then safely rejected by real canary gates, are both consistent.
    consistent = sub.outcome in {"promoted", "aborted_reverted"} and not release_invariants(release, sub.outcome)
    status = PASSED if consistent else (FAILED if sub.status == PASSED else sub.status)
    return Verdict(status, outcome, verdict.expected, verdict.reasons + sub.reasons,
                   {**verdict.measurements, "release": sub.measurements})


# --------------------------------------------------------------------------- HPA / load


HPA_TOLERANCE = 0.1  # Kubernetes default: scale only when |current/target - 1| > 0.1


def analyze_hpa(samples: list[dict], loads: list[dict]) -> Verdict:
    measurements: dict[str, Any] = {"profiles": {}, "samples": len(samples)}
    if any(not load.get("ok") for load in loads):
        failed = [load["name"] for load in loads if not load.get("ok")]
        return Verdict(INCONCLUSIVE, "load_failed", "hpa_consistent",
                       [f"k6 profile(s) {failed} did not complete; scaling result not attributable"], measurements)
    usable = [s for s in samples if s.get("cpu_utilization") is not None and s.get("desired_replicas") is not None]
    if len(usable) < 3:
        return Verdict(INCONCLUSIVE, "metrics_unavailable", "hpa_consistent",
                       ["fewer than 3 HPA samples with CPU metrics"], measurements)
    target = next(s["cpu_target"] for s in usable if s.get("cpu_target"))
    min_r = next((s["min_replicas"] for s in usable if s.get("min_replicas")), 1)
    max_r = next((s["max_replicas"] for s in usable if s.get("max_replicas")), None)
    baseline = usable[0]["desired_replicas"]
    in_load = [s for s in usable if str(s.get("phase", "")).startswith("load:")]
    after = [s for s in usable if s.get("phase") == "cooldown"]
    for name in dict.fromkeys(s["phase"][5:] for s in in_load):
        window = [s for s in in_load if s["phase"] == "load:" + name]
        rps = [s["rps"] for s in window if s.get("rps") is not None]
        measurements["profiles"][name] = {
            "cpu_max": max(s["cpu_utilization"] for s in window),
            "cpu_mean": round(sum(s["cpu_utilization"] for s in window) / len(window), 1),
            "desired_max": max(s["desired_replicas"] for s in window),
            "current_max": max(s.get("current_replicas") or 0 for s in window),
            "pods_ready_max": max(s.get("pods_ready") or 0 for s in window),
            "nodeclaims_max": max(s.get("nodeclaims") or 0 for s in window),
            "rps_max": round(max(rps), 2) if rps else None}
    peak = max((s["desired_replicas"] for s in in_load), default=baseline)
    hot = [s for s in in_load if s["cpu_utilization"] > target * (1 + HPA_TOLERANCE)]
    measurements.update(cpu_target=target, min_replicas=min_r, max_replicas=max_r, baseline_desired=baseline,
                        peak_desired=peak, hot_samples=len(hot),
                        final_desired=after[-1]["desired_replicas"] if after else None)
    scaled_out = peak > baseline
    reasons = [f"baseline desired={baseline}, peak desired={peak}, samples above {target}%×1.1: {len(hot)}"]
    if len(hot) >= 2 and not scaled_out and baseline < (max_r or baseline + 1):
        return Verdict(FAILED, "no_scale_out_despite_high_cpu", "hpa_consistent",
                       reasons + ["CPU stayed above target but HPA did not raise desired replicas"], measurements)
    if not scaled_out:
        return Verdict(PASSED, "no_scale_needed", "hpa_consistent",
                       reasons + ["CPU never exceeded the HPA tolerance band; staying at baseline is correct"], measurements)
    if not hot:
        reasons.append("scale-out happened although sampled CPU stayed in band (short spike between samples?)")
    if after and after[-1]["desired_replicas"] <= min_r:
        back = next(s["t"] for s in after if s["desired_replicas"] <= min_r)
        measurements["scale_in_seconds_after_load"] = round(back - after[0]["t"], 1)
        return Verdict(PASSED, "scaled_out_and_in", "hpa_consistent",
                       reasons + [f"returned to {min_r} replica(s) {measurements['scale_in_seconds_after_load']}s after load"],
                       measurements)
    return Verdict(INCONCLUSIVE, "scaled_out_scale_in_pending", "hpa_consistent",
                   reasons + ["scale-out verified; scale-in not observed within the window (stabilization 300s) — "
                              "re-run `verify` later or extend observe_after_seconds"], measurements)
