"""Scenario/preset loading, schema validation and safe argv rendering.

A scenario file says *what* to run and verify; a preset supplies the concrete inputs.
Every input is validated by type before it can reach a command template, and templates are
rendered token-by-token into argv lists (never a shell string).
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = ROOT / "demo/scenarios"
PRESET_DIR = ROOT / "demo/presets"

ENGINES = {"training", "pipeline", "release", "load"}
EFFECTS = {"local", "git-remote", "aws", "manual"}
SCENARIO_KEYS = {"id", "title", "engine", "goal", "mutates_aws", "lock", "estimated_minutes", "inputs",
                 "preflight", "initial_state", "input_change", "trigger", "github_ui", "observe", "verify",
                 "recovery"}
EXPECTATIONS = {"rejected", "approved_or_rejected", "promoted", "aborted_reverted", "hpa_consistent"}
# Mirrors scripts/workflow_guard.py; tests assert both accept/reject the same values.
MODEL_NAME = re.compile(r"[a-z][a-z0-9-]{1,62}")
MODEL_VERSION = re.compile(r"[1-9][0-9]*")
DEMO_CONFIG = re.compile(r"configs/demo-[a-z0-9-]+\.yaml")
# Only model capacity/regularisation may change; evaluation identity is guarded separately.
MODEL_PARAMS = {"objective": str, "n_estimators": int, "learning_rate": float, "num_leaves": int,
                "max_depth": int, "min_child_samples": int, "subsample": float, "colsample_bytree": float,
                "reg_alpha": float, "reg_lambda": float, "min_split_gain": float, "subsample_freq": int}
# m33.sh enforces the same bounds; failing early keeps dry-runs honest.
LOAD_LIMITS = {"rate": (1, 200), "vus": (1, 50), "duration_seconds": (30, 900)}
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class SpecError(ValueError):
    pass


@dataclass
class Scenario:
    id: str
    raw: dict[str, Any] = field(repr=False)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.raw[name]
        except KeyError as error:
            raise AttributeError(name) from error


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise SpecError(f"{path.name}: invalid YAML: {error}") from None
    if not isinstance(data, dict):
        raise SpecError(f"{path.name}: expected a mapping")
    return data


def validate_scenario(data: dict, source: str = "scenario") -> list[str]:
    errors = []
    missing = {"id", "title", "engine", "goal", "inputs", "preflight", "trigger", "verify", "recovery"} - set(data)
    if missing:
        return [f"{source}: missing keys {sorted(missing)}"]
    if unknown := set(data) - SCENARIO_KEYS:
        errors.append(f"{source}: unknown keys {sorted(unknown)}")
    if not re.fullmatch(r"S[0-9]+", str(data["id"])):
        errors.append(f"{source}: id must look like S1")
    if data["engine"] not in ENGINES:
        errors.append(f"{source}: engine must be one of {sorted(ENGINES)}")
    for name, spec in data["inputs"].items():
        kind = spec.get("type") if isinstance(spec, dict) else None
        if kind not in {"demo_config", "model_overrides", "enum", "model_name", "model_version", "int",
                        "load_profiles"}:
            errors.append(f"{source}: input {name} has unknown type {kind!r}")
        if kind == "enum" and not spec.get("values"):
            errors.append(f"{source}: enum input {name} needs values")
    from demo.runner.preflight import CHECKS  # local import: preflight imports spec
    from demo.runner.recovery import RECOVERY_CHECKS
    for check in data["preflight"]:
        if check not in CHECKS:
            errors.append(f"{source}: unknown preflight check {check!r}")
    for check in data["recovery"].get("checks", []):
        if check not in RECOVERY_CHECKS:
            errors.append(f"{source}: unknown recovery check {check!r}")
    if data["verify"].get("expect") not in EXPECTATIONS:
        errors.append(f"{source}: verify.expect must be one of {sorted(EXPECTATIONS)}")
    for step in data["trigger"]:
        if step.get("effect") not in EFFECTS:
            errors.append(f"{source}: trigger {step.get('id')} effect must be one of {sorted(EFFECTS)}")
        if step.get("effect") != "manual" and not step.get("run"):
            errors.append(f"{source}: trigger {step.get('id')} needs run")
    return errors


def load_scenarios(directory: Path = SCENARIO_DIR) -> dict[str, Scenario]:
    scenarios, errors = {}, []
    for path in sorted(directory.glob("*.yaml")):
        data = _read_yaml(path)
        errors += validate_scenario(data, path.name)
        if data.get("id") in scenarios:
            errors.append(f"{path.name}: duplicate id {data['id']}")
        scenarios[data.get("id")] = Scenario(data.get("id"), data)
    if errors:
        raise SpecError("\n".join(errors))
    return scenarios


def list_presets(directory: Path = PRESET_DIR) -> list[str]:
    return sorted(path.stem for path in directory.glob("*.yaml"))


def load_preset(name_or_path: str | Path, directory: Path = PRESET_DIR) -> dict:
    path = Path(name_or_path)
    if not path.suffix:
        path = directory / f"{name_or_path}.yaml"
    if not path.is_file():
        raise SpecError(f"Unknown preset {name_or_path!r}; available: {list_presets(directory)}")
    data = _read_yaml(path)
    if data.get("preset") != path.stem:
        raise SpecError(f"{path.name}: preset field must equal file name {path.stem!r}")
    for key in ("defaults", "scenarios"):
        if not isinstance(data.get(key), dict):
            raise SpecError(f"{path.name}: {key} must be a mapping")
    return data


def _check_input(name: str, spec: dict, value: Any) -> list[str]:
    kind = spec["type"]
    if kind == "demo_config":
        return [] if isinstance(value, str) and DEMO_CONFIG.fullmatch(value) else [
            f"{name}={value!r} must be configs/demo-<name>.yaml (outside the auto-training fingerprint)"]
    if kind == "model_name":
        return [] if isinstance(value, str) and MODEL_NAME.fullmatch(value) else [f"{name}={value!r} invalid"]
    if kind == "model_version":
        ok = isinstance(value, str) and (value == "champion" or MODEL_VERSION.fullmatch(value))
        return [] if ok else [f"{name}={value!r} must be a quoted numeric version or 'champion'"]
    if kind == "enum":
        return [] if value in spec["values"] else [f"{name}={value!r} not allowed here; allowed {spec['values']}"]
    if kind == "int":
        ok = isinstance(value, int) and not isinstance(value, bool) and spec.get("min", value) <= value <= spec.get("max", value)
        return [] if ok else [f"{name}={value!r} must be an integer in [{spec.get('min')}, {spec.get('max')}]"]
    if kind == "model_overrides":
        if not isinstance(value, dict) or not value:
            return [f"{name} must be a non-empty mapping of LightGBM parameters"]
        errors = []
        for key, item in value.items():
            expected = MODEL_PARAMS.get(key)
            if expected is None:
                errors.append(f"{name}.{key} is not an allowed model parameter (evaluation identity is fixed)")
            elif isinstance(item, bool) or not isinstance(item, (int, float) if expected is float else expected):
                errors.append(f"{name}.{key}={item!r} must be {expected.__name__}")
            elif expected is not str and item <= 0 and key not in {"max_depth", "reg_alpha", "reg_lambda", "min_split_gain", "subsample_freq"}:
                errors.append(f"{name}.{key} must be positive")
        return errors
    if kind == "load_profiles":
        if not isinstance(value, list) or not value:
            return [f"{name} must be a non-empty list"]
        errors = []
        for index, profile in enumerate(value):
            if not isinstance(profile, dict) or set(profile) != {"name", *LOAD_LIMITS}:
                errors.append(f"{name}[{index}] needs exactly name, rate, vus, duration_seconds")
                continue
            if not re.fullmatch(r"[a-z0-9-]{1,40}", str(profile["name"])):
                errors.append(f"{name}[{index}].name invalid")
            for key, (low, high) in LOAD_LIMITS.items():
                item = profile[key]
                if isinstance(item, bool) or not isinstance(item, int) or not low <= item <= high:
                    errors.append(f"{name}[{index}].{key}={item!r} outside m33.sh limits [{low}, {high}]")
        return errors
    return [f"{name}: unsupported type {kind}"]


def resolve_inputs(preset: dict, scenario: Scenario) -> dict[str, Any]:
    """Merge preset defaults with scenario-specific inputs and validate them against the scenario."""
    if scenario.id not in preset["scenarios"]:
        raise SpecError(f"Preset {preset['preset']!r} has no inputs for {scenario.id}")
    values = {**preset["defaults"], **preset["scenarios"][scenario.id], "preset": preset["preset"]}
    errors = []
    for name, spec in scenario.inputs.items():
        if name not in values:
            errors.append(f"{scenario.id}: missing input {name}")
        else:
            errors += [f"{scenario.id}: {error}" for error in _check_input(name, spec, values[name])]
    for key in ("github_repo", "aws_account_id", "aws_region", "cluster", "model_name"):
        if key not in values:
            errors.append(f"{preset['preset']}: defaults.{key} missing")
    if values.get("aws_account_id") and not re.fullmatch(r"[0-9]{12}", str(values["aws_account_id"])):
        errors.append("defaults.aws_account_id must be 12 digits (quote it)")
    if values.get("github_repo") and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", values["github_repo"]):
        errors.append("defaults.github_repo must be owner/name")
    if errors:
        raise SpecError("\n".join(errors))
    if "load_profiles" in values:
        values["load_profiles_summary"] = ", ".join(
            f"{p['name']}={p['rate']} RPS/{p['vus']} VUs/{p['duration_seconds']}s" for p in values["load_profiles"])
    if "model_overrides" in values:
        values["model_overrides_text"] = ", ".join(f"{k}={v}" for k, v in values["model_overrides"].items())
    return values


def validate_preset(preset: dict, scenarios: dict[str, Scenario]) -> list[str]:
    errors = [f"{preset['preset']}: unknown scenario {key}" for key in preset["scenarios"] if key not in scenarios]
    for scenario_id in scenarios:
        try:
            resolve_inputs(preset, scenarios[scenario_id])
        except SpecError as error:
            errors.append(str(error))
    return errors


def render_text(template: str, values: dict[str, Any]) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise SpecError(f"Template placeholder {{{key}}} has no value")
        value = values[key]
        if key == "model_overrides":
            value = values["model_overrides_text"]
        return str(value)
    return PLACEHOLDER.sub(replace, template)


def render_argv(template: str, values: dict[str, Any]) -> list[str]:
    """Split first, then substitute inside each token: values can never add arguments or shell syntax."""
    argv = [render_text(token, values) for token in shlex.split(template)]
    for token in argv:
        if "\n" in token or "\0" in token:
            raise SpecError("Rendered argument contains a control character")
    return argv
