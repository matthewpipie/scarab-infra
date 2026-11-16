#!/usr/bin/env python3
"""Offline tests of search.py: run the round loop against fake IPC surfaces.

    python tools/mlp_gsearch/test_search.py                    # synthetic surfaces
    python tools/mlp_gsearch/test_search.py --replay ipc_long.csv
        # also replay a real sweep (tools/mlp_gamma/report.py --out DIR writes DIR/ipc_long.csv): each
        # simpoint's IPC at any point is interpolated from that sweep's grid, held flat past its edges.
"""

import argparse
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import search as S  # noqa: E402


def loop(sps, fn, st, max_rounds=8, fail=None):
    """Run rounds until nothing is needed. fn(key, gi, gd) -> IPC. Returns (runs, rounds, units, model, sims)."""
    runs, sims = [], []
    for rnd in range(1, max_rounds + 1):
        needs, units, model = S.decide(sps, runs, st)
        if not needs:
            return runs, rnd - 1, units, model, sims
        n = 0
        for key, pts in needs.items():
            for p in pts:
                bad = fail and fail(key, p, rnd)
                runs.append((key[0], key[1], p[0], p[1], None if bad else fn(key, *p)))
                n += 1
        sims.append(n)
    needs, units, model = S.decide(sps, runs, st)
    return runs, max_rounds, units, model, sims


def unit(units, level, name):
    return next(u for u in units if u.level == level and u.name == name)


def sp(suite, wl, cl="0", w=1.0):
    return S.Simpoint(suite, f"{suite}/{suite}/{wl}", cl, w)


def test_peak_2d():
    """A sensitive simpoint with one smooth peak at (60, 84) is found exactly."""
    st = S.Settings()
    a = sp("google", "peak")
    fn = lambda k, gi, gd: 1.0 + 0.05 * math.exp(-((gi - 60) / 40) ** 2 - ((gd - 84) / 40) ** 2)
    runs, rounds, units, m, sims = loop([a], fn, st)
    u = unit(units, "simpoint", "google/google/peak:0")
    assert u.converged and u.best == (60, 84), (u.best, rounds, sims)
    print(f"peak_2d: best {u.best} in {rounds} rounds, sims per round {sims}")


def test_cap():
    """A simpoint that keeps improving with gamma_d stops at max_gamma, flagged at_cap."""
    st = S.Settings(max_gamma=(192, 120))
    a = sp("spec2006", "climb")
    fn = lambda k, gi, gd: 1.0 + 0.002 * gd
    runs, rounds, units, m, sims = loop([a], fn, st)
    u = unit(units, "simpoint", "spec2006/spec2006/climb:0")
    assert u.converged and u.best == (0, 120) and u.at_cap and u.axes == ("gd",), u
    assert max(p[1] for _, _, _, p1, _ in [(0, 0, 0, (r[2], r[3]), 0) for r in runs] for p in [p1]) <= 120
    print(f"cap: best {u.best} at_cap in {rounds} rounds, sims {sims}")


def test_flat_noise():
    """Noise under min_gain_pp neither moves the incumbent nor keeps the search going."""
    st = S.Settings()
    rng = random.Random(1)
    noise = defaultdict(lambda: 1.0 + rng.uniform(-0.0004, 0.0004))
    a = sp("spec2017", "flat")
    fn = lambda k, gi, gd: noise[(gi, gd)] if gi == 0 else noise[(0, gd)] * (1 + rng.uniform(-0.0005, 0.0005))
    runs, rounds, units, m, sims = loop([a], fn, st)
    u = unit(units, "simpoint", "spec2017/spec2017/flat:0")
    assert u.converged and rounds <= 2 and u.axes == ("gd",), (u, rounds, sims)
    print(f"flat_noise: converged in {rounds} rounds, sims {sims}, best {u.best}")


def test_insensitive_suite_imputes():
    """A suite whose simpoints ignore gamma_i runs no gamma_i>0 points past the initial probes, while one
    sensitive simpoint elsewhere searches both axes."""
    st = S.Settings()
    sps = [sp("spec2006", f"w{i}", str(c), 0.5) for i in range(4) for c in (1, 2)] + [sp("google", "g", "7")]
    def fn(k, gi, gd):
        if k[0].startswith("google"):
            return 1.0 + 0.02 * min(gi, 72) / 72 + 0.01 * math.exp(-((gd - 36) / 30) ** 2)
        i = int(k[0][-1])
        return 1.0 + 0.01 * (i + 1) * math.exp(-((gd - 12 * (i + 2)) / 30) ** 2)
    runs, rounds, units, m, sims = loop(sps, fn, st)
    spec_gi = {(r[2], r[3]) for r in runs if r[0].startswith("spec") and r[2] > 0}
    assert spec_gi <= {(96, 0), (96, 24), (96, 48)}, spec_gi
    su = unit(units, "suite", "spec2006")
    assert su.axes == ("gd",) and su.converged, su
    g = unit(units, "simpoint", "google/google/g:7")
    assert g.axes == ("gi", "gd") and g.converged and g.best[0] >= 72 - 12, g
    assert all(u.converged for u in units), [u for u in units if not u.converged]
    print(f"insensitive_suite: spec runs only probes at gi>0; google best {g.best}; {rounds} rounds, sims {sims}")


def test_failures():
    """A run that fails once is retried; a simpoint whose point fails twice is dropped, the rest carry on."""
    st = S.Settings()
    sps = [sp("spec2017", "a"), sp("spec2017", "b")]
    fn = lambda k, gi, gd: 1.0 + 0.001 * gd
    fail = lambda k, p, rnd: (k[0].endswith("/a") and p == (0, 24) and rnd == 1) or (k[0].endswith("/b") and p == (0, 12))
    runs, rounds, units, m, sims = loop(sps, fn, st, fail=fail)
    assert ("spec2017/spec2017/b", "0") in m.dropped, m.dropped
    ua = unit(units, "simpoint", "spec2017/spec2017/a:0")
    assert ua.converged and ua.best[1] > 48, ua
    assert unit(units, "suite", "spec2017").members == 1
    print(f"failures: a retried and converged at {ua.best}; b dropped; {rounds} rounds")


def test_init_extra():
    """A tag's init_extra points run in round 1 on its simpoints only, and a far optimum is reached sooner."""
    base = dict(max_gamma=(240, 120))
    g = S.Simpoint("google", "google/google/far", "0", 1.0, tag="google")
    r = S.Simpoint("spec2006", "spec2006/spec2006/flat", "0", 1.0, tag="rest")
    fn = lambda k, gi, gd: (1.0 + 0.04 * math.exp(-((gi - 192) / 40) ** 2 - ((gd - 36) / 30) ** 2)
                            if k[0].startswith("google") else 1.0 + 0.0001 * (gd == 12))
    st0 = S.Settings(**base)
    st1 = S.Settings(**base, init_extra={"google": {144: [12, 36], 192: [12, 36], 240: [12, 36]}})
    needs, _, _ = S.decide([g, r], [], st1)
    assert set(needs[g.key]) >= {(192, 36), (240, 12)} and (192, 36) not in needs[r.key], needs
    _, rounds0, u0, _, _ = loop([g, r], fn, st0)
    _, rounds1, u1, _, _ = loop([g, r], fn, st1)
    b0, b1 = unit(u0, "simpoint", "google/google/far:0"), unit(u1, "simpoint", "google/google/far:0")
    # Without the wide grid this optimum barely moves IPC at the gamma_i=96 probe, so the simpoint looks
    # gamma_i-insensitive and the search settles at gamma_i 0; with it, the optimum is found.
    assert b1.converged and b1.best == (192, 36), b1
    assert b0.max_gain < b1.max_gain - 0.1 or rounds1 < rounds0, (rounds0, b0.best, rounds1, b1.best)
    print(f"init_extra: wide google grid finds {b1.best} {b1.max_gain:+.2f}% in {rounds1} rounds; "
          f"without it {b0.best} {b0.max_gain:+.2f}% in {rounds0}")


def test_growing_lookahead():
    """A long steady climb converges in fewer rounds with a growing horizon, and no grid point on the path is
    skipped: every gamma_d step from the start to the optimum is measured."""
    g = S.Simpoint("spec2006", "spec2006/spec2006/climb", "0", 1.0)
    fn = lambda k, gi, gd: 1.0 + 0.03 * math.exp(-((gd - 168) / 60) ** 2)
    fixed = S.Settings(max_gamma=(192, 240), max_lookahead=2)
    grow = S.Settings(max_gamma=(192, 240), max_lookahead=8)
    _, r_fixed, u_fixed, _, _ = loop([g], fn, fixed, max_rounds=20)
    runs, r_grow, u_grow, m, _ = loop([g], fn, grow, max_rounds=20)
    a, b = unit(u_fixed, "simpoint", "spec2006/spec2006/climb:0"), unit(u_grow, "simpoint", "spec2006/spec2006/climb:0")
    assert a.best == b.best == (0, 168) and b.converged and r_grow < r_fixed, (r_fixed, a.best, r_grow, b.best)
    measured = {p[1] for p in m.ipc[g.key] if p[0] == 0}
    assert all(gd in measured for gd in range(0, 169, 12)), sorted(measured)
    print(f"growing_lookahead: optimum (0, 168) in {r_grow} rounds vs {r_fixed} with a fixed lookahead of 2, no step skipped")


def test_side_checks():
    """Side checks run the other-axis neighbors of points an incumbent jumped over."""
    st = S.Settings(max_gamma=(192, 192))
    sp = S.Simpoint("google", "google/google/x", "0", 1.0)
    ipc = {p: 1.0 for p in st.init_points}
    ipc.update({(96, 0): 1.01, (96, 24): 1.02, (96, 48): 1.01, (108, 24): 1.021, (120, 24): 1.05})
    runs = [(sp.workload, sp.cluster, gi, gd, v) for (gi, gd), v in ipc.items()]
    needs, units, m = S.decide([sp], runs, st)
    u = unit(units, "simpoint", "google/google/x:0")
    assert u.best == (120, 24), u          # climbed (96, 24) -> (120, 24) in one 2-step move past (108, 24)
    assert {(108, 12), (108, 36)} <= set(needs[sp.key]), needs[sp.key]
    print(f"side_checks: the climb jumped (96, 24) -> (120, 24); needs include (108, 12) and (108, 36)")


def test_workload_weights():
    """Workload values weight simpoints by their top-N weights."""
    st = S.Settings()
    sps = [sp("spec2006", "w", "1", 0.9), sp("spec2006", "w", "2", 0.1)]
    fn = lambda k, gi, gd: (1.0 + 0.001 * gd) if k[1] == "1" else (1.0 - 0.004 * gd)
    runs, _, units, m, _ = loop(sps, fn, S.Settings(max_gamma=(192, 48)), max_rounds=1)
    v = m.unit_value("workload", [s.key for s in sps], (0, 48))
    want = ((0.9 * 1.048 + 0.1 * (1 - 0.192)) / 1.0 - 1) * 100
    assert abs(v - want) < 1e-9, (v, want)
    print("workload_weights: ok")


def test_core():
    """Core units: sci's Avg (geomean of suites); the line units stay on their line and find its best."""
    st = S.Settings(max_gamma=(192, 192), core=True)
    sps = [sp("google", "a", "1", 0.7), sp("google", "a", "2", 0.3), sp("spec2006", "b", "1", 0.6),
           sp("spec2006", "b", "2", 0.4)]

    def fn(k, gi, gd):
        if k[0].startswith("google"):       # gamma_i-sensitive, peaks near (125, 25)
            return 1 + 0.002 * gi - 0.000008 * gi * gi + 0.001 * gd - 0.00002 * gd * gd
        return 1 + 0.0003 * gd - 0.000002 * gd * gd    # gamma_i-insensitive, peaks at gamma_d 75
    runs, rounds, units, m, sims = loop(sps, fn, st, max_rounds=12)
    core = {u.name: u for u in units if u.level == "core"}
    assert set(core) == {"all", "gd=0", "gi=0"}, core
    assert all(u.converged for u in core.values()), core
    assert core["gd=0"].axes == ("gi",) and core["gd=0"].best == (120, 0), core["gd=0"]
    assert core["gi=0"].axes == ("gd",) and core["gi=0"].best[0] == 0 and core["gi=0"].best[1] in (24, 36), core["gi=0"]
    assert core["all"].axes == ("gi", "gd") and core["all"].best in ((120, 24), (120, 36)), core["all"]
    keys = [x.key for x in sps]
    for u in core.values():
        g = [m.unit_value("suite", [k for k in keys if m.sps[k].suite == s], u.best) for s in ("google", "spec2006")]
        want = (math.sqrt((1 + g[0] / 100) * (1 + g[1] / 100)) - 1) * 100
        assert abs(m.unit_value("core", keys, u.best) - want) < 1e-9
    print(f"core: all {core['all'].best} {core['all'].gain:+.2f}%, gd=0 line {core['gd=0'].best}, "
          f"gi=0 line {core['gi=0'].best}; {rounds} rounds, sims per round {sims}")


def test_core_only():
    """levels ["core"]: only the core units are decided, they find the same optima as with every level on, and a
    round runs far fewer sims (no per-simpoint climbs). A start near the optimum converges in one round."""
    sps = [sp("google", "a", "1", 0.7), sp("google", "a", "2", 0.3), sp("spec2006", "b", "1", 0.6),
           sp("spec2006", "b", "2", 0.4), sp("spec2006", "c", "1", 1.0)]

    def fn(k, gi, gd):
        if k[0].startswith("google"):
            return 1 + 0.002 * gi - 0.000008 * gi * gi + 0.001 * gd - 0.00002 * gd * gd
        if k[0].endswith("/c"):                      # its own optimum far from the core's
            return 1 + 0.0004 * gd - 0.000001 * gd * gd
        return 1 + 0.0003 * gd - 0.000002 * gd * gd
    full = S.Settings.from_study({"step": 12, "max_gamma": [192, 192], "core": True})
    only = S.Settings.from_study({"step": 12, "max_gamma": [192, 192], "levels": ["core"]})
    assert only.core and only.searched == ("core",) and full.searched == S.LEVELS
    _, r_full, u_full, _, s_full = loop(sps, fn, full, max_rounds=12)
    _, r_only, u_only, _, s_only = loop(sps, fn, only, max_rounds=12)
    assert {u.level for u in u_only} == {"core"}, {u.level for u in u_only}
    best = lambda us: {u.name: u.best for u in us if u.level == "core"}
    assert best(u_only) == best(u_full), (best(u_only), best(u_full))
    assert all(u.converged for u in u_only)
    assert sum(s_only) < sum(s_full), (s_only, s_full)
    # Started at the optimum and its neighbors, it converges without another round.
    b = best(u_only)
    init = {0: [0, b["gi=0"][1] - 12, b["gi=0"][1], b["gi=0"][1] + 12]}
    for gi in (b["gd=0"][0] - 12, b["gd=0"][0], b["gd=0"][0] + 12):
        init.setdefault(gi, []).append(0)
    gi, gd = b["all"]
    for q in ((gi, gd), (gi - 12, gd), (gi + 12, gd), (gi, gd - 12), (gi, gd + 12)):
        init.setdefault(q[0], []).append(q[1])
    warm = S.Settings.from_study({"step": 12, "max_gamma": [192, 192], "levels": ["core"],
                                  "init": {str(k): sorted(set(v)) for k, v in init.items()}})
    _, r_warm, u_warm, m_warm, s_warm = loop(sps, fn, warm, max_rounds=12)
    # On a plateau the incumbent may settle on another point within min_gain_pp of the cold start's; the
    # partial-gain rule can ask for one more round.
    keys = [x.key for x in sps]
    tol = warm.min_gain_pp["core"]
    for u in u_warm:
        assert u.converged and m_warm.unit_value("core", keys, b[u.name]) - u.gain < tol, (u.name, u.best, b)
    assert r_warm <= 2 and sum(s_warm) < sum(s_only), (r_warm, s_warm, s_only)
    print(f"core_only: same core optima {b}; sims {sum(s_only)} in {r_only} rounds vs {sum(s_full)} in {r_full} "
          f"with every level; warm start {best(u_warm)}: {r_warm} rounds, {sum(s_warm)} sims")


def test_jump_ladder():
    """A frontier creeping up by less than min_gain_pp per step doesn't drag the incumbent along (jump "ladder")."""
    sp1 = S.Simpoint("google", "google/google/x", "0", 1.0)
    ipc = {(0, gd): 1.0 for gd in (0, 12, 24, 36, 48)}
    ipc.update({(96, 0): 1.0, (96, 24): 1.0, (96, 48): 1.0})
    # measured elsewhere: a slow ridge along gamma_d = 12, +0.02% per step from (96, 12) on
    ipc.update({(96 + 12 * i, 12): 1.012 + 0.0002 * i for i in range(0, 8)})
    ipc.update({(gi, 0): 1.0 for gi in range(96, 200, 12)})
    ipc.update({(gi, 24): 1.0 for gi in range(96, 200, 12)})
    runs = [(sp1.workload, sp1.cluster, gi, gd, v) for (gi, gd), v in ipc.items()]
    out = {}
    for j in ("ladder", "max"):
        st = S.Settings(max_gamma=(384, 384), jump=j)
        needs, units, m = S.decide([sp1], runs, st)
        out[j] = unit(units, "simpoint", "google/google/x:0")
    # ladder: from (0, 0) it climbs the ridge in jumps of at least 0.1 pp and stops within 0.1 pp of the best,
    # needing nothing more; max sits on the ridge's end and keeps asking for the next step out
    lad, mx = out["ladder"], out["max"]
    assert lad.max_gain - lad.gain < 0.1 and lad.best[1] == 12 and lad.best[0] < 180, lad
    assert not any(p[0] > 180 for p in lad.needs), lad
    assert mx.best == (180, 12) and (192, 12) in mx.needs, mx
    print(f"jump_ladder: ladder stays at {out['ladder'].best} (converged); max follows the ridge to {out['max'].best}, "
          f"needs {out['max'].needs}")


def replay(path):
    import pandas as pd
    d = pd.read_csv(path)
    d = d[(d["kind"] == "grid") & ~d["failed"].astype(bool)]
    total_full, total = 0, 0
    for arch, part in d.groupby("arch"):
        tables = {}
        for (wl, cl), g in part.groupby(["workload", "cluster"]):
            tables[(wl, str(cl))] = {(int(r.gi), int(r.gd)): r.ipc for r in g.itertuples()}
        gis = sorted({p[0] for t in tables.values() for p in t})
        gds = sorted({p[1] for t in tables.values() for p in t})

        def interp(axis, x):
            if x <= axis[0]:
                return axis[0], axis[0], 0.0
            if x >= axis[-1]:
                return axis[-1], axis[-1], 0.0
            hi = next(a for a in axis if a >= x)
            lo = max(a for a in axis if a <= x)
            return lo, hi, (0.0 if hi == lo else (x - lo) / (hi - lo))

        def fn(k, gi, gd):
            t = tables[k]
            i0, i1, fi = interp(gis, gi)
            d0, d1, fd = interp(gds, gd)
            v = lambda a, b: t[(a, b)]
            return ((1 - fi) * ((1 - fd) * v(i0, d0) + fd * v(i0, d1)) + fi * ((1 - fd) * v(i1, d0) + fd * v(i1, d1)))

        sps = [S.Simpoint(wl.split("/")[0], wl, cl, 1.0) for wl, cl in tables]
        st = S.Settings()
        runs, rounds, units, m, sims = loop(sps, fn, st)
        conv = sum(u.converged for u in units)
        print(f"replay {arch}: {len(sps)} simpoints, {rounds} rounds, sims per round {sims} (total {sum(sims)}), "
              f"{conv}/{len(units)} units converged, dropped {len(m.dropped)}")
        # How close is each simpoint's search to the best point of a dense step-12 grid on the same surface?
        worst = []
        for s in sps:
            u = unit(units, "simpoint", f"{s.workload}:{s.cluster}")
            dense = max(((fn(s.key, gi, gd) / fn(s.key, 0, 0) - 1) * 100, (gi, gd))
                        for gi in range(0, 121, 12) for gd in range(0, 61, 12))
            worst.append((dense[0] - u.max_gain, s.workload.split("/")[-1], s.cluster, u.best, u.max_point, dense[1]))
        worst.sort(reverse=True)
        for w in worst[:5]:
            print(f"   dense-grid best beats search by {w[0]:.3f} pp: {w[1]}:{w[2]} search incumbent {w[3]} "
                  f"best measured {w[4]} dense {w[5]}")
        for u in units:
            if u.level == "suite":
                print(f"   suite {u.name}: axes {u.axes} best {u.best} {u.gain:+.2f}% (max measured {u.max_point} "
                      f"{u.max_gain:+.2f}%), converged {u.converged}")
        total += sum(sims)
        total_full += len(sps) * 11 * 6
    print(f"replay: {total} sims vs {total_full} for a dense step-12 grid over gi 0-120 x gd 0-60")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay", help="ipc_long.csv of a real sweep to replay")
    a = ap.parse_args()
    test_peak_2d()
    test_cap()
    test_flat_noise()
    test_insensitive_suite_imputes()
    test_failures()
    test_workload_weights()
    test_init_extra()
    test_growing_lookahead()
    test_side_checks()
    test_core()
    test_core_only()
    test_jump_ladder()
    if a.replay:
        replay(a.replay)
    print("all tests passed")


if __name__ == "__main__":
    main()
