from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
REPORTS_DIR = PROJECT_ROOT / "reports"
DATA_DIR = PROJECT_ROOT / "data"

REQUIRED_ENV = (
    "AWS_REGION",
    "BUCKET_NAME",
    "TABLE_NAME",
    "LAMBDA_FUNCTION_NAME",
    "SNS_TOPIC_NAME",
    "SOURCE_QUEUE_NAME",
    "LAMBDA_ROLE_NAME",
)


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    aws_profile: str | None
    aws_region: str
    bucket_name: str
    table_name: str
    lambda_function_name: str
    sns_topic_name: str
    source_queue_name: str
    lambda_role_name: str
    bedrock_model_id: str | None
    alert_email: str | None
    owner_tag: str | None
    log_level: str

    @property
    def log_group_name(self) -> str:
        return f"/aws/lambda/{self.lambda_function_name}"


def _optional(env: dict[str, str], key: str) -> str | None:
    value = env.get(key, "").strip()
    return value or None


def load_settings(env: dict[str, str] | None = None, *, dotenv: bool = True) -> Settings:
    if env is None:
        if dotenv:
            load_dotenv(PROJECT_ROOT / ".env", override=False)
        env = dict(os.environ)

    missing = [k for k in REQUIRED_ENV if not env.get(k, "").strip()]
    if missing:
        raise ConfigError(
            "Missing environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

    return Settings(
        aws_profile=_optional(env, "AWS_PROFILE"),
        aws_region=env["AWS_REGION"].strip(),
        bucket_name=env["BUCKET_NAME"].strip(),
        table_name=env["TABLE_NAME"].strip(),
        lambda_function_name=env["LAMBDA_FUNCTION_NAME"].strip(),
        sns_topic_name=env["SNS_TOPIC_NAME"].strip(),
        source_queue_name=env["SOURCE_QUEUE_NAME"].strip(),
        lambda_role_name=env["LAMBDA_ROLE_NAME"].strip(),
        bedrock_model_id=_optional(env, "BEDROCK_MODEL_ID"),
        alert_email=_optional(env, "ALERT_EMAIL"),
        owner_tag=_optional(env, "OWNER_TAG"),
        log_level=_optional(env, "LOG_LEVEL") or "INFO",
    )


def _load_yaml(name: str) -> dict[str, Any]:
    with (CONFIG_DIR / name).open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"config/{name} must contain a mapping at the top level")
    return data


def load_thresholds() -> dict[str, Any]:
    return _load_yaml("thresholds.yaml")


def load_analysis() -> dict[str, Any]:
    return _load_yaml("analysis.yaml")


def boto3_session(settings: Settings):
    import boto3

    return boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
