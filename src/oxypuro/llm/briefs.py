from __future__ import annotations

import re
from typing import Any

import pandas as pd

from oxypuro.analytics.hotspots import SCORES_PATH
from oxypuro.analytics.quality import QC_PATH
from oxypuro.config import load_analysis, load_thresholds
from oxypuro.llm.bedrock import BedrockError, converse

PRETTY = {"pm25": "PM2.5", "pm10": "PM10"}
LANGUAGES = {"en": "English", "nl": "Dutch (as used in Flanders)", "fr": "French"}

SYSTEM = """You write short sales briefs for Oxypuro, a company that sells district-scale
outdoor air filtration units to Belgian cities. One unit cleans the air of a few blocks.

Rules:
- Use only the facts provided. Never add, compute, round or convert a number, and never
  mention a number that is not in the facts. You may write numbers and dates in the usual
  format of the language (thousand separators, decimal comma, month names), but never change
  their value.
- Never say whether the zone is legally compliant or not: the data period is far shorter
  than a year. Say that the results are indicative and name the data period.
- The EU allows at most the given number of days above the daily limit per calendar year.
  Do not attach any other meaning to that number.
- Translate everything, including the sensor type, into the language of the brief.
- 120 to 180 words, never more. Plain prose for a city official, no headings, no bullet
  points, no dashes as punctuation.
- Do not promise how much or how fast a unit lowers pollution: that is measured after
  installation. Do not describe the area beyond the facts (for example "densely populated").
- Structure: why this zone, the evidence, a pitch angle (the stricter EU limits that apply
  from 2030, public health), and one caveat.
- The excess over nearby stations is context: it only shows whether this station is worse
  than the wider surroundings.
- Write in {language}."""


def data_period() -> tuple[pd.Timestamp, pd.Timestamp]:
    ts = pd.read_parquet(QC_PATH, columns=["ts_local"])["ts_local"]
    return ts.min(), ts.max() - pd.Timedelta(hours=1)


def round_people(n: float) -> int:
    step = 1000 if n >= 1000 else 100
    return int(round(n / step) * step)


def station_facts(row: pd.Series, period: tuple[pd.Timestamp, pd.Timestamp]) -> dict[str, Any]:
    th = load_thresholds()["eu_2030"]
    radius_km = load_analysis()["hotspots"]["exposure_radius_m"] / 1000
    days = {}
    for p in PRETTY:
        if pd.notna(row.get(f"days_{p}")):
            days[PRETTY[p]] = {
                "days_with_data": int(row[f"days_{p}"]),
                "days_above_eu_2030_daily_limit": int(row[f"days_above_{p}"]),
                "eu_2030_daily_limit_ugm3": th["daily_limit"][p],
            }
    facts: dict[str, Any] = {
        "station": row["location"],
        "municipality": row["municipality"],
        "region": row["region"],
        "sensor_type": row["sensor_type"],
        "data_period": f"{period[0]:%Y-%m-%d} to {period[1]:%Y-%m-%d}",
        "eu_limits_apply_from_year": 2030,
        "eu_max_days_above_daily_limit_per_year": th["max_daily_exceedances_per_year"],
        "particles": days,
        "residents_within_km": int(radius_km) if radius_km.is_integer() else radius_km,
        "residents_within_radius_about": round_people(row["residents_1km"]),
    }
    excess = {}
    for name, col in (("PM", "excess_pm"), ("NO2", "excess_no2")):
        if pd.notna(row.get(col)):
            excess[f"{name}_ugm3"] = round(float(row[col]), 1)
    if excess:
        facts["excess_over_nearby_stations"] = excess
    return facts


_NOT_NUMBERS = re.compile(r"PM\s?2[.,]5|PM\s?10|NO\s?2|SO\s?2|O\s?3|m³|m3", re.IGNORECASE)
_NUMBER = re.compile(r"[0-9]+(?:[.,][0-9]+|[   ][0-9]{3}(?![0-9]))*")


def _readings(token: str) -> set[float]:
    t = re.sub(r"[   ]", "", token)
    out = set()
    for thousands, decimal in ((",", "."), (".", ","), ("", "")):
        s = t.replace(thousands, "") if thousands else t
        s = s.replace(decimal, ".") if decimal else s
        try:
            out.add(float(s))
        except ValueError:
            pass
    return out


def _fact_numbers(obj: Any, out: set[float]) -> set[float]:
    if isinstance(obj, dict):
        for v in obj.values():
            _fact_numbers(v, out)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, int | float):
        out.update({float(obj), abs(float(obj))})
    elif isinstance(obj, str):
        for tok in _NUMBER.findall(_NOT_NUMBERS.sub(" ", obj)):
            out.update(_readings(tok))
        for part in re.findall(r"[0-9]+", obj):
            out.add(float(part))
    return out


def unknown_numbers(brief: str, facts: dict[str, Any]) -> list[str]:
    allowed = _fact_numbers(facts, set())
    text = _NOT_NUMBERS.sub(" ", brief)
    return [tok for tok in _NUMBER.findall(text) if not (_readings(tok) & allowed)]


def write_brief(facts: dict[str, Any], lang: str) -> tuple[str, list[str]]:
    system = SYSTEM.format(language=LANGUAGES[lang])
    user = "Facts:\n" + "\n".join(f"- {k}: {v}" for k, v in facts.items())
    text = converse(system, user)
    return text, unknown_numbers(text, facts)


def main() -> int:
    scores = pd.read_parquet(SCORES_PATH)
    top = scores[scores["eligible"]].sort_values("rank").iloc[0]
    facts = station_facts(top, data_period())
    print("Facts:", facts, "\n")
    for lang in LANGUAGES:
        try:
            text, unknown = write_brief(facts, lang)
        except BedrockError as e:
            print(e)
            return 1
        print(f"--- {lang} ({len(text.split())} words)")
        print(text)
        print("number check:", "all numbers found in the facts" if not unknown else unknown)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
