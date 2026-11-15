"""Decision logic for the adaptive REPL_MLP gamma search (no I/O; gsearch.py does the running).

Given what has run so far, `decide` returns what each simpoint must run next, and where every unit
(simpoint, input, workload, suite) stands. A point is (gamma_i, gamma_d) on multiples of `step`; (0, 0) is
plain LRU and the baseline.

Values follow ./sci on main (scarab_stats/aggregation.py and stat_collector's IPC row), from each simpoint's
Periodic_Instructions and Periodic_Cycles (the measured region):
  simpoint   its IPC
  input      a workloads_db entry (spec2006 gcc_2): cycle-weighted, sum(w * insts) / sum(w * cycles) over its
             simpoints, w the top-N renormalized weight
  workload   the inputs of one benchmark (gcc, gcc_2 ... gcc_9, grouped as aggregation.Hierarchy does): the
             geomean of its inputs
  suite      the geomean of its workloads
  core       (study setting `core`) sci's Avg: the geomean of the variant's suites, each suite weighted equally
A unit's gain at a point is that value over its value at (0, 0); for a geomean the ratio of the means is the
mean of the ratios, so a suite's gain is the nested geomean of its inputs' gains.

The rules, per unit:
  - Its value at a point is defined only when every member has the point (run, or imputed).
  - gamma_i sensitivity: a simpoint whose runs at gamma_i > 0 all stay within `gi_sensitive_pct` of its
    gamma_i = 0 run at the same gamma_d is insensitive. Inside the box those runs cover, its gamma_i > 0
    cells are imputed from its gamma_i = 0 run; outside it they are run for real. A unit with no sensitive
    (or still unknown) member searches gamma_d only, at gamma_i = 0.
  - Incumbent: start at the initial grid's best point, then climb: move to a defined point 1 or 2 steps
    away along an axis while one beats the incumbent by at least `min_gain_pp`. Smaller wins don't count, so
    noise and plateaus don't keep a search going. If any point measured for the unit (by another unit's
    needs, or a folded shard) beats the result by `min_gain_pp`, it becomes the incumbent and the climb
    restarts there, so a converged unit is a local optimum no measured point beats.
  - Needs: each undefined neighbor 1 step away along each searched axis, inside [0, max_gamma]. While the
    climb is still rising in a direction (it last moved that way, or the point 1 step behind is lower by at
    least `min_gain_pp`), also `lookahead` steps that way. If the neighbor 1 step away beats the incumbent by
    less than `min_gain_pp` but at least `partial_gain` of it, the point 2 steps away runs too, so a slow
    trend (two steps of 0.08 pp) is still found.
  - Converged when it needs nothing. A unit whose best sits on max_gamma while still rising is at_cap.
With `core` on, a variant gets three core units over all its simpoints: `all` searches both gammas for the one best
static setting; `gd=0` searches gamma_i along gamma_d = 0 (instruction boost only); `gi=0` searches gamma_d along
gamma_i = 0 (data boost only). A line unit's incumbent and neighbors stay on its line.
A core unit's needs run on every simpoint of the variant, a suite's on all of its simpoints, a workload's or an
input's on theirs, a simpoint's on itself. A run that
fails is retried; a simpoint with a point that failed `max_attempts` times is dropped.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field

LEVELS = ("simpoint", "input", "workload", "suite", "core")
# Core units: name -> the line it searches along, as (index of the fixed gamma, its value), or None for both gammas.
CORE_LINES = {"all": None, "gd=0": (1, 0), "gi=0": (0, 0)}


@dataclass(frozen=True)
class Simpoint:
    suite: str
    workload: str      # suite/subsuite/workload, as collected_stats.csv writes it: the input
    cluster: str
    weight: float      # top-N renormalized weight within its input
    bench: str = None  # the workload (benchmark) its input belongs to; None = the input is its own workload
    tag: str = None    # its template's tag in the study (e.g. "google"), for per-template initial points

    @property
    def group(self):
        return self.bench or self.workload

    @property
    def key(self):
        return (self.workload, self.cluster)


@dataclass
class Settings:
    step: int = 12
    init: dict = field(default_factory=lambda: {0: [0, 12, 24, 36, 48], 96: [0, 24, 48]})
    lookahead: int = 2
    # While a unit keeps climbing the same way, its horizon doubles (lookahead, 2x, 4x ...) up to max_lookahead
    # steps, and every step out to the horizon runs: a long climb takes fewer rounds and skips no value.
    max_lookahead: int = 8
    # Suite values average many simpoints, so smaller steps there are real.
    min_gain_pp: dict = field(default_factory=lambda: {"simpoint": 0.1, "input": 0.1, "workload": 0.1, "suite": 0.05,
                                                       "core": 0.05})
    gi_sensitive_pct: float = 0.2
    max_gamma: tuple = (192, 192)
    max_attempts: int = 2
    partial_gain: float = 0.5
    # Extra round-1 points per template tag, on top of `init`: {tag: {gamma_i: [gamma_d, ...]}}. A wide coarse
    # grid for a suite whose optimum is far from the default start (google's gamma_i) saves the rounds a step-by-step
    # climb would take, and costs only that suite's simpoints.
    init_extra: dict = field(default_factory=dict)
    # Search the core units (sci's Avg over the variant's suites): the best static setting, and the best along each
    # gamma with the other at 0.
    core: bool = False
    # Where a unit's incumbent jumps when a measured point off its climb beats it by min_gain_pp: "ladder" (the
    # smallest-gamma such point, repeated) or "max" (the best measured point; lets a slow frontier drag it along).
    jump: str = "ladder"

    @classmethod
    def from_study(cls, s):
        st = cls()
        step = s.get("step", st.step)        # one grid step for both gammas, or [gamma_i step, gamma_d step]
        st.step = tuple(int(x) for x in step) if isinstance(step, (list, tuple)) else int(step)
        if "init" in s:
            st.init = {int(gi): [int(d) for d in gds] for gi, gds in s["init"].items()}
        st.lookahead = int(s.get("lookahead", st.lookahead))
        st.max_lookahead = max(st.lookahead, int(s.get("max_lookahead", st.max_lookahead)))
        mg = s.get("min_gain_pp", st.min_gain_pp)
        st.min_gain_pp = {lv: float(mg.get(lv, st.min_gain_pp[lv]) if isinstance(mg, dict) else mg) for lv in LEVELS}
        st.gi_sensitive_pct = float(s.get("gi_sensitive_pct", st.gi_sensitive_pct))
        st.max_gamma = tuple(int(g) for g in s.get("max_gamma", st.max_gamma))
        st.max_attempts = int(s.get("max_attempts", st.max_attempts))
        st.partial_gain = float(s.get("partial_gain", st.partial_gain))
        st.core = bool(s.get("core", st.core))
        st.jump = s.get("jump", st.jump)
        if st.jump not in ("ladder", "max"):
            raise ValueError(f"jump: 'ladder' or 'max', got {st.jump!r}")
        st.init_extra = {tag: {int(gi): [int(d) for d in gds] for gi, gds in rows.items()}
                         for tag, rows in s.get("init_extra", {}).items()}
        for rows in st.init_extra.values():
            for gi, gds in rows.items():
                for gd in gds:
                    if gi % st.steps[0] or gd % st.steps[1] or not (0 <= gi <= st.max_gamma[0] and 0 <= gd <= st.max_gamma[1]):
                        raise ValueError(f"init_extra point ({gi}, {gd}) is off the {st.steps} grid or past max_gamma")
        for gi, gds in st.init.items():
            for gd in gds:
                if gi % st.steps[0] or gd % st.steps[1] or not (0 <= gi <= st.max_gamma[0] and 0 <= gd <= st.max_gamma[1]):
                    raise ValueError(f"init point ({gi}, {gd}) is off the step-{st.step} grid or past max_gamma")
        if 0 not in st.init or 0 not in st.init[0]:
            raise ValueError("init must include (0, 0), the baseline")
        return st

    @property
    def steps(self):
        """(gamma_i step, gamma_d step)."""
        return tuple(self.step) if isinstance(self.step, (list, tuple)) else (self.step, self.step)

    def init_for(self, tag):
        """Round-1 points for a simpoint of template `tag`: init plus that tag's init_extra."""
        extra = {(gi, gd) for gi, gds in self.init_extra.get(tag, {}).items() for gd in gds}
        return sorted(set(self.init_points) | extra)

    @property
    def init_points(self):
        return sorted((gi, gd) for gi, gds in self.init.items() for gd in gds)


def config_name(p):
    return f"gi{p[0]}_gd{p[1]}"


@dataclass
class UnitResult:
    level: str
    name: str            # suite, workload, input, or input:cluster
    suite: str
    members: int
    axes: tuple          # ("gi", "gd") or ("gd",)
    best: tuple = None   # (gi, gd), the incumbent
    gain: float = math.nan       # % over (0, 0)
    max_point: tuple = None      # best point measured (may beat the incumbent by < min_gain_pp)
    max_gain: float = math.nan
    start: tuple = None
    needs: list = field(default_factory=list)
    defined: int = 0
    at_cap: bool = False
    pending: bool = False   # a member still waits on its initial grid; no decision yet
    smallest_within: tuple = None

    @property
    def converged(self):
        return not self.pending and not self.needs


class Model:
    """What has run for one variant (an independent search), and the values the rules read."""

    def __init__(self, simpoints, runs, settings):
        """runs: iterable of (input, cluster, gi, gd, insts, cycles), insts/cycles None for a failed run.
        (input, cluster, gi, gd, ipc) is also accepted, as insts = ipc over one cycle."""
        self.st = settings
        self.sps = {sp.key: sp for sp in simpoints}
        self.ipc = defaultdict(dict)
        self.ic = defaultdict(dict)       # (insts, cycles) per point
        self.fails = defaultdict(lambda: defaultdict(int))
        for run in runs:
            wl, cl, gi, gd = run[:4]
            ins, cyc = (run[4], 1.0) if len(run) == 5 else run[4:6]
            key, p = (wl, str(cl)), (int(gi), int(gd))
            if key not in self.sps:
                continue
            if (ins is not None and cyc is not None and ins > 0 and cyc > 0
                    and math.isfinite(ins) and math.isfinite(cyc)):
                self.ipc[key][p] = ins / cyc
                self.ic[key][p] = (float(ins), float(cyc))
            else:
                self.fails[key][p] += 1
        self.dropped = {k: sorted(p for p, n in f.items() if n >= settings.max_attempts and p not in self.ipc[k])
                        for k, f in self.fails.items()}
        self.dropped = {k: ps for k, ps in self.dropped.items() if ps}
        self.sens = {k: self._sensitivity(k) for k in self.sps}

    # ---------------------------------------------------------------- per simpoint
    def _sensitivity(self, key):
        """('sensitive' | 'insensitive' | 'unknown', gi box, gd box)."""
        runs = self.ipc[key]
        pairs = [(gi, gd) for gi, gd in runs if gi > 0 and (0, gd) in runs]
        if not pairs:
            return "unknown", 0, -1
        dev = max(abs(runs[p] / runs[(0, p[1])] - 1) * 100 for p in pairs)
        if dev > self.st.gi_sensitive_pct:
            return "sensitive", 0, -1
        return "insensitive", max(p[0] for p in pairs), max(p[1] for p in pairs)

    def source(self, key, p):
        """The run that gives simpoint `key` its value at p: p itself, or (0, gd) when imputable."""
        kind, gi_box, gd_box = self.sens[key]
        if p[0] > 0 and kind == "insensitive" and p[0] <= gi_box and p[1] <= gd_box:
            return (0, p[1])
        return p

    def value(self, key, p):
        return self.ipc[key].get(self.source(key, p))

    def value_ic(self, key, p):
        return self.ic[key].get(self.source(key, p))

    def imputed(self, key, p):
        return p not in self.ipc[key] and self.source(key, p) != p and self.value(key, p) is not None

    def active(self):
        return [k for k in self.sps if k not in self.dropped]

    def pending_init(self, key):
        return any(p not in self.ipc[key] for p in self.st.init_for(self.sps[key].tag))

    # ---------------------------------------------------------------- units
    def units(self):
        act = self.active()
        by_in, by_wl, by_suite = defaultdict(list), defaultdict(list), defaultdict(list)
        for k in act:
            by_in[self.sps[k].workload].append(k)
            by_wl[self.sps[k].group].append(k)
            by_suite[self.sps[k].suite].append(k)
        out = [("simpoint", f"{k[0]}:{k[1]}", self.sps[k].suite, [k]) for k in sorted(act)]
        out += [("input", wl, self.sps[ks[0]].suite, sorted(ks)) for wl, ks in sorted(by_in.items())]
        out += [("workload", wl, self.sps[ks[0]].suite, sorted(ks)) for wl, ks in sorted(by_wl.items())]
        out += [("suite", s, s, sorted(ks)) for s, ks in sorted(by_suite.items())]
        if self.st.core and act:
            out += [("core", name, "core", sorted(act)) for name in CORE_LINES]
        return out

    def unit_value(self, level, members, p):
        """% gain over (0, 0), or None when some member lacks p or (0, 0)."""
        if level == "simpoint":
            v, b = self.value(members[0], p), self.value(members[0], (0, 0))
            return None if v is None or b is None else (v / b - 1) * 100
        if level == "core":
            return self.aggregate_core(members, lambda k: self.value_ic(k, p), lambda k: self.value_ic(k, (0, 0)))
        return self.aggregate(members, lambda k: self.value_ic(k, p), lambda k: self.value_ic(k, (0, 0)))

    def aggregate_core(self, members, at, base):
        """sci's Avg: the geomean over the members' suites of each suite's nested-geomean gain."""
        by_suite = defaultdict(list)
        for k in members:
            by_suite[self.sps[k].suite].append(k)
        logs = []
        for ks in by_suite.values():
            g = self.aggregate(ks, at, base)
            if g is None:
                return None
            logs.append(math.log(1 + g / 100))
        return (math.exp(sum(logs) / len(logs)) - 1) * 100

    def aggregate(self, members, at, base):
        """% gain of members' nested value (sci's: cycle-weighted inputs, geomean workloads, geomean suite)
        with each simpoint's (insts, cycles) from at(key) over base(key); None if any is missing."""
        per_in = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
        for k in members:
            v, b = at(k), base(k)
            if v is None or b is None:
                return None
            w = self.sps[k].weight
            acc = per_in[self.sps[k].workload]
            acc[0] += w * v[0]
            acc[1] += w * v[1]
            acc[2] += w * b[0]
            acc[3] += w * b[1]
        per_wl = defaultdict(list)
        for wl, (ni, nc, bi, bc) in per_in.items():
            per_wl[self.sps[next(k for k in members if self.sps[k].workload == wl)].group].append(
                math.log((ni / nc) / (bi / bc)))
        logs = [sum(v) / len(v) for v in per_wl.values()]
        return (math.exp(sum(logs) / len(logs)) - 1) * 100

    def axes_of(self, members):
        return ("gd",) if all(self.sens[k][0] == "insensitive" for k in members) else ("gi", "gd")

    def candidates(self, members, axes):
        """Points every member has (run or imputed), within the searched axes."""
        pts = set()
        for k in members:
            pts |= set(self.ipc[k])
            if self.sens[k][0] == "insensitive":
                _, gi_box, gd_box = self.sens[k]
                pts |= {(gi, gd) for gi in range(0, gi_box + 1, self.st.steps[0])
                        for (g0, gd) in self.ipc[k] if g0 == 0 and gd <= gd_box}
        if axes == ("gd",):
            pts = {p for p in pts if p[0] == 0}
        return pts

    def _climb(self, b, vals, axes, tol, moves):
        """Move from b to a point 1 or 2 steps away along an axis while one beats it by tol."""
        ax_idx = {"gi": 0, "gd": 1}
        while True:
            options = []
            for ax in axes:
                i = ax_idx[ax]
                for k in (1, 2, -1, -2):
                    q = list(b)
                    q[i] += k * self.st.steps[i]
                    q = tuple(q)
                    if q in vals:
                        options.append(q)
            nxt = max(options, key=lambda q: vals[q], default=None)
            if nxt is None or vals[nxt] - vals[b] < tol:
                return b
            moves.append((b, nxt))
            b = nxt

    def decide_unit(self, level, name, suite, members):
        st, tol = self.st, self.st.min_gain_pp[level]
        line = CORE_LINES[name] if level == "core" else None
        axes = self.axes_of(members) if line is None else (("gd",) if line[0] == 0 else ("gi",))
        r = UnitResult(level, name, suite, len(members), axes)
        if any(self.pending_init(k) for k in members):
            r.pending = True
            return r
        on_line = (lambda p: True) if line is None else (lambda p: p[line[0]] == line[1])
        vals = {}
        for p in self.candidates(members, ("gi", "gd") if line is not None else r.axes):
            if not on_line(p):
                continue
            v = self.unit_value(level, members, p)
            if v is not None:
                vals[p] = v
        r.defined = len(vals)
        init_pts = set().union(*(st.init_for(self.sps[k].tag) for k in members))
        init = [p for p in sorted(init_pts) if p in vals and (r.axes != ("gd",) or p[0] == 0)]
        if not init:
            r.pending = True
            return r
        b = max(init, key=lambda p: (vals[p], -(p[0] + p[1]), -p[0]))
        r.start = b
        ax_idx = {"gi": 0, "gd": 1}
        moves = []
        jump_at = 0      # ladder: the climb moves since the last jump, moves[jump_at:], say which way the unit rises
        while True:
            b = self._climb(b, vals, r.axes, tol, moves)
            # A point measured anywhere (by another unit's needs, or a folded shard) that beats the local optimum
            # by min_gain_pp becomes the incumbent, and the climb restarts there. With jump "ladder" it is the
            # smallest-gamma such point, so the incumbent climbs the measured values in steps of at least
            # min_gain_pp and ends within min_gain_pp of the best: a frontier creeping up by less than that per
            # step doesn't drag the incumbent (and the next round's needs) along. "max" jumps straight to the best.
            better = [p for p in vals if vals[p] - vals[b] >= tol]
            if not better:
                break
            if st.jump == "max":
                nxt = max(better, key=lambda p: (vals[p], -(p[0] + p[1]), -p[0]))
                moves.append((b, nxt))
            else:
                # A jump is not a climb step: it says nothing about which way the new incumbent rises, so it
                # neither sets the direction nor lengthens a streak.
                nxt = min(better, key=lambda p: (p[0] + p[1], p[0], p[1]))
                jump_at = len(moves)
            b = nxt
        recent = moves[jump_at:]
        r.best, r.gain = b, vals[b]
        r.max_point = max(vals, key=lambda p: (vals[p], -(p[0] + p[1]), -p[0]))
        r.max_gain = vals[r.max_point]
        r.smallest_within = min((p for p in vals if vals[p] >= vals[b] - tol), key=lambda p: (p[0] + p[1], p[0], p[1]))

        needs = []
        for ax in r.axes:
            i = ax_idx[ax]
            hi = st.max_gamma[i]
            for d in (1, -1):
                def at(k):
                    q = list(b)
                    q[i] += d * k * st.steps[i]
                    return tuple(q) if 0 <= q[i] <= hi else None
                n1 = at(1)
                behind = at(-1)
                last_dir = None
                if recent:
                    a0, a1 = recent[-1]
                    if a0[1 - i] == a1[1 - i] and a1[i] != a0[i]:
                        last_dir = 1 if a1[i] > a0[i] else -1
                rising = last_dir == d or (behind is not None and behind in vals and vals[b] - vals[behind] >= tol)
                if n1 is None:
                    if rising and d == 1:
                        r.at_cap = True
                    continue
                if n1 not in vals:
                    needs.append(n1)
                elif 0 < st.partial_gain and vals[n1] - vals[b] >= st.partial_gain * tol:
                    rising = True
                if rising:
                    # Consecutive climb moves this way along this axis double the horizon; every step to it runs.
                    streak = 0
                    for a0, a1 in reversed(recent):
                        if a0[1 - i] == a1[1 - i] and (a1[i] - a0[i]) * d > 0:
                            streak += 1
                        else:
                            break
                    horizon = min(st.max_lookahead, st.lookahead * 2 ** max(0, streak - 1))
                    for k in range(2, horizon + 1):
                        q = at(k)
                        if q is None:
                            break
                        if q not in vals:
                            needs.append(q)
        # Side checks: where the incumbent jumped two or more steps along one axis, the points it passed over get
        # their +-1 neighbors on the other axis, so a side branch there is examined as a step-by-step climb would.
        if len(r.axes) == 2:
            for a0, a1 in moves:
                for i in (0, 1):
                    o = 1 - i
                    if a0[o] != a1[o] or abs(a1[i] - a0[i]) < 2 * st.steps[i]:
                        continue
                    lo, hi_i = sorted((a0[i], a1[i]))
                    for x in range(lo + st.steps[i], hi_i, st.steps[i]):
                        for e in (-1, 1):
                            q = [0, 0]
                            q[i], q[o] = x, a0[o] + e * st.steps[o]
                            q = tuple(q)
                            if 0 <= q[o] <= st.max_gamma[o] and q not in vals:
                                needs.append(q)
        r.needs = sorted(set(needs))
        return r


def decide(simpoints, runs, settings):
    """(needs, units, model): needs maps simpoint key -> sorted points to run next round."""
    m = Model(simpoints, runs, settings)
    needs = defaultdict(set)
    for k in m.active():
        for p in settings.init_for(m.sps[k].tag):
            if p not in m.ipc[k]:
                needs[k].add(p)
    units = []
    for level, name, suite, members in m.units():
        r = m.decide_unit(level, name, suite, members)
        units.append(r)
        for p in r.needs:
            for k in members:
                src = m.source(k, p)
                if src not in m.ipc[k]:
                    needs[k].add(src)
    return {k: sorted(ps) for k, ps in needs.items() if ps}, units, m
