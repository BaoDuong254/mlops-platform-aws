"""Live demo framework: local validation of schema, configs, decisions, rendering, verification,
preflight/recovery and dry-runs. Fixture data proves orchestration logic only, never cloud behaviour."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path

import pytest
import yaml

from demo.runner import cli, engines, preflight, recovery, report, spec, training, verdict
from demo.runner.context import Context
from demo.runner.probes import FixtureRegistry
from demo.runner.shell import FixtureShell

ROOT = Path(__file__).resolve().parents[2]
REPO = "HaiXon231/mlops-platform-aws"
STABLE_IMAGE = "160616607747.dkr.ecr.ap-southeast-1.amazonaws.com/mlops-aws-inference-dev@sha256:" + "a" * 64
NEW_IMAGE = STABLE_IMAGE[:-64] + "b" * 64
MAIN = "c" * 40


def script(name):
    spec_ = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(module)
    return module


@pytest.fixture
def scenarios():
    return spec.load_scenarios()


def values_for(preset_name, scenario_id):
    scenarios = spec.load_scenarios()
    return spec.resolve_inputs(spec.load_preset(preset_name), scenarios[scenario_id])


# ============================================================ 1. schema / config validation


def test_shipped_scenarios_and_presets_validate(scenarios):
    assert set(scenarios) == {"S1", "S2", "S3", "S4", "S5"}
    for name in ("verification", "demo"):
        preset = spec.load_preset(name)
        assert spec.validate_preset(preset, scenarios) == []
        assert set(preset["scenarios"]) == set(scenarios), "each preset carries inputs for every scenario"
    assert cli.main(["validate"]) == 0


@pytest.mark.parametrize("scenario_id,key,value,message", [
    ("S4", "canary_test_cpu", "200m", "not allowed"),
    ("S3", "canary_test_cpu", "100m", "not allowed"),       # S3 must never inject the fault
    ("S4", "canary_test_cpu", "off", "not allowed"),        # S4 must inject it
    ("S3", "model_version", "latest", "numeric version"),
    ("S3", "model_version", 3, "numeric version"),          # unquoted YAML int
    ("S1", "training_config", "configs/training-demo.yaml", "outside the auto-training fingerprint"),
    ("S1", "model_overrides", {"seed": 7}, "not an allowed model parameter"),
    ("S1", "model_overrides", {"num_leaves": 0}, "positive"),
    ("S1", "model_overrides", {"num_leaves": True}, "must be int"),
    ("S2", "mode", "yolo", "not allowed"),
    ("S5", "load_profiles", [{"name": "x", "rate": 500, "vus": 10, "duration_seconds": 120}], "limits"),
    ("S5", "load_profiles", [{"name": "x", "rate": 20, "vus": 10, "duration_seconds": 10}], "limits"),
    ("S5", "load_profiles", [{"name": "x", "rate": 20, "vus": 10}], "exactly"),
])
def test_invalid_preset_inputs_are_rejected(scenarios, scenario_id, key, value, message):
    preset = copy.deepcopy(spec.load_preset("verification"))
    preset["scenarios"][scenario_id][key] = value
    with pytest.raises(spec.SpecError, match=message):
        spec.resolve_inputs(preset, scenarios[scenario_id])


def test_broken_scenario_definitions_are_reported(tmp_path):
    good = yaml.safe_load((ROOT / "demo/scenarios/s3-canary-success.yaml").read_text())
    bad = copy.deepcopy(good) | {"id": "S9", "engine": "magic"}
    bad["preflight"] = ["rollout_stable", "read_minds"]
    bad["verify"] = {"expect": "vibes"}
    (tmp_path / "bad.yaml").write_text(yaml.safe_dump(bad))
    with pytest.raises(spec.SpecError) as error:
        spec.load_scenarios(tmp_path)
    text = str(error.value)
    assert "engine must be" in text and "read_minds" in text and "verify.expect" in text
    (tmp_path / "bad.yaml").write_text("id: S1\ntrigger: [a: b: c]\n")
    with pytest.raises(spec.SpecError, match="invalid YAML"):
        spec.load_scenarios(tmp_path)


def test_new_scenario_needs_only_a_yaml_file(tmp_path):
    for path in (ROOT / "demo/scenarios").glob("*.yaml"):
        (tmp_path / path.name).write_text(path.read_text())
    extra = yaml.safe_load((ROOT / "demo/scenarios/s3-canary-success.yaml").read_text())
    extra.update(id="S6", title="Re-release champion")
    (tmp_path / "s6.yaml").write_text(yaml.safe_dump(extra))
    loaded = spec.load_scenarios(tmp_path)
    preset = copy.deepcopy(spec.load_preset("demo"))
    preset["scenarios"]["S6"] = dict(preset["scenarios"]["S3"])
    values = spec.resolve_inputs(preset, loaded["S6"])
    assert engines.build_plan(loaded["S6"], values)[0].argv[:3] == ["gh", "workflow", "run"]


def test_values_cannot_inject_arguments_or_shell_syntax():
    argv = spec.render_argv("gh workflow run release.yml -f model_version={v}", {"v": "3 -f canary_test_cpu=100m; rm -rf /"})
    assert argv[-1] == "model_version=3 -f canary_test_cpu=100m; rm -rf /" and len(argv) == 6
    with pytest.raises(spec.SpecError, match="no value"):
        spec.render_argv("echo {missing}", {})


# ============================================================ 2. training config compatibility


@pytest.mark.parametrize("path", ["configs/demo-rejected.yaml", "configs/demo-candidate.yaml"])
def test_demo_configs_keep_evaluation_identity_and_match_presets(path):
    training.validate_identity(ROOT / path)               # same guard the cloud workflow runs
    config, base = (yaml.safe_load((ROOT / p).read_text()) for p in (path, "configs/training.yaml"))
    for key in training.IDENTITY_FIELDS + ("data_dir", "early_stopping_rounds", "parquet_batch_size"):
        assert config[key] == base[key], key
    assert script("workflow_guard").re.fullmatch(r"configs/[a-zA-Z0-9_-]+\.yaml", path)  # dispatchable
    assert cli.main(["render-configs"]) == 0             # committed files equal preset rendering


def test_s1_config_is_the_underfit_candidate():
    model = yaml.safe_load((ROOT / "configs/demo-rejected.yaml").read_text())["model"]
    assert (model["n_estimators"], model["num_leaves"]) == (1, 2)


def test_demo_configs_do_not_trigger_automatic_training():
    assert not training.in_auto_fingerprint("configs/demo-rejected.yaml")
    assert not training.in_auto_fingerprint("configs/demo-candidate.yaml")
    assert training.in_auto_fingerprint("configs/training.yaml")
    assert not any(training.in_auto_fingerprint(str(p.relative_to(ROOT)))
                   for p in (ROOT / "demo").rglob("*") if p.is_file())


def test_identity_change_is_rejected_by_the_cloud_guard():
    path = ROOT / "configs/demo-zz-identity-test.yaml"
    config = yaml.safe_load((ROOT / "configs/training.yaml").read_text())
    config["periods"]["validation"] = ["2025-02"]
    config["periods"]["train"] = ["2025-01"]
    try:
        path.write_text(yaml.safe_dump(config))
        with pytest.raises(ValueError, match="evaluation identity"):
            training.validate_identity(path)
    finally:
        path.unlink(missing_ok=True)


def test_apply_candidate_changes_only_the_model_block():
    base = (ROOT / "configs/training.yaml").read_text()
    patched = training.patch_model_block(base, {"num_leaves": 255, "reg_lambda": 5.0})
    changed = [(a, b) for a, b in zip(base.splitlines(), patched.splitlines()) if a != b]
    assert changed == [("  num_leaves: 127", "  num_leaves: 255")]
    assert patched.splitlines()[-1] == "  reg_lambda: 5.0"
    before, after = yaml.safe_load(base), yaml.safe_load(patched)
    assert {k: v for k, v in after.items() if k != "model"} == {k: v for k, v in before.items() if k != "model"}


def test_semantic_check_detects_drift_on_main():
    text = (ROOT / "configs/demo-rejected.yaml").read_text()
    base = (ROOT / "configs/training.yaml").read_text()
    assert training.semantic_match(text, base, {"n_estimators": 1, "num_leaves": 2}) == []
    assert training.semantic_match(text, base, {"n_estimators": 5}) == ["model.n_estimators=1 != 5"]
    drifted = base.replace("seed: 42", "seed: 43")
    assert "identity field seed differs" in training.semantic_match(text, drifted, {})


# ============================================================ 3. approved / rejected logic


def test_preview_uses_the_cloud_gate_threshold():
    from training.gate import Reference, passes_gate
    reference = 3.6445432012
    boundary = reference * (1 - training.gate_fraction())
    assert training.gate_fraction() == 0.01
    assert training.preview_decision(boundary, reference)["decision"] == "approved"
    assert training.preview_decision(boundary * 1.0000001, reference)["decision"] == "rejected"
    for mae in (3.0, 3.6139, 3.6081, 7.8621684646):
        expected = passes_gate(mae, Reference("champion", reference), 0.01)
        assert (training.preview_decision(mae, reference)["decision"] == "approved") == expected
    with pytest.raises(ValueError):
        training.preview_decision(float("nan"), reference)


def _training_obs(**overrides):
    obs = {"workflow": {"status": "completed", "conclusion": "success", "url": "u"}, "timed_out": False,
           "result": {"validation_status": "rejected", "model_version": None, "registry_status": None,
                      "validation_mae_minutes": 7.86, "mlflow_run_id": "r" * 32},
           "mlflow_run": {"status": "FINISHED", "validation_status": "rejected"}, "versions_for_run": [],
           "champion_before": "3", "champion_after": "3", "release_job": "skipped"}
    obs.update(overrides)
    return obs


@pytest.mark.parametrize("overrides,status,outcome", [
    ({}, "PASSED", "rejected"),
    ({"result": {"validation_status": "approved", "model_version": "4", "registry_status": "READY"},
      "mlflow_run": None}, "FAILED", "approved"),
    ({"versions_for_run": ["4"]}, "FAILED", "rejected"),
    ({"champion_after": "4"}, "FAILED", "rejected"),
    ({"mlflow_run": {"status": "FAILED", "validation_status": "rejected"}}, "FAILED", "rejected"),
    ({"release_job": "success"}, "FAILED", "rejected"),
    ({"workflow": {"status": "completed", "conclusion": "failure"}}, "INCONCLUSIVE", "workflow_failure"),
    ({"result": None}, "INCONCLUSIVE", "missing_training_result"),
    ({"workflow": {"status": "in_progress"}, "timed_out": True}, "TIMEOUT", "workflow_incomplete"),
    ({"workflow": {"status": "completed", "conclusion": "cancelled"}}, "TIMEOUT", "workflow_incomplete"),
])
def test_training_rejection_classification(overrides, status, outcome):
    result = verdict.classify_training(_training_obs(**overrides), "rejected")
    assert (result.status, result.outcome) == (status, outcome), result.reasons


def _release_obs(kind="promoted", **overrides):
    serving = {"phase": "Healthy", "abort": False, "paused": False, "stable_hash": "new", "current_hash": "new",
               "live_image": NEW_IMAGE}
    obs = {"workflow": {"status": "completed", "conclusion": "success", "url": "u"}, "timed_out": False,
           "release_id": "m32-9-1", "release_commit": "d" * 40, "revert_commit": None,
           "markers": {verdict.CANARY_REJECTED_MARKER: False}, "analysis": {"phase": "Successful", "name": "ar"},
           "serving_after": serving, "expected": {"image": NEW_IMAGE}, "result": {"champion": "promoted"},
           "model_version": "3", "champion_before": "3", "champion_after": "3", "stable_image_before": STABLE_IMAGE}
    if kind == "aborted":
        obs.update(workflow={"status": "completed", "conclusion": "failure"}, revert_commit="e" * 40,
                   markers={verdict.CANARY_REJECTED_MARKER: True}, analysis={"phase": "Failed"}, result=None,
                   serving_after=serving | {"stable_hash": "old", "current_hash": "old", "live_image": STABLE_IMAGE},
                   candidate_cpu="100m", stable_cpu="1")
    obs.update(overrides)
    return obs


def test_pipeline_reports_the_real_decision_either_way():
    rejected = verdict.classify_pipeline(_training_obs(preview={"decision": "approved"}))
    assert (rejected.status, rejected.outcome) == ("PASSED", "rejected")
    assert any("differs from local preview" in r for r in rejected.reasons)
    approved = _training_obs(result={"validation_status": "approved", "model_version": "4", "registry_status": "READY",
                                     "mlflow_run_id": "r" * 32},
                             mlflow_run={"status": "FINISHED", "validation_status": "approved"}, champion_after="4",
                             release_job="success",
                             release=_release_obs(model_version="4", champion_after="4"))
    assert verdict.classify_pipeline(approved).outcome == "approved_promoted"
    assert verdict.classify_pipeline(approved).status == "PASSED"
    safe_abort = dict(approved, release=_release_obs("aborted", model_version="4"))
    assert (verdict.classify_pipeline(safe_abort).status, verdict.classify_pipeline(safe_abort).outcome) == \
        ("PASSED", "approved_aborted_reverted")
    broken = dict(approved, release=_release_obs(release_commit=None, workflow={"status": "completed", "conclusion": "failure"}))
    assert verdict.classify_pipeline(broken).status == "INCONCLUSIVE"
    ran_anyway = _training_obs(release_job="success")
    assert verdict.classify_pipeline(ran_anyway).status == "FAILED"


# ============================================================ 4. release input validation


@pytest.mark.parametrize("name,version,cpu", [
    ("nyc-taxi-trip-duration", "3", "off"), ("nyc-taxi-trip-duration", "12", "100m"),
    ("Nyc", "3", "off"), ("nyc-taxi-trip-duration", "0", "off"), ("nyc-taxi-trip-duration", "v3", "off"),
    ("nyc-taxi-trip-duration", "3", "200m"), ("nyc-taxi-trip-duration", "3", "50m"),
])
def test_framework_release_rules_equal_the_workflow_guard(name, version, cpu):
    guard = script("workflow_guard")
    env = {"AWS_ACCOUNT_ID": "160616607747", "EXPECTED_REPOSITORY_ID": "1", "EXPECTED_OWNER_ID": "2",
           "GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY_ID": "1", "GITHUB_REPOSITORY_OWNER_ID": "2",
           "AWS_REGION": "ap-southeast-1", "MODEL_NAME": name, "MODEL_VERSION": version, "CANARY_TEST_CPU": cpu,
           "INFERENCE_ECR_REPOSITORY": "160616607747.dkr.ecr.ap-southeast-1.amazonaws.com/mlops-aws-inference-dev"}
    try:
        guard.validate("release", env)
        guard_ok = True
    except ValueError:
        guard_ok = False
    scenarios = spec.load_scenarios()
    framework_ok = False
    for scenario_id in ("S3", "S4"):
        preset = copy.deepcopy(spec.load_preset("demo"))
        preset["scenarios"][scenario_id].update(model_name=name, model_version=version, canary_test_cpu=cpu)
        try:
            spec.resolve_inputs(preset, scenarios[scenario_id])
            framework_ok = True
        except spec.SpecError:
            pass
    assert framework_ok == guard_ok
    release_yml = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assert release_yml["on"]["workflow_dispatch"]["inputs"]["canary_test_cpu"]["options"] == ["off", "100m"]


# ============================================================ 5. GitOps rendering


def test_fault_injection_changes_only_the_candidate_pod_template(release, tmp_path):
    gitops = script("m32_gitops")
    gitops.prepare_bundle(release, "m32-9-1", "f" * 40)
    source = ROOT / "deploy/gitops/inference/deployment.yaml"
    rollout_before = (ROOT / "deploy/gitops/inference/rollout.yaml").read_text()
    image = gitops.ECR + "@sha256:" + "b" * 64
    rendered = {}
    for cpu in ("off", "100m"):
        target = tmp_path / f"deployment-{cpu}.yaml"
        target.write_text(source.read_text())
        gitops.update_manifest(release, image, deployment_path=target, candidate_cpu=cpu)
        rendered[cpu] = yaml.safe_load(target.read_text())
    original = yaml.safe_load(source.read_text())
    base_res = original["spec"]["template"]["spec"]["containers"][0]["resources"]
    for cpu, doc in rendered.items():
        container = doc["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == image
        assert container["resources"]["requests"]["memory"] == base_res["requests"]["memory"]
        assert {k: v for k, v in doc["spec"].items() if k != "template"} == \
            {k: v for k, v in original["spec"].items() if k != "template"}, "selector/strategy untouched"
        assert doc["spec"]["template"] != original["spec"]["template"], "new template => new pod-template-hash"
    off, faulty = (rendered[c]["spec"]["template"] for c in ("off", "100m"))
    assert off["spec"]["containers"][0]["resources"] == base_res
    assert faulty["spec"]["containers"][0]["resources"]["requests"]["cpu"] == "100m"
    assert faulty["spec"]["containers"][0]["resources"]["limits"]["cpu"] == "100m"
    assert faulty["metadata"]["annotations"]["mlops.aws/test-original-cpu-limit"] == str(base_res["limits"]["cpu"])
    assert (ROOT / "deploy/gitops/inference/rollout.yaml").read_text() == rollout_before
    # A later normal release restores the original CPU from the annotations.
    restored = tmp_path / "restored.yaml"
    restored.write_text(yaml.safe_dump(rendered["100m"]))
    gitops.update_manifest(release, image, deployment_path=restored, candidate_cpu="off")
    back = yaml.safe_load(restored.read_text())["spec"]["template"]
    assert back["spec"]["containers"][0]["resources"] == base_res
    assert "mlops.aws/test-original-cpu-limit" not in back["metadata"]["annotations"]


# ============================================================ 6. release / HPA verification states


@pytest.mark.parametrize("obs,expected,status,outcome", [
    (_release_obs(), "promoted", "PASSED", "promoted"),
    (_release_obs("aborted"), "aborted_reverted", "PASSED", "aborted_reverted"),
    (_release_obs(), "aborted_reverted", "FAILED", "promoted"),            # fault did not trip gates
    (_release_obs("aborted"), "promoted", "FAILED", "aborted_reverted"),    # real rejection on S3
    (_release_obs("aborted", champion_after="4"), "aborted_reverted", "FAILED", "aborted_reverted"),
    (_release_obs("aborted", serving_after={"live_image": NEW_IMAGE}), "aborted_reverted", "FAILED", "aborted_reverted"),
    (_release_obs(champion_after="2"), "promoted", "FAILED", "promoted"),
    (_release_obs(serving_after={"phase": "Healthy", "stable_hash": "old", "current_hash": "new", "live_image": NEW_IMAGE}),
     "promoted", "FAILED", "promoted"),
    (_release_obs(release_commit=None, workflow={"status": "completed", "conclusion": "failure"}),
     "aborted_reverted", "INCONCLUSIVE", "failed_before_gitops"),
    (_release_obs(workflow={"status": "completed", "conclusion": "failure"}, result=None,
                  analysis={"phase": "Inconclusive"}, serving_after={"paused": True}),
     "promoted", "INCONCLUSIVE", "analysis_inconclusive"),
    (_release_obs(workflow={"status": "completed", "conclusion": "failure"}, result=None, analysis={"phase": "Running"},
                  serving_after={}), "aborted_reverted", "INCONCLUSIVE", "workflow_failure_after_gitops"),
    (_release_obs("aborted", revert_commit=None, serving_after={"abort": True}), "aborted_reverted",
     "FAILED", "aborted_not_reverted"),
    # A revert without the workflow's own rejection marker (e.g. manual revert) is not a gate verdict.
    (_release_obs("aborted", markers={verdict.CANARY_REJECTED_MARKER: False}, serving_after={}), "aborted_reverted",
     "INCONCLUSIVE", "workflow_failure_after_gitops"),
    (_release_obs(workflow={"status": "in_progress"}, timed_out=True), "promoted", "TIMEOUT", "workflow_incomplete"),
    (_release_obs(workflow={"status": "completed", "conclusion": "timed_out"}), "aborted_reverted", "TIMEOUT",
     "workflow_incomplete"),
])
def test_release_verdicts_separate_canary_rejection_from_failures(obs, expected, status, outcome):
    result = verdict.classify_release(obs, expected)
    assert (result.status, result.outcome) == (status, outcome), result.reasons


def _hpa(t, phase, cpu, desired, current=None):
    return {"t": t, "phase": phase, "cpu_utilization": cpu, "cpu_target": 60, "desired_replicas": desired,
            "current_replicas": current or desired, "min_replicas": 1, "max_replicas": 3, "pods_ready": desired,
            "nodeclaims": 2, "rps": 20.0 if phase.startswith("load") else 0.0}


OK_LOAD = [{"name": "peak", "ok": True}]


@pytest.mark.parametrize("samples,loads,status,outcome", [
    ([_hpa(0, "baseline", 5, 1), _hpa(15, "load:peak", 95, 2), _hpa(30, "load:peak", 120, 3),
      _hpa(45, "cooldown", 4, 3), _hpa(400, "cooldown", 3, 1)], OK_LOAD, "PASSED", "scaled_out_and_in"),
    ([_hpa(0, "baseline", 5, 1), _hpa(15, "load:peak", 30, 1), _hpa(30, "load:peak", 40, 1),
      _hpa(45, "cooldown", 3, 1)], OK_LOAD, "PASSED", "no_scale_needed"),
    ([_hpa(0, "baseline", 5, 1), _hpa(15, "load:peak", 95, 1), _hpa(30, "load:peak", 110, 1),
      _hpa(45, "cooldown", 3, 1)], OK_LOAD, "FAILED", "no_scale_out_despite_high_cpu"),
    ([_hpa(0, "baseline", 5, 1), _hpa(15, "load:peak", 95, 2), _hpa(30, "load:peak", 99, 3),
      _hpa(45, "cooldown", 4, 3)], OK_LOAD, "INCONCLUSIVE", "scaled_out_scale_in_pending"),
    ([_hpa(0, "baseline", None, 1), _hpa(15, "load:peak", None, 1)], OK_LOAD, "INCONCLUSIVE", "metrics_unavailable"),
    ([_hpa(0, "baseline", 5, 1)] * 4, [{"name": "peak", "ok": False}], "INCONCLUSIVE", "load_failed"),
])
def test_hpa_verdict_follows_measured_metrics(samples, loads, status, outcome):
    result = verdict.analyze_hpa(samples, loads)
    assert (result.status, result.outcome) == (status, outcome), result.reasons


def test_hpa_measurements_are_reported_per_profile():
    samples = [_hpa(0, "baseline", 5, 1), _hpa(15, "load:warm", 20, 1), _hpa(30, "load:peak", 120, 3),
               _hpa(45, "cooldown", 3, 1)]
    result = verdict.analyze_hpa(samples, [{"name": "warm", "ok": True}, {"name": "peak", "ok": True}])
    assert result.measurements["profiles"]["peak"]["cpu_max"] == 120
    assert result.measurements["profiles"]["warm"]["desired_max"] == 1
    assert result.measurements["peak_desired"] == 3 and result.measurements["scale_in_seconds_after_load"] == 0


# ============================================================ 7. preflight failure and recovery


def healthy_shell(account="160616607747", active_run=False, rollout_phase="Healthy", traffic_job=False,
                  cidrs=("1.2.3.4/32",)) -> FixtureShell:
    deployment = yaml.safe_load((ROOT / "deploy/gitops/inference/deployment.yaml").read_text())
    live_image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    release_id = deployment["spec"]["template"]["metadata"]["annotations"]["mlops.aws/release-id"]
    rollout = {"status": {"phase": rollout_phase, "stableRS": "h1", "currentPodHash": "h1"}, "spec": {}}
    app = {"status": {"sync": {"status": "Synced", "revision": MAIN}, "health": {"status": "Healthy"}}}
    live = {"spec": {"template": {"metadata": {"annotations": {"mlops.aws/release-id": release_id}},
                                  "spec": {"containers": [{"image": live_image}]}}}}
    jobs = {"items": [{"metadata": {"name": "m32-traffic-1-1"}, "status": {"active": 1}}] if traffic_job else []}
    runs = [{"databaseId": 1, "status": "in_progress", "workflowName": "Approved Model Release"}] if active_run else []
    hpa = {"spec": {"minReplicas": 1, "maxReplicas": 3, "metrics": [{"resource": {"name": "cpu", "target": {"averageUtilization": 60}}}]},
           "status": {"currentReplicas": 1, "desiredReplicas": 1,
                      "currentMetrics": [{"resource": {"name": "cpu", "current": {"averageUtilization": 4}}}]}}
    sh = FixtureShell()
    sh.add(["aws", "sts"], account + "\n").add(["aws", "eks", "describe-cluster"], list(cidrs))
    sh.add(["kubectl", "config", "current-context"], "arn:aws:eks:ap-southeast-1:1:cluster/mlops-aws-dev\n")
    sh.add(["kubectl", "--request-timeout=20s", "get", "namespace", "mlops"], {"metadata": {}})
    sh.add(["gh", "auth", "status"]).add(["gh", "api"], "true\n")
    sh.add(["git", "fetch"]).add(["git", "rev-parse"], MAIN + "\n")
    sh.add(["git", "show", "origin/main:deploy/gitops/inference/deployment.yaml"], yaml.safe_dump(deployment))
    for path in ("configs/demo-rejected.yaml", "configs/training.yaml"):
        sh.add(["git", "show", f"origin/main:{path}"], (ROOT / path).read_text())
    sh.add(["gh", "run", "list"], runs)
    sh.add(["kubectl", "--request-timeout=20s", "-n", "mlops-release-traffic", "get", "jobs"], jobs)
    sh.add(["kubectl", "--request-timeout=20s", "-n", "mlops", "get", "jobs"], {"items": []})
    sh.add(["kubectl", "--request-timeout=20s", "-n", "mlops", "get", "rollout.argoproj.io"], rollout)
    sh.add(["kubectl", "--request-timeout=20s", "-n", "argocd", "get", "application"], app)
    sh.add(["kubectl", "--request-timeout=20s", "-n", "mlops", "get", "deployment"], live)
    sh.add(["kubectl", "--request-timeout=20s", "-n", "mlops", "get", "hpa"], hpa)
    sh.add(["kubectl", "--request-timeout=20s", "-n", "mlops", "get", "pods"], {"items": []})
    sh.add(["kubectl", "--request-timeout=20s", "get", "nodeclaims"], {"items": []})
    sh.add(["kubectl", "--request-timeout=20s", "-n", "mlops", "get", "analysisrun.argoproj.io"], {"items": []})
    return sh


REGISTRY = {"champion": {"version": "3", "status": "READY", "run_id": "r3", "validation_status": "approved",
                         "validation_mae_minutes": 3.6445432012},
            "versions": [{"version": "3", "status": "READY", "run_id": "r3", "validation_status": "approved"},
                         {"version": "2", "status": "READY", "run_id": "r2", "validation_status": "rejected"}]}


def make_ctx(scenario_id, sh, tmp_path, preset="demo", registry=None, prometheus=lambda q: 1.0):
    scenario = spec.load_scenarios()[scenario_id]
    values = engines.run_values(values_for(preset, scenario_id), stamp="t")
    return Context(sh=sh, scenario=scenario, values=values, preset=preset, evidence_dir=tmp_path / "ev",
                   registry=FixtureRegistry(registry if registry is not None else copy.deepcopy(REGISTRY)),
                   prometheus=prometheus, clock=iter(range(0, 10 ** 6, 5)).__next__, sleep=lambda s: None)


def statuses(checks):
    return {c.id: c.status for c in checks}


def test_healthy_preflight_passes_and_resolves_champion(tmp_path):
    ctx = make_ctx("S4", healthy_shell(), tmp_path)
    checks = preflight.run_preflight(ctx)
    assert preflight.passed(checks), statuses(checks)
    assert ctx.values["model_version"] == "3" and ctx.snapshot["champion"]["version"] == "3"
    assert ctx.snapshot["eks_cidrs"] == ["1.2.3.4/32"] and ctx.snapshot["main_sha"] == MAIN
    assert not any(call[:3] == ["gh", "workflow", "run"] for call in ctx.sh.calls), "preflight is read-only"


@pytest.mark.parametrize("shell_kwargs,registry,failed", [
    ({"account": "999999999999"}, None, "aws_identity"),
    ({"active_run": True}, None, "no_active_workflows"),
    ({"traffic_job": True}, None, "no_active_jobs"),
    ({"rollout_phase": "Degraded"}, None, "rollout_stable"),
    ({}, {"champion": None, "versions": []}, "champion"),
    ({}, {"champion": REGISTRY["champion"] | {"version": "2"}, "versions": REGISTRY["versions"]}, "model_version_approved"),
])
def test_preflight_failures_block_the_trigger(tmp_path, monkeypatch, shell_kwargs, registry, failed):
    sh = healthy_shell(**shell_kwargs)
    ctx = make_ctx("S4", sh, tmp_path, registry=registry)
    checks = preflight.run_preflight(ctx)
    assert statuses(checks)[failed] == "FAIL"
    # End to end: `run --execute` stops before any dispatch.
    monkeypatch.setattr(cli, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(cli, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cli, "_context", lambda *a, **k: make_ctx("S4", sh, tmp_path, registry=registry))
    args = cli.build_parser().parse_args(["run", "--preset", "demo", "--scenario", "S4", "--execute", "--yes"])
    assert cli.cmd_run(args, sh=sh) == 2
    assert not any(call[:3] == ["gh", "workflow", "run"] for call in sh.calls)
    assert not (tmp_path / "lock").exists(), "lock released after abort"


def test_missing_tools_and_crashing_probes_fail_closed(tmp_path):
    sh = healthy_shell()
    sh.missing_tools = {"kubectl"}
    ctx = make_ctx("S3", sh, tmp_path, prometheus=lambda q: (_ for _ in ()).throw(RuntimeError("no tunnel")))
    result = statuses(preflight.run_preflight(ctx))
    assert result["tools"] == "FAIL" and result["monitoring"] == "FAIL"


def test_lock_prevents_parallel_scenarios(tmp_path):
    lock = tmp_path / "lock"
    cli.acquire_lock("demo:S3", lock)
    with pytest.raises(RuntimeError, match="another demo scenario"):
        cli.acquire_lock("demo:S5", lock)
    cli.release_lock(lock)
    lock.write_text(json.dumps({"pid": 2 ** 22 + 12345, "owner": "dead"}))   # stale owner
    cli.acquire_lock("demo:S5", lock)
    assert json.loads(lock.read_text())["pid"] == os.getpid()


def test_recovery_is_verified_only_from_read_back_state(tmp_path):
    ctx = make_ctx("S4", healthy_shell(), tmp_path)
    preflight.run_preflight(ctx)
    status, checks = recovery.run_recovery(ctx)
    assert status == recovery.VERIFIED, statuses(checks)
    # Leftover traffic Job, CIDR drift and a moved champion each require attention.
    for sh, registry, failed in ((healthy_shell(traffic_job=True), None, "no_traffic_jobs"),
                                 (healthy_shell(cidrs=("0.0.0.0/0",)), None, "eks_cidrs_restored"),
                                 (healthy_shell(), {"champion": REGISTRY["champion"] | {"version": "9"},
                                                    "versions": REGISTRY["versions"]}, "champion_unchanged")):
        after = make_ctx("S4", sh, tmp_path, registry=registry)
        after.snapshot = copy.deepcopy(ctx.snapshot)
        status, checks = recovery.run_recovery(after)
        assert status == recovery.NEEDS_ATTENTION and statuses(checks)[failed] == "FAIL"


def test_load_scenario_deletes_only_its_own_k6_jobs_even_on_failure(tmp_path):
    sh = healthy_shell()
    sh.add(["make", "monitoring-load-test", "M33_TARGET=service", "M33_VUS=10"],
           "Load test: job.batch/m33-k6-abc12. Watch HPA\n     iterations.....: 2400 19.9/s\n")
    sh.add(["make", "monitoring-load-test", "M33_TARGET=service", "M33_VUS=30"],
           "Load test: job.batch/m33-k6-def34. Watch HPA\n", returncode=1)
    sh.add(["kubectl", "-n", "mlops", "delete", "job"])
    ctx = make_ctx("S5", sh, tmp_path)
    obs, result = engines.execute_load(ctx, engines.build_plan(ctx.scenario, ctx.values), [])
    deleted = [call[-2] for call in sh.calls if call[:4] == ["kubectl", "-n", "mlops", "delete"]]
    assert deleted == ["m33-k6-abc12", "m33-k6-def34"]
    assert result.outcome == "load_failed" and result.status == "INCONCLUSIVE"
    assert obs["loads"][0]["k6_summary"]["iterations"].startswith("2400")


# ============================================================ 8. dry-runs and orchestration


GOLDEN = {
    "S1": "gh workflow run train.yml --repo HaiXon231/mlops-platform-aws --ref main -f training_config=configs/demo-rejected.yaml",
    "S3": "gh workflow run release.yml --repo HaiXon231/mlops-platform-aws --ref main -f model_name=nyc-taxi-trip-duration -f model_version=champion -f canary_test_cpu=off",
    "S4": "gh workflow run release.yml --repo HaiXon231/mlops-platform-aws --ref main -f model_name=nyc-taxi-trip-duration -f model_version=champion -f canary_test_cpu=100m",
}


@pytest.mark.parametrize("preset", ["verification", "demo"])
@pytest.mark.parametrize("scenario_id", ["S1", "S2", "S3", "S4", "S5"])
def test_dry_run_renders_exact_commands_without_executing(preset, scenario_id, capsys, monkeypatch):
    monkeypatch.setattr(cli, "LiveShell", lambda: pytest.fail("dry-run must not create a shell"))
    assert cli.main(["run", "--preset", preset, "--scenario", scenario_id]) == 0
    out = capsys.readouterr().out
    assert "nothing will be executed" in out
    if scenario_id in GOLDEN:
        assert GOLDEN[scenario_id] in out
    assert cli.main(["show", "--preset", preset, "--scenario", scenario_id]) == 0


def test_load_profiles_render_per_preset():
    plans = {p: [s.argv for s in engines.build_plan(spec.load_scenarios()["S5"], values_for(p, "S5"))]
             for p in ("verification", "demo")}
    assert plans["verification"] == [["make", "monitoring-load-test", "M33_TARGET=service", "M33_VUS=10",
                                      "M33_RATE=20", "M33_DURATION_SECONDS=120"]]
    assert [argv[3:] for argv in plans["demo"]] == [["M33_VUS=10", "M33_RATE=20", "M33_DURATION_SECONDS=120"],
                                                    ["M33_VUS=30", "M33_RATE=100", "M33_DURATION_SECONDS=300"]]


def test_s2_modes_and_git_safety():
    scenario = spec.load_scenarios()["S2"]
    preview = engines.build_plan(scenario, engines.run_values(values_for("verification", "S2"), "t"))
    assert [s.id for s in preview] == ["local-preview"]
    automatic = engines.build_plan(scenario, engines.run_values(values_for("demo", "S2"), "t"))
    commands = [s.argv for s in automatic if s.argv]
    git = [argv for argv in commands if argv[0] == "git"]
    assert all(argv[1:3] == ["-C", "artifacts/demo/worktrees/s2-t"] or argv[1] == "worktree" for argv in git), \
        "never touches the operator's working tree"
    push = next(argv for argv in commands if "push" in argv)
    assert push[-1] == "demo/s2-t" and "main" not in push and "--force" not in push
    assert automatic[-1].effect == "manual", "merge to main stays a human action"
    assert ["git", "-C", "artifacts/demo/worktrees/s2-t", "add", "configs/training.yaml"] in commands


def test_s1_execute_end_to_end_on_fixtures(tmp_path, monkeypatch):
    """Orchestration path only: fixture data, so the report must NOT claim cloud verification."""
    sh = healthy_shell()
    run = {"databaseId": 777, "status": "completed", "conclusion": "success", "createdAt": "2999-01-01T00:00:00Z",
           "url": "https://github.com/x/actions/runs/777", "event": "workflow_dispatch", "attempt": 1,
           "jobs": [{"name": "plan", "conclusion": "success"}, {"name": "MLOps Cloud Training", "conclusion": "success"},
                    {"name": "release", "conclusion": "skipped"}]}
    sh.responses = [r for r in sh.responses if r[0][:3] != ("gh", "run", "list")]
    sh.add(["gh", "run", "list"], [run]).add(["gh", "workflow", "run"]).add(["gh", "run", "view"], run)
    sh.add(["gh", "run", "download"])
    ctx = make_ctx("S1", sh, tmp_path)
    result_file = ctx.evidence_dir / "artifacts/train-result-777-1/training-result.json"
    result_file.parent.mkdir(parents=True)
    result_file.write_text(json.dumps({"validation_status": "rejected", "model_version": None, "registry_status": None,
                                       "validation_mae_minutes": 7.8621684646, "mlflow_run_id": "f" * 32}))
    ctx.registry.data["runs"] = {"f" * 32: {"status": "FINISHED", "validation_status": "rejected"}}
    monkeypatch.setattr(cli, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(cli, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cli, "_context", lambda *a, **k: ctx)
    monkeypatch.setattr("demo.runner.collect.find_run", lambda c, *a, **k: run)
    args = cli.build_parser().parse_args(["run", "--preset", "demo", "--scenario", "S1", "--execute", "--yes"])
    assert cli.cmd_run(args, sh=sh) == 0
    dispatched = [call for call in sh.calls if call[:3] == ["gh", "workflow", "run"]]
    assert dispatched == [GOLDEN["S1"].split()]
    evidence = json.loads((ctx.evidence_dir / "evidence.json").read_text())
    assert evidence["verdict"]["status"] == "PASSED" and evidence["recovery"]["status"] == "VERIFIED"
    assert evidence["scope"].startswith("local validated (fixture data")
    report_md = (ctx.evidence_dir / "report.md").read_text()
    assert "cloud verified" not in report_md and "actions/runs/777" in report_md


@pytest.mark.parametrize("mode,live,status,rec,label", [
    ("execute", True, "PASSED", "VERIFIED", "cloud verified"),
    ("execute", True, "PASSED", "NEEDS_ATTENTION", "NOT verified"),
    ("execute", True, "INCONCLUSIVE", "VERIFIED", "NOT verified"),
    ("execute", False, "PASSED", "VERIFIED", "fixture data"),
    ("preview", True, "PASSED", None, "local validated"),
    ("dry-run", False, None, None, "dry-run"),
    ("preflight", True, None, None, "read-only preflight"),
])
def test_scope_labels_never_overclaim(mode, live, status, rec, label):
    assert label in report.scope_label(mode, live, status, rec)


# Field lists printed by the installed gh 2.46 (`gh run view 1 --json` etc.). A field outside these
# makes gh exit non-zero, which live runs would misread as "run not found".
GH_246_FIELDS = {
    ("run", "list"): {"conclusion", "createdAt", "databaseId", "displayTitle", "event", "headBranch", "headSha",
                      "name", "number", "startedAt", "status", "updatedAt", "url", "workflowDatabaseId", "workflowName"},
    ("variable", "list"): {"name", "numSelectedRepos", "selectedReposURL", "updatedAt", "value", "visibility"},
}
GH_246_FIELDS[("run", "view")] = GH_246_FIELDS[("run", "list")] | {"jobs"}


def test_gh_json_fields_exist_in_gh_2_46(tmp_path):
    from demo.runner import probes
    sh = FixtureShell()
    probes.workflow_runs(sh, REPO, "train.yml")
    probes.run_detail(sh.add(["gh", "run", "view"], {"databaseId": 1}), REPO, "1")
    probes.gh_variable(sh, REPO, "AUTO_MLOPS_ENABLED")
    sh.add(["gh", "pr", "view"], {"state": "MERGED", "mergeCommit": {"oid": MAIN}})
    assert engines.wait_for_merge(make_ctx("S2", sh, tmp_path), "https://github.com/x/pull/1") == MAIN
    seen = 0
    for call in sh.calls:
        if call[0] == "gh" and "--json" in call:
            fields = set(call[call.index("--json") + 1].split(","))
            allowed = GH_246_FIELDS.get(tuple(call[1:3]), {"state", "mergeCommit"})
            assert fields <= allowed, (call[1:3], fields - allowed)
            seen += 1
    assert seen == 4
    assert ["gh", "api", f"repos/{REPO}/actions/runs/1", "--jq", ".run_attempt"] in sh.calls
    assert not any(call[:3] == ["gh", "variable", "get"] for call in sh.calls), "not available in gh 2.46"


def test_sequence_lists_every_scenario_in_order(capsys):
    assert cli.main(["sequence", "--preset", "demo"]) == 0
    out = capsys.readouterr().out
    positions = [out.index(f". {s} ") for s in ("S1", "S2", "S3", "S4", "S5")]
    assert positions == sorted(positions) and "Recovery = VERIFIED" in out
