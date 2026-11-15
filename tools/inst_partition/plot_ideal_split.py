#!/usr/bin/env python3
"""Histograms of the ideal REPL_INST_PARTITION split: each workload (or trace file) picks the split, the
MLC's instruction ways, with its highest IPC, and a bar counts the picks of each split. Paper figures in
the style of tools/mlp_oracle/plot.py (its colors, fonts and print size): one graph per image, axis labels
and a legend only, for a caption to explain.

    python tools/inst_partition/plot_ideal_split.py instpart_google_top6 instpart_google_top6_incl \\
        --out ideal_split.png

writes ideal_split_workload.png (one pick per workload) and ideal_split_trace.png (one pick per trace
file), a .csv of the counts beside each, and to stderr each series' run details and picks. Each descriptor
is one series, named in the legend by its architecture (--labels overrides); a lone series has no legend.

The candidates are the descriptor's configs named i<instruction ways>_d<data ways>; any other (lru) is not
one. A workload's IPC is sci's, from the aggregates.json ./sci --visualize writes (the geomean of its
inputs' cycle-weighted IPCs). A trace file's IPC is its simpoint's Periodic_Instructions / Periodic_Cycles
from collected_stats.csv: Google keeps one trace file per simpoint (traces/simp/<cluster>.zip), so the
script takes Google descriptors only. A unit counts only if every split finished it.
"""
import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import to_rgba  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

INFRA = Path(__file__).resolve().parents[2]
SPLIT = re.compile(r"^i(\d+)_d(\d+)$")
# tools/mlp_oracle/plot.py's THEMES; a series is styled (fill, hatch, hatch ink) like its bars, in the
# order oracle (dark solid), control (pale, stripes), ceiling (pale gray, dots).
THEMES = {
    "light": dict(surface="#ffffff", ink="#0b0b0b", ink2="#52514e", grid="#e1e0d9", base="#c3c2b7",
                  series=(("#184f95", None, None), ("#fad5c8", "////", "#ad4213"), ("#e6e4de", "....", "#5f5e59"))),
    "dark": dict(surface="#1a1a19", ink="#f0efec", ink2="#c3c2b7", grid="#2c2c2a", base="#383835",
                 series=(("#3987e5", None, None), ("#4b2c21", "////", "#eb7a50"), ("#383835", "....", "#a9a7a0"))),
}
LEVELS = {"workload": "Workloads", "trace": "Trace files"}


class Study:
    """One descriptor's splits and where its results are."""

    def __init__(self, desc):
        self.desc = desc
        d = json.loads((INFRA / "json" / f"{desc}.json").read_text())
        if any(s["suite"] != "google" for s in d["simulations"]):
            sys.exit(f"{desc}: Google descriptors only (one trace file per simpoint)")
        self.d = d
        self.arch = d["architecture"]
        self.exp = Path(d["root_dir"]) / "simulations" / d["experiment"]
        self.splits = {c: (int(m[1]), int(m[1]) + int(m[2])) for c in d["configurations"] if (m := SPLIT.match(c))}
        if not self.splits:
            sys.exit(f"{desc}: no i<n>_d<m> configs")

    def ipc(self, level):
        """{split config: {unit: IPC}}."""
        if level == "workload":
            path = self.exp / "aggregates.json"
            if not path.exists():
                sys.exit(f"{path} missing: run ./sci --visualize {self.desc}")
            agg = json.loads(path.read_text())["aggregates"]["IPC"]
            return {c: {w: v for w, v in agg[c].items() if "/" in w} for c in self.splits}
        df = pd.read_csv(self.exp / "collected_stats.csv", index_col=0, low_memory=False)
        ins, cyc = df.loc["Periodic_Instructions"], df.loc["Periodic_Cycles"]
        out = {c: {} for c in self.splits}
        for col in df.columns:
            parts = col.split(" ")
            if len(parts) == 3 and parts[0] in out and float(cyc[col]) > 0:
                out[parts[0]][f"{parts[1]} {parts[2]}"] = float(ins[col]) / float(cyc[col])
        return out

    def run_details(self):
        sims = self.d["simulations"]
        warm = sims[0].get("warmup")
        lim = re.search(r"--inst_limit (\d+)", next(iter(self.d["configurations"].values()))["params"])
        length = f"{warm / 1e6:g}M warmup + {(int(lim[1]) - warm) / 1e6:g}M measured" if warm and lim else "sci default"
        return f"{self.d['experiment']} · {self.arch} · top_simpoint {self.d['top_simpoint']} · {length}"


def picks(study, level):
    """{unit: best split config}, over the units every split finished; ties go to the fewer instruction ways."""
    ipc = study.ipc(level)
    units = set.intersection(*(set(k for k, v in vals.items() if v and math.isfinite(v)) for vals in ipc.values()))
    order = sorted(study.splits, key=lambda c: study.splits[c][0])
    return {u: max(order, key=lambda c: ipc[c][u]) for u in sorted(units)}


def figure(series, bins, assoc, level, t, a):
    """series: [(label, Counter of instruction ways)]. One graph: bars, axis labels, legend above if several."""
    fig = plt.figure(layout="constrained", figsize=(a.width, a.height), facecolor=t["surface"])
    fig.get_layout_engine().set(w_pad=2 / 72, h_pad=2 / 72)
    ax = fig.add_subplot()
    ax.set_facecolor(t["surface"])
    n = len(series)
    slot = 0.81 / n
    handles = []
    for j, ((label, counts), (fill, hatch, ink)) in enumerate(zip(series, t["series"])):
        xs = [i + (j - (n - 1) / 2) * slot for i in range(len(bins))]
        bars = ax.bar(xs, [counts.get(b, 0) for b in bins], width=slot * 0.88, color=fill, hatch=hatch,
                      linewidth=0, zorder=2)
        h = Patch(facecolor=fill, edgecolor="none", hatch=hatch, linewidth=0, label=label)
        if ink is not None:  # as plot.py: the hatch in its ink, not the (absent) edge color
            for p in list(bars) + [h]:
                p._hatch_color = to_rgba(ink)
        handles.append(h)
    top = max(max(c.values(), default=0) for _, c in series)
    ax.set_xlim(-0.6, len(bins) - 0.4)
    ax.set_ylim(0, max(top, 1) * 1.06)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xticks(range(len(bins)))
    ax.set_xticklabels([str(b) for b in bins])
    ax.set_xlabel(f"L2 instruction ways (of {assoc})", color=t["ink"], fontsize=8)
    ax.set_ylabel(LEVELS[level], color=t["ink"], fontsize=8)
    ax.axhline(0, color=t["base"], linewidth=0.75, zorder=3)
    ax.yaxis.grid(True, color=t["grid"], linewidth=0.6, linestyle="-")
    ax.set_axisbelow(True)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0, colors=t["ink2"], labelsize=8, pad=3)
    if n > 1:
        fig.legend(handles=handles, loc="outside upper center", ncol=n, frameon=False, fontsize=8,
                   handlelength=1.6, handleheight=1.1, handletextpad=0.5, columnspacing=1.2,
                   borderaxespad=0.1, borderpad=0, labelcolor=t["ink"])
    return fig


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("descriptors", nargs="+", help="sweep descriptors, one series each (at most 3)")
    p.add_argument("--levels", default="workload,trace", help="comma-separated: workload, trace (default both)")
    p.add_argument("--labels", default="", help="comma-separated legend names (default: the architectures)")
    p.add_argument("--width", type=float, default=3.3, help="figure width in inches (default 3.3, one column)")
    p.add_argument("--height", type=float, default=2.1, help="figure height in inches (default 2.1)")
    p.add_argument("--theme", choices=THEMES, default="light")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--out", default="ideal_split.png", help="image path stem: <stem>_<level>.png")
    a = p.parse_args()
    t = THEMES[a.theme]
    levels = [lv.strip() for lv in a.levels.split(",") if lv.strip()]
    if not levels or any(lv not in LEVELS for lv in levels):
        p.error("--levels takes workload and trace")
    studies = [Study(d) for d in a.descriptors]
    if len(studies) > len(t["series"]):
        p.error(f"at most {len(t['series'])} descriptors")
    labels = [s.strip() for s in a.labels.split(",")] if a.labels else [st.arch for st in studies]
    if len(labels) != len(studies):
        p.error("--labels needs one name per descriptor")
    assocs = {w for st in studies for _, w in st.splits.values()}
    if len(assocs) != 1:
        sys.exit(f"the descriptors' splits cover different associativities: {sorted(assocs)}")
    assoc = assocs.pop()
    bins = sorted({ways for st in studies for ways, _ in st.splits.values()})

    plt.rcParams.update({"font.family": "sans-serif",
                         "font.sans-serif": ["Inter", "Helvetica", "Arial", "Liberation Sans", "DejaVu Sans"],
                         "axes.unicode_minus": True, "pdf.fonttype": 42, "ps.fonttype": 42})
    out = Path(a.out)
    for level in levels:
        series, rows = [], []
        for st, label in zip(studies, labels):
            chosen = picks(st, level)
            counts = Counter(st.splits[c][0] for c in chosen.values())
            series.append((label, counts))
            rows += [{"series": label, "descriptor": st.desc, "level": level, "inst_ways": b,
                      "count": counts.get(b, 0)} for b in bins]
            print(f"{LEVELS[level]} · {label}: {st.run_details()} · {len(chosen)} units\n  "
                  + "  ".join(f"{u.split('/')[-1]}={c}" for u, c in chosen.items()), file=sys.stderr)
        path = out.with_name(f"{out.stem}_{level}{out.suffix}")
        # Squeeze the axis to the data: drop the splits at either end that no series picked.
        used = [b for b in bins if any(c.get(b, 0) for _, c in series)]
        shown = [b for b in bins if used[0] <= b <= used[-1]]
        rows = [r for r in rows if r["inst_ways"] in shown]
        fig = figure(series, shown, assoc, level, t, a)
        fig.savefig(path, dpi=a.dpi, bbox_inches="tight", pad_inches=0.02, facecolor=t["surface"])
        plt.close(fig)
        pd.DataFrame(rows).to_csv(path.with_suffix(".csv"), index=False)
        print(f"wrote {path} and {path.with_suffix('.csv')}", file=sys.stderr)


if __name__ == "__main__":
    main()
