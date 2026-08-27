"""Lightweight static sanity check for the scratch simulation script.

There is no C++ toolchain on the Windows side of this project, so this cannot
replace `./ns3 build` inside WSL. It only catches the cheap mistakes (unbalanced
delimiters, CLI flags that are declared but never read, and vice versa) before
you pay for a full ns-3 compile.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRATCH = REPO_ROOT / "ns-3.48" / "scratch" / "campus-wifi-msuiit.cc"
SWEEPS = REPO_ROOT / "tools" / "run_sweeps.py"


def strip_literals(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r'"(\\.|[^"\\])*"', '""', src)
    src = re.sub(r"'(\\.|[^'\\])*'", "''", src)
    return src


def check_balance(src: str) -> list[str]:
    problems = []
    for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
        n_open, n_close = src.count(opener), src.count(closer)
        if n_open != n_close:
            problems.append(
                f"unbalanced {opener}{closer}: {n_open} open vs {n_close} close"
            )
    return problems


def check_cli_flags(raw: str, stripped: str) -> list[str]:
    problems = []
    declared = set(re.findall(r'cmd\.AddValue\("([A-Za-z0-9_]+)"', raw))
    if not declared:
        problems.append("no cmd.AddValue() calls found -- did the parser break?")
        return problems

    # Every declared flag must bind a variable that the body actually uses.
    for flag in sorted(declared):
        bound = re.search(
            r'cmd\.AddValue\("' + re.escape(flag) + r'",\s*"[^"]*",\s*([A-Za-z0-9_]+)\s*\)',
            raw,
        )
        if not bound:
            problems.append(f"--{flag}: could not resolve the bound variable")
            continue
        var = bound.group(1)
        uses = len(re.findall(r"\b" + re.escape(var) + r"\b", stripped))
        # declaration + AddValue + at least one real read
        if uses < 3:
            problems.append(f"--{flag}: bound variable '{var}' is never read")

    if SWEEPS.exists():
        driver = SWEEPS.read_text(encoding="utf-8")
        passed = set(re.findall(r'f?"--([A-Za-z0-9_]+)=', driver))
        for flag in sorted(passed - declared):
            problems.append(f"run_sweeps.py passes --{flag}, which the script does not declare")

    return problems


def check_summary_schema() -> list[str]:
    """The C++ header row and the Python reader must agree."""
    problems = []
    raw = SCRATCH.read_text(encoding="utf-8")
    # The header is written as adjacent string literals, so join them before splitting.
    header = re.search(r'summary\s*<<\s*("scenario.*?)\s*;', raw, flags=re.S)
    if not header:
        problems.append("could not locate the summary.csv header row in the scratch script")
        return problems
    joined = "".join(re.findall(r'"((?:\\.|[^"\\])*)"', header.group(1)))
    cpp_cols = [c.strip() for c in joined.replace("\\n", "").split(",") if c.strip()]

    metrics = (REPO_ROOT / "tools" / "qos_metrics.py").read_text(encoding="utf-8")
    block = re.search(r"SUMMARY_COLUMNS\s*=\s*\[(.*?)\]", metrics, flags=re.S)
    if not block:
        problems.append("qos_metrics.py does not define SUMMARY_COLUMNS")
        return problems
    py_cols = re.findall(r'"([a-z0-9_]+)"', block.group(1))

    if cpp_cols != py_cols:
        only_cpp = [c for c in cpp_cols if c not in py_cols]
        only_py = [c for c in py_cols if c not in cpp_cols]
        if only_cpp:
            problems.append(f"columns written by C++ but unknown to Python: {only_cpp}")
        if only_py:
            problems.append(f"columns expected by Python but never written: {only_py}")
        if not only_cpp and not only_py:
            problems.append("summary columns match by name but differ in order")

    # The data row must emit exactly one separator fewer than there are columns,
    # otherwise the CSV silently shifts and every downstream metric is wrong.
    row = re.search(r"summary\s*<<\s*scenario\s*<<.*?;", raw, flags=re.S)
    if not row:
        problems.append("could not locate the summary.csv data row in the scratch script")
    else:
        literals = re.findall(r'"((?:\\.|[^"\\])*)"', row.group(0))
        separators = sum(lit.count(",") for lit in literals)
        if separators != len(cpp_cols) - 1:
            problems.append(
                f"data row emits {separators} separators for {len(cpp_cols)} header columns"
            )
    return problems


def main() -> int:
    if not SCRATCH.exists():
        print(f"missing {SCRATCH}")
        return 1

    raw = SCRATCH.read_text(encoding="utf-8")
    stripped = strip_literals(raw)

    problems = check_balance(stripped)
    problems += check_cli_flags(raw, stripped)
    problems += check_summary_schema()

    if problems:
        print(f"{SCRATCH.name}: {len(problems)} problem(s)")
        for item in problems:
            print(f"  - {item}")
        return 1

    flags = len(re.findall(r"cmd\.AddValue\(", raw))
    print(f"{SCRATCH.name}: delimiters balanced, {flags} CLI flags all read, summary schema matches")
    print("Reminder: this is not a compile. Run ./ns3 build in WSL before trusting the model.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
