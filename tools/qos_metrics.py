"""Shared QoS parsing and QoS-to-QoE translation for the MSU-IIT campus Wi-Fi thesis.

Both ``tools/run_sweeps.py`` and ``dashboard/app.py`` import from here so the MOS definition
exists in exactly one place.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

SUMMARY_COLUMNS = [
    "scenario",
    "zone",
    "zone_label",
    "n_sta",
    "n_ap",
    "n_bss",
    "direction",
    "rate_manager",
    "propagation",
    "path_loss_exponent",
    "reference_loss_db",
    "radius_m",
    "tx_power_dbm",
    "use_rts",
    "sim_time_s",
    "seed",
    "run",
    "traffic_class",
    "flows",
    "agg_throughput_mbps",
    "per_sta_throughput_mbps",
    "mean_delay_ms",
    "mean_jitter_ms",
    "loss_pct",
    "tx_packets",
    "rx_packets",
    "lost_packets",
]

SCENARIO_LABELS = {
    "baseline": "S1 Baseline (2.4 GHz, 20 MHz)",
    "rftuning": "S2 RF Tuning (dual-band offload)",
    "ax": "S3 Next-Gen (802.11ax + OFDMA)",
}

# Targets stated in the thesis roadmap, used as reference lines in the dashboard.
SCENARIO_TARGETS = {
    "baseline": {"mean_delay_ms": 150.0},
    "rftuning": {"mean_delay_ms": 40.0},
    "ax": {"mean_delay_ms": 40.0, "loss_pct": 1.0},
}


def estimate_mos(
    delay_ms: float | pd.Series,
    jitter_ms: float | pd.Series,
    loss_pct: float | pd.Series,
) -> float | pd.Series:
    """Estimate Mean Opinion Score from simulated QoS using a simplified ITU-T G.107 E-model.

    This is a **provisional** translation so the pipeline is complete end to end. Replace it with
    the correlational model your thesis defines; only this function needs to change.

    The effective one-way delay absorbs a de-jitter buffer approximated as twice the measured
    jitter plus a fixed 10 ms of playout delay. Equipment impairment uses the G.711 defaults
    (Ie = 0, Bpl = 4.3) since the simulated voice flows are uncompressed constant bit rate.
    """
    delay = np.asarray(delay_ms, dtype=float)
    jitter = np.asarray(jitter_ms, dtype=float)
    loss = np.clip(np.asarray(loss_pct, dtype=float), 0.0, 100.0)

    effective_delay = delay + 2.0 * jitter + 10.0

    # Delay impairment: negligible below ~177 ms, then it degrades sharply.
    excess = np.maximum(effective_delay - 177.3, 0.0)
    delay_impairment = 0.024 * effective_delay + 0.11 * excess

    # Packet-loss impairment with G.711 burst tolerance.
    loss_impairment = 95.0 * loss / (loss + 4.3)

    r_factor = np.clip(93.2 - delay_impairment - loss_impairment, 0.0, 100.0)

    mos = 1.0 + 0.035 * r_factor + r_factor * (r_factor - 60.0) * (100.0 - r_factor) * 7e-6
    mos = np.clip(mos, 1.0, 4.5)

    if isinstance(delay_ms, pd.Series):
        return pd.Series(mos, index=delay_ms.index, name="mos")
    return float(mos) if mos.ndim == 0 else mos


def mos_label(mos: float) -> str:
    """Map a MOS value onto the conventional user-satisfaction bands."""
    if mos >= 4.3:
        return "Very satisfied"
    if mos >= 4.0:
        return "Satisfied"
    if mos >= 3.6:
        return "Some users dissatisfied"
    if mos >= 3.1:
        return "Many users dissatisfied"
    if mos >= 2.6:
        return "Nearly all users dissatisfied"
    return "Not recommended"


def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Attach scenario labels and the estimated MOS to a summary DataFrame."""
    out = df.copy()
    # Older result files predate the per-traffic-class breakdown.
    if "traffic_class" not in out.columns:
        out["traffic_class"] = "all"
    out["scenario_label"] = out["scenario"].map(SCENARIO_LABELS).fillna(out["scenario"])
    out["mos"] = estimate_mos(out["mean_delay_ms"], out["mean_jitter_ms"], out["loss_pct"])
    out["mos_label"] = out["mos"].apply(mos_label)
    return out


# A run is uniquely identified by its configuration plus its run index. Re-running the same
# configuration (say, after rebuilding under a different profile) appends a second copy rather
# than replacing the first, so these keys are used to drop the stale rows.
RUN_IDENTITY_COLUMNS = [
    "scenario",
    "zone",
    "n_sta",
    "direction",
    "rate_manager",
    "propagation",
    "path_loss_exponent",
    "reference_loss_db",
    "tx_power_dbm",
    "use_rts",
    "sim_time_s",
    "seed",
    "run",
    "traffic_class",
]


def deduplicate_runs(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Keep only the most recent row for each identical run configuration.

    ``summary.csv`` is append-only, which is what makes it safe to interrupt a long sweep. The
    cost is that repeating a run silently leaves both copies behind, which would then be averaged
    as if they were independent samples. Genuine repeats are distinguished by ``run``, so rows
    matching on every identity column including ``run`` are re-runs and only the last one counts.
    """
    keys = [c for c in RUN_IDENTITY_COLUMNS if c in df.columns]
    if not keys:
        return df, 0
    before = len(df)
    out = df.drop_duplicates(subset=keys, keep="last").reset_index(drop=True)
    return out, before - len(out)


def load_summary(path: str | Path, traffic_class: str | None = None) -> pd.DataFrame:
    """Read ``summary.csv`` produced by campus-wifi-msuiit.cc and add derived columns.

    Each simulation appends one row per traffic class plus an ``all`` row covering the whole run.
    Pass ``traffic_class`` to keep only one of them; leave it as ``None`` to get every row.

    Duplicate rows from repeated runs are dropped; see :func:`deduplicate_runs`.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run a simulation first, for example:\n"
            '  ./ns3 run "campus-wifi-msuiit --scenario=baseline --nSta=10 --simTime=10s"'
        )
    df = pd.read_csv(path)
    optional = {"traffic_class"}
    missing = [c for c in SUMMARY_COLUMNS if c not in df.columns and c not in optional]
    if missing:
        raise ValueError(f"{path} is missing expected columns: {missing}")

    df, dropped = deduplicate_runs(df)
    out = add_derived_columns(df)
    # Reported by the CLI and the dashboard so the drop is visible rather than silent.
    out.attrs["duplicates_dropped"] = dropped
    if traffic_class is not None:
        out = out[out["traffic_class"] == traffic_class]
        if out.empty:
            raise ValueError(f"{path} contains no rows with traffic_class='{traffic_class}'")
    return out


def aggregate_runs(df: pd.DataFrame) -> pd.DataFrame:
    """Average repeated runs so each scenario/zone/density combination yields one row."""
    group_keys = [
        "scenario",
        "scenario_label",
        "zone",
        "zone_label",
        "n_sta",
        "direction",
        "traffic_class",
    ]
    metrics = [
        "agg_throughput_mbps",
        "per_sta_throughput_mbps",
        "mean_delay_ms",
        "mean_jitter_ms",
        "loss_pct",
        "mos",
    ]
    grouped = df.groupby(group_keys, as_index=False)[metrics].agg(["mean", "std"])
    grouped.columns = [
        col[0] if col[1] == "" else f"{col[0]}_{col[1]}" for col in grouped.columns
    ]
    return grouped.reset_index(drop=True)
