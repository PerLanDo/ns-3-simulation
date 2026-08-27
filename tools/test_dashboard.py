"""Headless smoke test for the Streamlit dashboard.

Runs the real app through Streamlit's AppTest harness and fails on any uncaught
exception, so the dashboard can be checked without a browser. Point it at a
summary.csv with `--summary`; defaults to the results tree inside WSL.

    python tools/test_dashboard.py --summary <path to summary.csv>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WSL_SUMMARY = (
    r"\\wsl.localhost\Ubuntu-24.04\home\msuiit\thesis\ns-3.48\results\summary.csv"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", default=DEFAULT_WSL_SUMMARY)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    try:
        from streamlit.testing.v1 import AppTest
    except ImportError:
        print("streamlit.testing is unavailable; upgrade streamlit to run this check")
        return 1

    if not Path(args.summary).exists():
        print(f"no summary at {args.summary}; run a simulation first")
        return 1

    app = AppTest.from_file(str(REPO_ROOT / "dashboard" / "app.py"), default_timeout=args.timeout)
    app.run()

    # The path defaults to a location that may not exist, so drive the real one in.
    app.sidebar.text_input[0].set_value(args.summary).run()

    if app.exception:
        print("FAIL: dashboard raised an exception")
        for exc in app.exception:
            print(f"  {exc.value}")
        return 1

    errors = [e.value for e in app.error]
    if errors:
        print("FAIL: dashboard rendered an error state")
        for message in errors:
            print(f"  {message}")
        return 1

    print("Dashboard rendered without exceptions.")
    print(f"  headings   : {len(app.subheader)}")
    print(f"  metrics    : {len(app.metric)}")
    print(f"  dataframes : {len(app.dataframe)}")
    print(f"  tabs       : {len(app.tabs)}")

    # The headline row is the part most likely to break on a thin dataset.
    if not app.metric:
        print("FAIL: no headline metrics were rendered")
        return 1

    for metric in app.metric[:4]:
        print(f"  {metric.label}: {metric.value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
