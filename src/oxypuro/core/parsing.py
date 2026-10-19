from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

from oxypuro.core.models import Measurement
from oxypuro.core.units import normalise_parameter, to_canonical


class ParseError(ValueError):
    pass


def parse_body(body: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError) as e:
        raise ParseError(f"invalid JSON: {e}") from e

    if (
        isinstance(data, dict)
        and data.get("Type") == "Notification"
        and isinstance(data.get("Message"), str)
    ):
        return parse_body(data["Message"])

    if isinstance(data, dict):
        return [data]
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        return data
    raise ParseError(
        f"expected a JSON object or a non-empty list of objects, got {type(data).__name__}"
    )


def _location_id(raw: Any) -> int:
    if isinstance(raw, bool):
        raise ParseError("locationId is a boolean")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    raise ParseError(f"locationId is not an integer: {raw!r}")


def _value(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ParseError(f"value is not a number: {raw!r}")
    v = float(raw)
    if not math.isfinite(v):
        raise ParseError(f"value is not finite: {raw!r}")
    return v


def _ts_utc(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ParseError("date.utc is missing")
    s = raw.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise ParseError(f"date.utc is not ISO 8601: {raw!r}") from e
    if dt.tzinfo is None:
        raise ParseError(f"date.utc has no time zone: {raw!r}")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _opt_str(obj: dict[str, Any], key: str) -> str | None:
    v = obj.get(key)
    return v if isinstance(v, str) and v != "" else None


def _opt_float(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    v = float(raw)
    return v if math.isfinite(v) else None


def to_measurement(obj: dict[str, Any]) -> Measurement:
    if "locationId" not in obj:
        raise ParseError("locationId is missing")
    location_id = _location_id(obj["locationId"])

    parameter_raw = obj.get("parameter")
    if not isinstance(parameter_raw, str) or not parameter_raw.strip():
        raise ParseError("parameter is missing or empty")
    parameter = normalise_parameter(parameter_raw)

    if "value" not in obj:
        raise ParseError("value is missing")
    value_raw = _value(obj["value"])

    unit_raw = obj.get("unit")
    if not isinstance(unit_raw, str):
        raise ParseError("unit is missing")

    date = obj.get("date")
    if not isinstance(date, dict):
        raise ParseError("date is missing or not an object")
    ts_utc = _ts_utc(date.get("utc"))
    ts_local = date.get("local") if isinstance(date.get("local"), str) else None

    value, unit, unit_status = to_canonical(parameter, value_raw, unit_raw)

    coords = obj.get("coordinates")
    lat = lon = None
    if isinstance(coords, dict):
        lat = _opt_float(coords.get("latitude"))
        lon = _opt_float(coords.get("longitude"))

    return Measurement(
        location_id=location_id,
        parameter=parameter,
        value_raw=value_raw,
        unit_raw=unit_raw,
        value=value,
        unit=unit,
        unit_status=unit_status,
        ts_utc=ts_utc,
        location=_opt_str(obj, "location"),
        city=_opt_str(obj, "city"),
        country=_opt_str(obj, "country"),
        lat=lat,
        lon=lon,
        ts_local=ts_local,
        entity=_opt_str(obj, "entity"),
        sensor_type=_opt_str(obj, "sensorType"),
        is_mobile=obj.get("isMobile") is True,
        is_analysis=obj.get("isAnalysis") is True,
    )
