#!/usr/bin/env python3
"""Plot a full sweep's geomean IPC gain over LRU per suite and policy, as a paper figure: one graph per image,
cropped tight, with axis labels and a legend and no other text but the values of bars the axis cuts (no title),
for a caption.

    tools/fullsweep/plot.py fs_cd1_small --by suite --ymax 6 --out gain_by_suite.png
    tools/fullsweep/plot.py fs_cd1_small --out gain_by_policy.pdf

The descriptor is the sweep's merged one (json/<name>.json); its experiment's collected_stats.csv
(fullsweep.py collect) has the stats. The extension of --out picks the format: .pdf gives a vector figure.

Policies (configs) are plotted in DEFAULT_CONFIGS' order: Catalyst (REPL_MLP), its ablations, then the prior
policies. A config of the descriptor that the list lacks is left out and named on stderr. --configs picks
others, in its order; --order geomean sorts by the all-suite geomean, best first.

--by policy (default): one group per policy, with a bar per suite, then the all-suite geomean (the dark solid
bar). --by suite: one group per suite, then a geomean group, with a bar per policy (Catalyst is the dark solid
bar).

--ymax cuts the y axis: a bar it cuts ends there under a break mark, with its value standing on top (rotated,
so neighbors' values never collide). The values also go to stderr and the CSV's cut column.

Method, as ./sci --visualize: a workload's IPC is its simpoints' weighted measured instructions over their
weighted measured cycles (Periodic_Instructions / Periodic_Cycles), over the simpoints that finished under
every plotted policy and the baseline (others are dropped and counted). Gains are over the baseline
(--baseline, default lru). The geomeans nest as sci's do (scarab_stats/aggregation.py): a benchmark's inputs
(gcc, gcc_2, ...) first, then the suite's benchmarks, then the suites for the all-suite geomean.

The bars read in color, under color blindness and in black and white. Besides the solid bar, every series has
a texture (HATCHES, in turn: past 7 series they repeat, 7 series apart) in dark ink on a pale tint. Each suite
and policy has its own tint (TINT_OF; Catalyst's ablations share its blue, the EMISSARY and Mockingjay variants
theirs), so a color means the same thing in every figure, and in the default order two series that share a
texture differ in color. See THEMES.

The figure is drawn at its print size (--width, --height in inches; default two columns, 7 x 2.2), so its
8 pt text prints at 8 pt. stdout gets the gain table, stderr the run details for the caption. Beside the
image goes a .csv of the same name with every plotted value: the chart's table view.
"""
import argparse
import io
import itertools
import json
import math
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INFRA = HERE.parent.parent
SCARABINFRA_PY = Path.home() / "miniconda3" / "envs" / "scarabinfra" / "bin" / "python"
if (SCARABINFRA_PY.exists() and Path(sys.prefix).resolve() != SCARABINFRA_PY.parent.parent.resolve()
        and not os.environ.get("FULLSWEEP_NO_REEXEC")):
    os.execv(str(SCARABINFRA_PY), [str(SCARABINFRA_PY), str(Path(__file__).resolve()), *sys.argv[1:]])

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import to_rgba  # noqa: E402
from matplotlib.patches import Patch, Polygon  # noqa: E402
from matplotlib.patheffects import withStroke  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402
from matplotlib.transforms import ScaledTranslation  # noqa: E402

sys.path.insert(0, str(INFRA))
from scarab_stats.aggregation import Hierarchy  # noqa: E402

META = ["Configuration", "Workload", "Cluster Id", "Weight", "Collect Failed", "Periodic_Instructions",
        "Periodic_Cycles"]
SUITES = {"google": "Google", "datacenter": "Datacenter", "spec2006": "SPEC06", "spec2017": "SPEC17",
          "spec2026": "SPEC26"}  # display names, in plot order; other suites follow under their own names
GEOMEAN = "Geomean"
# mockingjay_p1 is Mockingjay with --mockingjay_pf_penalty 1, i.e. without Flex-MIN's prefetch penalty (a reuse
# interval ending in a prefetch trains at its own length, not 2x); mockingjay keeps the artifact's 2x.
NAMES = {"lru": "LRU", "drrip": "DRRIP", "shippp": "SHiP++", "mockingjay": "Mockingjay (PF penalty)",
         "mockingjay_p1": "Mockingjay", "emissary": "EMISSARY", "emissary_r1": "EMISSARY r=1",
         "icarus": "ICARUS", "lacs": "LACS", "clip": "CLIP", "mlp_core": "Catalyst", "mlp_gd": "Catalyst D-only",
         "mlp_gi": "Catalyst I-only", "mlp_sp": "Catalyst per-SP", "mlp_wl": "Catalyst per-WL"}
# The policies plotted by default, in order (those the descriptor has).
DEFAULT_CONFIGS = ["mlp_core", "mlp_sp", "mlp_gd", "mlp_gi", "mlp_wl", "drrip", "shippp", "mockingjay_p1", "lacs",
                   "clip", "emissary", "icarus"]
# Configs with one of these prefixes share a tint; the first config of Catalyst's (mlp_) is the solid bar.
FAMILIES = ["mlp_", "emissary", "mockingjay"]
# Each suite's and policy's tint (THEMES), so a color means the same thing in every figure. Chosen so the policies
# that share a texture in the default --by suite order (7 apart: Catalyst's per-SP, D-only and I-only against CLIP,
# EMISSARY and ICARUS) differ in hue, and neighbors do too. Others take the unused tints in turn.
TINT_OF = {"google": "orange", "datacenter": "aqua", "spec2006": "yellow", "spec2017": "magenta", "spec2026": "violet",
           "mlp_": "blue", "drrip": "orange", "shippp": "aqua", "mockingjay": "violet", "lacs": "yellow",
           "clip": "magenta", "emissary": "gray", "icarus": "red"}
HATCHES = ["////", "....", "\\\\\\\\", "|||||", "xxx", "-----", "oo"]
# Tints are (pale fill, dark hatch ink); `solid` is the one dark solid bar.
THEMES = {
    "light": dict(surface="#ffffff", ink="#0b0b0b", ink2="#52514e", grid="#e1e0d9", base="#c3c2b7", solid="#184f95",
                  tints=dict(orange=("#fad5c8", "#ad4213"), aqua=("#c6ecdc", "#0b7350"),
                             yellow=("#fbe4ad", "#865a00"), magenta=("#f8d3e1", "#a3335e"),
                             violet=("#dcd7f7", "#4a3aa7"), gray=("#e6e4de", "#52514e"),
                             red=("#f9d2d1", "#a8302f"), blue=("#cde2fb", "#184f95"))),
    "dark": dict(surface="#1a1a19", ink="#f0efec", ink2="#c3c2b7", grid="#2c2c2a", base="#383835", solid="#3987e5",
                 tints=dict(orange=("#4b2c21", "#eb7a50"), aqua=("#173a2e", "#3dc996"),
                            yellow=("#43341a", "#e0a530"), magenta=("#45253a", "#e584ab"),
                            violet=("#2c2850", "#9a8ff0"), gray=("#383835", "#c3c2b7"),
                            red=("#4a2424", "#ec7473"), blue=("#1c2f4a", "#86b6ef"))),
}
YLABEL = "IPC gain over LRU (%)"


def load_descriptor(name):
    path = Path(name) if name.endswith(".json") else INFRA / "json" / f"{name}.json"
    d = json.loads(path.read_text())
    d["_name"] = path.stem
    return d


def read_rows(desc):
    """One row per (config, simpoint) of the experiment's collected_stats.csv, parsing only the rows used."""
    csv = Path(desc["root_dir"]) / "simulations" / desc["experiment"] / "collected_stats.csv"
    if not csv.is_file():
        sys.exit(f"no {csv}: run tools/fullsweep/fullsweep.py collect {desc['experiment']}")
    wanted = {s + "," for s in META}
    with open(csv) as f:
        lines = [next(f)] + [line for line in f if line[:line.find(",") + 1] in wanted]
    t = pd.read_csv(io.StringIO("".join(lines)), low_memory=False).set_index("stats")
    t = t[~t.index.duplicated(keep="first")].drop(columns=["write_protect", "groups"], errors="ignore").T
    num = lambda s: pd.to_numeric(t[s], errors="coerce")  # noqa: E731
    df = pd.DataFrame({"config": t["Configuration"].values, "label": t["Workload"].astype(str).values,
                       "cluster": t["Cluster Id"].astype(str).values,
                       "weight": num("Weight").fillna(1.0).values,
                       "failed": t["Collect Failed"].astype(str).str.strip().str.lower().eq("true").values,
                       "inst": num("Periodic_Instructions").values, "cycles": num("Periodic_Cycles").values})
    ipc = df["inst"] / df["cycles"]
    df["ok"] = ~df["failed"] & ipc.gt(0) & ipc.apply(math.isfinite)
    return df, csv


def pct(x):
    return (x - 1.0) * 100.0 if x is not None and math.isfinite(x) else math.nan


def gains(df, configs, base):
    """(table, rows, hierarchy, dropped): table is config x (suites..., GEOMEAN) of nested-geomean gains over
    base in %, over the simpoints every one of configs + base finished."""
    used = list(dict.fromkeys(configs + [base]))
    rows = df[df["config"].isin(used)]
    done = rows[rows["ok"]].groupby(["label", "cluster"])["config"].nunique()
    common = set(done[done == len(used)].index)
    every = set(zip(rows["label"], rows["cluster"]))
    rows = rows[[k in common for k in zip(rows["label"], rows["cluster"])]]
    by = [rows["config"], rows["label"]]
    ipc = ((rows["inst"] * rows["weight"]).groupby(by).sum()
           / (rows["cycles"] * rows["weight"]).groupby(by).sum()).unstack(0)
    h = Hierarchy(ipc.index)
    order = [s for s in SUITES if s in h.suites] + sorted(s for s in h.suites if s not in SUITES)
    table = {}
    for c in configs:
        agg = h.aggregate((ipc[c] / ipc[base]).to_dict(), geometric=True)
        table[c] = {**{s: pct(agg["suites"][s]) for s in order}, GEOMEAN: pct(agg["Avg"])}
    return pd.DataFrame(table).T[order + [GEOMEAN]], rows, h, len(every - common)


def run_lengths(desc, base, suite):
    """'50M warmup + 200M measured' per distinct length among the suite's simulations, from the descriptor
    (warmup, --inst_limit of the baseline's params and its suite-level workload_params) and workloads_db."""
    db = json.loads((INFRA / "workloads" / "workloads_db.json").read_text())
    cfg = desc["configurations"][base]
    limits = re.findall(r"--inst_limit[ =](\d+)", cfg.get("params", "") + " "
                        + (cfg.get("workload_params") or {}).get(suite, ""))
    out = set()
    for s in desc["simulations"]:
        if s.get("suite") != suite:
            continue
        sim = db.get(suite, {}).get(s.get("subsuite"), {}).get(s.get("workload"), {}).get("simulation", {})
        mode = sim.get(sim.get("prioritized_mode", ""), {}) or {}
        warm = s.get("warmup") if s.get("warmup") is not None else mode.get("warmup") or 0
        measured = int(limits[-1]) - warm if limits else mode.get("segment_size")
        out.add(f"{warm / 1e6:g}M warmup + {measured / 1e6:g}M measured" if measured else f"{warm / 1e6:g}M warmup")
    return " / ".join(sorted(out))


def details(desc, base, configs, rows, h, dropped, table):
    """The run details for the caption, one line per suite."""
    cfgs = desc["configurations"]
    bins = {}
    for c in [base] + configs:
        bins.setdefault(cfgs[c].get("binary", "?"), []).append(c)
    main = max(bins, key=lambda b: len(bins[b]))
    other = "".join(f"; {', '.join(cs)} on {b}" for b, cs in bins.items() if b != main)
    lines = [f"{desc['_name']} · {desc.get('architecture', '?')} · {main}{other} · gains over {base}"
             + (f" · {dropped} simpoints dropped (unfinished under some config)" if dropped else "")]
    sp = rows[rows["config"] == base].groupby(rows["label"].str.split("/").str[0]).size()
    for s in table.columns[:-1]:
        n_wl, n_in = len(h.suites[s]), sum(len(h.workloads[w]) for w in h.suites[s])
        count = f"{n_wl} workloads" + (f" ({n_in} inputs)" if n_in != n_wl else "")
        lines.append(f"  {SUITES.get(s, s)}: {count}, {sp.get(s, 0)} simpoints, {run_lengths(desc, base, s)}")
    lines.append(f"  {GEOMEAN}: geomean over the {len(table.columns) - 1} suites (inputs -> benchmark -> suite -> all)")
    return "\n".join(lines)


def family(config):
    return next((f for f in FAMILIES if config.startswith(f)), config)


def series_styles(t, keys, solid=None):
    """(fill, hatch, ink) per series, given each series' tint key (TINT_OF; an unknown key takes the next unused
    tint). Series solid (an index, or None) is the dark solid bar; the others take HATCHES in turn."""
    solid = None if solid is None else solid % len(keys)
    used = {TINT_OF[k] for k in keys if k in TINT_OF}
    spare = itertools.cycle([c for c in t["tints"] if c not in used and c != "blue"] or list(t["tints"]))
    tint, out, textured = {}, [], 0
    for i, key in enumerate(keys):
        if i == solid:
            out.append((t["solid"], None, None))
            continue
        if key not in tint:
            tint[key] = TINT_OF.get(key) or next(spare)
        fill, ink = t["tints"][tint[key]]
        out.append((fill, HATCHES[textured % len(HATCHES)], ink))
        textured += 1
    return out


def label_rotation(ax, renderer, gap):
    """Degrees to turn the x tick labels so neighbors clear each other by gap pixels: 0 when they fit flat,
    else the least of 30, 45 and 60 at which parallel labels at the tick spacing do, else 90."""
    boxes = sorted((lab.get_window_extent(renderer) for lab in ax.get_xticklabels() if lab.get_text()),
                   key=lambda b: b.x0)
    if all(b.x0 - a.x1 >= gap for a, b in zip(boxes, boxes[1:])):
        return 0
    xs = sorted(ax.transData.transform([(x, 0) for x in ax.get_xticks()])[:, 0])
    dx, h = min(b - a for a, b in zip(xs, xs[1:])), max(b.height for b in boxes)
    return next((deg for deg in (30, 45, 60) if dx * math.sin(math.radians(deg)) >= h + gap), 90)


def swatch(style, label):
    fill, hatch, ink = style
    p = Patch(facecolor=fill, edgecolor="none", hatch=hatch, linewidth=0, label=label)
    if ink is not None:
        p._hatch_color = to_rgba(ink)  # Patch ties the hatch to the edge color, and bars have no edge
    return p


def headroom(fig, ax, labels):
    """Raise the y axis's top until every value label fits under it, 1px clear."""
    for _ in range(6):  # the axes' height shifts a little with its ticks: settle
        fig.draw_without_rendering()
        renderer = fig.canvas.get_renderer()
        lo, hi = ax.get_ylim()
        h, px = ax.bbox.height, fig.dpi / 96
        need = hi
        for lab in labels:
            v = lab.xy[1]
            rise = lab.get_window_extent(renderer).y1 - ax.transData.transform((0, v))[1]
            if rise + px < h:
                need = max(need, lo + (v - lo) * h / (h - rise - px))
        if need <= hi * (1 + 1e-9):
            return
        ax.set_ylim(lo, need)


def figure(groups, series, values, styles, t, a, sep_last=False):
    """One graph of grouped bars: groups along x, a bar per series in each, values[(group, series)] in %; with
    sep_last, the last group (the geomean) is set off by a rule in the gap before it. Bars are plain rectangles
    with a 2px surface gap between neighbors. A bar past a.ymax ends there under a break mark, with its value on
    top. Returns (fig, cut bars as (group, series, value))."""
    fig = plt.figure(layout="constrained", figsize=(a.width, a.height), facecolor=t["surface"])
    fig.get_layout_engine().set(w_pad=2 / 72, h_pad=2 / 72)
    ax = fig.add_subplot()
    ax.set_facecolor(t["surface"])
    n = len(series)
    span = min(0.9, 0.7 + 0.02 * n)  # a group fills more of its width the more bars it has
    slot = span / n
    xs = list(range(len(groups)))
    bars, cut = [], []
    for x, g in zip(xs, groups):
        for i, (s, (fill, hatch, ink)) in enumerate(zip(series, styles)):
            v = values.get((g, s), math.nan)
            if not math.isfinite(v):
                continue
            shown = min(v, a.ymax) if a.ymax is not None else v
            (r,) = ax.bar(x + (i - (n - 1) / 2) * slot, shown, width=slot, facecolor=fill, edgecolor="none",
                          hatch=hatch, linewidth=0, zorder=2)
            if ink is not None:
                r._hatch_color = to_rgba(ink)
            bars.append(r)
            if shown != v:
                cut.append((g, s, v, r))
    vals = [v for v in values.values() if math.isfinite(v)]
    lo, hi = min([0.0] + vals), max([0.0] + vals)
    if a.ymax is not None:
        hi = min(hi, a.ymax)
    ax.set_xlim(xs[0] - span / 2 - 0.1, xs[-1] + span / 2 + 0.1)
    ax.set_ylim(lo * 1.06 if lo < 0 else 0, hi * 1.06 if hi > 0 else 0.5)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=8, steps=[1, 2, 5, 10]))
    ticks = ax.get_yticks()
    # A negative side with no gridline goes down to the one under the lowest bar (unless that bar barely dips).
    if lo < 0 and -lo > 0.05 * (hi - lo) and not any(lo * 1.06 <= v < 0 for v in ticks):
        ax.set_ylim(bottom=max((v for v in ticks if v <= lo * 1.06), default=lo * 1.06))
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:g}".replace("-", "−"))
    if sep_last:
        ax.axvline((xs[-2] + xs[-1]) / 2, color=t["grid"], linewidth=0.75, zorder=1)
    ax.axhline(0, color=t["base"], linewidth=0.75, zorder=3)
    ax.set_xticks(xs)
    ax.set_xticklabels(groups)
    ax.set_ylabel(YLABEL, color=t["ink"], fontsize=8)
    ax.yaxis.grid(True, color=t["grid"], linewidth=0.6, linestyle="-")
    ax.set_axisbelow(True)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0, colors=t["ink2"], labelsize=8, pad=3)
    # A cut bar's value on its top, standing up so neighbors' never collide, with a surface halo so gridlines
    # stop short of it.
    labels = [ax.annotate(f"{v:.1f}", xy=(r.get_x() + r.get_width() / 2, r.get_height()), xytext=(0, 1.5),
                          textcoords="offset points", rotation=90, ha="center", va="bottom", fontsize=7,
                          color=t["ink"], zorder=4, annotation_clip=False,
                          path_effects=[withStroke(linewidth=1.5, foreground=t["surface"])])
              for _, _, v, r in cut]
    for lab in labels:
        lab.set_in_layout(False)
    handles = [swatch(st, s) for s, st in zip(series, styles)]

    def legend(rows):
        """The legend in rows, filled row by row (matplotlib fills it column by column)."""
        ncol = math.ceil(len(handles) / rows)
        order = [r * ncol + c for c in range(ncol) for r in range(rows) if r * ncol + c < len(handles)]
        return fig.legend(handles=[handles[i] for i in order], loc="outside upper center", ncol=ncol, frameon=False,
                          fontsize=8, handlelength=1.6, handleheight=1.1, handletextpad=0.5, columnspacing=1.2,
                          borderaxespad=0.1, borderpad=0, labelcolor=t["ink"])

    renderer = fig.canvas.get_renderer()
    for rows in range(1, len(handles) + 1):  # as few rows as fit the figure's width
        leg = legend(rows)
        fig.draw_without_rendering()
        if leg.get_window_extent(renderer).width <= fig.bbox.width or rows == len(handles):
            break
        leg.remove()
    deg = label_rotation(ax, renderer, renderer.points_to_pixels(3))
    if deg:
        for lab in ax.get_xticklabels():
            lab.set(rotation=deg, ha="right", va="center", rotation_mode="anchor")
    headroom(fig, ax, labels)
    fig.draw_without_rendering()  # the axes' final size, for the gap, the break marks and the labels
    x0, x1 = ax.get_xlim()
    inset = (fig.dpi / 96) * (x1 - x0) / ax.bbox.width  # 1 CSS px (1/96 in) in data units
    for r in bars:
        r.set_x(r.get_x() + inset)
        r.set_width(max(r.get_width() - 2 * inset, 0))
    # A break mark across each cut bar: a slanted surface-colored band 8px below its end, reaching 2px past its
    # sides. Drawn in inches from the bar's top center, so it holds at any dpi.
    px, per_in = 1 / 96, (x1 - x0) / (ax.bbox.width / fig.dpi)  # 1 CSS px in inches; data units per inch
    for _, _, _, r in cut:
        half, y, slant, thick = r.get_width() / per_in / 2 + 2 * px, -8 * px, 2.5 * px, 2.5 * px
        at = ScaledTranslation(r.get_x() + r.get_width() / 2, r.get_height(), ax.transData)
        ax.add_artist(Polygon([(-half, y - slant), (half, y + slant), (half, y + slant + thick),
                               (-half, y - slant + thick)], closed=True, transform=fig.dpi_scale_trans + at,
                              facecolor=t["surface"], edgecolor="none", zorder=2.5, clip_on=False))
    return fig, [(g, s, v) for g, s, v, _ in cut]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("descriptor", help="the sweep's merged descriptor (json/<name>.json, given as <name>)")
    p.add_argument("--by", choices=("policy", "suite"), default="policy",
                   help="group bars by policy, a bar per suite (default), or by suite, a bar per policy")
    p.add_argument("--configs", default="", help="comma-separated policies (configs) to plot, in order; default: "
                   "DEFAULT_CONFIGS that the descriptor has")
    p.add_argument("--baseline", default="lru", help="the config gains are over (default lru)")
    p.add_argument("--order", choices=("given", "geomean"), default="given",
                   help="policy order: as listed (default), or by all-suite geomean, best first")
    p.add_argument("--names", default="", help="display names, e.g. mlp_core=Ours,drrip=DRRIP (defaults: NAMES)")
    p.add_argument("--ymax", type=float, help="cut the y axis here (in %%); a bar it cuts gets a break mark and "
                   "its value on top")
    p.add_argument("--width", type=float, default=7.0, help="figure width in inches (default 7, two columns)")
    p.add_argument("--height", type=float, default=2.2, help="figure height in inches (default 2.2)")
    p.add_argument("--theme", choices=THEMES, default="light")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--out", help="image path (default <descriptor>[_by_suite].png)")
    a = p.parse_args()
    t = THEMES[a.theme]
    desc = load_descriptor(a.descriptor)
    names = dict(NAMES, **dict(kv.split("=", 1) for kv in a.names.split(",") if "=" in kv))
    configs = [c.strip() for c in a.configs.split(",") if c.strip()]
    if not configs:
        configs = [c for c in DEFAULT_CONFIGS if c in desc["configurations"] and c != a.baseline]
        left = [c for c in desc["configurations"] if c not in configs and c != a.baseline]
        if left:
            print(f"not plotted (add with --configs): {', '.join(left)}", file=sys.stderr)
    missing = [c for c in configs + [a.baseline] if c not in desc["configurations"]]
    if missing:
        sys.exit(f"{desc['_name']} has no config {', '.join(missing)}; it has {', '.join(desc['configurations'])}")

    df, csv = read_rows(desc)
    absent = sorted(set(configs + [a.baseline]) - set(df["config"]))
    if absent:
        sys.exit(f"{csv} has no results for {', '.join(absent)}")
    table, rows, h, dropped = gains(df, configs, a.baseline)
    if a.order == "geomean":
        table = table.sort_values(GEOMEAN, ascending=False, kind="stable")
    suites = [SUITES.get(s, s) for s in table.columns[:-1]]
    shown = table.rename(index=lambda c: names.get(c, c), columns=lambda s: SUITES.get(s, s))

    plt.rcParams.update({"font.family": "sans-serif",
                         "font.sans-serif": ["Inter", "Helvetica", "Arial", "Liberation Sans", "DejaVu Sans"],
                         "axes.unicode_minus": True, "pdf.fonttype": 42, "ps.fonttype": 42})
    if a.by == "policy":
        groups, series, sep = list(shown.index), suites + [GEOMEAN], False
        styles = series_styles(t, list(table.columns[:-1]) + [GEOMEAN], solid=-1)  # the geomean
        values = {(g, s): shown.at[g, s] for g in groups for s in series}
    else:
        groups, series, sep = suites + [GEOMEAN], list(shown.index), True
        keys = [family(c) for c in table.index]
        styles = series_styles(t, keys, solid=keys.index(FAMILIES[0]) if FAMILIES[0] in keys else None)
        values = {(g, s): shown.at[s, g] for g in groups for s in series}
    out = Path(a.out or f"{desc['_name']}{'_by_suite' if a.by == 'suite' else ''}.png")
    fig, cut = figure(groups, series, values, styles, t, a, sep_last=sep)
    fig.savefig(out, dpi=a.dpi, bbox_inches="tight", pad_inches=0.02, facecolor=t["surface"])
    plt.close(fig)
    long = shown.reset_index(names="policy").melt(id_vars="policy", var_name="suite", value_name="gain_pct")
    long.insert(1, "config", [c for c in table.index] * len(shown.columns))
    cutset = {(s, g) if a.by == "suite" else (g, s) for g, s, _ in cut}  # (policy, suite)
    long["cut"] = [(p_, s) in cutset for p_, s in zip(long["policy"], long["suite"])]
    long.round({"gain_pct": 4}).to_csv(out.with_suffix(".csv"), index=False)
    print(f"wrote {out} and {out.with_suffix('.csv')}\n" + details(desc, a.baseline, configs, rows, h, dropped,
                                                                    table), file=sys.stderr)
    for g, s, v in cut:
        print(f"  cut at {a.ymax:g}: {g} {s} {v:+.2f}", file=sys.stderr)
    print(f"IPC gain over {names.get(a.baseline, a.baseline)} (%), nested geomean\n"
          + shown.to_string(float_format=lambda v: f"{v:+.2f}"))


if __name__ == "__main__":
    main()
