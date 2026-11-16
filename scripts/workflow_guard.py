"""Fail before credentials on a fork, wrong identity or unsafe dispatch input."""
import os
import re
import subprocess
import sys


def validate(mode, env):
    for name in ("AWS_ACCOUNT_ID", "EXPECTED_REPOSITORY_ID", "EXPECTED_OWNER_ID"):
        if not re.fullmatch(r"[0-9]+", env.get(name, "")):
            raise ValueError("Set repository variable " + name)
    if len(env["AWS_ACCOUNT_ID"]) != 12:
        raise ValueError("Invalid account ID")
    if env.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Dispatch allowed only from main")
    if env.get("GITHUB_REPOSITORY_ID") != env["EXPECTED_REPOSITORY_ID"] or env.get("GITHUB_REPOSITORY_OWNER_ID") != env["EXPECTED_OWNER_ID"]:
        raise ValueError("Immutable GitHub repository identity differs")
    if mode == "release":
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}",env.get("MODEL_NAME", "")) or not re.fullmatch(r"[1-9][0-9]*",env.get("MODEL_VERSION", "")):
            raise ValueError("Explicit valid model name and numeric version required")
        if env.get("CANARY_TEST_CPU", "off") not in {"off", "100m"}:
            raise ValueError("Unsafe candidate CPU test setting")
    else:
        if not re.fullmatch(r"configs/[a-zA-Z0-9_-]+\.yaml", env.get("TRAINING_CONFIG", "")):
            raise ValueError("Training config must be a committed configs/*.yaml path")
    key="INFERENCE_ECR_REPOSITORY" if mode == "release" else "TRAINING_ECR_REPOSITORY"
    prefix=env["AWS_ACCOUNT_ID"]+".dkr.ecr."+env["AWS_REGION"]+".amazonaws.com/"
    if not env.get(key, "").startswith(prefix):
        raise ValueError("Configure ECR repository for the authenticated account and region")
    if mode == "train" and not env.get("DATA_BUCKET", "").startswith("mlops-aws-datasets-"):
        raise ValueError("Configure the project datasets bucket")


def main():
    mode=sys.argv[1]
    if mode not in {"train", "release"}:
        raise ValueError("Unknown workflow")
    validate(mode, os.environ)
    if subprocess.check_output(["git","branch","--show-current"],text=True).strip() != "main":
        raise ValueError("Checkout must be main")
    if mode == "train":
        subprocess.run(["git","ls-files","--error-unmatch",os.environ["TRAINING_CONFIG"]],check=True)
    print("Trusted main-branch workflow inputs verified")


if __name__ == "__main__":
    main()
