from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "AuthorizationError",
    "UnauthorizedOperation",
    "AWS.SimpleQueueService.AccessDenied",
    "403",
}


def is_access_denied(err: BaseException) -> bool:
    if not isinstance(err, ClientError):
        return False
    return err.response.get("Error", {}).get("Code", "") in _DENIED_CODES


def error_code(err: ClientError) -> str:
    return err.response.get("Error", {}).get("Code", "Unknown")


@dataclass(frozen=True)
class QueueInfo:
    url: str
    arn: str
    visibility_timeout: int
    retention_seconds: int
    visible: int
    in_flight: int
    redrive_policy: str | None


def read_queue(sqs: Any, queue_name: str) -> QueueInfo:
    url = sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    return QueueInfo(
        url=url,
        arn=attrs["QueueArn"],
        visibility_timeout=int(attrs["VisibilityTimeout"]),
        retention_seconds=int(attrs["MessageRetentionPeriod"]),
        visible=int(attrs.get("ApproximateNumberOfMessages", 0)),
        in_flight=int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
        redrive_policy=attrs.get("RedrivePolicy"),
    )


def event_source_mappings(lambda_client: Any, queue_arn: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in lambda_client.get_paginator("list_event_source_mappings").paginate(
        EventSourceArn=queue_arn
    ):
        out.extend(page.get("EventSourceMappings", []))
    return out


ACTIVE_MAPPING_STATES = {"Enabled", "Enabling", "Creating", "Updating"}
