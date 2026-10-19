from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

from oxypuro.config import DATA_DIR
from oxypuro.core.units import normalise_parameter, to_canonical

RAW_DIR = DATA_DIR / "raw"
CURATED_DIR = DATA_DIR / "curated"
LOCAL_TZ = ZoneInfo("Europe/Brussels")
KEY = ["location_id", "parameter", "ts_utc"]
TEST_MESSAGE_PREFIX = "test-"

RAW_COLUMNS = {
    "locationId": "BIGINT",
    "location": "VARCHAR",
    "city": "VARCHAR",
    "country": "VARCHAR",
    "coordinates": "STRUCT(latitude DOUBLE, longitude DOUBLE)",
    "parameter": "VARCHAR",
    "value": "DOUBLE",
    "unit": "VARCHAR",
    "date": "STRUCT(utc VARCHAR, local VARCHAR)",
    "entity": "VARCHAR",
    "sensorType": "VARCHAR",
    "isMobile": "BOOLEAN",
    "isAnalysis": "BOOLEAN",
    "_meta": "STRUCT(message_id VARCHAR, ingested_at VARCHAR, sqs_sent_ts VARCHAR)",
}


def load_raw(raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    files = sorted(raw_dir.rglob("*.jsonl.gz"))
    if not files:
        raise SystemExit(f"No raw files under {raw_dir}. Run python scripts/sync_raw.py first.")
    cols = ", ".join(f"'{k}': '{v}'" for k, v in RAW_COLUMNS.items())
    query = f"""
        SELECT
            locationId              AS location_id,
            location, city, country,
            coordinates.latitude    AS lat,
            coordinates.longitude   AS lon,
            parameter               AS parameter_raw,
            value                   AS value_raw,
            unit                    AS unit_raw,
            date.utc                AS ts_utc_raw,
            date.local              AS date_local_raw,
            entity,
            sensorType              AS sensor_type,
            coalesce(isMobile, false)   AS is_mobile,
            coalesce(isAnalysis, false) AS is_analysis,
            _meta.message_id        AS message_id,
            _meta.ingested_at       AS ingested_at,
            _meta.sqs_sent_ts       AS sqs_sent_ts
        FROM read_json(?, format = 'newline_delimited', compression = 'gzip',
                       columns = {{{cols}}})
    """
    df = duckdb.connect().execute(query, [[str(f) for f in files]]).df()
    for c in ("ts_utc_raw", "ingested_at", "sqs_sent_ts"):
        df[c.removesuffix("_raw")] = pd.to_datetime(df[c], utc=True, format="ISO8601")
    return df.drop(columns=["ts_utc_raw"])


def unit_lookup(pairs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for p_raw, u_raw in (
        pairs[["parameter_raw", "unit_raw"]].drop_duplicates().itertuples(index=False)
    ):
        parameter = normalise_parameter(p_raw)
        factor, unit, status = to_canonical(parameter, 1.0, u_raw)
        rows.append((p_raw, u_raw, parameter, factor, unit, status))
    return pd.DataFrame(
        rows, columns=["parameter_raw", "unit_raw", "parameter", "factor", "unit", "unit_status"]
    )


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    lk = unit_lookup(df)
    out = df.merge(lk, on=["parameter_raw", "unit_raw"], how="left", validate="many_to_one")
    out["value"] = out["value_raw"] * out["factor"]
    return out.drop(columns=["factor"])


def add_local_time(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ts_local"] = out["ts_utc"].dt.tz_convert(LOCAL_TZ)
    out["date_local"] = out["ts_local"].dt.date
    out["hour_local"] = out["ts_local"].dt.hour.astype("int8")
    out["weekday_local"] = out["ts_local"].dt.weekday.astype("int8")
    return out


def local_time_mismatches(df: pd.DataFrame) -> int:
    raw = df["date_local_raw"].astype("string")
    has = raw.notna()
    if not has.any():
        return 0
    ours = df.loc[has, "ts_local"]
    wall = ours.dt.strftime("%Y-%m-%dT%H:%M:%S")
    offset_min = (
        ours.dt.tz_localize(None) - df.loc[has, "ts_utc"].dt.tz_localize(None)
    ) / pd.Timedelta(minutes=1)
    sign = offset_min.map(lambda m: "+" if m >= 0 else "-")
    absm = offset_min.abs().astype(int)
    off = sign + (absm // 60).map("{:02d}".format) + ":" + (absm % 60).map("{:02d}".format)
    r = raw[has]
    raw_wall = r.str.slice(0, 19)
    raw_off = r.str.slice(-6).str.replace("Z", "+00:00", regex=False)
    return int(((raw_wall != wall) | (raw_off != off)).sum())


def deduplicate(df: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    ordered = df.sort_values(
        KEY + ["ingested_at", "sqs_sent_ts", "message_id"],
        ascending=[True, True, True, False, False, False],
        na_position="last",
        kind="mergesort",
    )
    dup_mask = ordered.duplicated(KEY, keep=False)
    dups = ordered[dup_mask]
    conflicting = (
        int((dups.groupby(KEY)["value"].nunique(dropna=False) > 1).sum()) if len(dups) else 0
    )
    kept = ordered.drop_duplicates(KEY, keep="first")
    return kept, len(df) - len(kept), conflicting


def build_stations(m: pd.DataFrame) -> pd.DataFrame:
    latest = m.sort_values("ts_utc").groupby("location_id").tail(1).set_index("location_id")
    g = m.groupby("location_id")
    st = pd.DataFrame(
        {
            "location": latest["location"],
            "city": latest["city"],
            "lat": latest["lat"],
            "lon": latest["lon"],
            "entity": latest["entity"],
            "sensor_type": latest["sensor_type"],
            "parameters": g["parameter"].agg(lambda s: sorted(s.unique())),
            "first_ts": g["ts_utc"].min(),
            "last_ts": g["ts_utc"].max(),
            "n_obs": g.size(),
            "coords_vary": (g["lat"].nunique() > 1) | (g["lon"].nunique() > 1),
            "sensor_type_varies": g["sensor_type"].nunique() > 1,
        }
    )
    return st.reset_index().sort_values("location_id").reset_index(drop=True)


def curate(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    stats: dict[str, Any] = {"raw_lines": len(raw)}

    is_test = raw["message_id"].astype("string").str.startswith(TEST_MESSAGE_PREFIX).fillna(False)
    stats["excluded_test_records"] = int(is_test.sum())
    df = normalise(raw[~is_test])
    df = add_local_time(df)
    stats["local_time_mismatches"] = local_time_mismatches(df)

    df, n_dup, n_conflict = deduplicate(df)
    stats["duplicate_rows_removed"] = n_dup
    stats["conflicting_duplicate_keys"] = n_conflict
    stats["rows_after_dedup"] = len(df)

    stats["excluded_mobile"] = int(df["is_mobile"].sum())
    stats["excluded_analysis"] = int(df["is_analysis"].sum())
    stats["excluded_unknown_unit"] = int((df["unit_status"] == "unknown").sum())
    keep = ~df["is_mobile"] & ~df["is_analysis"] & (df["unit_status"] != "unknown")
    m = df[keep].drop(columns=["is_mobile", "is_analysis"])
    m = m.sort_values(KEY, kind="mergesort").reset_index(drop=True)
    stats["curated_rows"] = len(m)

    stations = build_stations(m)
    stats["stations"] = len(stations)
    stats["parameters"] = sorted(m["parameter"].unique().tolist())
    stats["unit_pairs"] = (
        unit_lookup(raw)[["parameter_raw", "unit_raw", "parameter", "unit_status"]]
        .sort_values(["parameter", "unit_raw"])
        .to_dict(orient="records")
    )
    return m, stations, stats


def iso_z(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    raw = load_raw()
    m, stations, stats = curate(raw)
    CURATED_DIR.mkdir(parents=True, exist_ok=True)
    m.to_parquet(CURATED_DIR / "measurements.parquet", index=False)
    stations.to_parquet(CURATED_DIR / "stations.parquet", index=False)
    now = {"now_utc": iso_z(m["ts_utc"].max())}
    (CURATED_DIR / "now.json").write_text(json.dumps(now, indent=2) + "\n", encoding="utf-8")
    (CURATED_DIR / "curation_stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"raw lines {stats['raw_lines']} -> curated rows {stats['curated_rows']} "
        f"({stats['duplicate_rows_removed']} duplicates, "
        f"{stats['conflicting_duplicate_keys']} conflicting keys, "
        f"{stats['excluded_test_records']} test records, "
        f"{stats['excluded_mobile']} mobile, {stats['excluded_analysis']} analysis, "
        f"{stats['excluded_unknown_unit']} unknown unit)"
    )
    print(f"stations {stats['stations']}, parameters {', '.join(stats['parameters'])}")
    print(f"local time mismatches {stats['local_time_mismatches']}, now {now['now_utc']}")
    print(f"wrote {CURATED_DIR.relative_to(DATA_DIR.parent)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
