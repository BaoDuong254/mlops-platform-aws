"""Evidence output: machine-readable evidence.json plus a short report.md per run."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from demo.runner.recovery import VERIFIED
from demo.runner.verdict import PASSED


def scope_label(mode: str, live: bool, status: str | None, recovery: str | None) -> str:
    """`cloud verified` only for a live AWS run whose verdict passed and whose final state was read back."""
    if mode == "dry-run":
        return "dry-run (nothing executed)"
    if mode == "preflight":
        return "read-only preflight (nothing triggered)"
    if mode == "preview":
        return "local validated (preview only, no cloud decision)"
    if not live:
        return "local validated (fixture data — not evidence of cloud behaviour)"
    if status == PASSED and recovery == VERIFIED:
        return "cloud verified"
    return "cloud run — NOT verified (see verdict/recovery)"


def write(evidence_dir: Path, payload: dict[str, Any]) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "evidence.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    samples = (payload.get("observations") or {}).get("samples")
    if samples:
        keys = sorted({key for sample in samples for key in sample})
        with (evidence_dir / "hpa-samples.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=keys)
            writer.writeheader()
            writer.writerows(samples)
    path = evidence_dir / "report.md"
    path.write_text(render_markdown(payload), encoding="utf-8")
    return path


def _table(rows: list[dict], columns: list[str]) -> list[str]:
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(c, "")).replace("|", "\\|").replace("\n", " ") for c in columns) + " |")
    return lines


def render_markdown(p: dict[str, Any]) -> str:
    verdict, recovery = p.get("verdict") or {}, p.get("recovery") or {}
    out = [f"# {p['scenario']} — {p['title']} ({p['preset']})", "",
           f"- **Scope:** {p['scope']}",
           f"- **Mode:** {p['mode']} · started {p.get('started')} · finished {p.get('finished')}",
           f"- **Verdict:** {verdict.get('status', '-')} — outcome `{verdict.get('outcome', '-')}` "
           f"(expected `{verdict.get('expected', '-')}`)",
           f"- **Recovery:** {recovery.get('status') or '-'}", ""]
    if verdict.get("reasons"):
        out += ["## Lý do", ""] + [f"- {reason}" for reason in verdict["reasons"]] + [""]
    if verdict.get("measurements"):
        out += ["## Giá trị đo thực tế", "", "```json",
                json.dumps(verdict["measurements"], indent=2, sort_keys=True, default=str), "```", ""]
    if p.get("preflight"):
        out += ["## Preflight", ""] + _table(p["preflight"], ["id", "status", "detail"]) + [""]
    if p.get("plan"):
        out += ["## Lệnh", ""] + _table(p["plan"], ["id", "effect", "display"]) + [""]
    if p.get("log"):
        out += ["## Execution log", ""] + _table(
            [{"step": e.get("step"), "at": e.get("started") or e.get("at"), "rc": e.get("returncode", ""),
              "ref": e.get("url") or e.get("run_id") or e.get("merged_commit") or e.get("command", "")} for e in p["log"]],
            ["step", "at", "rc", "ref"]) + [""]
    if recovery.get("checks"):
        out += ["## Recovery checks", ""] + _table(recovery["checks"], ["id", "status", "detail"]) + [""]
        if recovery.get("status") != VERIFIED:
            out += ["### Hướng xử lý", ""] + [f"- {note}" for note in p.get("recovery_notes", [])] + [""]
    refs = p.get("references") or {}
    if refs:
        out += ["## Log references", ""] + [f"- {key}: {value}" for key, value in refs.items() if value] + [""]
    out.append("Evidence đầy đủ: `evidence.json` (và `hpa-samples.csv` với S5) trong cùng thư mục.")
    return "\n".join(out) + "\n"
