#!/usr/bin/env python3
"""Plot mlp_oracle bounds per workload: the ceiling, oracle and control gains over LRU, with the control
of any extra seed beside its seed-0 control, as paper figures: one graph per image, cropped tight, with
axis labels, a legend and a factor bracket per group and no other text (no title, no value labels), for a
caption to explain.

    python tools/mlp_oracle/plot.py mlp_oracle_google_defmark mlp_oracle_spec2006_defmark \\
        --seeds mlp_oracle_google_defmark_seed1 mlp_oracle_spec2006_defmark_seed1 --sides d,b --out oracle.png

That writes one image per study and side (--sides i,d,b): oracle_google_defmark_d.png,
oracle_google_defmark_b.png, ... (the --out stem, the study without its mlp_oracle_ prefix, the side). With
a single study and side the image is --out itself. The extension picks the format: .pdf gives a vector
figure. A graph has one group per workload (ceiling, oracle and control bars, then a bar per extra
control seed) and ends with a geomean group. --geomean picks it: suite (default) is the suite's geomean
as report.py computes it (nested: inputs, then benchmark, then suite) over every workload of the study,
not only the plotted ones; plotted is the same over the plotted workloads only (with the extra seeds'
controls when every plotted workload has them); off leaves it out.

--metric ipc plots absolute IPC instead of the gain over LRU, with LRU as a bar of its own (first in each
group); a geomean group is then the geomean of each config's IPC, so its ratios to LRU are the gain
geomeans. The factor brackets and the stdout gap table stay in gains over LRU.

Each group has a bracket from its oracle bar to its (seed-0) control bar, labeled with the factor: the
oracle's gain over the control's, e.g. 3.4× (one decimal, whole from 10×; none unless both gain). The y
axis rises as far as the labels need. --bracket vertical stands it up instead: from the shorter bar's top
to the taller's, with the factor beside it (a group whose bars' tops are too close for that keeps the
horizontal one); --bracket none leaves it out. --side-label names the graph's side in a box in the
plot's top right corner (Instructions, Data, Instructions & Data); the y axis rises until no bar or
bracket reaches under it.

The figure is drawn at its print size (--width, --height, in inches; default about a single column), so
its 8 pt text prints at 8 pt. --ymax cuts the axis: a bar it cuts gets a break mark, and its value goes to
the CSV and to stderr. stderr also gets each graph's run details and geomeans, for the caption.

The bars read in color, under color blindness and in black and white: the ceiling is light and solid, the
oracle dark and solid, the controls striped (dark stripes on a pale fill; 45° for seed 0, 135° for extra
seeds). See THEMES.

stdout gets each graph's gap table, per plotted workload and for the suite geomean, against each seed's
control: the gap (oracle gain minus control gain, in points) and the factor (oracle gain over control gain).

The gains come from report.py's Study, so they match its reports: a workload's IPC is its simpoints'
weighted instructions over weighted cycles, over the simpoints every config of the descriptor finished.

--seeds takes descriptors of extra control draws (controls run with --mlp_oracle_seed N against the
study's reference, plus lru; see the README). Each goes with the study of the same suite and core, and its
lru must equal the study's: the script checks, and refuses a mismatch, since then the two runs are not the
same experiment.

--workloads picks the workloads to plot (names as report.py prints them; a name a study lacks is skipped
there). Default: the suite's DEFAULT_WORKLOADS (Google: arizona, sierra.a.4, tahoe), else the study's
workloads with an extra seed, else all of them. --order sorts them: alphabetically (default; digit runs as
numbers), as given (--workloads' order, or the default list's), or by the both-sides ceiling, largest first.

Beside each image it writes a .csv of the same name with every plotted value: the chart's table view.
"""
import argparse
import math
import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.artist import Artist  # noqa: E402
from matplotlib.colors import to_rgba  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.text import Text  # noqa: E402
from matplotlib.path import Path as MPath  # noqa: E402
from matplotlib.transforms import IdentityTransform  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402

# Bar styles, chosen to read in color, under color blindness and printed in black and white: every series has
# its own texture and its own tone, so no two look alike in grayscale.
#   ceiling   pale gray #e6e4de, #5f5e59 dots            (context, so recessive)
#   oracle    blue #184f95, solid                        (blue ramp step 600; the one dark solid bar)
#   control   pale orange #fad5c8, #ad4213 stripes at 45° (dark ink on a pale fill: the stripes survive a
#                                                         small print and never blur into a flat gray)
#   seed N    the same, stripes at 135°                  (the control again)
#   lru       light gray #d3d1c9, #52514e vertical lines (--metric ipc only: the baseline)
# In color it is blue against orange (categorical slots 1 and 2, CVD-safe) and a gray. Checked by eye in
# grayscale and as deuteranopes and protanopes see it (Machado 2009 at full severity).
# A style is (fill, hatch, hatch ink).
THEMES = {
    "light": dict(surface="#ffffff", ink="#0b0b0b", ink2="#52514e", muted="#898781", grid="#e1e0d9", base="#c3c2b7",
                  bars=dict(lru=("#d3d1c9", "|||", "#52514e"), ceiling=("#e6e4de", "....", "#5f5e59"),
                            oracle=("#184f95", None, None), control=("#fad5c8", "////", "#ad4213")),
                  seeds=(("#fad5c8", "\\\\\\\\", "#ad4213"),)),
    "dark": dict(surface="#1a1a19", ink="#f0efec", ink2="#c3c2b7", muted="#898781", grid="#2c2c2a", base="#383835",
                 bars=dict(lru=("#2c2c2a", "|||", "#a9a7a0"), ceiling=("#383835", "....", "#a9a7a0"),
                           oracle=("#3987e5", None, None), control=("#4b2c21", "////", "#eb7a50")),
                 seeds=(("#4b2c21", "\\\\\\\\", "#eb7a50"),)),
}
# Workloads a suite's graphs show by default (else: those with an extra seed, else all).
DEFAULT_WORKLOADS = {"google": ("arizona", "sierra.a.4", "tahoe")}  # data, both, instruction: see README §7
SUITE_NAME = {"google": "Google", "spec2006": "SPEC CPU2006", "spec2017": "SPEC CPU2017",
              "spec2026": "SPEC CPU2026", "datacenter": "Datacenter"}
SIDE_TITLE = {"i": "instruction side", "d": "data side", "b": "both sides"}
SIDE_LABEL = {"i": "Instructions", "d": "Data", "b": "Instructions & Data"}  # --side-label's box
MEASURES = ("ceiling", "oracle", "control")
ORDER = ("lru",) + MEASURES  # a group's bars, left to right, before any extra seeds
YLABEL = {"gain": "IPC gain over LRU (%)", "ipc": "IPC"}


def css_px(renderer):
    """One CSS pixel (1/96 in) in device pixels."""
    return renderer.points_to_pixels(72 / 96)


def slot_center(ax, renderer, x, i, n, slot):
    """Display x of slot i (of n) in the group centered at data x: slots are slot data units wide, at
    most 24px, and touch, so a group's bars stay together however wide the panel is."""
    to = ax.transData.transform
    w = min(to((x + slot, 0))[0] - to((x, 0))[0], 24 * css_px(renderer))
    return to((x, 0))[0] + (i - (n - 1) / 2) * w, w


class Bar(Patch):
    """A rectangular bar laid out in display space at draw time, so its specs hold at any size and dpi: slot i
    of a group of n at data x (see slot_center), inset 1px a side (a 2px surface gap between neighbors). A
    hatch is drawn in hatch_color (Patch ties the hatch to the edge color, and bars have no edge)."""

    def __init__(self, ax, x, i, n, slot, value, hatch_color=None, **kw):
        super().__init__(**kw)
        self.ax, self.x, self.i, self.n, self.slot, self.value = ax, x, i, n, slot, value
        self.set_transform(IdentityTransform())
        self.set_in_layout(False)
        self._path = MPath(np.zeros((1, 2)))
        if hatch_color is not None:
            self._hatch_color = to_rgba(hatch_color)

    def get_path(self):
        return self._path

    def draw(self, renderer):
        px = css_px(renderer)
        cx, w = slot_center(self.ax, renderer, self.x, self.i, self.n, self.slot)
        w -= 2 * px
        y0, y1 = (self.ax.transData.transform((self.x, v))[1] for v in (0.0, self.value))
        h = y1 - y0
        if w <= 0 or abs(h) < 0.5:
            return
        left, right = cx - w / 2, cx + w / 2
        self._path = MPath([(left, y0), (left, y1), (right, y1), (right, y0), (left, y0)],
                           [MPath.MOVETO, MPath.LINETO, MPath.LINETO, MPath.LINETO, MPath.CLOSEPOLY])
        super().draw(renderer)


class BarBreak(Patch):
    """A break mark across a Bar the axis cuts short: a slanted surface-colored band 8px below its end,
    reaching 2px past its sides, so the bar reads as continuing past the axis."""

    def __init__(self, ax, x, i, n, slot, value, **kw):
        super().__init__(**kw)
        self.ax, self.x, self.i, self.n, self.slot, self.value = ax, x, i, n, slot, value
        self.set_transform(IdentityTransform())
        self.set_in_layout(False)
        self._path = MPath(np.zeros((1, 2)))

    def get_path(self):
        return self._path

    def draw(self, renderer):
        px = css_px(renderer)
        cx, w = slot_center(self.ax, renderer, self.x, self.i, self.n, self.slot)
        y = self.ax.transData.transform((self.x, self.value))[1] - 8 * px
        half, slant, thick = w / 2 + px, 2.5 * px, 2.5 * px
        left, right = cx - half, cx + half
        self._path = MPath([(left, y - slant), (right, y + slant), (right, y + slant + thick),
                            (left, y - slant + thick), (left, y - slant)],
                           [MPath.MOVETO, MPath.LINETO, MPath.LINETO, MPath.LINETO, MPath.CLOSEPOLY])
        super().draw(renderer)


def factor_label(oracle, control):
    """The factor (oracle gain over control gain) as a bracket shows it, e.g. '3.4×'; None unless both gain."""
    if not (oracle > 0 and control > 0):
        return None
    f = oracle / control
    return f"{f:.1f}×" if f < 9.95 else f"{f:.0f}×"


class FactorBracket(Artist):
    """A bracket between the bars in slots a (the oracle) and b (the control), labeled with the factor, laid out
    in display space at draw time like Bar (tops: (slot, value) of the group's bars), its label on a patch of
    the surface so gridlines stop short of it.

    horizontal: from a's center to b's, its 3px legs ending 2px above the tallest bar under the bracket or its
        label, the label 1.5px above it.
    vertical: from the shorter bar's top to the taller's, a spine in the shorter bar's slot 6px from the taller
        bar, its 3px legs pointing at it; the label beside the spine, on the far side, centered on it and kept
        2px above any bar under it. Where the bars' tops are under 6px apart (too short a bracket to read) or
        the label cannot sit beside it (a bar on the far side reaching past the bracket's top, as the ceiling
        does when the oracle is the shorter bar), the group gets the horizontal bracket instead.

    After a draw, base and rise say what bounds its top: a data value and the pixels the label's top reaches
    above it (constant as the axis scales), for headroom() to make room for it."""

    def __init__(self, ax, x, a, b, n, slot, tops, label, t, orient="horizontal"):
        super().__init__()
        self.ax, self.x, self.a, self.b, self.n, self.slot, self.tops = ax, x, a, b, n, slot, tops
        self.orient = orient
        self.set_in_layout(False)
        self.set_zorder(4)  # over the grid and the bars
        self.line = Line2D([], [], color=t["ink"], linewidth=0.6, solid_joinstyle="miter", solid_capstyle="butt",
                           transform=IdentityTransform(), clip_on=False)
        self.text = Text(0, 0, label, color=t["ink"], fontsize=7, transform=IdentityTransform(), clip_on=False,
                         bbox=dict(boxstyle="square,pad=0.1", facecolor=t["surface"], edgecolor="none"))
        self.base, self.rise, self.xr = 0.0, 0.0, (0.0, 0.0)

    def set_figure(self, fig):
        super().set_figure(fig)
        self.line.set_figure(fig)
        self.text.set_figure(fig)

    def center(self, renderer, i):
        return slot_center(self.ax, renderer, self.x, i, self.n, self.slot)

    def top_under(self, renderer, left, right, also=()):
        """The tallest of the bars in slots also and of those whose slot overlaps [left, right] (display x)."""
        w = self.center(renderer, 0)[1]
        return max(v for i, v in self.tops
                   if i in also or abs(self.center(renderer, i)[0] - (left + right) / 2) < (w + right - left) / 2)

    def label_size(self, renderer):
        e = self.text.get_window_extent(renderer)
        return e.width, e.height

    def draw(self, renderer):
        (self.draw_vertical if self.orient == "vertical" else self.draw_horizontal)(renderer)
        self.line.draw(renderer)
        self.text.draw(renderer)
        e, xs = self.text.get_window_extent(renderer), list(self.line.get_xdata())
        self.xr = (min([e.x0] + xs), max([e.x1] + xs))  # its display x extent, for headroom()

    def draw_horizontal(self, renderer):
        px = css_px(renderer)
        ca, cb = self.center(renderer, self.a)[0], self.center(renderer, self.b)[0]
        mid = (ca + cb) / 2
        self.text.set(ha="center", va="bottom", position=(mid, 0))
        half = self.label_size(renderer)[0] / 2
        self.base = self.top_under(renderer, min(ca, mid - half) - 2 * px, max(cb, mid + half) + 2 * px,
                                   also=(self.a, self.b))
        y = self.ax.transData.transform((self.x, self.base))[1] + 2 * px
        yl = y + 3 * px
        self.line.set_data([ca, ca, cb, cb], [y, yl, yl, y])
        self.text.set_position((mid, yl + 1.5 * px))
        self.rise = self.text.get_window_extent(renderer).y1 - (y - 2 * px)

    def draw_vertical(self, renderer):
        px = css_px(renderer)
        to = self.ax.transData.transform
        vals = dict(self.tops)
        tall, short = (self.a, self.b) if vals[self.a] >= vals[self.b] else (self.b, self.a)
        ct, cs = self.center(renderer, tall)[0], self.center(renderer, short)[0]
        side = 1 if cs > ct else -1  # from the taller bar toward the shorter
        spine = (ct + cs) / 2 + side * 6 * px  # the slots meet halfway: the taller bar's edge is 1px short of it
        y_top = to((self.x, vals[tall]))[1]
        y_bot = to((self.x, vals[short]))[1] + 1.5 * px
        if y_top - y_bot < 6 * px:
            return self.draw_horizontal(renderer)
        width, height = self.label_size(renderer)
        x0 = spine + side * 2 * px
        floor_v = self.top_under(renderer, *sorted((x0, x0 + side * width)), also=(short,))
        mid_v = (vals[short] + vals[tall]) / 2
        on_mid = (y_bot + y_top) / 2 >= to((self.x, floor_v))[1] + 2 * px + height / 2
        y = (y_bot + y_top) / 2 if on_mid else to((self.x, floor_v))[1] + 2 * px + height / 2
        if y - height / 2 > y_top:  # it would float above the bracket
            return self.draw_horizontal(renderer)
        leg = spine - side * 3 * px
        self.line.set_data([leg, spine, spine, leg], [y_bot, y_bot, y_top, y_top])
        self.text.set(ha="left" if side > 0 else "right", va="center", position=(x0, y))
        self.base = mid_v if on_mid else floor_v
        self.rise = self.text.get_window_extent(renderer).y1 - to((self.x, self.base))[1]


def suite_of(st):
    return st.desc["simulations"][0]["suite"]


def seeds_of(st):
    """{seed: role suffix} for the extra-seed controls a study has, e.g. {1: '+seed1'}."""
    out = {}
    for role in st.gain:
        if role.startswith("control_") and "+seed" in role:
            out[int(role.split("+seed")[1].split("+")[0])] = True
    return sorted(out)


def pair_seeds(studies, seed_studies):
    """{study name: [seed Study, ...]}, by suite and core; exits if a seed study's lru differs."""
    pairs = {st.name: [] for st in studies}
    for ss in seed_studies:
        main = [st for st in studies if suite_of(st) == suite_of(ss) and st.arch == ss.arch]
        if not main:
            sys.exit(f"{ss.name}: no study of suite {suite_of(ss)} on {ss.arch} among the ones to plot")
        st = main[0]
        a, b = st.ipc[st.roles["lru"]], ss.ipc[ss.roles["lru"]]
        common = a.index.intersection(b.index)
        worst = float(((b[common] / a[common]) - 1).abs().max()) if len(common) else math.inf
        if not len(common) or worst > 1e-9:
            sys.exit(f"{ss.name}: its lru differs from {st.name}'s (worst {worst:.3g} relative, "
                     f"{len(common)} workloads in common), so its controls are not comparable")
        print(f"{ss.name} -> {st.name}: lru identical on {len(common)} workloads", file=sys.stderr)
        pairs[st.name].append(ss)
    return pairs


def natural(name):
    """Sort key: alphabetical, ignoring case, with digit runs as numbers (sierra.a.4 before sierra.a.10)."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower()) for t in re.split(r"(\d+)", name) if t]


def nested_geo(st, per_workload, names=None):
    """The nested geomean (inputs -> benchmark -> suite, as report.Study.geo) of {workload: ratio or IPC} over
    the study's workloads, or over names only."""
    h = st.hierarchy if names is None else report.Hierarchy(st.labels[w] for w in names)
    vals = {st.labels[w]: float(v) for w, v in per_workload.items() if v > 0 and math.isfinite(v)}
    avg = h.aggregate(vals, geometric=True)["Avg"]
    return avg if avg is not None else math.nan


def value(study, role, w, metric):
    """A role's value for workload w: its IPC gain over lru (%) or its IPC; None if the study lacks it."""
    if metric == "gain":
        g = study.gain.get(role)
        return report.pct(float(g[w])) if g is not None and w in g else None
    ipc = study.ipc.get(study.roles.get(role))
    return float(ipc[w]) if ipc is not None and w in ipc else None


def rows_for(st, seed_studies, side, picks, order, metric="gain", geomean="suite"):
    """The values one panel plots: (workload, {measure: value}) per group, then the geomean group (geomean:
    'suite' over every workload of the study, as report.py; 'plotted' over the plotted workloads only,
    extra seeds included when every plotted workload has them; 'off' none). metric: 'gain' (% over lru)
    or 'ipc' (absolute, with an lru bar)."""
    seeds = sorted({n for ss in seed_studies for n in seeds_of(ss)})
    with_seed = [w for ss in seed_studies for w in ss.workloads]
    if picks:
        names = [w for w in picks if w in st.workloads]
    elif suite_of(st) in DEFAULT_WORKLOADS:
        names = [w for w in DEFAULT_WORKLOADS[suite_of(st)] if w in st.workloads]
    elif with_seed:
        names = [w for w in st.workloads if w in with_seed]
    else:
        names = list(st.workloads)
    if order == "alpha":
        names.sort(key=natural)
    elif order == "ceiling":  # one order for every panel of the study: by the both-sides ceiling
        ceil = st.gain.get("ceiling_b", st.gain.get(f"ceiling_{side}"))
        if ceil is not None:
            names.sort(key=lambda w: -float(ceil.get(w, 0)))
    roles = ([("lru", "lru")] if metric == "ipc" else []) + [(m, f"{m}_{side}") for m in MEASURES]
    groups = []
    for w in names:
        vals = {m: v for m, role in roles if (v := value(st, role, w, metric)) is not None}
        for n in seeds:
            for ss in seed_studies:
                v = value(ss, f"control_{side}+seed{n}", w, metric)
                if v is not None:
                    vals[f"control_seed{n}"] = v
        groups.append((w, vals))
    if geomean == "suite":
        if metric == "gain":
            geo = {m: st.geo(role) for m, role in roles if role in st.gain}
        else:
            geo = {m: nested_geo(st, st.ipc[st.roles[role]]) for m, role in roles if st.roles.get(role) in st.ipc}
        groups.append(("Geomean", geo))
    elif geomean == "plotted":
        keys = [k for k in groups[0][1] if all(k in vals for _, vals in groups)] if groups else []
        conv, back = ((lambda v: 1 + v / 100, report.pct) if metric == "gain" else (lambda v: v, lambda v: v))
        geo = {k: back(nested_geo(st, {w: conv(vals[k]) for w, vals in groups}, names)) for k in keys}
        groups.append(("Geomean", geo))
    return groups, seeds


def draw_panel(ax, groups, seeds, t, ymax, geomean=True, bracket="horizontal"):
    """Draw one graph's bars; returns the bars the axis cuts, as (workload, measure, value). A group's bars are
    the ceiling, oracle and control, then each extra seed's control; a group without some (the geomean has no
    extra seeds) centers the ones it has. With geomean, the last group is the geomean, set off by a rule."""
    styles = dict(t["bars"], **{f"control_seed{s}": st for s, st in zip(seeds, t["seeds"])})
    order = list(ORDER) + [f"control_seed{s}" for s in seeds]
    bars = [[m for m in order if m in vals and math.isfinite(vals[m])] for _, vals in groups]
    slot = 0.81 / max(len(b) for b in bars)
    n = len(groups)
    xs = list(range(n - 1)) + [n - 1 + 0.35] if geomean else list(range(n))  # a little air before the geomean
    tops = [v for _, vals in groups for v in vals.values() if math.isfinite(v)]
    hi = max([0.0] + tops)
    lo = min([0.0] + tops)
    top = min(hi, ymax) if ymax else hi
    ax.set_xlim(-0.6, xs[-1] + 0.6)
    ax.set_ylim(lo * 1.1 if lo < 0 else 0, top * 1.06 + (0.5 if top == 0 else 0))
    cut = []
    for x, (name, vals), present in zip(xs, groups, bars):
        tops = []
        for i, m in enumerate(present):
            v = vals[m]
            shown = min(v, ymax) if ymax else v
            tops.append((i, shown))
            fill, hatch, ink = styles[m]
            ax.add_patch(Bar(ax, x, i, len(present), slot, shown, facecolor=fill, edgecolor="none",
                             hatch=hatch, hatch_color=ink, linewidth=0, zorder=2))
            if shown != v:
                ax.add_patch(BarBreak(ax, x, i, len(present), slot, shown, facecolor=t["surface"],
                                      edgecolor="none", zorder=2.5))
                cut.append((name, m, v))
        label = None
        if {"oracle", "control"} <= set(present):  # absolute IPC: the factor is still of the gains over lru
            o, c = vals["oracle"], vals["control"]
            label = (factor_label(o, c) if "lru" not in vals
                     else factor_label(o / vals["lru"] - 1, c / vals["lru"] - 1))
        if label and bracket != "none":  # oracle over the (seed 0) control
            ax.add_artist(FactorBracket(ax, x, present.index("oracle"), present.index("control"), len(present),
                                        slot, tops, label, t, bracket))
    if geomean and n > 1:
        ax.axvline((xs[-2] + xs[-1]) / 2, color=t["grid"], linewidth=0.75, zorder=1)
    ax.axhline(0, color=t["base"], linewidth=0.75, zorder=3)
    ax.set_xticks(xs)
    ax.set_xticklabels([g[0] for g in groups])
    ax.yaxis.grid(True, color=t["grid"], linewidth=0.6, linestyle="-")
    ax.set_axisbelow(True)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0, colors=t["ink2"], labelsize=8, pad=3)
    return cut


def legend_handles(t, seeds, measures=MEASURES):
    def swatch(style, label):
        fill, hatch, ink = style
        h = Patch(facecolor=fill, edgecolor="none", hatch=hatch, linewidth=0, label=label)
        if ink is not None:
            h._hatch_color = to_rgba(ink)  # as Bar
        return h

    b = t["bars"]
    names = {"lru": "LRU", "ceiling": "Ceiling", "oracle": "Oracle",
             "control": "Control (seed 0)" if seeds else "Control"}
    handles = [swatch(b[m], names[m]) for m in measures]
    handles += [swatch(st, f"Control (seed {s})") for s, st in zip(seeds, t["seeds"])]
    return handles


def label_rotation(ax, renderer, gap):
    """Degrees to turn the x tick labels so neighbors clear each other by gap pixels: 0 when they fit flat,
    else the least of 30, 45 and 60 at which parallel labels at the tick spacing do, else 90."""
    labs = [lab for lab in ax.get_xticklabels() if lab.get_text()]
    boxes = sorted((lab.get_window_extent(renderer) for lab in labs), key=lambda b: b.x0)
    if all(b.x0 - a.x1 >= gap for a, b in zip(boxes, boxes[1:])):
        return 0
    xs = np.sort(ax.transData.transform([(x, 0) for x in ax.get_xticks()])[:, 0])
    dx, h = float(np.diff(xs).min()), max(b.height for b in boxes)
    return next((deg for deg in (30, 45, 60) if dx * math.sin(math.radians(deg)) >= h + gap), 90)


def add_bracket_arg(p):
    p.add_argument("--bracket", choices=("horizontal", "vertical", "none"), default="horizontal",
                   help="the oracle-over-control factor bracket: across the two bars' tops (default), up from "
                   "the shorter bar's top to the taller's with the factor beside it, or none")


def figure(groups, seeds, t, a, geomean=True, measures=MEASURES, xlabel=None, ylabel=YLABEL["gain"], tag=None):
    """One graph: bars, y label, x tick labels (and an x label if given), legend above, and with tag, a box
    holding it in the plot's top right corner (the y axis rises until no bar or bracket reaches under it).
    Returns (fig, cut bars)."""
    n = len(groups)
    width = a.width or min(7.0, max(3.3, 0.55 * n + 1.0))  # one column up to two (7 in)
    fig = plt.figure(layout="constrained", figsize=(width, a.height), facecolor=t["surface"])
    fig.get_layout_engine().set(w_pad=2 / 72, h_pad=2 / 72)
    ax = fig.add_subplot()
    ax.set_facecolor(t["surface"])
    cut = draw_panel(ax, groups, seeds, t, a.ymax, geomean, getattr(a, "bracket", "horizontal"))
    ax.set_ylabel(ylabel, color=t["ink"], fontsize=8)
    if xlabel:
        ax.set_xlabel(xlabel, color=t["ink"], fontsize=8)
    box = None
    if tag:
        box = ax.annotate(tag, xy=(1, 1), xycoords="axes fraction", xytext=(-3, -3), textcoords="offset points",
                          ha="right", va="top", fontsize=8, color=t["ink"], zorder=5, annotation_clip=False,
                          bbox=dict(boxstyle="square,pad=0.35", facecolor=t["surface"], edgecolor=t["ink2"],
                                    linewidth=0.6))
    handles = legend_handles(t, seeds, measures)

    def legend(ncol):
        return fig.legend(handles=handles, loc="outside upper center", ncol=ncol, frameon=False, fontsize=8,
                          handlelength=1.6, handleheight=1.1, handletextpad=0.5, columnspacing=1.2,
                          borderaxespad=0.1, borderpad=0, labelcolor=t["ink"])

    leg = legend(len(handles))
    renderer = fig.canvas.get_renderer()
    fig.draw_without_rendering()
    if leg.get_window_extent(renderer).width > fig.bbox.width:  # too wide for one row: two
        leg.remove()
        leg = legend(math.ceil(len(handles) / 2))
        fig.draw_without_rendering()
    deg = label_rotation(ax, renderer, renderer.points_to_pixels(3))
    if deg:
        for lab in ax.get_xticklabels():
            lab.set(rotation=deg, ha="right", va="center", rotation_mode="anchor")
    headroom(fig, ax, box)
    return fig, cut


def headroom(fig, ax, box=None):
    """Raise the y axis's top until every factor bracket's label fits under it (1px clear), and, with box (an
    annotation in the top right corner), until no bar or bracket under it reaches within 2px of it."""
    brackets = [b for b in ax.artists if isinstance(b, FactorBracket)]
    bars = [b for b in ax.patches if isinstance(b, Bar)]
    for _ in range(6):  # the axes' height shifts a little with its ticks: settle
        fig.draw_without_rendering()
        renderer = fig.canvas.get_renderer()
        lo, hi = ax.get_ylim()
        h = ax.bbox.height
        px = css_px(renderer)
        reach = [(b.base, b.rise) for b in brackets]  # (value, px above it) whose top must clear the axis top
        if box is not None:
            e = box.get_bbox_patch().get_window_extent(renderer)
            drop = ax.bbox.y1 - e.y0 + 2 * px  # from the axis top to 2px below the box
            for b in bars:
                xs = b.get_path().vertices[:, 0]
                if len(xs) > 1 and xs.min() < e.x1 and xs.max() > e.x0:
                    reach.append((max(b.value, 0.0), drop - px))
            reach += [(b.base, b.rise + drop - px) for b in brackets if b.xr[0] < e.x1 and b.xr[1] > e.x0]
        need = max([hi] + [lo + (v - lo) * h / (h - r - px) for v, r in reach if r + px < h])
        if need <= hi * (1 + 1e-9):
            return
        ax.set_ylim(lo, need)


def gap_table(path, st, side, groups, seeds):
    """The oracle against the control, per plotted workload and the suite geomean, against each seed's control:
    the gap (oracle gain minus control gain, in points of IPC gain over LRU) and the factor (oracle gain over
    control gain: how many times the control's gain the oracle gets; '-' when the control gains nothing)."""
    ctrls = [("control", "control")] + [(f"control_seed{s}", f"control s{s}") for s in seeds]
    head = f"{'workload':<14}{'oracle':>8}" + "".join(f"{lab:>{max(11, len(lab) + 2)}}{'gap':>8}{'factor':>8}"
                                                     for _, lab in ctrls)
    lines = [f"{path.name}: {SUITE_NAME.get(suite_of(st), suite_of(st))} · {st.arch} · {SIDE_TITLE[side]}",
             "gain = IPC gain over LRU (%); gap = oracle - control (points); factor = oracle gain / control gain",
             head]
    for name, vals in groups:
        o = vals.get("oracle")
        row = f"{name:<14}" + (f"{o:>+8.2f}" if o is not None else f"{'':>8}")
        for m, lab in ctrls:
            w = max(11, len(lab) + 2)
            c = vals.get(m)
            if c is None or o is None:
                row += f"{'':>{w}}{'':>8}{'':>8}"
                continue
            factor = f"{o / c:.2f}x" if c > 0 else "-"
            row += f"{c:>+{w}.2f}{o - c:>+8.2f}{factor:>8}"
        lines.append(row.rstrip())
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("studies", nargs="+", help="study descriptors (the replay ones, e.g. mlp_oracle_google_defmark)")
    p.add_argument("--seeds", nargs="*", default=[], help="extra-seed control descriptors")
    p.add_argument("--sides", default="b", help="comma-separated: i (instruction), d (data), b (both); default b")
    p.add_argument("--workloads", default="", help="comma-separated workloads to plot (default: DEFAULT_WORKLOADS "
                   "for the suite, else those with an extra seed, else all)")
    p.add_argument("--order", choices=("alpha", "given", "ceiling"), default="alpha",
                   help="workload order: alphabetical (default), as given/listed, or by the both-sides ceiling")
    p.add_argument("--geomean", choices=("suite", "plotted", "off"), default="suite",
                   help="the last group: the geomean over every workload of the study (default), over the plotted "
                   "workloads only, or none")
    p.add_argument("--metric", choices=("gain", "ipc"), default="gain",
                   help="y axis: IPC gain over LRU in %% (default), or absolute IPC with LRU as a bar of its own")
    p.add_argument("--ymax", type=float, help="cut the y axis here; a bar it cuts gets a break mark")
    add_bracket_arg(p)
    p.add_argument("--side-label", action="store_true",
                   help="put the side in a box in the plot's top right corner: " + ", ".join(
                       f"{k} '{v}'" for k, v in SIDE_LABEL.items()))
    p.add_argument("--width", type=float, help="figure width in inches (default: 3.3, wider past 4 groups, up to 7)")
    p.add_argument("--height", type=float, default=2.1, help="figure height in inches (default 2.1)")
    p.add_argument("--theme", choices=THEMES, default="light")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--out", default="mlp_oracle.png",
                   help="image path; with several graphs, the stem of each (stem_<study>_<side>.png)")
    a = p.parse_args()
    t = THEMES[a.theme]
    sides = [s.strip() for s in a.sides.split(",") if s.strip()]
    if not sides or any(s not in SIDE_TITLE for s in sides):
        p.error("--sides takes i, d and b")
    picks = [w.strip() for w in a.workloads.split(",") if w.strip()]

    studies = [report.Study(s, include_cold=False) for s in a.studies]
    seed_studies = [report.Study(s, include_cold=False) for s in a.seeds]
    pairs = pair_seeds(studies, seed_studies)
    # gain groups feed the gap table whatever the metric; plot groups are what the figure shows
    graphs = []
    for st in studies:
        for side in sides:
            gains, seeds = rows_for(st, pairs[st.name], side, picks, a.order, "gain", a.geomean)
            plot = gains if a.metric == "gain" else rows_for(st, pairs[st.name], side, picks, a.order, "ipc",
                                                            a.geomean)[0]
            graphs.append((st, side, gains, plot, seeds))
    n_geo = 0 if a.geomean == "off" else 1
    if any(len(gains) <= n_geo for _, _, gains, _, _ in graphs):
        sys.exit("a study has none of the picked workloads")
    if any(len(seeds) > len(t["seeds"]) for *_, seeds in graphs):
        sys.exit(f"the {a.theme} theme has a style for {len(t['seeds'])} extra seed(s); add one to THEMES")

    plt.rcParams.update({"font.family": "sans-serif",
                         "font.sans-serif": ["Inter", "Helvetica", "Arial", "Liberation Sans", "DejaVu Sans"],
                         "axes.unicode_minus": True, "pdf.fonttype": 42, "ps.fonttype": 42})
    out = Path(a.out)
    measures = ("lru",) + MEASURES if a.metric == "ipc" else MEASURES
    unit = "gain_pct" if a.metric == "gain" else "ipc"
    for st, side, gains, groups, seeds in graphs:
        path = out if len(graphs) == 1 else out.with_name(f"{out.stem}_{st.name.removeprefix('mlp_oracle_')}_{side}"
                                                          f"{out.suffix}")
        fig, cut = figure(groups, seeds, t, a, geomean=a.geomean != "off", measures=measures,
                          ylabel=YLABEL[a.metric], tag=SIDE_LABEL[side] if a.side_label else None)
        fig.savefig(path, dpi=a.dpi, bbox_inches="tight", pad_inches=0.02, facecolor=t["surface"])
        plt.close(fig)
        cutset = {(w, m) for w, m, _ in cut}
        suite = suite_of(st)
        rows = [{"study": st.name, "suite": suite, "core": st.arch, "side": side, "workload": name, "measure": k,
                 unit: round(v, 4), "cut": (name, k) in cutset}
                for name, vals in groups for k, v in vals.items()]
        csv = path.with_suffix(".csv")
        pd.DataFrame(rows).to_csv(csv, index=False)
        suite_geo = {m: st.geo(f"{m}_{side}") for m in MEASURES}
        lines = [f"wrote {path} and {csv}",
                 f"  {SUITE_NAME.get(suite, suite)} · {st.arch} · {SIDE_TITLE[side]} · {st.count()}, "
                 f"{st.n_sp} simpoints · {st.length}",
                 "  suite geomean gain: " + ", ".join(f"{m} {v:+.1f}" for m, v in suite_geo.items())]
        if a.geomean == "plotted":
            lines.append(f"  Geomean bar = geomean of the {len(gains) - 1} plotted workloads only: "
                         + ", ".join(f"{m} {v:+.1f}" for m, v in gains[-1][1].items() if m in MEASURES))
        if a.metric == "ipc" and a.geomean != "off":
            lines.append("  Geomean bar IPC: " + ", ".join(f"{m} {v:.3f}" for m, v in groups[-1][1].items()))
        lines += [f"  cut at {a.ymax:g}: {w} {m} {v:+.1f}" for w, m, v in cut]
        print("\n".join(lines), file=sys.stderr)
        print(gap_table(path, st, side, gains, seeds) + "\n", flush=True)


if __name__ == "__main__":
    main()
