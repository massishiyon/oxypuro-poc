from __future__ import annotations

import gzip
import json
from decimal import Decimal
from typing import Any

from botocore.config import Config
from botocore.exceptions import ClientError

BOTO_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 10})


def _gzip_jsonl(lines: list[dict[str, Any]]) -> bytes:
    text = "\n".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in lines)
    return gzip.compress((text + "\n").encode("utf-8"))


def _compact(ts: str) -> str:
    return ts.replace("-", "").replace(":", "")


class S3RawSink:
    def __init__(self, bucket: str, s3_client: Any):
        self.bucket = bucket
        self.s3 = s3_client
        self._seq = 0

    def _key(self, prefix: str, dt: str, ingested_at: str, request_id: str) -> str:
        self._seq += 1
        name = f"{_compact(ingested_at)}-{request_id[:8]}-{self._seq}.jsonl.gz"
        return f"{prefix}/dt={dt}/{name}"

    def _put(self, key: str, lines: list[dict[str, Any]]) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=_gzip_jsonl(lines),
            ContentType="application/x-ndjson",
            ContentEncoding="gzip",
        )

    def write_raw(
        self, groups: dict[str, list[dict[str, Any]]], ingested_at: str, request_id: str
    ) -> int:
        n = 0
        for dt, lines in sorted(groups.items()):
            self._put(self._key("raw", dt, ingested_at, request_id), lines)
            n += len(lines)
        return n

    def write_rejected(
        self, entries: list[dict[str, Any]], ingested_at: str, request_id: str
    ) -> int:
        self._put(self._key("rejected", ingested_at[:10], ingested_at, request_id), entries)
        return len(entries)


def _dec(x: float | None) -> Decimal | None:
    return None if x is None else Decimal(str(x))


class DynamoSink:
    def __init__(self, table: Any):
        self.table = table

    def put_measurements(self, items: list[dict[str, Any]]) -> int:
        with self.table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as w:
            for item in items:
                w.put_item(Item=item)
        return len(items)

    def upsert_stations(self, stations: list[dict[str, Any]]) -> int:
        for st in stations:
            values = {
                "location": st.get("location"),
                "city": st.get("city"),
                "country": st.get("country"),
                "lat": _dec(st.get("lat")),
                "lon": _dec(st.get("lon")),
                "entity": st.get("entity"),
                "sensor_type": st.get("sensor_type"),
            }
            present = {k: v for k, v in values.items() if v is not None}
            sets = [f"#{k} = :{k}" for k in present]
            sets.append("#first_seen_ts = if_not_exists(#first_seen_ts, :first_seen_ts)")
            names = {f"#{k}": k for k in present}
            names["#first_seen_ts"] = "first_seen_ts"
            names["#parameters"] = "parameters"
            vals = {f":{k}": v for k, v in present.items()}
            vals[":first_seen_ts"] = st["first_seen_ts"]
            vals[":parameters"] = set(st["parameters"])
            self.table.update_item(
                Key={"pk": "STATION", "sk": str(st["location_id"])},
                UpdateExpression="SET " + ", ".join(sets) + " ADD #parameters :parameters",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=vals,
            )
        return len(stations)

    def advance_watermark(self, max_ts: str, updated_at: str) -> bool:
        try:
            self.table.update_item(
                Key={"pk": "CLOCK", "sk": "WATERMARK"},
                UpdateExpression="SET max_ts_utc = :ts, updated_at = :now",
                ConditionExpression="attribute_not_exists(max_ts_utc) OR max_ts_utc < :ts",
                ExpressionAttributeValues={":ts": max_ts, ":now": updated_at},
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
