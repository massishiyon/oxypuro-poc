from __future__ import annotations

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from oxypuro.analytics.curate import CURATED_DIR  # noqa: E402
from oxypuro.analytics.quality import (  # noqa: E402
    ALL_FLAGS,
    QC_PATH,
    load_station_flags,
)
from oxypuro.analytics.report_utils import md_table  # noqa: E402
from oxypuro.config import DATA_DIR, REPORTS_DIR  # noqa: E402

REPORT_PATH = REPORTS_DIR / "quality.md"
FIGURES_DIR = REPORTS_DIR / "figures"
TEST_DIR = DATA_DIR / "test"
INJECTED_QC_PATH = TEST_DIR / "injected_qc.parquet"
INJECTION_RESULTS_PATH = TEST_DIR / "fault_injection.json"
FIGSIZE = (12.8, 7.2)
PRETTY = {"pm25": "PM2.5", "pm10": "PM10", "no2": "NO2", "o3": "O3", "so2": "SO2"}


def _has(qc: pd.DataFrame, flag: str) -> pd.Series:
    return qc["qc_reasons"].map(lambda r: flag in r)


def _series(qc, loc, param, t0, t1):
    s = qc[(qc.location_id == loc) & (qc.parameter == param)]
    return s[(s.ts_utc >= t0) & (s.ts_utc <= t1)].sort_values("ts_utc")


def _plot_point_example(qc, stations, row, flag, title, path, neighbours_of=None):
    t0, t1 = row.ts_utc - pd.Timedelta(hours=48), row.ts_utc + pd.Timedelta(hours=24)
    names = stations.set_index("location_id")["location"]
    fig, ax = plt.subplots(figsize=FIGSIZE)
    for n in neighbours_of or []:
        s = _series(qc, n, row.parameter, t0, t1)
        ax.plot(s.ts_utc, s.value, color="0.75", lw=1)
    if neighbours_of:
        ax.plot([], [], color="0.75", lw=1, label="neighbouring stations")
    s = _series(qc, row.location_id, row.parameter, t0, t1)
    ax.plot(
        s.ts_utc,
        s.value,
        color="C0",
        lw=2,
        label=f"{row.location_id} {names.get(row.location_id, '')}",
    )
    hit = s[_has(s, flag)]
    ax.scatter(hit.ts_utc, hit.value, color="C3", zorder=5, s=50, label=f"flagged: {flag}")
    ax.set_ylabel(f"{PRETTY.get(row.parameter, row.parameter)} µg/m³")
    ax.set_title(title)
    ax.legend()
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _neighbours(stations, loc, param, k=5):
    from oxypuro.analytics.quality import haversine_km

    st = stations[stations["parameters"].map(lambda ps: param in list(ps))]
    st = pd.concat([st, stations[stations.location_id == loc]]).drop_duplicates("location_id")
    st = st.set_index("location_id")
    d = pd.Series(
        haversine_km(st.loc[loc, "lat"], st.loc[loc, "lon"], st["lat"].values, st["lon"].values),
        index=st.index,
    ).drop(loc)
    excluded = set(load_station_flags())
    return [i for i in d.sort_values().index if i not in excluded][:k]


def make_examples(qc, injected, stations) -> dict[str, str]:
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    notes: dict[str, str] = {}

    for flag, fault, fname, what in (
        ("isolated_spike", "spike", "qc_example_spike.png", "Isolated spike"),
        ("flatline", "flatline", "qc_example_flatline.png", "Flatline"),
    ):
        real = qc[_has(qc, flag)]
        if len(real):
            src, row, origin = qc, real.iloc[0], "real data"
        elif injected is not None and len(
            injected[(injected.fault == fault) & _has(injected, flag)]
        ):
            src = injected
            row = injected[(injected.fault == fault) & _has(injected, flag)].iloc[0]
            origin = "injected fault, not in the real data"
        else:
            notes[fname] = f"No {what.lower()} example available."
            continue
        nbrs = (
            _neighbours(stations, row.location_id, row.parameter)
            if flag == "isolated_spike"
            else None
        )
        _plot_point_example(
            src,
            stations,
            row,
            flag,
            f"{what} at {row.location_id}, {PRETTY.get(row.parameter, row.parameter)} ({origin})",
            FIGURES_DIR / fname,
            nbrs,
        )
        notes[fname] = origin

    fname = "qc_example_regional.png"
    real = qc[_has(qc, "regional_event")]
    if real.empty:
        notes[fname] = "No regional event in the data."
        return notes
    row = real.iloc[0]
    param, centre = row.parameter, row.ts_utc
    locs = [row.location_id] + _neighbours(stations, row.location_id, param)
    origin = "real data, regional spike"
    fig, ax = plt.subplots(figsize=FIGSIZE)
    names = stations.set_index("location_id")["location"]
    t0, t1 = centre - pd.Timedelta(days=3), centre + pd.Timedelta(days=2)
    for loc in locs:
        s = _series(qc, loc, param, t0, t1)
        line = ax.plot(s.ts_utc, s.value, lw=1.5, label=f"{loc} {names.get(loc, '')}")
        hit = s[_has(s, "regional_event")]
        ax.scatter(hit.ts_utc, hit.value, color=line[0].get_color(), edgecolor="black", zorder=5)
    ax.set_ylabel(f"{PRETTY.get(param, param)} µg/m³")
    ax.set_title(f"Regional event: stations rising together ({origin}); dots = regional_event")
    ax.legend(fontsize=9, ncol=2)
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / fname, dpi=120)
    plt.close(fig)
    notes[fname] = origin
    return notes


def build_report(qc, stations, injection) -> str:
    names = stations.set_index("location_id")["location"]

    def kv(rows: list[tuple]) -> str:
        return md_table(pd.DataFrame(rows, columns=["metric", "value"]))

    out: list[str] = ["# Data quality\n"]
    out.append(
        kv(
            [
                ("values", len(qc)),
                ("first ts_utc", f"{qc.ts_utc.min():%Y-%m-%d %H:%M}"),
                ("last ts_utc", f"{qc.ts_utc.max():%Y-%m-%d %H:%M}"),
            ]
        )
    )

    out.append("\n## Status\n")
    st = qc["qc_status"].value_counts().reindex(["valid", "suspect", "invalid"], fill_value=0)
    out.append(
        md_table(
            pd.DataFrame(
                {"status": st.index, "values": st.values, "share %": st.values / len(qc) * 100}
            ),
            floatfmt="{:.2f}",
        )
    )

    out.append("\n## Rules\n")
    rows = []
    for flag in ALL_FLAGS:
        has = _has(qc, flag)
        per = qc[has].groupby("parameter").size()
        rows.append(
            {
                "rule": flag,
                **{PRETTY[p]: int(per.get(p, 0)) for p in PRETTY},
                "total": int(has.sum()),
            }
        )
    out.append(md_table(pd.DataFrame(rows)))

    out.append("\n## Stations\n")
    flags = load_station_flags()
    if flags:
        out.append(
            md_table(
                pd.DataFrame(
                    [
                        {"location_id": k, "name": v.get("name", ""), "flag": "location_suspect"}
                        for k, v in flags.items()
                    ]
                )
            )
        )
        out.append("")
    nn = qc[_has(qc, "no_neighbours")][["location_id", "parameter"]].drop_duplicates()
    if len(nn):
        out.append(md_table(nn.assign(flag="no_neighbours")))
        out.append("")
    sus = (
        qc.assign(s=qc.qc_status != "valid")
        .groupby("location_id")["s"]
        .mean()
        .sort_values(ascending=False)
    )
    sus = sus[sus > 0].head(10)
    if len(sus):
        out.append(
            md_table(
                pd.DataFrame(
                    {
                        "location_id": sus.index,
                        "name": names.reindex(sus.index).values,
                        "non-valid %": sus.values * 100,
                    }
                ),
                "{:.2f}",
            )
        )

    out.append("\n## Non-valid values\n")
    bad = qc[qc.qc_status != "valid"].sort_values("ts_utc")
    if len(bad):
        out.append(
            md_table(
                pd.DataFrame(
                    {
                        "location_id": bad.location_id,
                        "name": names.reindex(bad.location_id).values,
                        "parameter": bad.parameter,
                        "ts_utc": bad.ts_utc.dt.strftime("%Y-%m-%d %H:%M"),
                        "value": bad.value,
                        "robust_z": bad.robust_z,
                        "status": bad.qc_status,
                        "reasons": bad.qc_reasons.map(", ".join),
                    }
                ),
                "{:.1f}",
            )
        )
    out.append("")
    out.append(
        kv(
            [
                ("isolated_spike", int(_has(qc, "isolated_spike").sum())),
                ("regional_event", int(_has(qc, "regional_event").sum())),
            ]
        )
    )

    out.append("\n## Fault injection\n")
    if injection is not None:
        out.append(kv([("faults", injection["n_total"]), ("seed", injection["seed"])]))
        out.append("")
        out.append(md_table(pd.DataFrame(injection["table"]), floatfmt="{:.1f}"))
        mc = injection.get("missed_counts")
        if mc:
            out.append("")
            out.append(
                kv(
                    [
                        ("missed spikes", mc["missed"]),
                        ("missed: robust z not above threshold", mc["z"]),
                        ("missed: under absolute floor", mc["floor"]),
                        ("missed: taken for regional event", mc["regional"]),
                    ]
                )
            )
        if injection.get("missed_spikes"):
            out.append("")
            out.append(md_table(pd.DataFrame(injection["missed_spikes"]), "{:.1f}"))
        out.append("")
        out.append(
            kv(
                [
                    ("non-valid in real data too", injection["already_flagged_in_real_data"]),
                    ("PM partner of injected PM fault", injection["collateral_pm_partner"]),
                    ("other side effects of injected faults", injection["other_side_effects"]),
                ]
            )
        )
    out.append("")
    return "\n".join(out)


def write_report() -> None:
    qc = pd.read_parquet(QC_PATH)
    stations = pd.read_parquet(CURATED_DIR / "stations.parquet")
    injected = pd.read_parquet(INJECTED_QC_PATH) if INJECTED_QC_PATH.exists() else None
    injection = (
        json.loads(INJECTION_RESULTS_PATH.read_text()) if INJECTION_RESULTS_PATH.exists() else None
    )
    make_examples(qc, injected, stations)
    REPORT_PATH.write_text(build_report(qc, stations, injection), encoding="utf-8")
    print(f"wrote {REPORT_PATH.relative_to(REPORTS_DIR.parent)} and figures")
