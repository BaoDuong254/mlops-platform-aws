"""Fail before building/releasing unless the current GitOps template is verified stable."""
import json
import yaml
from m32_verify import live_state


def expected_from_template(deployment):
    template = deployment["spec"]["template"]
    annotations = template["metadata"]["annotations"]
    return {"image": template["spec"]["containers"][0]["image"],
            "model_version": template["metadata"]["labels"]["mlops.aws/model-version"],
            "release_id": annotations["mlops.aws/release-id"],
            "artifact_checksum": annotations["mlops.aws/artifact-checksum"]}


def main():
    from pathlib import Path
    deployment = yaml.safe_load(Path("deploy/gitops/inference/deployment.yaml").read_text())
    expected = expected_from_template(deployment)
    if live_state(expected) != "promoted":
        raise RuntimeError("Current Git template is not stable and Ready; refusing a new release")
    print(json.dumps(expected, sort_keys=True))


if __name__ == "__main__":
    main()
