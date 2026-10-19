from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from oxypuro.core.parsing import ParseError, parse_body, to_measurement
from oxypuro.core.units import normalise_unit_string

REQUIRED_FIELDS = ("locationId", "parameter", "value", "unit", "date.utc")
OPTIONAL_FIELDS = (
    "location",
    "city",
    "country",
    "coordinates.latitude",
    "coordinates.longitude",
    "date.local",
    "entity",
    "sensorType",
    "isMobile",
    "isAnalysis",
)
CONTRACT_FIELDS = frozenset(REQUIRED_FIELDS + OPTIONAL_FIELDS)
_CONTAINERS = frozenset({"coordinates", "date"})


@dataclass(frozen=True)
class PeekedMessage:
    message_id: str
    body: str
    sent_ts_ms: int | None = None
    receive_count: int | None = None


@dataclass
class SchemaSummary:
    messages: int = 0
    records: int = 0
    bodies_per_message: Counter = field(default_factory=Counter)
    parse_failures: list[tuple[str, str]] = field(default_factory=list)
    parameters: Counter = field(default_factory=Counter)
    units: Counter = field(default_factory=Counter)
    entities: Counter = field(default_factory=Counter)
    sensor_types: Counter = field(default_factory=Counter)
    is_mobile: Counter = field(default_factory=Counter)
    is_analysis: Counter = field(default_factory=Counter)
    countries: Counter = field(default_factory=Counter)
    location_ids: set[int] = field(default_factory=set)
    location_names_per_id: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))
    ts_utc: list[str] = field(default_factory=list)
    extra_fields: Counter = field(default_factory=Counter)
    missing_fields: Counter = field(default_factory=Counter)
    values_by_parameter: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    natural_keys: Counter = field(default_factory=Counter)
    sent_ts_ms: list[int] = field(default_factory=list)
    receive_counts: Counter = field(default_factory=Counter)


def _field_paths(obj: dict[str, Any], prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for k, v in obj.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict) and (path in _CONTAINERS or prefix):
            paths |= _field_paths(v, f"{path}.")
        else:
            paths.add(path)
    return paths


_ABSENT = object()


def _repr(v: Any) -> str:
    return "(absent)" if v is _ABSENT else repr(v)


def summarise(messages: list[PeekedMessage]) -> SchemaSummary:
    s = SchemaSummary()
    for msg in messages:
        s.messages += 1
        if msg.sent_ts_ms is not None:
            s.sent_ts_ms.append(msg.sent_ts_ms)
        if msg.receive_count is not None:
            s.receive_counts[msg.receive_count] += 1
        try:
            objs = parse_body(msg.body)
        except ParseError as e:
            s.parse_failures.append((msg.message_id, f"body: {e}"))
            continue
        s.bodies_per_message[len(objs)] += 1

        for obj in objs:
            s.records += 1
            paths = _field_paths(obj)
            for p in paths - CONTRACT_FIELDS:
                s.extra_fields[p] += 1
            for p in CONTRACT_FIELDS - paths:
                s.missing_fields[p] += 1
            s.entities[_repr(obj.get("entity", _ABSENT))] += 1
            s.sensor_types[_repr(obj.get("sensorType", _ABSENT))] += 1
            s.is_mobile[_repr(obj.get("isMobile", _ABSENT))] += 1
            s.is_analysis[_repr(obj.get("isAnalysis", _ABSENT))] += 1
            s.countries[_repr(obj.get("country", _ABSENT))] += 1
            try:
                m = to_measurement(obj)
            except ParseError as e:
                s.parse_failures.append((msg.message_id, str(e)))
                continue
            s.parameters[m.parameter] += 1
            s.units[(m.unit_raw, normalise_unit_string(m.unit_raw), m.unit_status)] += 1
            s.location_ids.add(m.location_id)
            if m.location:
                s.location_names_per_id[m.location_id].add(m.location)
            s.ts_utc.append(m.ts_utc)
            s.values_by_parameter[m.parameter].append(m.value_raw)
            s.natural_keys[(m.location_id, m.parameter, m.ts_utc)] += 1
    return s


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def _counter_table(title: str, c: Counter) -> str:
    rows = [[f"`{k}`", n] for k, n in c.most_common()]
    return f"### {title}\n\n" + _table(["Value", "Records"], rows)


def _fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_markdown(s: SchemaSummary, *, requested: int, generated_at: str) -> str:
    parsed = s.records - sum(1 for _, r in s.parse_failures if not r.startswith("body:"))
    dup_keys = sum(1 for n in s.natural_keys.values() if n > 1)
    names_shared = Counter()
    for names in s.location_names_per_id.values():
        for n in names:
            names_shared[n] += 1
    shared = sorted(n for n, k in names_shared.items() if k > 1)

    out: list[str] = []
    out.append("# Schema check (peek sample)\n")

    out.append("## Counts\n")
    out.append(
        _table(
            ["Metric", "Value"],
            [
                ["Generated", generated_at],
                ["Messages requested", requested],
                ["Messages read", s.messages],
                ["Records in those messages", s.records],
                ["Records parsed into a Measurement", parsed],
                ["Parse failures", len(s.parse_failures)],
                [
                    "Records per message (count: messages)",
                    ", ".join(f"{k}: {v}" for k, v in sorted(s.bodies_per_message.items())),
                ],
                ["Distinct `locationId`", len(s.location_ids)],
                ["Distinct natural keys", len(s.natural_keys)],
                ["Natural keys seen more than once", dup_keys],
                ["Min `date.utc`", min(s.ts_utc) if s.ts_utc else "n/a"],
                ["Max `date.utc`", max(s.ts_utc) if s.ts_utc else "n/a"],
                ["Min SQS `SentTimestamp`", _fmt_ms(min(s.sent_ts_ms)) if s.sent_ts_ms else "n/a"],
                ["Max SQS `SentTimestamp`", _fmt_ms(max(s.sent_ts_ms)) if s.sent_ts_ms else "n/a"],
                [
                    "`ApproximateReceiveCount` (value: messages)",
                    ", ".join(f"{k}: {v}" for k, v in sorted(s.receive_counts.items())) or "n/a",
                ],
            ],
        )
    )
    out.append("")

    if s.parse_failures:
        out.append("## Parse failures\n")
        reasons = Counter(r for _, r in s.parse_failures)
        out.append(_table(["Reason", "Count"], [[r, n] for r, n in reasons.most_common()]))
        out.append("")

    out.append("## Fields\n")
    out.append(
        _table(
            ["Field", "Contract", "Records missing it"],
            [[f"`{f}`", "required", s.missing_fields.get(f, 0)] for f in REQUIRED_FIELDS]
            + [[f"`{f}`", "optional", s.missing_fields.get(f, 0)] for f in OPTIONAL_FIELDS]
            + [[f"`{k}`", "not in contract", 0] for k in s.extra_fields],
        )
    )
    out.append("")

    out.append("## Distinct values\n")
    out.append(_counter_table("`parameter` (normalised)", s.parameters))
    out.append("")
    unit_rows = [
        [f"`{raw}`", f"`{norm}`", status, n] for (raw, norm, status), n in s.units.most_common()
    ]
    out.append("### `unit`\n")
    out.append(_table(["Raw", "Normalised", "Status", "Records"], unit_rows))
    out.append("")
    for title, c in (
        ("`entity`", s.entities),
        ("`sensorType`", s.sensor_types),
        ("`isMobile`", s.is_mobile),
        ("`isAnalysis`", s.is_analysis),
        ("`country`", s.countries),
    ):
        out.append(_counter_table(title, c))
        out.append("")

    out.append("## Raw values per parameter\n")
    rows = []
    for p, vals in sorted(s.values_by_parameter.items()):
        rows.append(
            [
                f"`{p}`",
                len(vals),
                f"{min(vals):g}",
                f"{statistics.median(vals):g}",
                f"{max(vals):g}",
                sum(1 for v in vals if v < 0),
            ]
        )
    out.append(_table(["Parameter", "n", "Min", "Median", "Max", "Negative"], rows))
    out.append("")

    out.append("## Station names\n")
    multi = {i: n for i, n in s.location_names_per_id.items() if len(n) > 1}
    out.append(
        _table(
            ["Metric", "Value"],
            [
                ["`locationId` with more than one `location` name", len(multi)],
                ["`location` names shared by more than one `locationId`", len(shared)],
            ],
        )
    )
    out.append("")
    return "\n".join(out)
