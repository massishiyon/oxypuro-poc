from __future__ import annotations

import json
from typing import Any

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from oxypuro.analytics.curate import CURATED_DIR  # noqa: E402
from oxypuro.analytics.quality import (  # noqa: E402
    QC_PATH,
    QCConfig,
    find_neighbours,
    load_station_flags,
)
from oxypuro.analytics.report_utils import md_table  # noqa: E402
from oxypuro.config import DATA_DIR, REPORTS_DIR, load_analysis, load_thresholds  # noqa: E402

MARTS_DIR = DATA_DIR / "marts"
SECTORS_PATH = DATA_DIR / "external" / "sectors.parquet"
STATS_PATH = DATA_DIR / "external" / "statbel_stats.json"
SCORES_PATH = MARTS_DIR / "station_scores.parquet"
REPORT_PATH = REPORTS_DIR / "hotspots.md"
FIGURES_DIR = REPORTS_DIR / "figures"
PM = ("pm25", "pm10")
PRETTY = {"pm25": "PM2.5", "pm10": "PM10", "no2": "NO2"}
FIGSIZE = (12.8, 7.2)


def locate_stations(stations: pd.DataFrame, sectors: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    pts = gpd.GeoDataFrame(
        stations[["location_id"]],
        geometry=gpd.points_from_xy(stations["lon"], stations["lat"]),
        crs="EPSG:4326",
    ).to_crs(sectors.crs)
    cols = [
        "sector", "municipality_nis", "municipality_nl", "municipality_fr", "region", "geometry",
    ]  # fmt: skip
    j = gpd.sjoin(pts, sectors[cols], how="left", predicate="within")
    j = j.drop(columns="index_right").drop_duplicates("location_id")
    j["municipality"] = j["municipality_nl"].where(j["region"] != "Wallonia", j["municipality_fr"])
    return j.drop(columns=["municipality_nl", "municipality_fr"])


def residents_within(points: gpd.GeoDataFrame, sectors: gpd.GeoDataFrame, radius_m: float):
    circles = gpd.GeoDataFrame(
        points[["location_id"]], geometry=points.geometry.buffer(radius_m), crs=points.crs
    )
    sec = sectors[["population", "geometry"]].copy()
    sec["sector_area"] = sec.geometry.area
    parts = gpd.overlay(circles, sec, how="intersection", keep_geom_type=True)
    parts["residents"] = parts["population"] * parts.geometry.area / parts["sector_area"]
    out = parts.groupby("location_id")["residents"].sum()
    return out.reindex(points["location_id"]).fillna(0.0)


def exceedance_days(valid: pd.DataFrame, thresholds: dict[str, Any]) -> pd.DataFrame:
    limits = thresholds["eu_2030"]["daily_limit"]
    min_h = thresholds["data_capture"]["min_hours_for_daily_mean"]
    pm = valid[valid["parameter"].isin(PM)]
    daily = (
        pm.groupby(["location_id", "parameter", "date_local"])["value"]
        .agg(["mean", "size"])
        .reset_index()
    )
    daily = daily[daily["size"] >= min_h]
    daily["above"] = daily["mean"] > daily["parameter"].map(limits)
    return (
        daily.groupby(["location_id", "parameter"])["above"]
        .agg(days="size", days_above="sum")
        .reset_index()
    )


def mean_excess(valid: pd.DataFrame, neighbours: dict[tuple[int, str], list[int]]) -> pd.DataFrame:
    rows = []
    for param in ("pm25", "pm10", "no2"):
        wide = valid[valid["parameter"] == param].pivot_table(
            index="ts_utc", columns="location_id", values="value"
        )
        for loc in wide.columns:
            nbrs = [n for n in neighbours.get((loc, param), []) if n in wide.columns]
            if not nbrs:
                continue
            excess = (wide[loc] - wide[nbrs].median(axis=1)).dropna()
            if len(excess):
                rows.append((loc, param, float(excess.mean()), len(excess)))
    return pd.DataFrame(rows, columns=["location_id", "parameter", "excess", "hours"])


def pct_rank(s: pd.Series) -> pd.Series:
    return s.rank(pct=True)


def round_people(n: float) -> str:
    step = 1000 if n >= 1000 else 100
    return f"{int(round(n / step) * step):,}"


def reason_text(r: pd.Series) -> str:
    p = r["worst_pm"]
    parts = [
        f"{PRETTY[p]} above the 2030 EU daily limit on "
        f"{int(r[f'days_above_{p}'])} of {int(r[f'days_{p}'])} days"
    ]
    parts.append(f"about {round_people(r['residents_1km'])} people within 1 km")
    pairs = (("PM", "excess_pm"), ("NO2", "excess_no2"))
    excess = [f"{name} {r[col]:+.1f}" for name, col in pairs if pd.notna(r[col])]
    if excess:
        parts.append("compared with nearby stations: " + ", ".join(excess) + " µg/m³")
    return "; ".join(parts)


def score_stations(
    stations: pd.DataFrame,
    located: gpd.GeoDataFrame,
    residents: pd.Series,
    days: pd.DataFrame,
    excess: pd.DataFrame,
    n_days: int,
    flags: dict[int, Any],
    cfg: dict[str, Any],
) -> pd.DataFrame:
    df = stations[["location_id", "location", "sensor_type", "lat", "lon"]].merge(
        located.drop(columns="geometry"), on="location_id", how="left"
    )
    df["residents_1km"] = df["location_id"].map(residents)

    d = days.pivot(index="location_id", columns="parameter", values=["days", "days_above"])
    for p in PM:
        df[f"days_{p}"] = df["location_id"].map(d[("days", p)]) if ("days", p) in d else np.nan
        df[f"days_above_{p}"] = (
            df["location_id"].map(d[("days_above", p)]) if ("days_above", p) in d else np.nan
        )
        df[f"share_above_{p}"] = df[f"days_above_{p}"] / df[f"days_{p}"]
    share_cols = [f"share_above_{p}" for p in PM]
    df["share_above"] = df[share_cols].mean(axis=1)
    df["worst_pm"] = np.array(PM)[df[share_cols].fillna(-1).to_numpy().argmax(axis=1)]
    df["valid_pm_days"] = df[[f"days_{p}" for p in PM]].max(axis=1)

    e = excess.pivot(index="location_id", columns="parameter", values="excess")
    for p in ("pm25", "pm10", "no2"):
        df[f"excess_{p}"] = df["location_id"].map(e[p]) if p in e else np.nan
    df["excess_pm"] = df[["excess_pm25", "excess_pm10"]].mean(axis=1)

    reason = pd.Series("", index=df.index)
    checks = [
        (df["location_id"].isin(flags), "coordinates not trusted (config/station_flags.yaml, D19)"),
        (df["municipality_nis"].isna(), "location outside every Belgian municipality"),
        (df["valid_pm_days"].isna(), "measures no PM2.5 or PM10"),
        (
            df["valid_pm_days"] < cfg["min_valid_day_share"] * n_days,
            f"fewer than {cfg['min_valid_day_share']:.0%} of the {n_days} days with valid PM data",
        ),
    ]
    for mask, why in checks:
        reason = reason.mask(mask & (reason == ""), why)
    df["exclusion_reason"] = reason
    df["eligible"] = reason == ""

    el = df["eligible"]
    w = cfg["weights"]
    df.loc[el, "severity"] = pct_rank(df.loc[el, "share_above"])
    df.loc[el, "exposure"] = pct_rank(df.loc[el, "residents_1km"])
    df["score"] = 100 * (w["severity"] * df["severity"] + w["exposure"] * df["exposure"])
    df.loc[el, "rank"] = df.loc[el, "score"].rank(ascending=False, method="min")
    df["rank"] = df["rank"].astype("Int64")
    df["reason"] = ""
    df.loc[el, "reason"] = df[el].apply(reason_text, axis=1)
    return df.sort_values(["eligible", "score"], ascending=[False, False]).reset_index(drop=True)


def municipality_outlines(sectors: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    m = sectors[["municipality_nis", "geometry"]].dissolve(by="municipality_nis")
    m["geometry"] = m.geometry.simplify(100)
    return m


def plot_map(df: pd.DataFrame, outlines: gpd.GeoDataFrame, top_n: int) -> None:
    pts = gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326"
    ).to_crs(outlines.crs)
    fig, ax = plt.subplots(figsize=FIGSIZE)
    outlines.boundary.plot(ax=ax, color="0.8", lw=0.4, zorder=1)
    el = pts[pts["eligible"]]
    el.plot(
        ax=ax,
        column="score",
        cmap="Reds",
        markersize=90,
        edgecolor="black",
        legend=True,
        legend_kwds={"label": "score", "shrink": 0.7},
        zorder=3,
    )
    ex = pts[~pts["eligible"] & pts["municipality_nis"].notna()]
    if len(ex):
        ex.plot(ax=ax, marker="x", color="0.4", markersize=60, zorder=3)
    for r in el.nsmallest(top_n, "rank").itertuples():
        ax.annotate(
            f"{r.rank} {r.location}", (r.geometry.x, r.geometry.y), xytext=(6, 4),
            textcoords="offset points", fontsize=10,
        )  # fmt: skip
    ax.set_axis_off()
    note = ", grey x = not ranked" if len(ex) else ""
    ax.set_title(f"Hotspot score per station (top {top_n} labelled{note})")
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "hotspots_map.png", dpi=120)
    plt.close(fig)


def plot_top(df: pd.DataFrame, cfg: dict[str, Any]) -> None:
    top = df[df["eligible"]].nsmallest(cfg["top_n"], "rank").iloc[::-1]
    w = cfg["weights"]
    fig, ax = plt.subplots(figsize=FIGSIZE)
    left = np.zeros(len(top))
    labels = top["rank"].astype(str) + " " + top["location"]
    for comp, colour in (("severity", "C3"), ("exposure", "C0")):
        part = 100 * w[comp] * top[comp].to_numpy()
        ax.barh(labels, part, left=left, color=colour, label=f"{comp} (weight {w[comp]:.2f})")
        left += part
    ax.set_xlabel("score (0 to 100), split by component")
    ax.set_title(f"Top {cfg['top_n']}: what drives each score")
    ax.legend(loc="lower right")
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "hotspots_top10.png", dpi=120)
    plt.close(fig)


def build_report(df, sectors, period, n_days, cfg, unknown_pop) -> str:
    w, top_n = cfg["weights"], cfg["top_n"]
    el = df[df["eligible"]]

    def kv(rows: list[tuple]) -> str:
        return md_table(pd.DataFrame(rows, columns=["metric", "value"]))

    out = ["# Hotspot ranking\n"]
    out.append(
        kv(
            [
                ("first day", f"{period[0]:%Y-%m-%d}"),
                ("last day", f"{period[1]:%Y-%m-%d}"),
                ("days with a complete daily mean", n_days),
                ("stations ranked", len(el)),
                ("stations total", len(df)),
                ("weight severity", f"{w['severity']:.2f}"),
                ("weight exposure", f"{w['exposure']:.2f}"),
                ("exposure radius (m)", cfg["exposure_radius_m"]),
                ("residents without a known sector (left out)", f"{unknown_pop:,}"),
            ]
        )
    )

    out.append(f"\n## Top {top_n}\n")
    top = el.nsmallest(top_n, "rank")
    out.append(
        md_table(
            pd.DataFrame(
                {
                    "rank": top["rank"],
                    "station": top["location"],
                    "municipality": top["municipality"],
                    "sensor": top["sensor_type"],
                    "score": top["score"].round(1),
                    "reason": top["reason"],
                }
            )
        )
    )

    out.append("\n## All ranked stations\n")
    out.append(
        md_table(
            pd.DataFrame(
                {
                    "rank": el["rank"],
                    "station": el["location"],
                    "region": el["region"],
                    "sensor": el["sensor_type"],
                    "days above limit %": (el["share_above"] * 100).round().astype(int),
                    "PM excess": el["excess_pm"].round(1),
                    "NO2 excess": el["excess_no2"].round(1),
                    "residents 1 km": el["residents_1km"].round(-2).astype(int),
                    "severity rank": el["severity"].map("{:.2f}".format),
                    "exposure rank": el["exposure"].map("{:.2f}".format),
                    "score": el["score"].round(1),
                }
            )
        ).replace("| nan |", "| - |")
    )

    ex = df[~df["eligible"]]
    if len(ex):
        out.append("\n## Not ranked\n")
        out.append(
            md_table(
                pd.DataFrame(
                    {
                        "location_id": ex["location_id"],
                        "station": ex["location"],
                        "reason": ex["exclusion_reason"],
                    }
                )
            )
        )

    out.append("\n## Coverage\n")
    with_station = set(df["municipality_nis"].dropna())
    muni = sectors.groupby("municipality_nis")["population"].sum()
    no_st = muni[~muni.index.isin(with_station)]
    out.append(
        kv(
            [
                ("municipalities", len(muni)),
                ("municipalities without a station", len(no_st)),
                ("residents", f"{muni.sum():,}"),
                ("residents in municipalities without a station", f"{no_st.sum():,}"),
                ("share of residents without a station", f"{no_st.sum() / muni.sum():.0%}"),
            ]
        )
    )
    out.append("")
    return "\n".join(out)


def main() -> int:
    cfg = load_analysis()["hotspots"]
    total = sum(cfg["weights"].values())
    cfg["weights"] = {k: v / total for k, v in cfg["weights"].items()}
    thresholds = load_thresholds()
    qc_cfg = QCConfig.load()
    flags = load_station_flags()

    qc = pd.read_parquet(QC_PATH)
    stations = pd.read_parquet(CURATED_DIR / "stations.parquet")
    sectors = gpd.read_parquet(SECTORS_PATH)

    valid = qc[qc["qc_status"] == "valid"].copy()
    valid["value"] = valid["value"].clip(lower=0)
    min_h = thresholds["data_capture"]["min_hours_for_daily_mean"]
    per_day = valid.groupby(["location_id", "parameter", "date_local"]).size()
    n_days = int(per_day[per_day >= min_h].index.get_level_values("date_local").nunique())
    period = (valid["ts_local"].min(), valid["ts_local"].max() - pd.Timedelta(hours=1))

    trusted = stations[~stations["location_id"].isin(flags)]
    located = locate_stations(trusted, sectors)
    inside = located[located["municipality_nis"].notna()]
    residents = residents_within(inside, sectors, cfg["exposure_radius_m"])
    days = exceedance_days(valid, thresholds)
    neighbours = find_neighbours(qc, stations, qc_cfg, set(flags))
    excess = mean_excess(valid, neighbours)

    df = score_stations(stations, located, residents, days, excess, n_days, flags, cfg)
    MARTS_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(SCORES_PATH, index=False)

    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    plot_map(df, municipality_outlines(sectors), cfg["top_n"])
    plot_top(df, cfg)
    unknown_pop = json.loads(STATS_PATH.read_text())["unknown_sector_population"]
    REPORT_PATH.write_text(
        build_report(df, sectors, period, n_days, cfg, unknown_pop), encoding="utf-8"
    )

    el = df[df["eligible"]]
    print(f"ranked {len(el)} of {len(df)} stations over {n_days} days")
    print(el.nsmallest(cfg["top_n"], "rank")[["rank", "location", "score"]].to_string(index=False))
    print(f"wrote {SCORES_PATH.relative_to(DATA_DIR.parent)} and {REPORT_PATH.name} with figures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
