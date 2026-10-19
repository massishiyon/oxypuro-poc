from __future__ import annotations

import json
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.linear_model import LinearRegression  # noqa: E402
from sklearn.metrics import r2_score  # noqa: E402

from oxypuro.analytics.curate import CURATED_DIR, LOCAL_TZ  # noqa: E402
from oxypuro.analytics.hotspots import SCORES_PATH  # noqa: E402
from oxypuro.analytics.quality import QC_PATH, haversine_km, load_station_flags  # noqa: E402
from oxypuro.analytics.report_utils import md_table  # noqa: E402
from oxypuro.config import DATA_DIR, REPORTS_DIR, load_analysis, load_thresholds  # noqa: E402

MARTS_DIR = DATA_DIR / "marts"
IMPACT_PATH = MARTS_DIR / "impact_stations.parquet"
REPORT_PATH = REPORTS_DIR / "impact.md"
FIGURES_DIR = REPORTS_DIR / "figures"
FIGSIZE = (12.8, 7.2)


def hourly_wide(valid: pd.DataFrame, param: str) -> pd.DataFrame:
    return valid[valid["parameter"] == param].pivot_table(
        index="ts_utc", columns="location_id", values="value"
    )


def split_test(wide: pd.DataFrame, test_days: int) -> pd.Timestamp:
    local_day = wide.index.tz_convert(LOCAL_TZ).normalize()
    hours = pd.Series(1, index=local_day).groupby(level=0).size()
    full = hours[hours >= 23].index
    return full[-test_days].tz_convert("UTC")


def weekly_swings(valid: pd.DataFrame, loc: int, param: str, min_hours: int):
    pm = valid[valid["parameter"] == param]
    daily = pm.groupby(["location_id", "date_local"])["value"].agg(["mean", "size"])
    daily = daily[daily["size"] >= min_hours]["mean"].unstack("location_id")
    station = daily[loc].dropna()
    network = daily.median(axis=1).reindex(station.index)
    n_weeks = len(station) // 7
    weeks = pd.DataFrame(
        [
            {
                "week": f"{station.index[i * 7]} to {station.index[i * 7 + 6]}",
                "mean": station.iloc[i * 7 : i * 7 + 7].mean(),
            }
            for i in range(n_weeks)
        ]
    )
    weeks["change %"] = weeks["mean"].pct_change() * 100
    corr = float(np.corrcoef(station, network)[0, 1])
    return station, network, weeks, corr


def fit_station(
    wide: pd.DataFrame,
    target: int,
    candidates: list[int],
    test_start: pd.Timestamp,
    n_controls: int,
) -> dict[str, Any] | None:
    train, test = wide[wide.index < test_start], wide[wide.index >= test_start]
    corr = train[candidates].corrwith(train[target]).dropna().sort_values(ascending=False)
    controls = corr.index[:n_controls].tolist()
    if len(controls) < n_controls:
        return None
    tr = train[[target, *controls]].dropna()
    te = test[[target, *controls]].dropna()
    if tr.empty or te.empty:
        return None
    model = LinearRegression().fit(tr[controls], tr[target])
    pred = pd.Series(model.predict(te[controls]), index=te.index)
    actual = te[target]
    return {
        "location_id": target,
        "controls": controls,
        "train_hours": len(tr),
        "test_hours": len(te),
        "r2_test": float(r2_score(actual, pred)),
        "gap_pct": float((actual.mean() - pred.mean()) / pred.mean() * 100),
        "actual": actual,
        "predicted": pred,
    }


def run_models(wide, stations, flags, test_start, cfg) -> list[dict[str, Any]]:
    present = wide.notna().mean()
    eligible = [
        int(s)
        for s in present[present >= cfg["min_hourly_completeness"]].index
        if int(s) not in flags
    ]
    st = stations.set_index("location_id")
    results = []
    for target in eligible:
        others = [s for s in eligible if s != target]
        dist = haversine_km(
            st.loc[target, "lat"],
            st.loc[target, "lon"],
            st.loc[others, "lat"].to_numpy(),
            st.loc[others, "lon"].to_numpy(),
        )
        far = [o for o, d in zip(others, dist, strict=True) if d >= cfg["min_control_distance_km"]]
        r = fit_station(wide, target, far, test_start, cfg["n_controls"])
        if r is not None:
            results.append(r)
    return results


def pick_example(results: list[dict[str, Any]], scores: pd.DataFrame) -> tuple[dict, str]:
    by_id = {r["location_id"]: r for r in results}
    median_r2 = float(np.median([r["r2_test"] for r in results]))
    ranked = scores[scores["eligible"]].sort_values("rank")
    top = [int(i) for i in ranked["location_id"] if int(i) in by_id]
    first = by_id[top[0]]
    if first["r2_test"] >= median_r2:
        return first, "the top-ranked station in the hotspot ranking"
    best = max((by_id[i] for i in top[:10]), key=lambda r: r["r2_test"])
    return best, "the best-fitting station of the hotspot top 10 (rank 1 fits below the median)"


def plot_before_after(station, network, name: str) -> None:
    x = pd.to_datetime(pd.Series(station.index.astype(str)))
    fig, ax = plt.subplots(figsize=FIGSIZE)
    for i in range(0, len(x) // 7 * 7, 14):
        ax.axvspan(x.iloc[i], x.iloc[min(i + 6, len(x) - 1)], color="0.93", zorder=0)
    ax.plot(x, station.to_numpy(), color="C3", lw=2.5, marker="o", label=f"{name} (no unit)")
    ax.plot(x, network.to_numpy(), color="0.4", lw=1.5, ls="--", label="median of all stations")
    ax.set_ylabel("daily mean PM2.5, µg/m³")
    ax.set_title(f"{name}: the weather moves the weekly average, with no unit installed")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "impact_before_after.png", dpi=120)
    plt.close(fig)


def plot_example(r: dict[str, Any], name: str, detectable: float) -> None:
    a = r["actual"].tz_convert(LOCAL_TZ)
    p = r["predicted"].tz_convert(LOCAL_TZ)
    fig, ax = plt.subplots(figsize=FIGSIZE)
    ax.plot(a.index, a.to_numpy(), color="C3", lw=1.8, label="actual (no unit)")
    ax.plot(p.index, p.to_numpy(), color="0.3", lw=1.8, ls="--", label="predicted from 5 stations")
    ax.plot(
        a.index,
        a.to_numpy() * (1 - detectable / 100),
        color="C2",
        lw=1.5,
        label=f"simulated unit at the detectable effect (-{detectable:.0f} %)",
    )
    ax.set_ylabel("hourly PM2.5, µg/m³")
    ax.set_title(f"{name}, test week: actual, predicted, and a simulated unit")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "impact_example.png", dpi=120)
    plt.close(fig)


def build_report(ctx: dict[str, Any]) -> str:
    cfg, res, names = ctx["cfg"], ctx["table"], ctx["names"]
    ex = ctx["example"]

    def kv(rows: list[tuple]) -> str:
        return md_table(pd.DataFrame(rows, columns=["metric", "value"]))

    out = ["# Impact baseline\n"]
    out.append(
        kv(
            [
                ("first day", f"{ctx['period'][0]:%Y-%m-%d}"),
                ("last day", f"{ctx['period'][1]:%Y-%m-%d}"),
                ("parameter", cfg["parameter"]),
                ("training days", ctx["train_days"]),
                ("test start", f"{ctx['test_start']:%Y-%m-%d}"),
                ("test days", cfg["test_days"]),
                ("comparison stations per target", cfg["n_controls"]),
                ("minimum comparison distance (km)", cfg["min_control_distance_km"]),
                ("median comparison distance (km)", f"{ctx['control_km']:.0f}"),
                ("stations", len(res)),
            ]
        )
    )

    out.append(f"\n## Weekly averages, {ctx['opener_name']}\n")
    w = ctx["weeks"].copy()
    w["mean"] = w["mean"].round(1)
    w["change %"] = w["change %"].round(0)
    out.append(md_table(w).replace("| nan |", "| |"))
    out.append("")
    out.append(kv([("correlation with median of all stations", f"{ctx['corr']:.2f}")]))

    out.append("\n## Prediction error\n")
    g = res["gap %"].abs()
    out.append(
        kv(
            [
                (
                    "median R² (test, hourly)",
                    f"{np.median([r['r2_test'] for r in ctx['results']]):.2f}",
                ),
                ("median absolute gap %", f"{g.median():.1f}"),
                (
                    f"detectable effect % (p{cfg['detectable_quantile'] * 100:.0f})",
                    f"{ctx['detectable']:.1f}",
                ),
                ("largest absolute gap %", f"{g.max():.1f}"),
                ("example station", names[ex["location_id"]]),
                ("example R²", f"{ex['r2_test']:.2f}"),
                ("example gap %", f"{ex['gap_pct']:+.1f}"),
            ]
        )
    )

    out.append("\n## Stations\n")
    out.append(md_table(res.sort_values("gap %", key=abs)))
    out.append("")
    return "\n".join(out)


def main() -> int:
    cfg = load_analysis()["impact"]
    min_hours = load_thresholds()["data_capture"]["min_hours_for_daily_mean"]
    flags = load_station_flags()
    qc = pd.read_parquet(QC_PATH)
    stations = pd.read_parquet(CURATED_DIR / "stations.parquet")
    scores = pd.read_parquet(SCORES_PATH)
    names = stations.set_index("location_id")["location"].to_dict()
    now = pd.Timestamp(json.loads((CURATED_DIR / "now.json").read_text())["now_utc"])

    valid = qc[(qc["qc_status"] == "valid") & (qc["ts_utc"] <= now)].copy()
    valid["value"] = valid["value"].clip(lower=0)
    wide = hourly_wide(valid, cfg["parameter"])
    test_start = split_test(wide, cfg["test_days"])

    results = run_models(wide, stations, flags, test_start, cfg)
    detectable = float(
        np.quantile([abs(r["gap_pct"]) for r in results], cfg["detectable_quantile"])
    )
    example, why = pick_example(results, scores)

    top_id = int(scores[scores["eligible"]].sort_values("rank")["location_id"].iloc[0])
    station, network, weeks, corr = weekly_swings(valid, top_id, cfg["parameter"], min_hours)

    table = pd.DataFrame(
        {
            "station": [names[r["location_id"]] for r in results],
            "comparison stations": [", ".join(names[c] for c in r["controls"]) for r in results],
            "R² test": [f"{r['r2_test']:.2f}" for r in results],
            "gap %": [round(r["gap_pct"], 1) for r in results],
        }
    )
    MARTS_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [{k: v for k, v in r.items() if k not in ("actual", "predicted")} for r in results]
    ).to_parquet(IMPACT_PATH, index=False)

    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    plot_before_after(station, network, names[top_id])
    plot_example(example, names[example["location_id"]], detectable)
    train_days = (test_start - wide.index.min()).days
    st = stations.set_index("location_id")
    control_km = float(
        np.median(
            [
                d
                for r in results
                for d in haversine_km(
                    st.loc[r["location_id"], "lat"],
                    st.loc[r["location_id"], "lon"],
                    st.loc[r["controls"], "lat"].to_numpy(),
                    st.loc[r["controls"], "lon"].to_numpy(),
                )
            ]
        )
    )
    ctx = {
        "cfg": cfg,
        "table": table,
        "names": names,
        "example": example,
        "example_name": why,
        "detectable": detectable,
        "weeks": weeks,
        "corr": corr,
        "opener_name": names[top_id],
        "period": (valid["ts_local"].min(), valid["ts_local"].max() - pd.Timedelta(hours=1)),
        "test_start": test_start.tz_convert(LOCAL_TZ),
        "train_days": train_days,
        "results": results,
        "control_km": control_km,
    }
    REPORT_PATH.write_text(build_report(ctx), encoding="utf-8")

    print(f"stations {len(results)}, test from {test_start.tz_convert(LOCAL_TZ):%Y-%m-%d}")
    print(
        f"median R² {np.median([r['r2_test'] for r in results]):.2f}, detectable {detectable:.1f} %"
    )
    print(f"example: {names[example['location_id']]} ({why})")
    print(weeks.round(1).to_string(index=False), f"\ncorrelation {corr:.2f}")
    print(f"wrote {IMPACT_PATH.relative_to(DATA_DIR.parent)} and {REPORT_PATH.name} with figures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
