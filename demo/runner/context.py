"""Per-invocation state shared by preflight, triggers, verification and recovery."""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from demo.runner import probes
from demo.runner.spec import ROOT, Scenario

EVIDENCE_ROOT = ROOT / "artifacts/demo"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Context:
    sh: Any
    scenario: Scenario
    values: dict[str, Any]
    preset: str
    evidence_dir: Path
    registry: Any = None                     # fixture registry in tests; live opened lazily
    prometheus: Callable[[str], float | None] | None = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    snapshot: dict[str, Any] = field(default_factory=dict)
    _stack: contextlib.ExitStack = field(default_factory=contextlib.ExitStack)

    @property
    def live(self) -> bool:
        return bool(getattr(self.sh, "live", False))

    def open_registry(self):
        if self.registry is None:
            self.registry = self._stack.enter_context(probes.registry(self))
        return self.registry

    def query(self, promql: str) -> float | None:
        if self.prometheus is None:
            url = self._stack.enter_context(
                probes.port_forward("monitoring", "m33-prometheus-server", 80, "/-/ready"))
            self.prometheus = lambda q: probes.prometheus_query(url, q)
        return self.prometheus(promql)

    def close(self) -> None:
        self._stack.close()


def new_evidence_dir(preset: str, scenario_id: str, root: Path = EVIDENCE_ROOT) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{stamp}_{preset}_{scenario_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path
