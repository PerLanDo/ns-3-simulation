#!/usr/bin/env python3
"""Streamlit dashboard for the MSU-IIT campus Wi-Fi thesis.

Reads the ``summary.csv`` accumulated by ``campus-wifi-msuiit.cc`` and visualises the QoS
metrics across the three comparative scenarios, together with the QoS-to-QoE (MOS) translation.

Run with::

    streamlit run dashboard/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from qos_metrics import (  # noqa: E402
    SCENARIO_LABELS,
    SCENARIO_TARGETS,
    load_summary,
    mos_label,
)

# Simulations run inside WSL, but the dashboard is usually launched from Windows, so the
# results normally sit on the other side of the \\wsl.localhost bridge. Prefer a local
# results tree when one exists and fall back to the WSL one.
WSL_SUMMARY = Path(
    r"\\wsl.localhost\Ubuntu-24.04\home\msuiit\thesis\ns-3.48\results\summary.csv"
)


def _default_summary() -> Path:
    local = REPO_ROOT / "ns-3.48" / "results" / "summary.csv"
    if local.exists():
        return local
    try:
        if WSL_SUMMARY.exists():
            return WSL_SUMMARY
    except OSError:
        # Raised when WSL is not running; fall through to the local path.
        pass
    return local


DEFAULT_SUMMARY = _default_summary()
PLACEHOLDER_BASELINE = REPO_ROOT / "data" / "dummy" / "qos_baseline_placeholders.csv"

CLASS_ORDER = ["all", "browsing", "video", "voip", "other"]

METRICS = {
    "agg_throughput_mbps": ("Aggregate throughput", "Mbps", "higher"),
    "per_sta_throughput_mbps": ("Per-station throughput", "Mbps", "higher"),
    "mean_delay_ms": ("Mean latency", "ms", "lower"),
    "mean_jitter_ms": ("Mean jitter", "ms", "lower"),
    "loss_pct": ("Packet loss", "%", "lower"),
    "mos": ("Estimated MOS", "", "higher"),
}

st.set_page_config(page_title="MSU-IIT Campus Wi-Fi", layout="wide")


def scenario_chart(df: pd.DataFrame, metric: str) -> alt.Chart:
    title, unit, _ = METRICS[metric]
    axis_title = f"{title} ({unit})" if unit else title

    base = alt.Chart(df).encode(
        x=alt.X("n_sta:Q", title="Number of stations"),
        color=alt.Color("scenario_label:N", title="Scenario"),
    )
    line = base.mark_line(point=True).encode(
        y=alt.Y(f"mean({metric}):Q", title=axis_title),
        tooltip=[
            alt.Tooltip("scenario_label:N", title="Scenario"),
            alt.Tooltip("n_sta:Q", title="Stations"),
            alt.Tooltip(f"mean({metric}):Q", title=title, format=".3f"),
        ],
    )
    band = base.mark_errorband(extent="stdev").encode(
        y=alt.Y(f"{metric}:Q", title=axis_title),
    )
    return (band + line).properties(height=320).interactive()


def main() -> None:
    st.title("MSU-IIT Campus Wi-Fi — Simulation Results")
    st.caption(
        "Undergraduate thesis instrument. Compares the baseline campus deployment against "
        "RF tuning and an 802.11ax upgrade using ns-3 FlowMonitor measurements."
    )

    st.sidebar.header("Data source")
    summary_path = st.sidebar.text_input("summary.csv path", value=str(DEFAULT_SUMMARY))

    try:
        df = load_summary(summary_path)
    except (FileNotFoundError, ValueError) as exc:
        st.error(str(exc))
        st.info(
            "Generate results first:\n\n"
            "```bash\n"
            "cd ns-3.48\n"
            './ns3 run "campus-wifi-msuiit --scenario=baseline --nSta=10 --simTime=10s"\n'
            "```\n\n"
            "Or run the full sweep:\n\n"
            "```bash\n"
            "python3 tools/run_sweeps.py --sta-counts 10 30 60 --runs 3\n"
            "```"
        )
        return

    dropped = df.attrs.get("duplicates_dropped", 0)
    if dropped:
        st.sidebar.caption(
            f"Ignored {dropped} duplicate row(s) from re-running an identical configuration; "
            "the most recent result was kept."
        )

    st.sidebar.header("Filters")
    zones = sorted(df["zone"].unique())
    selected_zones = st.sidebar.multiselect("Zone", zones, default=zones)

    scenarios = [s for s in SCENARIO_LABELS if s in set(df["scenario"])]
    scenarios += [s for s in sorted(df["scenario"].unique()) if s not in SCENARIO_LABELS]
    selected_scenarios = st.sidebar.multiselect("Scenario", scenarios, default=scenarios)

    directions = sorted(df["direction"].unique())
    selected_direction = st.sidebar.selectbox("Traffic direction", directions)

    # Each run contributes one row per application class plus an "all" row. Mixing them would
    # average the classes together, so exactly one is selected at a time.
    classes = [c for c in CLASS_ORDER if c in set(df["traffic_class"])]
    classes += [c for c in sorted(df["traffic_class"].unique()) if c not in CLASS_ORDER]
    selected_class = st.sidebar.selectbox(
        "Traffic class",
        classes,
        index=classes.index("all") if "all" in classes else 0,
        help="'all' aggregates every flow in the run; the others isolate one application class.",
    )

    view = df[
        df["zone"].isin(selected_zones)
        & df["scenario"].isin(selected_scenarios)
        & (df["direction"] == selected_direction)
        & (df["traffic_class"] == selected_class)
    ]

    if view.empty:
        st.warning("No rows match the current filters.")
        return

    densities = sorted(view["n_sta"].unique())
    headline_density = st.sidebar.select_slider(
        "Headline density", options=densities, value=densities[-1]
    )

    st.subheader(f"Headline comparison at {headline_density} stations ({selected_class} traffic)")
    headline = view[view["n_sta"] == headline_density]

    columns = st.columns(max(len(selected_scenarios), 1))
    baseline_row = headline[headline["scenario"] == "baseline"]

    for column, scenario in zip(columns, selected_scenarios):
        rows = headline[headline["scenario"] == scenario]
        if rows.empty:
            column.metric(SCENARIO_LABELS.get(scenario, scenario), "no data")
            continue

        delay = rows["mean_delay_ms"].mean()
        loss = rows["loss_pct"].mean()
        throughput = rows["agg_throughput_mbps"].mean()
        mos = rows["mos"].mean()

        delta = None
        if not baseline_row.empty and scenario != "baseline":
            reference = baseline_row["mean_delay_ms"].mean()
            if reference > 0:
                delta = f"{(delay - reference) / reference * 100:+.1f}% latency vs baseline"

        with column:
            st.markdown(f"**{SCENARIO_LABELS.get(scenario, scenario)}**")
            st.metric("Mean latency", f"{delay:.1f} ms", delta=delta, delta_color="inverse")
            st.metric("Aggregate throughput", f"{throughput:.2f} Mbps")
            st.metric("Packet loss", f"{loss:.2f} %")
            st.metric("Estimated MOS", f"{mos:.2f}", help=mos_label(mos))

            target = SCENARIO_TARGETS.get(scenario, {}).get("mean_delay_ms")
            if target is not None:
                met = delay <= target if scenario != "baseline" else delay >= target
                expectation = (
                    f"latency >= {target:.0f} ms (collapse expected)"
                    if scenario == "baseline"
                    else f"latency <= {target:.0f} ms"
                )
                st.caption(("Meets" if met else "Does not meet") + f" roadmap target: {expectation}")

    st.divider()
    st.subheader("QoS versus client density")

    metric_keys = list(METRICS)
    tabs = st.tabs([METRICS[key][0] for key in metric_keys])
    for tab, key in zip(tabs, metric_keys):
        with tab:
            st.altair_chart(scenario_chart(view, key), width='stretch')
            _, unit, better = METRICS[key]
            st.caption(
                f"Shaded band is one standard deviation across repeated runs. "
                f"{'Higher' if better == 'higher' else 'Lower'} is better"
                + (f" ({unit})." if unit else ".")
            )

    st.divider()
    st.subheader("QoS to QoE translation")
    st.caption(
        "MOS is derived from the simulated latency, jitter and loss using a simplified "
        "ITU-T G.107 E-model. It is provisional — swap in the thesis correlational model by "
        "editing `estimate_mos` in `tools/qos_metrics.py`."
    )

    mos_table = (
        view.groupby(["scenario_label", "n_sta"], as_index=False)
        .agg(
            latency_ms=("mean_delay_ms", "mean"),
            jitter_ms=("mean_jitter_ms", "mean"),
            loss_pct=("loss_pct", "mean"),
            mos=("mos", "mean"),
        )
        .sort_values(["scenario_label", "n_sta"])
    )
    mos_table["user_perception"] = mos_table["mos"].apply(mos_label)
    st.dataframe(mos_table.round(3), width='stretch', hide_index=True)

    if PLACEHOLDER_BASELINE.exists():
        with st.expander("Calibration target (placeholder survey data)"):
            st.caption(
                "These values are placeholders, not measurements. Replace "
                "`data/dummy/qos_baseline_placeholders.csv` with your MSU-IIT site survey, then "
                "tune `--pathLossExponent` and `--referenceLoss` until the simulated baseline "
                "matches the measured one."
            )
            st.dataframe(
                pd.read_csv(PLACEHOLDER_BASELINE), width='stretch', hide_index=True
            )

    with st.expander("Raw simulation records"):
        st.dataframe(view, width='stretch', hide_index=True)
        st.download_button(
            "Download filtered results as CSV",
            data=view.to_csv(index=False).encode("utf-8"),
            file_name="campus_wifi_results.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    main()
