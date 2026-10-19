from __future__ import annotations

import dataclasses
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from oxypuro.core import stream_checks
from oxypuro.core.models import Measurement
from oxypuro.core.parsing import ParseError, parse_body, to_measurement


class RawSink(Protocol):
    def write_raw(
        self, groups: dict[str, list[dict[str, Any]]], ingested_at: str, request_id: str
    ) -> int: ...

    def write_rejected(
        self, entries: list[dict[str, Any]], ingested_at: str, request_id: str
    ) -> int: ...


class DynamoSink(Protocol):
    def put_measurements(self, items: list[dict[str, Any]]) -> int: ...

    def upsert_stations(self, stations: list[dict[str, Any]]) -> int: ...

    def advance_watermark(self, max_ts: str, updated_at: str) -> bool: ...


@dataclass
class BatchResult:
    received: int = 0
    measurements: int = 0
    raw_written: int = 0
    dynamo_written: int = 0
    stations_upserted: int = 0
    filtered_mobile: int = 0
    filtered_analysis: int = 0
    unknown_unit: int = 0
    rejected: int = 0
    flagged: int = 0
    min_ts: str | None = None
    max_ts: str | None = None
    watermark_advanced: bool = False
    sink_error: str | None = None
    rejected_reasons: list[str] = field(default_factory=list)
    failed_message_ids: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["failed"] = len(d.pop("failed_message_ids"))
        d["rejected_reasons"] = d["rejected_reasons"][:5]
        return d


def _sent_ts(record: dict[str, Any]) -> str | None:
    ms = record.get("attributes", {}).get("SentTimestamp")
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def _station_update(m: Measurement) -> dict[str, Any]:
    return {
        "location_id": m.location_id,
        "location": m.location,
        "city": m.city,
        "country": m.country,
        "lat": m.lat,
        "lon": m.lon,
        "entity": m.entity,
        "sensor_type": m.sensor_type,
        "parameters": set(),
        "first_seen_ts": m.ts_utc,
    }


def process_records(
    records: list[dict[str, Any]],
    raw_sink: RawSink,
    dynamo_sink: DynamoSink,
    ingested_at: str,
    request_id: str = "local",
) -> BatchResult:
    res = BatchResult(received=len(records))
    raw_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rejected: list[dict[str, Any]] = []
    items: dict[tuple[str, str], dict[str, Any]] = {}
    stations: dict[int, dict[str, Any]] = {}
    curated_max: str | None = None

    def reject(message_id: str, body: Any, error: str) -> None:
        rejected.append(
            {"message_id": message_id, "ingested_at": ingested_at, "error": error, "body": body}
        )
        if len(res.rejected_reasons) < 5:
            res.rejected_reasons.append(error)

    for rec in records:
        message_id = rec.get("messageId", "?")
        body = rec.get("body")
        try:
            objs = parse_body(body)
        except ParseError as e:
            reject(message_id, body, f"body: {e}")
            continue
        except Exception as e:
            reject(message_id, body, f"body: unexpected {type(e).__name__}: {e}")
            continue

        meta = {"message_id": message_id, "ingested_at": ingested_at, "sqs_sent_ts": _sent_ts(rec)}
        for i, obj in enumerate(objs):
            prefix = f"object {i}: " if len(objs) > 1 else ""
            try:
                m = to_measurement(obj)
            except ParseError as e:
                reject(message_id, body, f"{prefix}{e}")
                continue
            except Exception as e:
                reject(message_id, body, f"{prefix}unexpected {type(e).__name__}: {e}")
                continue

            m = dataclasses.replace(m, qc_flags=stream_checks.check(m))
            res.measurements += 1
            res.min_ts = m.ts_utc if res.min_ts is None else min(res.min_ts, m.ts_utc)
            res.max_ts = m.ts_utc if res.max_ts is None else max(res.max_ts, m.ts_utc)
            if m.qc_flags:
                res.flagged += 1

            raw_groups[m.ts_utc[:10]].append({**obj, "_meta": meta})

            res.filtered_mobile += m.is_mobile
            res.filtered_analysis += m.is_analysis
            res.unknown_unit += m.unit_status == "unknown"
            if not m.is_curated_eligible():
                continue

            items[m.key()] = m.to_dynamo_item(message_id, ingested_at)
            st = stations.setdefault(m.location_id, _station_update(m))
            st["parameters"].add(m.parameter)
            st["first_seen_ts"] = min(st["first_seen_ts"], m.ts_utc)
            curated_max = m.ts_utc if curated_max is None else max(curated_max, m.ts_utc)

    res.rejected = len(rejected)

    try:
        if raw_groups:
            res.raw_written = raw_sink.write_raw(dict(raw_groups), ingested_at, request_id)
        if rejected:
            raw_sink.write_rejected(rejected, ingested_at, request_id)
        if items:
            res.dynamo_written = dynamo_sink.put_measurements(list(items.values()))
        if stations:
            res.stations_upserted = dynamo_sink.upsert_stations(list(stations.values()))
        if curated_max is not None:
            res.watermark_advanced = dynamo_sink.advance_watermark(curated_max, ingested_at)
    except Exception as e:
        res.sink_error = f"{type(e).__name__}: {e}"
        res.failed_message_ids = [r.get("messageId", "?") for r in records]
    return res
