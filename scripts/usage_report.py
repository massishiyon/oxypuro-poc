from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any

from oxypuro.config import DATA_DIR, boto3_session, load_analysis, load_settings

START = datetime(2026, 10, 1, tzinfo=UTC)
USAGE_PATH = DATA_DIR / "usage.json"


def metric_sum(cw, namespace: str, name: str, dims: list[dict], end: datetime) -> float:
    resp = cw.get_metric_statistics(
        Namespace=namespace,
        MetricName=name,
        Dimensions=dims,
        StartTime=START,
        EndTime=end,
        Period=86400,
        Statistics=["Sum"],
    )
    return float(sum(p["Sum"] for p in resp["Datapoints"]))


def main() -> int:
    settings = load_settings()
    session = boto3_session(settings)
    cw = session.client("cloudwatch")
    end = datetime.now(UTC)
    out: dict[str, Any] = {"period_start": START.isoformat(), "period_end": end.isoformat()}

    fn = [{"Name": "FunctionName", "Value": settings.lambda_function_name}]
    out["lambda"] = {
        "invocations": metric_sum(cw, "AWS/Lambda", "Invocations", fn, end),
        "errors": metric_sum(cw, "AWS/Lambda", "Errors", fn, end),
        "duration_ms_total": metric_sum(cw, "AWS/Lambda", "Duration", fn, end),
        "memory_mb": session.client("lambda").get_function_configuration(
            FunctionName=settings.lambda_function_name
        )["MemorySize"],
    }

    s3 = session.client("s3")
    prefixes: dict[str, dict[str, int]] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=settings.bucket_name):
        for o in page.get("Contents", []):
            p = prefixes.setdefault(o["Key"].split("/", 1)[0], {"objects": 0, "bytes": 0})
            p["objects"] += 1
            p["bytes"] += o["Size"]
    out["s3"] = prefixes

    t = session.client("dynamodb").describe_table(TableName=settings.table_name)["Table"]
    tbl = [{"Name": "TableName", "Value": settings.table_name}]
    out["dynamodb"] = {
        "items": t["ItemCount"],
        "bytes": t["TableSizeBytes"],
        "created": t["CreationDateTime"].isoformat(),
        "write_units": metric_sum(cw, "AWS/DynamoDB", "ConsumedWriteCapacityUnits", tbl, end),
        "read_units": metric_sum(cw, "AWS/DynamoDB", "ConsumedReadCapacityUnits", tbl, end),
    }

    lg = f"/aws/lambda/{settings.lambda_function_name}"
    groups = session.client("logs").describe_log_groups(logGroupNamePrefix=lg)["logGroups"]
    g = next((x for x in groups if x["logGroupName"] == lg), {})
    out["logs"] = {
        "stored_bytes": g.get("storedBytes", 0),
        "retention_days": g.get("retentionInDays"),
    }

    q = [{"Name": "QueueName", "Value": settings.source_queue_name}]
    out["sqs"] = {
        "messages_received": metric_sum(cw, "AWS/SQS", "NumberOfMessagesReceived", q, end),
        "empty_receives": metric_sum(cw, "AWS/SQS", "NumberOfEmptyReceives", q, end),
        "messages_deleted": metric_sum(cw, "AWS/SQS", "NumberOfMessagesDeleted", q, end),
    }

    out["bedrock"] = {}
    for model in load_analysis()["llm"]["candidate_model_ids"]:
        m = [{"Name": "ModelId", "Value": model}]
        calls = metric_sum(cw, "AWS/Bedrock", "Invocations", m, end)
        if calls:
            out["bedrock"][model] = {
                "invocations": calls,
                "input_tokens": metric_sum(cw, "AWS/Bedrock", "InputTokenCount", m, end),
                "output_tokens": metric_sum(cw, "AWS/Bedrock", "OutputTokenCount", m, end),
            }

    USAGE_PATH.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"wrote {USAGE_PATH.relative_to(DATA_DIR.parent)}; read-only, nothing was changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
