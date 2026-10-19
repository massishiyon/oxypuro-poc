from __future__ import annotations

import json

import pandas as pd
import pydeck as pdk
import streamlit as st

from oxypuro.analytics.curate import CURATED_DIR
from oxypuro.analytics.hotspots import SCORES_PATH
from oxypuro.llm.bedrock import BedrockError
from oxypuro.llm.briefs import LANGUAGES, data_period, station_facts, write_brief

st.set_page_config(page_title="Oxypuro Sales Dashboard", layout="wide")
st.markdown(
    "<style>html, body, [class*='css'] {font-size: 20px;} "
    "h1 {font-size: 2.6rem;} .stDataFrame {font-size: 18px;}</style>",
    unsafe_allow_html=True,
)


@st.cache_data
def load() -> tuple[pd.DataFrame, tuple[pd.Timestamp, pd.Timestamp], str]:
    scores = pd.read_parquet(SCORES_PATH)
    now = json.loads((CURATED_DIR / "now.json").read_text())["now_utc"]
    return scores[scores["eligible"]].copy(), data_period(), now


def rescore(df: pd.DataFrame, w_severity: float, w_exposure: float) -> pd.DataFrame:
    if w_severity + w_exposure == 0:
        w_severity = w_exposure = 1.0
    total = w_severity + w_exposure
    out = df.copy()
    out["score"] = 100 * (w_severity * out["severity"] + w_exposure * out["exposure"]) / total
    out["rank"] = out["score"].rank(ascending=False, method="min").astype(int)
    return out.sort_values("rank")


def colour(score: float) -> list[int]:
    s = max(0.0, min(score / 100, 1.0))
    return [int(255 - 75 * s), int(220 * (1 - s)), int(200 * (1 - s)), 220]


stations, period, now = load()

st.title("Oxypuro Sales Dashboard")
st.markdown(f"Data from {period[0]:%-d %B} to {period[1]:%-d %B %Y}")

map_area = st.container()

weights_col, table_col = st.columns([1, 3])
with weights_col:
    st.subheader("Weights")
    w_s = st.slider("Severity (How often the air is above the EU 2030 limit)", 0.0, 1.0, 0.5, 0.05)
    w_e = st.slider("Exposure (How many people live within 1 km)", 0.0, 1.0, 0.5, 0.05)
    ranked = rescore(stations, w_s, w_e)
    share = 0.5 if w_s + w_e == 0 else w_s / (w_s + w_e)
    st.caption(f"Weights after normalising: {share:.0%} severity, {1 - share:.0%} exposure.")

with table_col:
    st.subheader("Ranking")
    st.dataframe(
        ranked[["rank", "location", "municipality", "sensor_type", "score", "reason"]]
        .rename(columns={"location": "station", "sensor_type": "sensor"})
        .round({"score": 1}),
        hide_index=True,
        width="stretch",
        height=420,
    )

with map_area:
    st.subheader("Station scores map")
    pts = ranked.assign(fill=ranked["score"].map(colour), score_txt=ranked["score"].round(1))
    st.pydeck_chart(
        pdk.Deck(
            layers=[
                pdk.Layer(
                    "ScatterplotLayer",
                    data=pts,
                    get_position=["lon", "lat"],
                    get_fill_color="fill",
                    get_radius=3500,
                    stroked=True,
                    get_line_color=[40, 40, 40],
                    line_width_min_pixels=1,
                    pickable=True,
                )
            ],
            initial_view_state=pdk.ViewState(latitude=50.55, longitude=4.6, zoom=7.4),
            tooltip={"text": "#{rank} {location}\nscore {score_txt}"},
            map_style="light",
        ),
        height=560,
    )

st.subheader("Sales brief")
c1, c2, c3 = st.columns([2, 1, 1])
with c1:
    choice = st.selectbox("Station", ranked["location"].tolist())
with c2:
    lang = st.radio("Language", list(LANGUAGES), horizontal=True, format_func=str.upper)
with c3:
    st.write("")
    go = st.button("Write sales brief", type="primary")

if go:
    row = ranked[ranked["location"] == choice].iloc[0]
    facts = station_facts(row, period)
    with st.spinner("Writing the brief with Amazon Bedrock..."):
        try:
            st.session_state["brief"] = (choice, lang, *write_brief(facts, lang), facts)
        except BedrockError as e:
            st.session_state["brief"] = None
            st.error(f"The brief could not be written: {e}")

brief = st.session_state.get("brief")
if brief:
    name, lang_used, text, unknown, facts = brief
    st.markdown(f"**{name}, {lang_used.upper()}**")
    st.write(text)
    if unknown:
        st.warning(
            "Check before sending: these numbers are not in the computed facts: "
            + ", ".join(unknown)
        )
    else:
        st.success("Every number in this brief comes from the computed facts.")
    with st.expander("Facts given to the model"):
        st.json(facts)
