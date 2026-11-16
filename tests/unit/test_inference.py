import hashlib
import json

import pytest

from inference.bundle import inventory, validate_release
from training.model import load_model

def test_api_prediction_timezone_parity_and_metrics(release):
    from fastapi.testclient import TestClient
    from inference.app import make_app

    with TestClient(make_app(release)) as client:
        payload = {"pickup_location_id": 1, "dropoff_location_id": 2, "pickup_datetime": "2025-04-07T16:00:00Z"}
        response = client.post("/predict", json=payload)
        assert response.status_code == 200
        result = response.json()
        expected = load_model(release).predict({"tpep_pickup_datetime": "2025-04-07T12:00:00", "PULocationID": 1, "DOLocationID": 2})
        assert result == {"predicted_duration_minutes": expected, "model_name": "nyc-taxi-trip-duration", "model_version": "1"}
        payload["pickup_datetime"] = "2025-04-07T12:00:00-04:00"
        assert client.post("/predict", json=payload).json() == result
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.get("/model-info").status_code == 200
        assert client.get("/docs").status_code == 200
        metrics = client.get("/metrics").text
        assert 'inference_http_requests_total{method="POST",path="/predict",status="200"} 2' in metrics
        assert 'inference_model_info{model_name="nyc-taxi-trip-duration",model_version="1"' in metrics
        assert "pickup_datetime" not in metrics


@pytest.mark.parametrize("change", [
    {"pickup_datetime": "2025-04-07T12:00:00"},
    {"pickup_datetime": "NaN"},
    {"pickup_datetime": None},
    {"pickup_location_id": 0},
    {"dropoff_location_id": 264},
    {"pickup_location_id": 1.5},
    {"pickup_location_id": "Infinity"},
    {"dropoff_location_id": None},
    {"unexpected": "value"},
])
def test_invalid_input_returns_422(release, change):
    from fastapi.testclient import TestClient
    from inference.app import make_app

    with TestClient(make_app(release)) as client:
        payload = {"pickup_location_id": 1, "dropoff_location_id": 2, "pickup_datetime": "2025-04-07T12:00:00-04:00"}
        payload.update(change)
        assert client.post("/predict", json=payload).status_code == 422


def test_missing_model_bad_schema_and_metadata_fail_closed(release):
    validate_release(release)
    original_model = (release / "model.txt").read_bytes()
    (release / "model.txt").unlink()
    with pytest.raises(ValueError, match="checksum"):
        validate_release(release)
    # Restore the model and prove the feature contract and approval are checked.
    (release / "model.txt").write_bytes(original_model)
    schema = json.loads((release / "feature_schema.json").read_text())
    schema["feature_order"] = list(reversed(schema["feature_order"]))
    (release / "feature_schema.json").write_text(json.dumps(schema))
    metadata = json.loads((release / "release.json").read_text())
    metadata["files"] = inventory(release)
    metadata["artifact_checksum"] = hashlib.sha256(json.dumps(metadata["files"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (release / "release.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="schema"):
        validate_release(release)
    metadata["approval_status"] = "rejected"
    (release / "release.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="unapproved"):
        validate_release(release)


def test_no_release_readiness_503(tmp_path):
    from fastapi.testclient import TestClient
    from inference.app import make_app

    with TestClient(make_app(tmp_path / "missing")) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        assert client.get("/model-info").status_code == 503
        payload = {"pickup_location_id": 1, "dropoff_location_id": 2, "pickup_datetime": "2025-04-07T12:00:00-04:00"}
        assert client.post("/predict", json=payload).status_code == 503


@pytest.mark.parametrize('prediction', [float('nan'), float('inf'), -1.0, 0.0])
def test_bad_model_output_is_a_serving_error_and_counted(release, prediction):
    from fastapi.testclient import TestClient
    from inference.app import make_app
    app = make_app(release)
    with TestClient(app) as client:
        app.state.model = type('BadModel', (), {'predict': lambda self, features: prediction})()
        response = client.post('/predict', json={'pickup_location_id':1,'dropoff_location_id':2,'pickup_datetime':'2025-04-07T16:00:00Z'})
        assert response.status_code == 500
        assert 'inference_http_errors_total{method="POST",path="/predict",status="500"} 1' in client.get('/metrics').text
