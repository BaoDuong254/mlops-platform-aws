"""Self-contained synthetic ML fixtures: never read ignored developer artifacts."""
import hashlib
import json
import shutil

import pytest


@pytest.fixture
def training_run(tmp_path):
    from tests.integration.test_mlflow_tracking import _training_run
    return _training_run(tmp_path)


@pytest.fixture
def release(training_run, tmp_path):
    from inference.bundle import inventory
    root = tmp_path / "release"
    root.mkdir()
    for source, target in (("model.txt", "model.txt"), ("feature_schema.json", "feature_schema.json"),
                           ("manifest.json", "training_manifest.json")):
        shutil.copy2(training_run / source, root / target)
    (root / "mlflow_model").mkdir()
    (root / "mlflow_model/MLmodel").write_text("flavors: {}\n")
    manifest = json.loads((training_run / "manifest.json").read_text())
    files = inventory(root)
    metadata = {"format_version": 1, "approval_status": "approved", "model_name": "nyc-taxi-trip-duration",
                "model_version": "1", "mlflow_run_id": "test-mlflow-run", "source_run_id": manifest["run_id"],
                "git_sha": manifest.get("git_sha") or "unknown", "files": files,
                "artifact_checksum": hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    (root / "release.json").write_text(json.dumps(metadata))
    return root


def pytest_sessionfinish(session, exitstatus):
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter and reporter.stats.get("skipped"):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
