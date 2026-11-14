#!/usr/bin/env python3
"""Skip the instruction-gamma runs of simpoints that instruction gamma does not change.

A gamma sweep descriptor SRC (make_descriptors.py, or any descriptor whose REPL_MLP configs set
--mlp_lin_instr_lambda and --mlp_lin_data_lambda) is split in two:

  SRC_gi0  every simpoint, under the configs with instruction gamma 0 (and any non-REPL_MLP config, e.g. lru)
           plus one probe config with instruction gamma > 0 (by default the corner: largest gamma_i, then
           largest gamma_d)
  SRC_gi   only the simpoints whose IPC the probe moved by more than --tol percent, under the other
           gamma_i>0 configs

report.py SRC_gi reads both as one sweep: a simpoint SRC_gi leaves out gets, for each of its gamma_i>0 configs,
its run under the gamma_i=0 config with otherwise the same params (same gamma_d, binary and other flags).

    python tools/mlp_gamma/prune.py column SRC [SRC ...]        # write SRC_gi0; run it, collect its stats
    python tools/mlp_gamma/prune.py rest SRC [SRC ...]          # then write SRC_gi from SRC_gi0's stats
    python tools/mlp_gamma/prune.py rest SRC --like REF [REF ...]   # or decide from an earlier sweep

rest leaves a simpoint out when
  default   its probe run's IPC is within --tol percent (default 0.2) of its run under the probe's gamma_i=0
            partner. On the golden_small and golden_small_incl gamma sweeps, the corner probe kept the
            equal-suite grid within 0.006 pt of the full sweep at tol 0.2 (README.md).
  --like    its IPC moved by at most --tol percent between gamma_i=0 and every gamma_i>0 at the same gamma_d in
            the reference sweeps (simpoints they lack are run). Needs no probe, so both halves can run at
            once (column --probe none), but it cannot see what a new binary changes.
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
INFRA = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(INFRA / "workloads"))
import report as R  # noqa: E402

INSTR_FLAG = re.compile(r"--mlp_lin_instr_lambda[ =]\S+")


def gammas(cfg):
    """(gamma_i, gamma_d) of a REPL_MLP config, (0, None) for any other config."""
    kind = R.classify(cfg.get("params", ""))
    if kind is None and R.flag_value(cfg.get("params", ""), "mlp_lin_instr_lambda") not in (None, "0"):
        raise SystemExit(f"config params {cfg['params']!r} set an instruction gamma without REPL_MLP "
                         "(--mlc_cache_repl_policy 9); prune.py handles REPL_MLP only")
    return (kind[1], kind[2]) if kind and kind[0] == "grid" else (0.0, None)


def same_but_gi(cfg):
    """What a config's gamma_i=0 partner shares with it: binary and every param but the instruction gamma."""
    return cfg.get("binary"), tuple(INSTR_FLAG.sub("", cfg.get("params", "")).split())


def split_configs(src):
    """(gamma_i=0 configs, {gamma_i>0 config: its gamma_i=0 partner})."""
    confs = src["configurations"]
    zero = {n: c for n, c in confs.items() if gammas(c)[0] == 0}
    by_key = {}
    for n, c in zero.items():
        by_key.setdefault(same_but_gi(c), n)
    pairs = {n: by_key.get(same_but_gi(c)) for n, c in confs.items() if n not in zero}
    orphans = [n for n, p in pairs.items() if p is None]
    if orphans:
        raise SystemExit(f"{src['_name']}: no gamma_i=0 config with otherwise equal params for {orphans}")
    if not pairs:
        raise SystemExit(f"{src['_name']}: no configs with instruction gamma > 0, nothing to prune")
    return zero, pairs


def corner(src, pairs):
    return max(pairs, key=lambda n: gammas(src["configurations"][n]))


def copy_desc(src, name, configs, purpose):
    d = json.loads(json.dumps(src))
    d.pop("_name", None)
    d["experiment"] = name
    d["_experiment"] = f"{purpose} Written by tools/mlp_gamma/prune.py from {src['_name']}; report with report.py."
    d["configurations"] = {n: src["configurations"][n] for n in configs}
    vis = d.get("visualize")
    if isinstance(vis, dict):
        if "configs" in vis:
            vis["configs"] = [c for c in vis["configs"] if c in configs] or list(configs)
        if vis.get("baseline") not in configs:
            vis["baseline"] = next(iter(configs))
    pa = d.get("perf_analyze")
    if isinstance(pa, dict) and pa.get("baseline") not in configs:
        pa["baseline"] = next(iter(configs))
    return d


def write(name, desc, note):
    path = INFRA / "json" / f"{name}.json"
    path.write_text(json.dumps(desc, indent=2, separators=(",", ":")) + "\n")
    print(f"wrote {path.relative_to(INFRA)}: {note}")


def workloads_db(desc):
    """The workload DB sci runs desc against (utilities.load_workloads_for_descriptor)."""
    import extract_top_simpoints
    top = desc.get("top_simpoint")
    top_n = None if top in (None, False) else True if top is True else int(top)
    db = json.load(open(INFRA / "workloads" / ("workloads_top_simp.json" if top_n is True else "workloads_db.json")))
    if top_n not in (None, True):
        extract_top_simpoints.modify_simpoints_in_place(db, top_n)
    return db


def simpoints(desc):
    """Every ((suite, subsuite, workload, cluster_id), simulation entry) desc runs, expanded as sci's runners
    expand them."""
    db = workloads_db(desc)
    out = []
    for sim in desc["simulations"]:
        wls = sim["workload"] if isinstance(sim["workload"], list) else [sim["workload"]]
        for wl in wls:
            suite = sim["suite"]
            for sub in [sim["subsuite"]] if sim["subsuite"] else list(db[suite]):
                if not isinstance(db[suite].get(sub), dict):
                    continue
                for w in [wl] if wl else [w for w, v in db[suite][sub].items() if isinstance(v, dict)]:
                    if w not in db[suite][sub]:
                        continue
                    entry = db[suite][sub][w]
                    if sim["cluster_id"] is not None:
                        clusters = [sim["cluster_id"] if sim["cluster_id"] > 0 else 0]
                    elif "simpoints" in entry:
                        clusters = [sp["cluster_id"] for sp in entry["simpoints"]]
                    else:
                        clusters = [0]
                    out += [((suite, sub, w, int(c)), sim) for c in clusters]
    return out


def column(src, probe):
    zero, pairs = split_configs(src)
    if probe == "corner":
        probe = corner(src, pairs)
    elif probe == "none":
        probe = None
    elif probe not in pairs:
        raise SystemExit(f"--probe {probe}: not a gamma_i>0 config of {src['_name']} ({', '.join(pairs)})")
    if probe and len(pairs) == 1:
        raise SystemExit(f"{src['_name']}: the probe is its only gamma_i>0 config; run {src['_name']} itself")
    name = f"{src['_name']}_gi0"
    configs = list(zero) + ([probe] if probe else [])
    d = copy_desc(src, name, configs, f"The gamma_i=0 configs of {src['experiment']}"
                  + (f" and the probe {probe}" if probe else "") + ", every simpoint.")
    d["_gamma_prune_column"] = ("prune.py rest reads `probe` (a gamma_i>0 config run for every simpoint) to decide "
                                "which simpoints the other gamma_i>0 configs run.")
    d["gamma_prune_column"] = {"source": src["_name"], "probe": probe}
    write(name, d, f"{len(simpoints(src))} simpoints x {len(configs)} configs ({', '.join(configs)})")


def effect_from_probe(col_name):
    """|IPC(probe) / IPC(its gamma_i=0 partner) - 1| (%) per (workload, cluster); inf where either run failed."""
    col = R.load_descriptor(col_name)
    probe = col.get("gamma_prune_column", {}).get("probe")
    if not probe:
        raise SystemExit(f"{col_name} has no probe config (written with --probe none); use rest --like")
    _, pairs = split_configs(col)
    g = R.load_long(col)
    piv = g[g["ok"]].pivot_table(index=["workload", "cluster"], columns="config", values="ipc", aggfunc="first")
    eff = (piv[probe] / piv[pairs[probe]] - 1).abs() * 100
    every = g.groupby(["workload", "cluster"]).size().index
    return eff.reindex(every).fillna(math.inf), probe


def effect_from_refs(refs):
    """Largest |IPC(gi,gd) / IPC(0,gd) - 1| (%) per (workload, cluster) over the reference sweeps."""
    parts = []
    for ref in refs:
        g = R.load_sweep(R.load_descriptor(ref))
        g = g[(g["kind"] == "grid") & g["ok"]]
        for _, part in g.groupby(["arch", "gd"]):
            piv = part.pivot_table(index=["workload", "cluster"], columns="gi", values="ipc", aggfunc="first")
            if 0.0 not in piv or piv.shape[1] < 2:
                continue
            dev = piv.drop(columns=0.0).div(piv[0.0], axis=0).sub(1).abs().max(axis=1) * 100
            parts.append(dev[piv.notna().all(axis=1)])
    if not parts:
        raise SystemExit(f"{refs}: no simpoint ran both gamma_i=0 and gamma_i>0 at the same gamma_d")
    return pd.concat(parts).groupby(level=[0, 1]).max()


def rest(src, args):
    zero, pairs = split_configs(src)
    col_name = f"{src['_name']}_gi0"
    if not (INFRA / "json" / f"{col_name}.json").is_file():
        raise SystemExit(f"no json/{col_name}.json; run prune.py column {src['_name']} first")
    probe = R.load_descriptor(col_name).get("gamma_prune_column", {}).get("probe")
    if args.like:
        score = effect_from_refs(args.like)
        where = "the reference sweeps"
        rule = f"IPC moved by at most {args.tol}% between gamma_i=0 and every gamma_i>0 in {', '.join(args.like)}"
    else:
        score, probe = effect_from_probe(col_name)
        where = col_name
        rule = f"{probe} moved IPC by at most {args.tol}% from {pairs[probe]} in {col_name}"
    rest_pairs = {c: p for c, p in pairs.items() if c != probe}
    sps = simpoints(src)
    vals = [score.get((f"{s}/{sub}/{w}", str(c)), math.nan) for (s, sub, w, c), _ in sps]
    prune = [v <= args.tol for v in vals]  # NaN (missing) and inf (failed) are run
    kept = [(k, sim) for (k, sim), p in zip(sps, prune) if not p]
    name = f"{src['_name']}_gi"
    d = copy_desc(src, name, list(rest_pairs), f"The gamma_i>0 configs of {src['experiment']} but the probe, on the "
                  f"{len(kept)} of {len(sps)} simpoints instruction gamma moved.")
    d["_simulations"] = (f"The simpoints of {src['experiment']} that fail the rule in gamma_prune; the other "
                         f"{len(sps) - len(kept)} are read from {col_name}.")
    d["simulations"] = [{"suite": s, "subsuite": sub, "workload": w, "cluster_id": c,
                         "simulation_type": sim["simulation_type"], "warmup": sim["warmup"]}
                        for (s, sub, w, c), sim in kept]
    d["_gamma_prune"] = ("report.py reads this sweep with `column`: a simpoint not in `simulations` gets, for each "
                         "config in `pairs`, its run under the paired gamma_i=0 config. `rule` is what left it out.")
    d["gamma_prune"] = {"source": src["_name"], "column": col_name, "rule": rule, "pairs": rest_pairs}
    n_all = len(src["configurations"])
    skipped = (len(sps) - len(kept)) * len(rest_pairs)
    by_suite = pd.Series([k[0] for (k, _), p in zip(sps, prune) if p], dtype=str).value_counts().to_dict()
    write(name, d, f"{len(kept)} simpoints x {len(rest_pairs)} configs; skips {skipped} of {len(sps) * n_all} sims "
          f"({100 * skipped / (len(sps) * n_all):.0f}% of {src['_name']}); left out by suite {by_suite}")
    missing = sum(1 for v in vals if not v <= math.inf)
    failed = sum(1 for v in vals if v == math.inf)
    if missing or failed:
        print(f"  run in full: {missing} simpoints missing from {where}, {failed} that failed there")
    if not kept:
        print(f"  every simpoint was left out: nothing to run; report.py {name} fills every cell from {col_name}")
    if args.list:
        for ((s, sub, w, c), _), v, p in zip(sps, vals, prune):
            print(f"  {'skip' if p else 'run '} {s}/{sub}/{w}:{c} {v:.3g}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("column", help="write SRC_gi0: every simpoint, the gamma_i=0 configs and a probe")
    c.add_argument("sources", nargs="+")
    c.add_argument("--probe", default="corner",
                   help="gamma_i>0 config run for every simpoint (default corner: largest gamma_i, then gamma_d; "
                   "'none' for rest --like)")
    r = sub.add_parser("rest", help="write SRC_gi: the simpoints instruction gamma moved, the other gamma_i>0 configs")
    r.add_argument("sources", nargs="+")
    r.add_argument("--tol", type=float, default=0.2,
                   help="leave out simpoints whose IPC moved by at most this many percent (default 0.2)")
    r.add_argument("--like", nargs="+", metavar="REF", help="decide from these earlier sweeps instead of the probe")
    r.add_argument("--list", action="store_true", help="print each simpoint's value and decision")
    a = ap.parse_args()
    for n in a.sources:
        src = R.load_descriptor(n)
        if "gamma_prune" in src or "gamma_prune_column" in src:
            raise SystemExit(f"{n} was written by prune.py; pass the full sweep descriptor")
        column(src, a.probe) if a.cmd == "column" else rest(src, a)


if __name__ == "__main__":
    main()
