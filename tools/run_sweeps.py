#!/usr/bin/env python3
"""Drive the ns-3 campus Wi-Fi simulation from the dummy parameter CSVs.

Week 4 of the thesis roadmap: a Python wrapper that calls the ns-3 C++ binary through
``./ns3 run`` and collects the FlowMonitor results that the simulation appends to
``<outDir>/summary.csv``.

The CSVs under ``data/dummy`` are the single source of truth for zone propagation, client
counts and the application mix; this script translates them into command-line arguments.

Examples
--------
    python3 tools/run_sweeps.py --ns3-dir ~/thesis/ns-3.48 --zone library
    python3 tools/run_sweeps.py --ns3-dir ~/thesis/ns-3.48 --zone library \
        --scenarios baseline ax --sta-counts 10 30 60 --runs 3
    python3 tools/run_sweeps.py --ns3-dir ~/thesis/ns-3.48 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

SCRATCH_PROGRAM = "campus-wifi-msuiit"
DEFAULT_SCENARIOS = ("baseline", "rftuning", "ax")
FALSEY = {"", "none", "no", "false", "0"}


def pick(row: dict[str, str], *names: str) -> str | None:
    """First present, non-empty value among alternative column spellings."""
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


@dataclass
class Zone:
    """A campus zone as described by data/dummy/zones.csv."""

    zone: str
    label: str
    radius_m: float
    path_loss_exponent: float
    reference_loss_db: float
    propagation: str


@dataclass
class TrafficMix:
    """Traffic shares, rates and markings from data/dummy/traffic_mix.csv."""

    browsing_share: float
    video_share: float
    rate_kbps: dict[str, int]
    packet_bytes: dict[str, int]
    tos: dict[str, int]


def read_zones(data_dir: Path) -> dict[str, Zone]:
    path = data_dir / "zones.csv"
    zones: dict[str, Zone] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = row["zone"]
            fading = (pick(row, "fading") or "none").strip().lower()
            zones[name] = Zone(
                zone=name,
                label=pick(row, "zone_label", "description") or name,
                radius_m=float(pick(row, "radius_m", "coverage_radius_m") or 25.0),
                path_loss_exponent=float(row["path_loss_exponent"]),
                reference_loss_db=float(pick(row, "reference_loss_db") or 40.0),
                # The C++ model treats Nakagami as fading layered on top of log-distance.
                propagation="nakagami" if fading not in FALSEY else "logdistance",
            )
    if not zones:
        raise SystemExit(f"No zones found in {path}")
    return zones


def read_sta_counts(data_dir: Path, zone: str) -> list[int]:
    """Sweep points for a zone. Falls back to every listed load if none are flagged."""
    path = data_dir / "sta_counts.csv"
    flagged: list[int] = []
    all_counts: list[int] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["zone"] != zone:
                continue
            count = int(row["n_sta"])
            all_counts.append(count)
            ladder = (pick(row, "sweep_ladder", "sweep_point") or "yes").strip().lower()
            if ladder not in FALSEY:
                flagged.append(count)
    return sorted(set(flagged or all_counts))


def read_traffic_mix(data_dir: Path) -> TrafficMix:
    path = data_dir / "traffic_mix.csv"
    rows: dict[str, dict[str, str]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows[row["traffic_class"]] = row

    missing = {"browsing", "video", "voip"} - set(rows)
    if missing:
        raise SystemExit(f"{path} is missing traffic classes: {', '.join(sorted(missing))}")

    total = sum(float(row["share"]) for row in rows.values())
    if abs(total - 1.0) > 1e-6:
        raise SystemExit(f"{path}: shares sum to {total}, expected 1.0")

    def rate_kbps(row: dict[str, str]) -> int:
        raw = pick(row, "rate_kbps", "data_rate")
        if raw is None:
            raise SystemExit(f"{path}: no rate column for {row['traffic_class']}")
        text = raw.strip().lower()
        if text.endswith("mbps"):
            return int(float(text[:-4]) * 1000)
        if text.endswith("kbps"):
            return int(float(text[:-4]))
        return int(float(text))

    def packet_bytes(row: dict[str, str]) -> int:
        return int(pick(row, "packet_bytes", "packet_size_bytes") or 1000)

    def tos(row: dict[str, str]) -> int:
        return int(str(pick(row, "tos_hex") or "0x00"), 16)

    return TrafficMix(
        browsing_share=float(rows["browsing"]["share"]),
        video_share=float(rows["video"]["share"]),
        rate_kbps={name: rate_kbps(row) for name, row in rows.items()},
        packet_bytes={name: packet_bytes(row) for name, row in rows.items()},
        tos={name: tos(row) for name, row in rows.items()},
    )


def build_program_args(
    scenario: str,
    zone: Zone,
    n_sta: int,
    mix: TrafficMix,
    args: argparse.Namespace,
    run: int,
) -> list[str]:
    program_args = [
        f"--scenario={scenario}",
        f"--zone={zone.zone}",
        f"--nSta={n_sta}",
        f"--simTime={args.sim_time}",
        f"--direction={args.direction}",
        f"--rateManager={args.rate_manager}",
        f"--propagation={zone.propagation}",
        f"--radius={zone.radius_m}",
        f"--pathLossExponent={zone.path_loss_exponent}",
        f"--referenceLoss={zone.reference_loss_db}",
        f"--browsingShare={mix.browsing_share}",
        f"--videoShare={mix.video_share}",
        f"--browsingRateKbps={mix.rate_kbps['browsing']}",
        f"--videoRateKbps={mix.rate_kbps['video']}",
        f"--voipRateKbps={mix.rate_kbps['voip']}",
        f"--browsingBytes={mix.packet_bytes['browsing']}",
        f"--videoBytes={mix.packet_bytes['video']}",
        f"--voipBytes={mix.packet_bytes['voip']}",
        f"--browsingTos={mix.tos['browsing']}",
        f"--videoTos={mix.tos['video']}",
        f"--voipTos={mix.tos['voip']}",
        f"--uplinkRatio={args.uplink_ratio}",
        f"--txPower={args.tx_power}",
        f"--seed={args.seed}",
        f"--run={run}",
        f"--outDir={args.out_dir}",
    ]
    if scenario == "rftuning":
        program_args.append(f"--fiveToTwoRatio={args.five_to_two_ratio}")
    if scenario == "ax":
        program_args.append(f"--apSpacing={args.ap_spacing}")
    if args.use_rts:
        program_args.append("--useRts=1")
    if args.pcap:
        program_args.append("--pcap=1")
    return program_args


def run_one(ns3_dir: Path, program_args: list[str], dry_run: bool) -> bool:
    """Invoke ./ns3 run for a single configuration. Returns True on success."""
    run_string = " ".join([SCRATCH_PROGRAM, *program_args])
    command = ["./ns3", "run", run_string]

    print(f"\n$ (cd {ns3_dir} && {shlex.join(command)})", flush=True)
    if dry_run:
        return True

    started = time.monotonic()
    result = subprocess.run(command, cwd=ns3_dir, check=False)
    elapsed = time.monotonic() - started

    if result.returncode != 0:
        print(f"  FAILED (exit {result.returncode}) after {elapsed:.1f}s", file=sys.stderr)
        return False
    print(f"  done in {elapsed:.1f}s", flush=True)
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--ns3-dir", type=Path, required=True, help="Path to the built ns-3 tree, e.g. ~/thesis/ns-3.48"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=repo_root / "data" / "dummy",
        help="Directory holding zones.csv, sta_counts.csv and traffic_mix.csv",
    )
    parser.add_argument("--zone", default="library", help="Zone identifier from zones.csv")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=list(DEFAULT_SCENARIOS),
        choices=list(DEFAULT_SCENARIOS),
        help="Scenarios to run",
    )
    parser.add_argument(
        "--sta-counts",
        nargs="+",
        type=int,
        default=None,
        help="Station counts; defaults to the sweep ladder in sta_counts.csv",
    )
    parser.add_argument("--runs", type=int, default=1, help="Independent replicates per configuration")
    parser.add_argument("--sim-time", default="20s", help="Simulated traffic duration")
    parser.add_argument("--direction", default="both", choices=["downlink", "uplink", "both"])
    parser.add_argument("--uplink-ratio", type=float, default=0.2)
    parser.add_argument("--five-to-two-ratio", type=float, default=0.75, help="rftuning: 5 GHz share")
    parser.add_argument("--ap-spacing", type=float, default=30.0, help="ax: metres between the APs")
    parser.add_argument("--rate-manager", default="ideal", choices=["ideal", "minstrel"])
    parser.add_argument("--tx-power", type=float, default=20.0, help="Transmit power in dBm")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--use-rts", action="store_true", help="Enable RTS/CTS for every frame")
    parser.add_argument("--out-dir", default="results", help="Output directory relative to the ns-3 tree")
    parser.add_argument("--pcap", action="store_true", help="Also write pcap traces")
    parser.add_argument("--dry-run", action="store_true", help="Print the commands without running them")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    ns3_dir = args.ns3_dir.expanduser().resolve()
    if not (ns3_dir / "ns3").exists():
        raise SystemExit(f"{ns3_dir} does not look like an ns-3 tree (no ./ns3 script)")

    data_dir = args.data_dir.expanduser().resolve()
    zones = read_zones(data_dir)
    if args.zone not in zones:
        raise SystemExit(f"Unknown zone '{args.zone}'. Available: {', '.join(sorted(zones))}")
    zone = zones[args.zone]
    mix = read_traffic_mix(data_dir)

    sta_counts = args.sta_counts or read_sta_counts(data_dir, args.zone)
    if not sta_counts:
        raise SystemExit(f"No station counts for zone '{args.zone}'; pass --sta-counts explicitly")

    total = len(args.scenarios) * len(sta_counts) * args.runs
    print(
        f"{zone.label} ({zone.zone}): {len(args.scenarios)} scenario(s) x "
        f"{len(sta_counts)} load point(s) {sta_counts} x {args.runs} run(s) = {total} simulations"
    )

    failures = 0
    for scenario in args.scenarios:
        for n_sta in sta_counts:
            for run in range(1, args.runs + 1):
                program_args = build_program_args(scenario, zone, n_sta, mix, args, run)
                if not run_one(ns3_dir, program_args, args.dry_run):
                    failures += 1

    summary = ns3_dir / args.out_dir / "summary.csv"
    if args.dry_run:
        print(f"\nDry run: {total} simulation(s) planned, none executed")
    else:
        print(f"\nCompleted {total - failures}/{total} simulations")
        if summary.exists():
            print(f"Results appended to {summary}")
            print(f"Next: python3 tools/analyze.py --summary {summary}")
        else:
            print(f"Expected {summary} but it was not created", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
