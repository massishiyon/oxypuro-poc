from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from oxypuro.analytics.curate import CURATED_DIR, iso_z  # noqa: E402
from oxypuro.analytics.report_utils import md_table  # noqa: E402
from oxypuro.config import REPORTS_DIR, load_analysis  # noqa: E402

FIGURES_DIR = REPORTS_DIR / "figures"
REPORT_PATH = REPORTS_DIR / "profile.md"
SERIES = ["location_id", "parameter"]
HOUR = pd.Timedelta(hours=1)
FIGSIZE = (12.8, 7.2)


@dataclass(frozen=True)
class Pace:
    sim_hours_per_real_hour: float | None
    r2: float | None
    real_hours_covered: float
    median_lag_days: float | None
    n_points: int


def replay_pace(df: pd.DataFrame, real_col: str, min_points: int = 3) -> Pace:
    d = df[[real_col, "ts_utc"]].dropna().sort_values(real_col)
    if d.empty:
        return Pace(None, None, 0.0, None, 0)
    d = d.assign(watermark=d["ts_utc"].cummax(), real_hour=d[real_col].dt.floor("h"))
    pts = d.groupby("real_hour")["watermark"].max()
    real_h = (pts.index - pts.index[0]) / HOUR
    sim_h = (pts.values - pts.values[0]) / HOUR
    covered = float(real_h[-1]) if len(pts) else 0.0
    lag = (d[real_col] - d["ts_utc"]).median() / pd.Timedelta(days=1)
    if len(pts) < min_points or covered == 0:
        return Pace(None, None, covered, float(lag), len(pts))
    x, y = np.asarray(real_h, float), np.asarray(sim_h, float)
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (slope * x + intercept)
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1 - (resid**2).sum() / ss_tot if ss_tot else None
    return Pace(float(slope), None if r2 is None else float(r2), covered, float(lag), len(pts))


LIVE_MAX_QUEUE_TIME = pd.Timedelta(minutes=5)


def live_rows(m: pd.DataFrame) -> pd.DataFrame:
    return m[(m["ingested_at"] - m["sqs_sent_ts"]) <= LIVE_MAX_QUEUE_TIME]


def backward_jumps(df: pd.DataFrame, order_col: str, threshold: pd.Timedelta) -> pd.DataFrame:
    d = df[[order_col, "ts_utc", "location_id", "parameter"]].dropna().sort_values(order_col)
    prev_max = d["ts_utc"].cummax().shift(1)
    jumps = d[d["ts_utc"] < prev_max - threshold].copy()
    jumps["behind_by"] = (prev_max[jumps.index] - jumps["ts_utc"]).values
    return jumps


def repeated_value_share(df: pd.DataFrame, period: pd.Timedelta) -> float | None:
    a = df[SERIES + ["ts_utc", "value"]]
    b = a.assign(ts_utc=a["ts_utc"] + period)
    joined = a.merge(b, on=SERIES + ["ts_utc"], suffixes=("", "_before"))
    if joined.empty:
        return None
    return float(np.isclose(joined["value"], joined["value_before"]).mean())


def hour_grid(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq="h", tz="UTC")


def expected_hours_per_local_day(start: pd.Timestamp, end: pd.Timestamp, tz) -> pd.Series:
    grid = hour_grid(start, end)
    return pd.Series(1, index=grid.tz_convert(tz).date).groupby(level=0).sum()


def completeness_by_station_day(m: pd.DataFrame, tz) -> pd.DataFrame:
    start, end = m["ts_utc"].min(), m["ts_utc"].max()
    exp_hours = expected_hours_per_local_day(start, end, tz)
    n_params = m.groupby("location_id")["parameter"].nunique()
    obs = m.groupby(["location_id", "date_local"]).size().unstack(fill_value=0)
    obs = obs.reindex(columns=exp_hours.index, fill_value=0)
    expected = np.outer(n_params.reindex(obs.index).values, exp_hours.values)
    return obs / expected


def series_gaps(m: pd.DataFrame) -> pd.DataFrame:
    start, end = m["ts_utc"].min(), m["ts_utc"].max()
    span_hours = int((end - start) / HOUR) + 1
    s = m.sort_values(SERIES + ["ts_utc"])
    step = s.groupby(SERIES)["ts_utc"].diff() / HOUR - 1
    s = s.assign(gap=step)
    first = s.groupby(SERIES)["ts_utc"].min()
    last = s.groupby(SERIES)["ts_utc"].max()
    out = pd.DataFrame(
        {
            "n_obs": s.groupby(SERIES).size(),
            "longest_internal_gap_h": s.groupby(SERIES)["gap"].max().fillna(0).astype(int),
            "missing_at_start_h": ((first - start) / HOUR).astype(int),
            "missing_at_end_h": ((end - last) / HOUR).astype(int),
        }
    )
    out["completeness"] = out["n_obs"] / span_hours
    return out.reset_index()


def network_missing_hours(m: pd.DataFrame) -> list[pd.Timestamp]:
    grid = hour_grid(m["ts_utc"].min(), m["ts_utc"].max())
    return list(grid.difference(pd.DatetimeIndex(m["ts_utc"].unique())))


def pm_ratio_violations(m: pd.DataFrame, tol_rel: float, tol_abs: float) -> tuple[int, int, int]:
    pm = m[m["parameter"].isin(["pm25", "pm10"])]
    w = pm.pivot_table(index=["location_id", "ts_utc"], columns="parameter", values="value")
    if not {"pm25", "pm10"} <= set(w.columns):
        return 0, 0, 0
    w = w.dropna(subset=["pm25", "pm10"])
    excess = w["pm25"] - w["pm10"]
    margin = np.maximum(tol_abs, tol_rel * w["pm10"])
    return len(w), int((excess > 0).sum()), int((excess > margin).sum())


def flatline_runs(m: pd.DataFrame, min_hours: int) -> pd.DataFrame:
    s = m.sort_values(SERIES + ["ts_utc"])[SERIES + ["ts_utc", "value"]]
    same_series = (s["location_id"] == s["location_id"].shift()) & (
        s["parameter"] == s["parameter"].shift()
    )
    continues = (
        same_series
        & (s["ts_utc"] - s["ts_utc"].shift() == HOUR)
        & np.isclose(s["value"], s["value"].shift())
    )
    run_id = (~continues).cumsum()
    runs = (
        s.assign(run=run_id)
        .groupby("run")
        .agg(
            location_id=("location_id", "first"),
            parameter=("parameter", "first"),
            start=("ts_utc", "first"),
            hours=("ts_utc", "size"),
            value=("value", "first"),
        )
    )
    return runs[runs["hours"] >= min_hours].reset_index(drop=True)


def value_stats(m: pd.DataFrame) -> pd.DataFrame:
    g = m.groupby("parameter")["value"]
    return pd.DataFrame(
        {
            "n": g.size(),
            "min": g.min(),
            "p1": g.quantile(0.01),
            "p50": g.median(),
            "p99": g.quantile(0.99),
            "max": g.max(),
            "negatives": g.apply(lambda v: int((v < 0).sum())),
            "zeros": g.apply(lambda v: int((v == 0).sum())),
        }
    )


def fig_completeness(comp: pd.DataFrame, stations: pd.DataFrame, path) -> None:
    labels = stations.set_index("location_id").reindex(comp.index)
    ylabels = [f"{i} {n}" for i, n in zip(comp.index, labels["location"], strict=True)]
    fig, ax = plt.subplots(figsize=(FIGSIZE[0], max(FIGSIZE[1], 0.2 * len(comp) + 1.5)))
    im = ax.imshow(comp.values, aspect="auto", cmap="viridis", vmin=0, vmax=1)
    ax.set_xticks(range(len(comp.columns)))
    ax.set_xticklabels([d.strftime("%m-%d") for d in comp.columns], rotation=90, fontsize=9)
    ax.set_yticks(range(len(comp.index)))
    ax.set_yticklabels(ylabels, fontsize=7)
    ax.set_xlabel("Local date (first and last day are partial: expected hours limited to the span)")
    ax.set_title("Share of expected hourly values per station and day")
    fig.colorbar(im, ax=ax, fraction=0.025, label="completeness")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def fig_diurnal(m: pd.DataFrame, path) -> None:
    params = sorted(m["parameter"].unique())
    fig, axes = plt.subplots(1, len(params), figsize=FIGSIZE, sharex=True)
    for ax, p in zip(np.atleast_1d(axes), params, strict=True):
        d = m[m["parameter"] == p]
        for st_type, grp in d.groupby("sensor_type"):
            med = grp.groupby("hour_local")["value"].median()
            ax.plot(med.index, med.values, marker="o", ms=3, label=st_type)
        ax.set_title(p.upper().replace("PM25", "PM2.5"))
        ax.set_xlabel("local hour")
        ax.set_xticks([0, 6, 12, 18, 23])
        ax.grid(alpha=0.3)
    np.atleast_1d(axes)[0].set_ylabel("median µg/m³")
    np.atleast_1d(axes)[0].legend(fontsize=9)
    fig.suptitle("Median concentration by local hour, per sensor type")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def fig_map(stations: pd.DataFrame, path) -> None:
    fig, ax = plt.subplots(figsize=FIGSIZE)
    for st_type, grp in stations.groupby("sensor_type"):
        ax.scatter(grp["lon"], grp["lat"], s=40, label=st_type, alpha=0.8)
    for r in stations.itertuples():
        ax.annotate(
            str(r.location_id),
            (r.lon, r.lat),
            fontsize=7,
            xytext=(3, 3),
            textcoords="offset points",
        )
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_aspect(1 / np.cos(np.radians(stations["lat"].mean())))
    ax.set_title("Station locations (no basemap)")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def build_report(m: pd.DataFrame, stations: pd.DataFrame, stats: dict[str, Any], now: str) -> str:
    qc = load_analysis()["qc"]
    start, end = m["ts_utc"].min(), m["ts_utc"].max()
    span_days = (end - start) / pd.Timedelta(days=1)

    gaps = series_gaps(m)
    comp_station = gaps.groupby("location_id").apply(
        lambda g: g["n_obs"].sum() / (g["n_obs"] / g["completeness"]).sum(), include_groups=False
    )
    missing_hours = network_missing_hours(m)

    live = live_rows(m)
    pace_sent = replay_pace(m, "sqs_sent_ts")
    pace_live = replay_pace(live, "ingested_at")
    jumps = backward_jumps(m, "sqs_sent_ts", pd.Timedelta(days=1))
    window = pd.Timedelta(days=60)
    repeat = repeated_value_share(m, window) if span_days > window.days else None

    n_pm, n_pm_gt, n_pm_viol = pm_ratio_violations(
        m, qc["pm_ratio_tolerance_rel"], qc["pm_ratio_tolerance_abs"]
    )
    flats = flatline_runs(m, qc["flatline_min_hours"])
    vstats = value_stats(m).reset_index()

    def kv(rows: list[tuple]) -> str:
        return md_table(pd.DataFrame(rows, columns=["metric", "value"]))

    def pace_row(name: str, p: Pace) -> tuple:
        return (
            name,
            "n/a" if p.sim_hours_per_real_hour is None else f"{p.sim_hours_per_real_hour:.2f}×",
            "n/a" if p.r2 is None else f"{p.r2:.4f}",
            f"{p.real_hours_covered:.0f} h",
            "n/a" if p.median_lag_days is None else f"{p.median_lag_days:.2f} days",
        )

    out: list[str] = ["# Data profile\n"]
    out.append(
        kv(
            [
                ("first ts_utc", iso_z(start)),
                ("last ts_utc", iso_z(end)),
                ("span (days)", f"{span_days:.2f}"),
                ("now (watermark)", now),
            ]
        )
    )
    out.append("\n## Volume\n")
    out.append(
        kv(
            [
                ("raw lines", stats["raw_lines"]),
                ("console test records excluded", stats["excluded_test_records"]),
                ("duplicate rows removed", stats["duplicate_rows_removed"]),
                ("conflicting duplicate keys", stats["conflicting_duplicate_keys"]),
                ("excluded: mobile", stats["excluded_mobile"]),
                ("excluded: analysis", stats["excluded_analysis"]),
                ("excluded: unknown unit", stats["excluded_unknown_unit"]),
                ("local time mismatches", stats["local_time_mismatches"]),
                ("curated rows", stats["curated_rows"]),
            ]
        )
    )

    out.append("\n## Coverage\n")
    out.append(
        kv(
            [
                ("stations", len(stations)),
                ("parameters", m["parameter"].nunique()),
                ("station-parameter series", len(gaps)),
            ]
        )
    )
    per_param = m.groupby("parameter").agg(
        rows=("value", "size"), stations=("location_id", "nunique")
    )
    out.append("")
    out.append(md_table(per_param.reset_index()))
    combos = stations["parameters"].map(lambda p: ", ".join(p)).value_counts()
    out.append("")
    out.append(md_table(combos.rename_axis("parameters").reset_index(name="stations")))
    st = stations.assign(
        parameters=stations["parameters"].map(lambda p: ", ".join(p)),
        completeness_pct=(comp_station.reindex(stations["location_id"]).values * 100),
    )[
        [
            "location_id",
            "location",
            "city",
            "lat",
            "lon",
            "sensor_type",
            "parameters",
            "completeness_pct",
        ]
    ]
    out.append("")
    out.append(md_table(st, floatfmt="{:.4g}"))

    out.append("\n## Time\n")
    out.append(
        kv(
            [
                ("hours without data from any station", len(missing_hours)),
                ("series with an internal gap", int((gaps["longest_internal_gap_h"] > 0).sum())),
                ("longest internal gap (h)", gaps["longest_internal_gap_h"].max()),
            ]
        )
    )
    worst = gaps.sort_values(["longest_internal_gap_h", "completeness"], ascending=[False, True])
    worst = worst[(worst["longest_internal_gap_h"] > 0) | (worst["completeness"] < 1)].head(10)
    if len(worst):
        out.append("")
        out.append(md_table(worst.assign(completeness=worst["completeness"] * 100), "{:.1f}"))

    out.append("\n## Replay\n")
    out.append(
        md_table(
            pd.DataFrame(
                [
                    pace_row("sqs_sent_ts", pace_sent),
                    pace_row("ingested_at (live)", pace_live),
                ],
                columns=[
                    "clock",
                    "sim h per real h",
                    "R²",
                    "real time covered",
                    "median lag",
                ],
            )
        )
    )
    out.append("")
    out.append(
        kv(
            [
                ("rows more than a day behind earlier sends", len(jumps)),
                (
                    f"values equal to {window.days} days earlier",
                    "n/a" if repeat is None else f"{repeat:.1%}",
                ),
            ]
        )
    )

    out.append("\n## Values\n")
    out.append(md_table(vstats, floatfmt="{:.1f}"))
    out.append("")
    out.append(md_table(pd.DataFrame(stats["unit_pairs"])))

    out.append("\n## Metadata\n")
    for col in ("entity", "sensor_type"):
        vc = stations[col].value_counts(dropna=False)
        out.append(md_table(vc.rename_axis(col).reset_index(name="stations")))
        out.append("")
    out.append(
        kv(
            [
                ("is_mobile rows", stats["excluded_mobile"]),
                ("is_analysis rows", stats["excluded_analysis"]),
            ]
        )
    )

    out.append("\n## Physics checks\n")
    out.append(
        kv(
            [
                ("station-hours with PM2.5 and PM10", n_pm),
                ("PM2.5 > PM10", n_pm_gt),
                ("PM2.5 > PM10 beyond tolerance", n_pm_viol),
                (f"flatline runs (>= {qc['flatline_min_hours']} h)", len(flats)),
            ]
        )
    )
    out.append("")
    return "\n".join(out)


def main() -> int:
    m = pd.read_parquet(CURATED_DIR / "measurements.parquet")
    stations = pd.read_parquet(CURATED_DIR / "stations.parquet")
    stats = json.loads((CURATED_DIR / "curation_stats.json").read_text(encoding="utf-8"))
    now = json.loads((CURATED_DIR / "now.json").read_text(encoding="utf-8"))["now_utc"]

    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    comp = completeness_by_station_day(m, m["ts_local"].dt.tz)
    fig_completeness(comp, stations, FIGURES_DIR / "completeness.png")
    fig_diurnal(m, FIGURES_DIR / "diurnal.png")
    fig_map(stations, FIGURES_DIR / "stations_map.png")

    REPORT_PATH.write_text(build_report(m, stations, stats, now), encoding="utf-8")
    print(f"wrote {REPORT_PATH.relative_to(REPORTS_DIR.parent)} and 3 figures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
