"""Move the MLflow `champion` alias to an explicit approved version, with history and restore.

Used to stage a demo (e.g. point the gate reference at v2 so a real candidate can be approved).
Safety rules:
  * the target must be READY + validation_status=approved;
  * it must use the same validation dataset key and metric as the current champion, otherwise the
    cloud gate would refuse to compare (and the demo would fail for the wrong reason);
  * apply re-reads the alias and refuses if it changed since the plan (no blind overwrite);
  * every change is appended to a history file; restore only undoes the latest change and only if
    the alias still points where that change left it.
The serving image is NOT changed: alias and running model may differ until the next release.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ALIAS = "champion"


def _version(client, model: str, number: str) -> dict | None:
    try:
        v = client.get_model_version(model, number)
    except Exception:  # noqa: BLE001 - MLflow raises MlflowException for a missing version
        return None
    tags = v.tags or {}
    mae = tags.get("validation_mae_minutes")
    return {"version": str(v.version), "status": v.status, "run_id": v.run_id,
            "validation_status": tags.get("validation_status"),
            "validation_mae_minutes": float(mae) if mae else None,
            "validation_dataset_key": tags.get("validation_dataset_key"),
            "validation_metric_definition": tags.get("validation_metric_definition")}


def current(client, model: str) -> dict | None:
    try:
        v = client.get_model_version_by_alias(model, ALIAS)
    except Exception:  # noqa: BLE001 - alias not set
        return None
    return _version(client, model, str(v.version))


def plan(client, model: str, target: str, fraction: float = 0.01) -> dict[str, Any]:
    """Read-only: what would change and whether it is safe."""
    before, after = current(client, model), _version(client, model, target)
    errors, notes = [], []
    if after is None:
        errors.append(f"version {target} does not exist")
    else:
        if after["status"] != "READY" or after["validation_status"] != "approved":
            errors.append(f"version {target} is {after['status']}/{after['validation_status']}; must be READY/approved")
        if before and (after["validation_dataset_key"] != before["validation_dataset_key"] or
                       after["validation_metric_definition"] != before["validation_metric_definition"]):
            errors.append("target uses a different validation dataset/metric than the current champion; "
                          "the cloud gate would refuse to compare")
    noop = bool(before and after and before["version"] == after["version"])
    if noop:
        notes.append(f"champion is already v{target}; nothing to do")
    elif after and after.get("validation_mae_minutes") and not errors:
        notes.append(f"gate reference becomes v{target} (MAE {after['validation_mae_minutes']:.6f}): a new candidate "
                     f"needs MAE <= {after['validation_mae_minutes'] * (1 - fraction):.6f}")
        if before:
            notes.append(f"serving keeps running v{before['version']} until a release is promoted; until then do not "
                         f"run S3/S4 with model_version=champion")
    return {"model": model, "before": before, "after": after, "errors": errors, "notes": notes, "noop": noop}


def _history(path: Path) -> list[dict]:
    return json.loads(path.read_text()) if path.exists() else []


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2) + "\n")


def apply(client, model: str, target: str, expected_before: str | None, history: Path, at: str,
          reason: str = "") -> dict[str, Any]:
    result = plan(client, model, target)
    if result["errors"]:
        raise ValueError("; ".join(result["errors"]))
    if result["noop"]:
        return result
    actual = (result["before"] or {}).get("version")
    if actual != expected_before:
        raise RuntimeError(f"champion changed from v{expected_before} to v{actual} since the plan; re-run the plan")
    client.set_registered_model_alias(model, ALIAS, target)
    confirmed = current(client, model)
    if not confirmed or confirmed["version"] != target:
        raise RuntimeError(f"read-back failed: champion is {confirmed and confirmed['version']}, expected {target}")
    _write(history, _history(history) + [{"at": at, "model": model, "from": actual, "to": target, "reason": reason}])
    return result | {"applied": True, "confirmed": confirmed}


def restore_target(client, model: str, history: Path) -> dict:
    """The latest recorded, not yet restored change; refuses if the alias has moved since."""
    pending = [r for r in _history(history) if r["model"] == model and not r.get("restored") and not r.get("restore_of")]
    if not pending:
        raise ValueError("no recorded champion change to restore")
    last = pending[-1]
    now = (current(client, model) or {}).get("version")
    if now != last["to"]:
        raise RuntimeError(f"champion is v{now}, not v{last['to']} as left by the last change; "
                           "a release probably moved it — refusing to undo")
    if last["from"] is None:
        raise ValueError("there was no champion before the last change; nothing to restore to")
    return last


def restore(client, model: str, history: Path, at: str) -> dict[str, Any]:
    record = restore_target(client, model, history)
    result = apply(client, model, record["from"], record["to"], history, at, reason="restore")
    records = _history(history)
    for item in records:
        if item["at"] == record["at"] and item["model"] == model and item["to"] == record["to"]:
            item["restored"] = at
    records[-1]["restore_of"] = record["at"]
    _write(history, records)
    return result
