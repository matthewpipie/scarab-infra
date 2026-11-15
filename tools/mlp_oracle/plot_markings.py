#!/usr/bin/env python3
"""Compare markings on one workload: the oracle's and its control's IPC gain over LRU under each of several
studies that differ only in their marking, as paper figures (plot.py's style and rules: one graph per image,
axis labels and a legend, no other text).

    python tools/mlp_oracle/plot_markings.py mlp_oracle_google_arizona_db0 mlp_oracle_google_arizona_db1 \\
        mlp_oracle_google_arizona_db2 mlp_oracle_google_defmark --workload arizona --sides d,b --out arizona_db.png

writes arizona_db_d.png and arizona_db_b.png (with one side, --out itself; .pdf for a vector figure), each with a
CSV of its values beside it. One group per study, in the order given: the oracle (the reference's marked misses
forced to hit) and its control (as many random misses on as many lines), with plot.py's factor bracket
between them (--bracket as in plot.py). The tick labels name each study's
marking: --labels sets them; by default, studies that differ in --mlp_data_bound get the name of its count
(DATA_BOUND_LABELS) and the x axis says so, else the study's name. The studies must agree on the workload's LRU
IPC (the same machine, binary and run length), which the script checks.

stdout gets a table per side: per study, the share of the workload's non-cold misses on that side its reference
marked, the oracle's and the control's gains, the gap (oracle minus control, in points) and the factor (oracle
gain over control gain). stderr gets the run details, for the caption.
"""
import argparse
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plot as P  # noqa: E402
import report  # noqa: E402

# The count each --mlp_data_bound mode thresholds (memory.param.def): fu_wait cycles per load waiting on the
# request (0) or once per request (1); cycles the ROB head was a load waiting on it (2), and those of them in
# which nothing issued either (3, the default).
DATA_BOUND_LABELS = {"0": "fu_wait\nper load", "1": "fu_wait\nper miss", "2": "ROB-head\nstall",
                     "3": "ROB-head\nno issue"}
MEASURES = ("oracle", "control")


def data_bound(st, side):
    return report.flag(st.desc["configurations"][st.roles[f"oracle_{side}"]]["params"], "mlp_data_bound")


def marked_share(st, w, side):
    """The workload's share of non-cold misses (on side) the study's reference marked."""
    g = st.rows[(st.rows["config"] == st.roles[f"oracle_{side}"]) & (st.rows["workload"] == w)]
    sides = "ID" if side == "b" else side.upper()
    marked = sum(g[f"{s}_REF_MARKED"].sum() for s in sides)
    misses = sum(g[f"{s}_REF_MISSES"].sum() for s in sides)
    return marked / misses if misses else math.nan


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("studies", nargs="+", help="replay descriptors, one per marking, in plotting order")
    p.add_argument("--workload", required=True, help="the workload, as report.py names it")
    p.add_argument("--sides", default="d,b", help="comma-separated: i, d, b; default d,b")
    p.add_argument("--labels", help="comma-separated tick labels, one per study (\\n for a line break)")
    p.add_argument("--xlabel", help="x axis label (default: 'Data-bound count' for data-bound labels, else none)")
    p.add_argument("--ymax", type=float, help="cut the y axis here; a bar it cuts gets a break mark")
    P.add_bracket_arg(p)
    p.add_argument("--width", type=float, help="figure width in inches (default 3.3)")
    p.add_argument("--height", type=float, default=2.1, help="figure height in inches (default 2.1)")
    p.add_argument("--theme", choices=P.THEMES, default="light")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--out", default="markings.png", help="image path; with several sides, the stem of each")
    a = p.parse_args()
    t = P.THEMES[a.theme]
    sides = [s.strip() for s in a.sides.split(",") if s.strip()]
    if not sides or any(s not in P.SIDE_TITLE for s in sides):
        p.error("--sides takes i, d and b")
    studies = [report.Study(s, include_cold=False) for s in a.studies]
    w = a.workload
    for st in studies:
        if w not in st.workloads:
            sys.exit(f"{st.name} has no workload {w}")
        missing = [s for s in sides if f"oracle_{s}" not in st.roles or f"control_{s}" not in st.roles]
        if missing:
            sys.exit(f"{st.name} has no oracle and control on side(s) {', '.join(missing)}")
    base = studies[0].ipc[studies[0].roles["lru"]][w]
    for st in studies[1:]:
        other = st.ipc[st.roles["lru"]][w]
        if abs(other / base - 1) > 1e-9:
            sys.exit(f"{st.name}'s lru IPC for {w} ({other:.6f}) differs from {studies[0].name}'s ({base:.6f}): "
                     "not the same machine or run")
    print(f"lru identical for {w} across {len(studies)} studies", file=sys.stderr)

    xlabel = a.xlabel
    if a.labels:
        labels = [x.replace("\\n", "\n") for x in a.labels.split(",")]
        if len(labels) != len(studies):
            p.error(f"--labels has {len(labels)} labels for {len(studies)} studies")
    else:
        modes = [data_bound(st, sides[0]) for st in studies]
        if len(set(modes)) == len(modes) and all(m in DATA_BOUND_LABELS for m in modes):
            labels = [DATA_BOUND_LABELS[m] for m in modes]
            xlabel = xlabel or "Data-bound count"
        else:
            labels = [st.name.removeprefix("mlp_oracle_") for st in studies]

    plt.rcParams.update({"font.family": "sans-serif",
                         "font.sans-serif": ["Inter", "Helvetica", "Arial", "Liberation Sans", "DejaVu Sans"],
                         "axes.unicode_minus": True, "pdf.fonttype": 42, "ps.fonttype": 42})
    out = Path(a.out)
    st0 = studies[0]
    for side in sides:
        path = out if len(sides) == 1 else out.with_name(f"{out.stem}_{side}{out.suffix}")
        groups = [(lab, {m: report.pct(float(st.gain[f"{m}_{side}"][w])) for m in MEASURES})
                  for st, lab in zip(studies, labels)]
        fig, cut = P.figure(groups, [], t, a, geomean=False, measures=MEASURES, xlabel=xlabel)
        fig.savefig(path, dpi=a.dpi, bbox_inches="tight", pad_inches=0.02, facecolor=t["surface"])
        plt.close(fig)
        rows = []
        lines = [f"{path.name}: {w} · {st0.arch} · {P.SIDE_TITLE[side]}",
                 "marked = the reference's marked share of non-cold misses; gain = IPC gain over LRU (%); "
                 "gap = oracle - control (points); factor = oracle gain / control gain",
                 f"{'study':<34}{'marking':<20}{'marked':>8}{'oracle':>9}{'control':>9}{'gap':>8}{'factor':>8}"]
        for st, (lab, vals) in zip(studies, groups):
            o, c = vals["oracle"], vals["control"]
            share = marked_share(st, w, side)
            rows.append({"study": st.name, "marking": lab.replace("\n", " "), "workload": w, "side": side,
                         "marked_share": round(share, 6), "oracle_pct": round(o, 4), "control_pct": round(c, 4),
                         "cut": any(n == lab for n, _, _ in cut)})
            factor = f"{o / c:.2f}x" if c > 0 else "-"
            lines.append(f"{st.name:<34}{lab.replace(chr(10), ' '):<20}{share:>8.1%}{o:>+9.2f}{c:>+9.2f}"
                         f"{o - c:>+8.2f}{factor:>8}")
        csv = path.with_suffix(".csv")
        pd.DataFrame(rows).to_csv(csv, index=False)
        print(f"wrote {path} and {csv}\n  {w} · {st0.arch} · {P.SIDE_TITLE[side]} · "
              f"{st0.rows[st0.rows['workload'] == w]['cluster'].nunique()} simpoints · {st0.length}"
              + "".join(f"\n  cut at {a.ymax:g}: {n} {m} {v:+.1f}" for n, m, v in cut), file=sys.stderr)
        print("\n".join(lines) + "\n", flush=True)


if __name__ == "__main__":
    main()
