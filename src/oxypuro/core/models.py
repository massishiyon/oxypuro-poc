from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


def _dec(x: float | None) -> Decimal | None:
    return None if x is None else Decimal(str(x))


@dataclass(frozen=True)
class Measurement:
    location_id: int
    parameter: str
    value_raw: float
    unit_raw: str
    value: float | None
    unit: str | None
    unit_status: str
    ts_utc: str
    location: str | None = None
    city: str | None = None
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    ts_local: str | None = None
    entity: str | None = None
    sensor_type: str | None = None
    is_mobile: bool = False
    is_analysis: bool = False
    qc_flags: tuple[str, ...] = ()

    def key(self) -> tuple[str, str]:
        return f"MEAS#{self.location_id}#{self.parameter}", self.ts_utc

    def is_curated_eligible(self) -> bool:
        return not self.is_mobile and not self.is_analysis and self.unit_status != "unknown"

    def to_dynamo_item(self, message_id: str, ingested_at: str) -> dict[str, Any]:
        pk, sk = self.key()
        item: dict[str, Any] = {
            "pk": pk,
            "sk": sk,
            "location_id": self.location_id,
            "parameter": self.parameter,
            "value": _dec(self.value),
            "unit": self.unit,
            "value_raw": _dec(self.value_raw),
            "unit_raw": self.unit_raw,
            "unit_status": self.unit_status,
            "ts_local": self.ts_local,
            "qc_flags": list(self.qc_flags),
            "message_id": message_id,
            "ingested_at": ingested_at,
        }
        return {k: v for k, v in item.items() if v is not None}
