#!/usr/bin/env python3
"""Turn the ns-3 FlowMonitor summary into thesis tables and figures.

Reads ``summary.csv`` through :mod:`tools.qos_metrics`, averages repeated runs, and reports the
three-scenario comparison per traffic class together with the QoS-to-QoE (MOS) translation.

Examples
--------
    python3 tools/analyze.py --summary ns-3.48/results/summary.csv
    python3 tools/analyze.py --summary ns-3.48/results/summary.csv --plots out/figures
    python3 tools/analyze.py --summary ns-3.48/results/summary.csv --export out/analysis.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qos_metrics import (  # noqa: E402
    SCENARIO_LABELS,
    SCENARIO_TARGETS,
    aggregate_runs,
    load_summary,
    mos_label,
)

CLASS_ORDER = ["browsing", "video", "voip", "other", "all"]

# The roadmap puts the baseline's throughput collapse at 40-80 clients, so verdicts about
# contention are only meaningful once a sweep reaches this density.
BASELINE_COLLAPSE_DENSITY = 60
PLOT_METRICS = [
    ("agg_throughput_mbps_mean", "Aggregate throughput (Mbps)"),
    ("mean_delay_ms_mean", "Mean latency (ms)"),
    ("mean_jitter_ms_mean", "Mean jitter (ms)"),
    ("loss_pct_mean", "Packet loss (%)"),
]


def ordered_classes(df: pd.DataFrame) -> list[str]:
    present = set(df["traffic_class"])
    return [c for c in CLASS_ORDER if c in present]


def print_report(df: pd.DataFrame) -> None:
    pd.set_option("display.width", 170)
    pd.set_option("display.max_columns", 40)
    pd.set_option("display.float_format", lambda v: f"{v:,.2f}")

    for zone, zone_df in df.groupby("zone"):
        label = zone_df["zone_label"].iloc[0]
        print(f"\n{'=' * 82}\nZone: {label} ({zone})\n{'=' * 82}")

        overall = zone_df[zone_df["traffic_class"] == "all"]
        if not overall.empty:
            print("\nAggregate QoS by scenario and client density")
            print(
                overall.pivot_table(
                    index="n_sta",
                    columns="scenario_label",
                    values=[
                        "agg_throughput_mbps_mean",
                        "mean_delay_ms_mean",
                        "loss_pct_mean",
                    ],
                ).to_string()
            )

            print("\nRoadmap targets")
            for scenario, targets in SCENARIO_TARGETS.items():
                rows = overall[overall["scenario"] == scenario]
                if rows.empty:
                    continue
                target = targets.get("mean_delay_ms")
                worst = rows["mean_delay_ms_mean"].max()
                if scenario == "baseline":
                    # The roadmap predicts collapse under contention, so a low latency at low
                    # density confirms the model rather than contradicting it. Only call the
                    # prediction unmet once the sweep actually reaches a congested density.
                    peak_density = rows["n_sta"].max()
                    if worst >= target:
                        verdict = "reproduced"
                    elif peak_density < BASELINE_COLLAPSE_DENSITY:
                        verdict = (
                            f"not yet tested (sweep peaks at N={peak_density}; "
                            f"collapse is expected from N>={BASELINE_COLLAPSE_DENSITY})"
                        )
                    else:
                        verdict = "not reproduced"
                    print(
                        f"  {SCENARIO_LABELS[scenario]}: expect latency >= {target:.0f} ms at "
                        f"high load, observed max {worst:.1f} ms -> {verdict}"
                    )
                else:
                    peak_density = rows["n_sta"].max()
                    if worst > target:
                        verdict = "not met"
                    elif peak_density < BASELINE_COLLAPSE_DENSITY:
                        # Passing a latency target on an uncongested network is not evidence
                        # that the optimization works; the comparison only bites under load.
                        verdict = f"met, but only tested up to N={peak_density}"
                    else:
                        verdict = "met"
                    print(
                        f"  {SCENARIO_LABELS[scenario]}: expect latency <= {target:.0f} ms, "
                        f"observed max {worst:.1f} ms -> {verdict}"
                    )

        voip = zone_df[zone_df["traffic_class"] == "voip"]
        if not voip.empty:
            print("\nVoIP QoE (estimated MOS)")
            print(
                voip.pivot_table(
                    index="n_sta", columns="scenario_label", values="mos_mean"
                ).to_string()
            )

        print("\nPer-traffic-class detail")
        detail = zone_df.copy()
        detail["traffic_class"] = pd.Categorical(
            detail["traffic_class"], ordered_classes(detail), ordered=True
        )
        detail = detail.sort_values(["scenario", "n_sta", "traffic_class"])
        detail["qoe"] = detail["mos_mean"].apply(mos_label)
        cols = [
            "scenario",
            "n_sta",
            "traffic_class",
            "agg_throughput_mbps_mean",
            "per_sta_throughput_mbps_mean",
            "mean_delay_ms_mean",
            "mean_jitter_ms_mean",
            "loss_pct_mean",
            "mos_mean",
            "qoe",
        ]
        print(detail[[c for c in cols if c in detail.columns]].to_string(index=False))


def write_plots(df: pd.DataFrame, out_dir: Path) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not installed; skipping plots", file=sys.stderr)
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    overall = df[df["traffic_class"] == "all"]

    for zone, zone_df in overall.groupby("zone"):
        fig, axes = plt.subplots(2, 2, figsize=(11, 8))
        for ax, (column, title) in zip(axes.flat, PLOT_METRICS):
            if column not in zone_df.columns:
                continue
            for scenario_label, scen in zone_df.groupby("scenario_label"):
                scen = scen.sort_values("n_sta")
                ax.plot(scen["n_sta"], scen[column], marker="o", label=scenario_label)
            ax.set_xlabel("Number of stations")
            ax.set_ylabel(title)
            ax.set_title(title)
            ax.grid(True, alpha=0.3)
        axes.flat[0].legend(fontsize=8)
        fig.suptitle(f"Campus Wi-Fi QoS versus client density: {zone}")
        fig.tight_layout()
        path = out_dir / f"qos-{zone}.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        print(f"Wrote {path}")

    voip = df[df["traffic_class"] == "voip"]
    if not voip.empty:
        fig, ax = plt.subplots(figsize=(7.5, 4.5))
        for (zone, scenario_label), grp in voip.groupby(["zone", "scenario_label"]):
            grp = grp.sort_values("n_sta")
            ax.plot(grp["n_sta"], grp["mos_mean"], marker="s", label=f"{zone} / {scenario_label}")
        ax.axhline(3.6, linestyle="--", color="grey", linewidth=1)
        ax.text(ax.get_xlim()[0], 3.63, "acceptable (MOS 3.6)", fontsize=8, color="grey")
        ax.set_xlabel("Number of stations")
        ax.set_ylabel("VoIP MOS (E-model estimate)")
        ax.set_ylim(1, 4.6)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=7)
        fig.tight_layout()
        path = out_dir / "voip-mos.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        print(f"Wrote {path}")


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=repo_root / "ns-3.48" / "results" / "summary.csv",
        help="Path to summary.csv",
    )
    parser.add_argument("--zone", help="Restrict the report to one zone")
    parser.add_argument("--direction", help="Restrict to one traffic direction")
    parser.add_argument("--export", type=Path, help="Write the analyzed table to this CSV")
    parser.add_argument("--plots", type=Path, help="Write PNG figures into this directory")
    args = parser.parse_args(argv)

    try:
        raw = load_summary(args.summary.expanduser())
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    dropped = raw.attrs.get("duplicates_dropped", 0)
    if dropped:
        print(
            f"Note: ignored {dropped} duplicate row(s) from re-running an identical "
            "configuration; the most recent result was kept."
        )

    if args.zone:
        raw = raw[raw["zone"] == args.zone]
        if raw.empty:
            raise SystemExit(f"No rows for zone '{args.zone}'")
    if args.direction:
        raw = raw[raw["direction"] == args.direction]
        if raw.empty:
            raise SystemExit(f"No rows for direction '{args.direction}'")

    df = aggregate_runs(raw)
    print_report(df)

    if args.export:
        args.export.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.export, index=False)
        print(f"\nWrote {args.export}")

    if args.plots:
        write_plots(df, args.plots)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
