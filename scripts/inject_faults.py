from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from oxypuro.analytics.curate import CURATED_DIR
from oxypuro.analytics.quality import (
    INFO_FLAGS,
    QC_PATH,
    QCConfig,
    load_station_flags,
    run_qc,
)
from oxypuro.analytics.quality_report import INJECTED_QC_PATH, INJECTION_RESULTS_PATH, TEST_DIR
from oxypuro.config import DATA_DIR, load_analysis

HOUR = pd.Timedelta(hours=1)
SERIES_BUFFER = pd.Timedelta(hours=24)
CROSS_STATION_BUFFER = pd.Timedelta(hours=2)
INTENDED_RULE = {
    "spike": "isolated_spike",
    "flatline": "flatline",
    "negative": "negative",
    "unit_glitch": "implausible_high",
    "pm_ratio": "pm_ratio",
}


@dataclass
class Reservations:
    series: dict[tuple[int, str], list[tuple[pd.Timestamp, pd.Timestamp]]] = field(
        default_factory=dict
    )
    hours: dict[str, list[pd.Timestamp]] = field(default_factory=dict)

    def free(self, loc: int, param: str, t0: pd.Timestamp, t1: pd.Timestamp, cross: bool) -> bool:
        for a, b in self.series.get((loc, param), []):
            if t0 <= b + SERIES_BUFFER and t1 >= a - SERIES_BUFFER:
                return False
        if cross:
            for h in self.hours.get(param, []):
                if abs(h - t0) <= CROSS_STATION_BUFFER:
                    return False
        return True

    def take(self, loc: int, param: str, t0: pd.Timestamp, t1: pd.Timestamp, cross: bool) -> None:
        self.series.setdefault((loc, param), []).append((t0, t1))
        if cross:
            self.hours.setdefault(param, []).append(t0)


def eligible_rows(m: pd.DataFrame, min_history: pd.Timedelta, excluded: set[int]) -> pd.DataFrame:
    first = m.groupby(["location_id", "parameter"])["ts_utc"].transform("min")
    return m[(m["ts_utc"] >= first + min_history) & ~m["location_id"].isin(excluded)]


def plan_faults(m: pd.DataFrame, fi: dict[str, Any], excluded: set[int], rng) -> list[dict]:
    elig = eligible_rows(m, pd.Timedelta(hours=fi["min_history_hours"]), excluded)
    by_key = m.set_index(["location_id", "parameter", "ts_utc"])["value"]
    res = Reservations()
    faults: list[dict] = []

    def candidates(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.iloc[rng.permutation(len(frame))]

    n_hours = fi["flatline_hours"]
    for r in candidates(elig).itertuples(index=False):
        if sum(f["type"] == "flatline" for f in faults) >= fi["n_flatlines"]:
            break
        hours = [r.ts_utc + i * HOUR for i in range(n_hours)]
        if not all((r.location_id, r.parameter, h) in by_key.index for h in hours):
            continue
        if not res.free(r.location_id, r.parameter, hours[0], hours[-1], cross=False):
            continue
        res.take(r.location_id, r.parameter, hours[0], hours[-1], cross=False)
        faults.append(
            {
                "type": "flatline",
                "rows": [(r.location_id, r.parameter, h) for h in hours],
                "value": float(r.value),
            }
        )

    pm25 = elig[elig["parameter"] == "pm25"]
    for r in candidates(pm25).itertuples(index=False):
        if sum(f["type"] == "pm_ratio" for f in faults) >= fi["n_pm_ratio"]:
            break
        pm10 = by_key.get((r.location_id, "pm10", r.ts_utc))
        if pm10 is None or np.isnan(pm10):
            continue
        if not (
            res.free(r.location_id, "pm25", r.ts_utc, r.ts_utc, cross=True)
            and res.free(r.location_id, "pm10", r.ts_utc, r.ts_utc, cross=False)
        ):
            continue
        res.take(r.location_id, "pm25", r.ts_utc, r.ts_utc, cross=True)
        res.take(r.location_id, "pm10", r.ts_utc, r.ts_utc, cross=False)
        faults.append(
            {
                "type": "pm_ratio",
                "rows": [(r.location_id, "pm25", r.ts_utc), (r.location_id, "pm10", r.ts_utc)],
                "value": float(pm10) * fi["pm_ratio_factor"],
            }
        )

    for ftype, n_key in (
        ("spike", "n_spikes"),
        ("unit_glitch", "n_unit_glitches"),
        ("negative", "n_negatives"),
    ):
        cross = ftype in ("spike", "unit_glitch")
        for r in candidates(elig).itertuples(index=False):
            if sum(f["type"] == ftype for f in faults) >= fi[n_key]:
                break
            if not res.free(r.location_id, r.parameter, r.ts_utc, r.ts_utc, cross):
                continue
            res.take(r.location_id, r.parameter, r.ts_utc, r.ts_utc, cross)
            value = {
                "spike": r.value * fi["spike_factor"],
                "unit_glitch": r.value * fi["unit_glitch_factor"],
                "negative": float(fi["negative_value"]),
            }[ftype]
            faults.append(
                {
                    "type": ftype,
                    "rows": [(r.location_id, r.parameter, r.ts_utc)],
                    "value": float(value),
                    "original": float(r.value),
                }
            )

    for i, f in enumerate(faults):
        f["id"] = i
    return faults


def apply_faults(m: pd.DataFrame, faults: list[dict]) -> pd.DataFrame:
    out = m.copy()
    out["fault"] = pd.Series(pd.NA, index=out.index, dtype="string")
    out["fault_id"] = pd.Series(pd.NA, index=out.index, dtype="Int64")
    pos = pd.Series(
        out.index, index=pd.MultiIndex.from_frame(out[["location_id", "parameter", "ts_utc"]])
    )
    for f in faults:
        idx = pos.loc[f["rows"]].values
        target = idx[:1] if f["type"] == "pm_ratio" else idx
        out.loc[target, "value"] = f["value"]
        out.loc[idx, "fault"] = f["type"]
        out.loc[idx, "fault_id"] = f["id"]
    return out


def evaluate(
    qc: pd.DataFrame, faults: list[dict], cfg: QCConfig, already_flagged: pd.Series
) -> dict[str, Any]:
    flagged = {
        rule: qc["qc_reasons"].map(lambda r, rule=rule: rule in r)
        for rule in INTENDED_RULE.values()
    }
    injected = qc["fault"].notna()
    table = []
    missed_counts = {"missed": 0, "z": 0, "floor": 0, "regional": 0}
    for ftype, rule in INTENDED_RULE.items():
        ids = [f["id"] for f in faults if f["type"] == ftype]
        rows = qc[qc["fault_id"].isin(ids)]
        per_fault = rows.groupby("fault_id")
        detected = per_fault.apply(lambda g, rule=rule: flagged[rule].loc[g.index].all())
        not_valid = per_fault.apply(lambda g: (g["qc_status"] != "valid").all())
        hits = flagged[rule] & ~already_flagged
        precision = (hits & injected).sum() / hits.sum() if hits.sum() else float("nan")
        table.append(
            {
                "fault": ftype,
                "intended rule": f"`{rule}`",
                "injected": len(ids),
                "detected": int(detected.sum()),
                "recall %": round(100 * detected.mean(), 1) if len(ids) else float("nan"),
                "not valid %": round(100 * not_valid.mean(), 1) if len(ids) else float("nan"),
                "precision %": round(100 * precision, 1),
            }
        )

    spike_rows = qc[qc["fault"] == "spike"]
    missed = spike_rows[~flagged["isolated_spike"].loc[spike_rows.index]]
    if len(missed):
        regional = missed["qc_reasons"].map(lambda r: "regional_event" in r)
        low_z = ~regional & (missed["robust_z"] <= cfg.spike_robust_z)
        floor = ~regional & ~low_z
        orig = {f["id"]: f["original"] for f in faults if f["type"] == "spike"}
        detail = missed.assign(
            original=[orig[int(i)] for i in missed["fault_id"]],
            reason=np.select([regional, low_z], ["regional", "z"], default="floor"),
        )
        missed_counts = {
            "missed": len(missed),
            "z": int(low_z.sum()),
            "floor": int(floor.sum()),
            "regional": int(regional.sum()),
        }
        missed_detail = detail.assign(local_hour=detail["ts_local"].dt.hour)[
            [
                "location_id",
                "parameter",
                "ts_utc",
                "local_hour",
                "original",
                "value",
                "robust_z",
                "reason",
            ]
        ]
    else:
        missed_detail = pd.DataFrame()

    other = qc[(qc["qc_status"] != "valid") & ~injected & ~already_flagged]
    pm_partner = injected.groupby([qc["location_id"], qc["ts_utc"]]).transform("any") & qc[
        "parameter"
    ].isin(["pm25", "pm10"])
    collateral = other[
        other["qc_reasons"].map(lambda r: set(r) - set(INFO_FLAGS) == {"pm_ratio"})
        & pm_partner.loc[other.index]
    ]
    return {
        "table": table,
        "missed_counts": missed_counts,
        "missed_spikes": missed_detail.astype({"ts_utc": str}).to_dict(orient="records")
        if len(missed_detail)
        else [],
        "already_flagged_in_real_data": int(already_flagged.sum()),
        "collateral_pm_partner": int(len(collateral)),
        "other_side_effects": int(len(other) - len(collateral)),
    }


def main() -> int:
    analysis = load_analysis()
    fi = analysis["qc"]["fault_injection"]
    for path in (INJECTED_QC_PATH, INJECTION_RESULTS_PATH):
        assert path.parent == TEST_DIR and TEST_DIR.parent == DATA_DIR, path
    m = pd.read_parquet(CURATED_DIR / "measurements.parquet")
    stations = pd.read_parquet(CURATED_DIR / "stations.parquet")
    flags = load_station_flags()
    cfg = QCConfig.load()

    rng = np.random.default_rng(fi["random_seed"])
    faults = plan_faults(m, fi, set(flags), rng)
    copy = apply_faults(m, faults)
    qc = run_qc(copy, stations, cfg, flags)

    key = ["location_id", "parameter", "ts_utc"]
    real_status = pd.read_parquet(QC_PATH, columns=key + ["qc_status"]).set_index(key)["qc_status"]
    already = pd.Series(
        real_status.reindex(pd.MultiIndex.from_frame(qc[key])).to_numpy() != "valid",
        index=qc.index,
    )
    result = evaluate(qc, faults, cfg, already)
    result.update({"seed": fi["random_seed"], "n_total": len(faults)})
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    qc.to_parquet(INJECTED_QC_PATH, index=False)
    INJECTION_RESULTS_PATH.write_text(
        json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
    )
    for row in result["table"]:
        print(
            f"{row['fault']:<12} recall {row['recall %']:>5}%  not valid {row['not valid %']:>5}%  "
            f"precision {row['precision %']:>5}%  ({row['detected']}/{row['injected']})"
        )
    print("missed spikes:", result["missed_counts"])
    print(
        "non-injected flags:",
        {
            k: result[k]
            for k in ("already_flagged_in_real_data", "collateral_pm_partner", "other_side_effects")
        },
    )

    from oxypuro.analytics.quality_report import write_report

    write_report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
