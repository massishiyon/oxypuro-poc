from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from oxypuro.ingest.pipeline import process_records

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_sinks: tuple[Any, Any] | None = None


def _get_sinks() -> tuple[Any, Any]:
    global _sinks
    if _sinks is None:
        import boto3

        from oxypuro.ingest.sinks import BOTO_CONFIG, DynamoSink, S3RawSink

        s3 = boto3.client("s3", config=BOTO_CONFIG)
        table = boto3.resource("dynamodb", config=BOTO_CONFIG).Table(os.environ["TABLE_NAME"])
        _sinks = (S3RawSink(os.environ["BUCKET_NAME"], s3), DynamoSink(table))
    return _sinks


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    ingested_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    request_id = getattr(context, "aws_request_id", "local")
    raw_sink, dynamo_sink = _get_sinks()

    result = process_records(
        event.get("Records", []), raw_sink, dynamo_sink, ingested_at, request_id
    )

    line = {
        "event": "batch_summary",
        "request_id": request_id,
        "ingested_at": ingested_at,
        **result.summary(),
    }
    if result.sink_error:
        logger.error(json.dumps(line))
    else:
        logger.info(json.dumps(line))

    return {"batchItemFailures": [{"itemIdentifier": i} for i in result.failed_message_ids]}
