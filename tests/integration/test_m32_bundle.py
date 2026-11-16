"""Verify runner release metadata against a self-contained synthetic bundle."""

import importlib.util
import shutil
from pathlib import Path


from inference.bundle import validate_release

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("m32_gitops", ROOT / "scripts/m32_gitops.py")
gitops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gitops)


def test_real_bundle_prepared_for_runner_and_prediction_unchanged(tmp_path, release):
    source = release
    original, original_model = validate_release(source)
    bundle = tmp_path / "bundle"
    shutil.copytree(source, bundle)
    release = gitops.prepare_bundle(bundle, "m32-123-1", "a" * 40)
    checked, checked_model = validate_release(bundle)
    sample = {"tpep_pickup_datetime": "2025-04-07T12:00:00", "PULocationID": 1, "DOLocationID": 2}
    assert release["artifact_checksum"] == original["artifact_checksum"]
    assert checked["release_id"] == "m32-123-1"
    assert checked["serving_git_sha"] == "a" * 40
    assert checked_model.predict(sample) == original_model.predict(sample)
