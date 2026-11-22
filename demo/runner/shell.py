"""Command execution boundary. Tests replace LiveShell with FixtureShell; nothing else spawns processes."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field

from demo.runner.spec import ROOT


@dataclass
class Result:
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def json(self):
        return json.loads(self.stdout)


class LiveShell:
    """Runs argv lists (never shell=True) from the repository root with a hard timeout."""

    live = True

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], timeout: int = 60, stream: bool = False) -> Result:
        self.calls.append(list(argv))
        try:
            if stream:  # interactive progress for long triggers (k6/make); output stays visible
                completed = subprocess.run(argv, cwd=ROOT, timeout=timeout, text=True)
                return Result(argv, completed.returncode)
            completed = subprocess.run(argv, cwd=ROOT, timeout=timeout, text=True, capture_output=True)
            return Result(argv, completed.returncode, completed.stdout, completed.stderr)
        except FileNotFoundError:
            return Result(argv, 127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            return Result(argv, 124, "", f"timeout after {timeout}s")

    def which(self, tool: str) -> bool:
        return shutil.which(tool) is not None

    def spawn(self, argv: list[str]) -> "Handle":
        """Background process (k6 load) so the runner can sample while it runs."""
        self.calls.append(list(argv))
        return Handle(subprocess.Popen(argv, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT))


class Handle:
    def __init__(self, process: subprocess.Popen) -> None:
        self.process, self._output = process, None

    def poll(self):
        return self.process.poll()

    @property
    def returncode(self):
        return self.process.returncode

    def output(self) -> str:
        if self._output is None:
            self._output, _ = self.process.communicate()
        return self._output


@dataclass
class FakeHandle:
    result: Result
    polls: int = 2

    def poll(self):
        if self.polls > 0:
            self.polls -= 1
            return None
        return self.result.returncode

    @property
    def returncode(self):
        return self.result.returncode

    def output(self) -> str:
        return self.result.stdout


@dataclass
class FixtureShell:
    """Deterministic shell for tests: the first matching argv prefix answers; unmatched calls fail."""

    responses: list[tuple[tuple[str, ...], Result]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    missing_tools: set[str] = field(default_factory=set)
    live = False

    def which(self, tool: str) -> bool:
        return tool not in self.missing_tools

    def add(self, prefix: tuple[str, ...] | list[str], stdout="", returncode=0, stderr="") -> "FixtureShell":
        if not isinstance(stdout, str):
            stdout = json.dumps(stdout)
        self.responses.append((tuple(prefix), Result(list(prefix), returncode, stdout, stderr)))
        return self

    def run(self, argv: list[str], timeout: int = 60, stream: bool = False) -> Result:
        self.calls.append(list(argv))
        for prefix, result in self.responses:
            if tuple(argv[: len(prefix)]) == prefix:
                return Result(list(argv), result.returncode, result.stdout, result.stderr)
        return Result(list(argv), 99, "", "fixture: no response for " + " ".join(argv))

    def spawn(self, argv: list[str], polls: int = 2) -> FakeHandle:
        return FakeHandle(self.run(argv), polls)
