from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from oxypuro.analytics.schema_check import PeekedMessage, render_markdown, summarise
from oxypuro.aws_read import (
    ACTIVE_MAPPING_STATES,
    error_code,
    event_source_mappings,
    is_access_denied,
    read_queue,
)
from oxypuro.config import REPORTS_DIR, ConfigError, boto3_session, load_settings

PEEK_MAX_PER_RECEIVE = 10
PEEK_WAIT_SECONDS = 5
PEEK_VISIBILITY_SECONDS = 30
PEEK_MAX_EMPTY_RECEIVES = 3


def peek(sqs, queue_url: str, n: int) -> tuple[list[PeekedMessage], int]:
    seen: dict[str, PeekedMessage] = {}
    re_received = 0
    empty = 0
    while len(seen) < n and empty < PEEK_MAX_EMPTY_RECEIVES:
        resp = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=min(PEEK_MAX_PER_RECEIVE, n - len(seen)),
            WaitTimeSeconds=PEEK_WAIT_SECONDS,
            VisibilityTimeout=PEEK_VISIBILITY_SECONDS,
            MessageSystemAttributeNames=["SentTimestamp", "ApproximateReceiveCount"],
        )
        batch = resp.get("Messages", [])
        if not batch:
            empty += 1
            continue
        empty = 0
        for m in batch:
            if m["MessageId"] in seen:
                re_received += 1
                continue
            attrs = m.get("Attributes", {})
            seen[m["MessageId"]] = PeekedMessage(
                message_id=m["MessageId"],
                body=m["Body"],
                sent_ts_ms=int(attrs["SentTimestamp"]) if "SentTimestamp" in attrs else None,
                receive_count=(
                    int(attrs["ApproximateReceiveCount"])
                    if "ApproximateReceiveCount" in attrs
                    else None
                ),
            )
        print(f"  received {len(seen)}/{n}", end="\r", flush=True)
    print()
    return list(seen.values()), re_received


def run_peek(n: int, out: Path, allow_redrive: bool) -> int:
    settings = load_settings()
    session = boto3_session(settings)
    sqs = session.client("sqs")

    try:
        q = read_queue(sqs, settings.source_queue_name)
    except ClientError as e:
        if is_access_denied(e):
            print(
                f"AccessDenied: sqs:GetQueueUrl/GetQueueAttributes on {settings.source_queue_name}"
            )
        else:
            print(f"Error reading the queue: {error_code(e)}")
        return 1

    if q.redrive_policy and not allow_redrive:
        print(f"Refusing to peek: the queue has a RedrivePolicy ({q.redrive_policy}).")
        print("Receiving raises the receive count. Re-run with --allow-redrive if that is safe.")
        return 1

    try:
        mappings = event_source_mappings(session.client("lambda"), q.arn)
        active = [m for m in mappings if m.get("State") in ACTIVE_MAPPING_STATES]
        if active:
            fns = ", ".join(m.get("FunctionArn", "?").rsplit(":", 1)[-1] for m in active)
            print(f"Refusing to peek: an active Lambda trigger reads this queue ({fns}).")
            return 1
    except ClientError as e:
        if not is_access_denied(e):
            raise
        print(
            f"Warning: AccessDenied on lambda:ListEventSourceMappings for {q.arn}; "
            "cannot confirm that no trigger is active. Continuing (peek never deletes)."
        )

    print(
        f"Peeking up to {n} messages from {settings.source_queue_name} "
        f"(visible: {q.visible}, in flight: {q.in_flight}). Nothing is deleted."
    )
    try:
        messages, re_received = peek(sqs, q.url, n)
    except ClientError as e:
        if is_access_denied(e):
            print(f"AccessDenied: sqs:ReceiveMessage on {q.arn}")
            return 1
        raise

    summary = summarise(messages)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = render_markdown(summary, requested=n, generated_at=generated_at)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    print(
        f"Read {summary.messages} messages, {summary.records} records, "
        f"{len(summary.parse_failures)} parse failures, "
        f"{len(summary.location_ids)} distinct locationId"
        + (f", {re_received} re-received after VT expiry" if re_received else "")
        + "."
    )
    print(f"Report: {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local SQS consumer (peek only).")
    parser.add_argument(
        "--peek",
        type=int,
        metavar="N",
        required=True,
        help="read up to N messages without deleting them",
    )
    parser.add_argument("--out", type=Path, default=REPORTS_DIR / "schema_check.md")
    parser.add_argument(
        "--allow-redrive", action="store_true", help="peek even if the queue has a RedrivePolicy"
    )
    args = parser.parse_args(argv)
    if args.peek < 1:
        parser.error("--peek must be at least 1")
    try:
        return run_peek(args.peek, args.out.resolve(), args.allow_redrive)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1
    except BotoCoreError as e:
        print(f"AWS client error (credentials or network): {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
