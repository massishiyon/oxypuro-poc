from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import yaml

from oxypuro.analytics.curate import CURATED_DIR
from oxypuro.config import CONFIG_DIR, load_analysis, load_thresholds

QC_PATH = CURATED_DIR / "measurements_qc.parquet"
SERIES = ["location_id", "parameter"]
HOUR = pd.Timedelta(hours=1)
MAD_TO_SIGMA = 1.4826

INVALID_RULES = ("negative", "implausible_high")
SUSPECT_RULES = ("flatline", "pm_ratio", "isolated_spike")
INFO_FLAGS = ("small_negative", "regional_event", "no_neighbours", "location_suspect")
ALL_FLAGS = INVALID_RULES + SUSPECT_RULES + INFO_FLAGS


@dataclass(frozen=True)
class QCConfig:
    negative_tolerance: float
    physical_limits: dict[str, float]
    flatline_min_hours: int
    pm_ratio_tolerance_rel: float
    pm_ratio_tolerance_abs: float
    spike_window_hours: int
    spike_min_points: int
    spike_robust_z: float
    spike_abs_floor: dict[str, float]
    mad_floor: dict[str, float]
    neighbour_radius_km: float
    neighbour_fallback_k: int
    neighbour_fallback_max_km: float
    neighbour_agree_z: float
    neighbour_agree_share: float

    @classmethod
    def load(cls) -> QCConfig:
        qc, th = load_analysis()["qc"], load_thresholds()
        return cls(
            negative_tolerance=th["negative_tolerance"],
            physical_limits=th["physical_limits"],
            flatline_min_hours=qc["flatline_min_hours"],
            pm_ratio_tolerance_rel=qc["pm_ratio_tolerance_rel"],
            pm_ratio_tolerance_abs=qc["pm_ratio_tolerance_abs"],
            spike_window_hours=qc["spike_window_hours"],
            spike_min_points=qc["spike_min_points"],
            spike_robust_z=qc["spike_robust_z"],
            spike_abs_floor=qc["spike_abs_floor"],
            mad_floor=qc["mad_floor"],
            neighbour_radius_km=qc["neighbour_radius_km"],
            neighbour_fallback_k=qc["neighbour_fallback_k"],
            neighbour_fallback_max_km=qc["neighbour_fallback_max_km"],
            neighbour_agree_z=qc["neighbour_agree_z"],
            neighbour_agree_share=qc["neighbour_agree_share"],
        )


def load_station_flags() -> dict[int, dict[str, Any]]:
    path = CONFIG_DIR / "station_flags.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {int(k): v for k, v in (data.get("location_suspect") or {}).items()}


def haversine_km(lat1, lon1, lat2, lon2):
    p = np.radians
    a = (
        np.sin(p(lat2 - lat1) / 2) ** 2
        + np.cos(p(lat1)) * np.cos(p(lat2)) * np.sin(p(lon2 - lon1) / 2) ** 2
    )
    return 6371.0 * 2 * np.arcsin(np.sqrt(a))


def find_neighbours(
    m: pd.DataFrame, stations: pd.DataFrame, cfg: QCConfig, excluded: set[int]
) -> dict[tuple[int, str], list[int]]:
    st = stations.set_index("location_id")
    series = m[SERIES].drop_duplicates()
    measuring = series.groupby("parameter")["location_id"].apply(set).to_dict()
    out: dict[tuple[int, str], list[int]] = {}
    for loc, param in series.itertuples(index=False):
        if loc in excluded:
            out[(loc, param)] = []
            continue
        cands = sorted(measuring[param] - {loc} - excluded)
        if not cands:
            out[(loc, param)] = []
            continue
        d = pd.Series(
            haversine_km(
                st.loc[loc, "lat"],
                st.loc[loc, "lon"],
                st.loc[cands, "lat"].values,
                st.loc[cands, "lon"].values,
            ),
            index=cands,
        ).sort_values()
        within = d[d <= cfg.neighbour_radius_km]
        if len(within) < 2:
            within = d[d <= cfg.neighbour_fallback_max_km].head(cfg.neighbour_fallback_k)
        out[(loc, param)] = within.index.tolist()
    return out


def rule_negative(m: pd.DataFrame, cfg: QCConfig) -> pd.Series:
    return m["value"] < cfg.negative_tolerance


def rule_small_negative(m: pd.DataFrame, cfg: QCConfig) -> pd.Series:
    return (m["value"] >= cfg.negative_tolerance) & (m["value"] < 0)


def rule_implausible_high(m: pd.DataFrame, cfg: QCConfig) -> pd.Series:
    limits = m["parameter"].map(cfg.physical_limits)
    return limits.notna() & (m["value"] > limits)


def rule_flatline(m: pd.DataFrame, cfg: QCConfig) -> pd.Series:
    s = m.sort_values(SERIES + ["ts_utc"])
    continues = (
        (s["location_id"] == s["location_id"].shift())
        & (s["parameter"] == s["parameter"].shift())
        & (s["ts_utc"] - s["ts_utc"].shift() == HOUR)
        & np.isclose(s["value"], s["value"].shift())
    )
    run = (~continues).cumsum()
    run_len = run.map(run.value_counts())
    return (run_len >= cfg.flatline_min_hours).reindex(m.index)


def rule_pm_ratio(m: pd.DataFrame, cfg: QCConfig, usable: pd.Series | None = None) -> pd.Series:
    pm = m[m["parameter"].isin(["pm25", "pm10"])]
    if usable is not None:
        pm = pm[usable.loc[pm.index]]
    w = pm.pivot_table(index=["location_id", "ts_utc"], columns="parameter", values="value")
    flagged = pd.Series(False, index=m.index)
    if not {"pm25", "pm10"} <= set(w.columns):
        return flagged
    w = w.dropna(subset=["pm25", "pm10"])
    bad = w.index[
        w["pm25"] > w["pm10"] * (1 + cfg.pm_ratio_tolerance_rel) + cfg.pm_ratio_tolerance_abs
    ]
    key = pd.MultiIndex.from_frame(pm[["location_id", "ts_utc"]])
    flagged.loc[pm.index] = key.isin(bad)
    return flagged


def trailing_robust_z(m: pd.DataFrame, cfg: QCConfig, usable: pd.Series) -> pd.DataFrame:
    s = m.sort_values(SERIES + ["ts_utc"])
    window = f"{cfg.spike_window_hours}h"

    def per_series(g: pd.DataFrame) -> pd.DataFrame:
        x = g.set_index("ts_utc")["value"].where(usable.loc[g.index].values)
        roll = x.rolling(window, closed="left", min_periods=cfg.spike_min_points)
        med = roll.median()
        mad = roll.apply(
            lambda w: np.median(np.abs(w[~np.isnan(w)] - np.median(w[~np.isnan(w)]))), raw=True
        )
        floor = cfg.mad_floor.get(g.name[1], 0.0)
        scale = np.maximum(MAD_TO_SIGMA * mad.values, floor)
        excess = g["value"].values - med.values
        z = excess / scale
        warm = (g["ts_utc"] < g["ts_utc"].min() + pd.Timedelta(window)).values
        z[warm] = np.nan
        excess[warm] = np.nan
        return pd.DataFrame({"robust_z": z, "excess": excess}, index=g.index)

    return s.groupby(SERIES, group_keys=False).apply(per_series).reindex(m.index)


def neighbour_agreement(
    m: pd.DataFrame, z: pd.Series, neighbours: dict[tuple[int, str], list[int]], cfg: QCConfig
) -> pd.Series:
    share = pd.Series(np.nan, index=m.index)
    frame = m[SERIES + ["ts_utc"]].assign(z=z)
    for param, grp in frame.groupby("parameter"):
        wide = grp.pivot_table(index="ts_utc", columns="location_id", values="z")
        high = wide > cfg.neighbour_agree_z
        for loc, rows in grp.groupby("location_id"):
            nbrs = [n for n in neighbours.get((loc, param), []) if n in wide.columns]
            if not nbrs:
                continue
            present = wide[nbrs].notna().sum(axis=1)
            agree = high[nbrs].sum(axis=1) / present.where(present > 0)
            share.loc[rows.index] = agree.reindex(rows["ts_utc"]).values
    return share


def spike_mask(m: pd.DataFrame, rz: pd.DataFrame, cfg: QCConfig) -> pd.Series:
    floor = m["parameter"].map(cfg.spike_abs_floor).fillna(0.0)
    return (rz["robust_z"] > cfg.spike_robust_z) & (rz["excess"] > floor)


def run_qc(
    m: pd.DataFrame, stations: pd.DataFrame, cfg: QCConfig, station_flags: dict[int, Any]
) -> pd.DataFrame:
    m = m.reset_index(drop=True)
    excluded = set(station_flags)
    flags: dict[str, pd.Series] = {
        "negative": rule_negative(m, cfg),
        "small_negative": rule_small_negative(m, cfg),
        "implausible_high": rule_implausible_high(m, cfg),
        "flatline": rule_flatline(m, cfg),
    }
    usable = ~(flags["negative"] | flags["implausible_high"])
    flags["pm_ratio"] = rule_pm_ratio(m, cfg, usable)
    rz = trailing_robust_z(m, cfg, usable & ~flags["flatline"])
    neighbours = find_neighbours(m, stations, cfg, excluded)
    share = neighbour_agreement(m, rz["robust_z"].where(usable), neighbours, cfg)
    spike = spike_mask(m, rz, cfg) & usable

    is_excluded = m["location_id"].isin(excluded)
    has_nbrs = pd.Series(
        [bool(neighbours.get(k)) for k in zip(m["location_id"], m["parameter"], strict=True)],
        index=m.index,
    )
    regional = spike & (share >= cfg.neighbour_agree_share)
    flags["isolated_spike"] = spike & ~regional
    flags["regional_event"] = regional
    flags["location_suspect"] = is_excluded
    flags["no_neighbours"] = ~is_excluded & ~has_nbrs

    out = m.copy()
    out["robust_z"] = rz["robust_z"]
    out["neighbour_share"] = share
    flag_frame = pd.DataFrame({k: flags[k].fillna(False).astype(bool) for k in ALL_FLAGS})
    names = np.array(ALL_FLAGS)
    out["qc_reasons"] = [names[row].tolist() for row in flag_frame.to_numpy()]
    invalid = flag_frame[list(INVALID_RULES)].any(axis=1)
    suspect = flag_frame[list(SUSPECT_RULES)].any(axis=1)
    out["qc_status"] = np.select([invalid, suspect], ["invalid", "suspect"], default="valid")
    return out


def main() -> int:
    from oxypuro.analytics.quality_report import write_report

    cfg = QCConfig.load()
    station_flags = load_station_flags()
    m = pd.read_parquet(CURATED_DIR / "measurements.parquet")
    stations = pd.read_parquet(CURATED_DIR / "stations.parquet")

    qc = run_qc(m, stations, cfg, station_flags)
    qc.to_parquet(QC_PATH, index=False)

    status = qc["qc_status"].value_counts().to_dict()
    print(f"qc: {len(qc)} values, status {status}")
    write_report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
