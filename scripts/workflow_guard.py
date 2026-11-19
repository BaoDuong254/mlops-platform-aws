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


def assert_source(env, head, current_main):
    expected = env.get("SOURCE_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", expected) or head != expected or env.get("GITHUB_SHA") != expected:
        raise ValueError("Checkout must equal the exact admitted CI/dispatch SHA")
    if current_main != expected:
        raise ValueError("Stale source: main moved; no cloud mutation or release allowed")
    if env.get("AUTOMATIC") == "true" and (
        env.get("AUTO_MLOPS_ENABLED") != "true" or env.get("GITHUB_EVENT_NAME") != "push"
    ):
        raise ValueError("Automatic pipeline disabled or not called by main push CI")


def main():
    mode=sys.argv[1]
    if mode not in {"train", "release"}:
        raise ValueError("Unknown workflow")
    validate(mode, os.environ)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    current = subprocess.check_output(["git", "ls-remote", "origin", "refs/heads/main"], text=True).split()[0]
    assert_source(os.environ, head, current)
    if mode == "train":
        subprocess.run(["git","ls-files","--error-unmatch",os.environ["TRAINING_CONFIG"]],check=True)
    print("Trusted main-branch workflow inputs verified")


if __name__ == "__main__":
    main()
