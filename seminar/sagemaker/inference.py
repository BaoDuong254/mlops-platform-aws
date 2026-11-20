import json
import os

import lightgbm as lgb
from features import preprocess_request  # pyright: ignore[reportMissingImports] 


def model_fn(model_dir):
    with open(
        os.path.join(model_dir, "feature_schema.json"), encoding="utf-8"
    ) as stream:
        schema = json.load(stream)
    return (
        lgb.Booster(model_file=os.path.join(model_dir, "model.txt")),
        schema["feature_order"],
    )


def input_fn(body, content_type):
    if content_type != "application/json":
        raise ValueError(f"Unsupported content type: {content_type}")
    return json.loads(body)


def predict_fn(request, model):
    booster, feature_order = model
    features = preprocess_request(request)
    if list(features.columns) != feature_order:
        raise ValueError(
            "Inference feature order does not match the saved model contract"
        )
    return float(booster.predict(features)[0])


def output_fn(prediction, _accept):
    return json.dumps({"predicted_duration_minutes": prediction}), "application/json"
