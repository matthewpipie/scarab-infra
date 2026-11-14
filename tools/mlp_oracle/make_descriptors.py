#!/usr/bin/env python3
"""Write the two descriptors of one mlp_oracle study.

The study bounds what cost-sensitive L2 (MLC) replacement can gain from REPL_MLP's marking. A
reference run (json/mlp_oracle_google_<tag>_ref.json) runs LRU and records every MLC miss with
the mark REPL_MLP's marking gives it. The replay runs (json/mlp_oracle_google_<tag>.json) read that
record and force misses to hit: the marked ones (the oracle), or as many random ones on as many
lines (the control), per side. Both leave each line's first miss alone (--mlp_oracle_reuse_only),
since no replacement policy can save a cold miss. LRU is the baseline, and REPL_MLP itself and the
ceilings (every non-cold miss forced) can run alongside.

Everything that changes what counts as marked goes in --marking. Those flags reach the reference
(where the marks are recorded) and every replay config, including REPL_MLP's.

    python tools/mlp_oracle/make_descriptors.py --tag base
    python tools/mlp_oracle/make_descriptors.py --tag dmin8 --marking "--mlp_data_bound_min 8" --sides d,b
    python tools/mlp_oracle/make_descriptors.py --tag dmin8 --marking "--mlp_data_bound_min 8" --check

Then run both with tools/mlp_oracle/run_study.sh mlp_oracle_google_<tag> and read the results with
tools/mlp_oracle/report.py. See tools/mlp_oracle/README.md.
"""

import argparse
import getpass
import json
import re
import sys
from pathlib import Path

INFRA = Path(__file__).resolve().parents[2]
USER = getpass.getuser()

# scarab PR #36 (mlp_oracle) merged with main 03c737d, plus the instruction-prefetch kind, sticky
# marks and the forced misses' reuse distances (RD needs them): the oracle records main's mark, so the default
# marking is main's (data: no-issue ROB-head stalls, --mlp_data_bound 3 --mlp_data_bound_min 16
# --mlp_lin_data_thresh 0; instruction: --mlp_instr_mark 2 --mlp_instr_starve 2). A build with a
# different marking must contain both the mlp_oracle code and that marking. The baseline study
# (baseline/mlp_oracle_google_t3) ran on c8bb06d, with the 0.7 ratio data marking.
DEFAULT_BINARY = "scarab_b30d6c1"

# Google "simpoints" are whole cores of equal weight, so a workload's first three by index are as
# good as any three. Cores whose trace holds under 560M instructions are swapped for the next ones
# (charlie 0 at 550M and 1 at 440M, merced 2 at 450M), since a run needs warmup + measured
# instructions plus a chunk of slack. whiskey has only two cores.
GOOGLE_PICKS = {
    "arizona": [0, 1, 2], "bravo.a": [0, 1, 2], "charlie": [2, 3, 6], "delta": [0, 1, 2],
    "merced": [0, 1, 3], "sierra.a.3": [0, 1, 2], "sierra.a.4": [0, 1, 2],
    "sierra.a.6": [0, 1, 2], "tahoe": [0, 1, 2], "tango": [0, 1, 2], "whiskey": [0, 1],
    "yankee": [0, 1, 2],
}
CHECK_PICKS = {"delta": [0]}

SIDES = {"i": (1, "instruction"), "d": (2, "data"), "b": (3, "both sides")}
LRU = "--mlc_cache_repl_policy 0"
REPLAY_MEM_MB = 1024  # a replay run holds the whole reference in memory
# Reuse-distance stats (MLP_RD*, stats only), on the reference and the record check alike: a line counts
# as marked once any of its fills was, and an instruction prefetch's lookup (FDIP's, mostly) is a reuse,
# as it renews the line in LRU. report.py --reuse reads the reference's; --miss-reuse the record check's
# MLP_RDMISS_*, the distances of the misses the oracle and the control force.
RD = "--mlp_rd_sticky 1 --mlp_rd_iprf_reuse 1"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tag", required=True, help="names the study: mlp_oracle_google_<tag> and mlp_oracle_google_<tag>_ref")
    p.add_argument("--marking", default="", help='Scarab flags that change the marking, e.g. "--mlp_data_bound_min 8"')
    p.add_argument("--binary", default=DEFAULT_BINARY, help=f"scarab_<hash> for every config (default {DEFAULT_BINARY})")
    p.add_argument("--sides", default="i,d,b", help="sides that get an oracle and a control: i, d, b (both), comma-separated")
    p.add_argument("--repl-mlp", default="64:0,64:16",
                   help="REPL_MLP configs to run with the same marking, as gamma_i:gamma_d pairs; empty for none")
    p.add_argument("--ceilings", action="store_true",
                   help="also run the ceilings; they do not depend on the marking, so report.py can take them from another study")
    p.add_argument("--variants", action="store_true",
                   help="also run no-reinstall oracle and control and a misses-only control (both sides)")
    p.add_argument("--seeds", type=int, default=0, help="extra independent control draws per side (default 0)")
    p.add_argument("--ref-params", default="",
                   help="extra stats-only Scarab flags for the reference and the record check, such as "
                        f'"--mlp_rd_iprf_reuse 0" (both always get {RD} first)')
    p.add_argument("--no-replay-check", action="store_true",
                   help="skip replay_check, which replays the reference forcing nothing and must equal lru, "
                        "and holds the forced misses' reuse distances (report.py --miss-reuse)")
    p.add_argument("--warmup", type=int, default=50_000_000)
    p.add_argument("--measured", type=int, default=500_000_000)
    p.add_argument("--check", action="store_true",
                   help="smoke test: delta core 0, 1M warmup + 3M measured, experiments mlp_oracle_check_<tag>*")
    p.add_argument("--architecture", default="golden_small")
    p.add_argument("--scarab-path", default=f"/home/{USER}/carp/scarab-oracle/",
                   help="a private clone of matthewpipie/scarab; sci checks hashes out in it in place")
    p.add_argument("--root-dir", default=f"/mnt/storage/{USER}/", help="output goes to <root-dir>/simulations/<experiment>")
    p.add_argument("--traces-dir", default="/mnt/storage/traces/shanen/")
    p.add_argument("--application-dir", default=f"/home/{USER}/carp/applications")
    p.add_argument("--max-parallel", type=int, default=256,
                   help="written into both descriptors; run_study.sh resizes it from the machine's load at launch")
    a = p.parse_args()
    if not re.fullmatch(r"[a-z0-9_]+", a.tag):
        p.error("--tag takes lowercase letters, digits and underscores")
    a.sides = [s.strip() for s in a.sides.split(",") if s.strip()]
    if not a.sides or any(s not in SIDES for s in a.sides):
        p.error("--sides takes i, d and b")
    if a.variants and "b" not in a.sides:
        p.error("--variants needs side b")
    toks = a.marking.split()
    if toks and not toks[0].startswith("--"):
        p.error("--marking takes Scarab flags, e.g. \"--mlp_data_bound_min 8\"")
    if re.search(r"--(mlp_oracle\w*|mlc_cache_repl_policy|inst_limit)\b", a.marking):
        p.error("--marking must not set mlp_oracle*, mlc_cache_repl_policy or inst_limit; the generator sets those")
    if re.search(r"--(mlp_oracle\w*|mlc_cache_repl_policy|inst_limit)\b", a.ref_params):
        p.error("--ref-params must not set mlp_oracle*, mlc_cache_repl_policy or inst_limit; the generator sets those")
    a.pairs = []
    for pair in filter(None, (s.strip() for s in a.repl_mlp.split(","))):
        gi, _, gd = pair.partition(":")
        if not (gi.isdigit() and gd.isdigit()):
            p.error(f"--repl-mlp: {pair!r} is not gamma_i:gamma_d")
        a.pairs.append((int(gi), int(gd)))
    if not re.fullmatch(r"scarab_[0-9a-f]{7,40}", a.binary):
        p.error("--binary takes scarab_<githash>")
    if a.check:
        a.warmup, a.measured = 1_000_000, 3_000_000
    return a


def names(a):
    stem = f"mlp_oracle_check_{a.tag}" if a.check else f"mlp_oracle_google_{a.tag}"
    return stem, f"{stem}_ref"


def millions(n):
    return f"{n / 1e6:g}M"


def simulations(a):
    picks = CHECK_PICKS if a.check else GOOGLE_PICKS
    return [{"suite": "google", "subsuite": "google", "workload": w, "cluster_id": c,
             "simulation_type": "memtrace", "warmup": a.warmup}
            for w, cs in picks.items() for c in cs]


def config(a, doc, params, mem=64):
    params = " ".join(f"{params} {a.marking} --inst_limit {a.warmup + a.measured}".split())
    return {"_params": doc, "params": params, "binary": a.binary, "slurm_options": "", "memory_overhead_mb": mem}


def replay_configs(a, ref_exp):
    ref = f"--mlp_oracle_ref {ref_exp}/track"
    c = {"lru": config(a, "Baseline: LRU in the MLC.", LRU)}
    if not a.no_replay_check:
        c["replay_check"] = config(
            a, "Replays the reference forcing nothing (mode 1). Must equal lru on every simpoint; if not, the "
               "reference was recorded with a different binary or flags. It draws the control as ctrl_*_ro do "
               "(same --mlp_oracle_reuse_only), so its MLP_RDMISS_* are the reuse distances of the misses the "
               "oracle and the control force (report.py --miss-reuse).",
            f"--mlp_oracle 1 --mlp_oracle_side 3 --mlp_oracle_reuse_only 1 {RD} {a.ref_params} {ref} {LRU}",
            REPLAY_MEM_MB)
    for gi, gd in a.pairs:
        c[f"mlp{gi}_{gd}"] = config(
            a, f"REPL_MLP with this marking: instruction gamma {gi}, data gamma {gd}.",
            f"--mlc_cache_repl_policy 9 --mlp_boost_in_fills 1 --mlp_lin_instr_lambda {gi} --mlp_lin_data_lambda {gd}")
    for s in a.sides:
        n, side = SIDES[s]
        base = f"--mlp_oracle_side {n} --mlp_oracle_reuse_only 1 {ref} {LRU}"
        c[f"oracle_{s}_ro"] = config(
            a, f"Oracle, {side}: the reference's marked misses hit, each line's first miss aside.",
            f"--mlp_oracle 2 {base}", REPLAY_MEM_MB)
        c[f"ctrl_{s}_ro"] = config(
            a, f"Control, {side}: as many random non-cold reference misses as oracle_{s}_ro forces, on as many lines.",
            f"--mlp_oracle 3 {base}", REPLAY_MEM_MB)
        for k in range(1, a.seeds + 1):
            c[f"ctrl_{s}_ro_s{k}"] = config(
                a, f"ctrl_{s}_ro with an independent draw (mlp_oracle_seed {k}).",
                f"--mlp_oracle 3 {base} --mlp_oracle_seed {k}", REPLAY_MEM_MB)
    if a.variants:
        base = f"--mlp_oracle_side 3 --mlp_oracle_reuse_only 1 {ref} {LRU}"
        c["ctrl_misses_b_ro"] = config(
            a, "ctrl_b_ro matched on the number of misses only, not lines (mlp_oracle_ctrl_lines 0).",
            f"--mlp_oracle 3 {base} --mlp_oracle_ctrl_lines 0", REPLAY_MEM_MB)
        c["oracle_b_noinst_ro"] = config(
            a, "oracle_b_ro without reinstalling forced lines in the MLC (mlp_oracle_install 0): they stay in the LLC.",
            f"--mlp_oracle 2 {base} --mlp_oracle_install 0", REPLAY_MEM_MB)
        c["ctrl_b_noinst_ro"] = config(
            a, "ctrl_b_ro without reinstalling forced lines: the control for oracle_b_noinst_ro.",
            f"--mlp_oracle 3 {base} --mlp_oracle_install 0", REPLAY_MEM_MB)
    if a.ceilings:
        for s in a.sides:
            n, side = SIDES[s]
            c[f"ceiling_{s}"] = config(
                a, f"Ceiling, {side}: every miss to a line the MLC has held before hits. Independent of the marking.",
                f"--mlp_oracle 4 --mlp_oracle_side {n} {LRU}")
    return c


def descriptor(a, experiment, purpose, configs, max_parallel):
    sims = simulations(a)
    picks = "delta core 0" if a.check else (
        "All 12 google workloads, the first three cores of each (whiskey has two), skipping cores whose trace "
        "is too short for the run (charlie 0 and 1, merced 2)")
    return {
        "_descriptor_type": "A type of descriptor. One of 'simulation', 'trace', and 'perf'.",
        "descriptor_type": "simulation",
        "_architecture": "PARAMS.<architecture> in scarab_path.",
        "architecture": a.architecture,
        "_experiment": f"{purpose} Generated by tools/mlp_oracle/make_descriptors.py; run with tools/mlp_oracle/run_study.sh.",
        "experiment": experiment,
        "_workload_manager": "manual: run the simulations on this machine, at most max_parallel at a time.",
        "workload_manager": "manual",
        "_max_parallel": "run_study.sh sets this at launch from idle cores, free memory and your other runs.",
        "max_parallel": min(max_parallel, len(sims) * len(configs)),
        "_container_reuse": "One reused container per slot instead of one per sim.",
        "container_reuse": True,
        "_root_dir": "Output goes to <root_dir>/simulations/<experiment>. The reference and the replay runs must share it.",
        "root_dir": a.root_dir,
        "_application_dir": "The path to the benchmark application directory used for tracing or simulation.",
        "application_dir": a.application_dir,
        "_scarab_path": "A private clone, so sci's in-place hash checkout cannot disturb a working checkout.",
        "scarab_path": a.scarab_path,
        "_scarab_build": "scarab build mode. 'opt', 'opt-avx', 'dbg', or null",
        "scarab_build": "opt",
        "_traces_dir": "The local directory containing all simpointed traces, resolved as <traces_dir>/<suite>/<subsuite>/<workload>.",
        "traces_dir": a.traces_dir,
        "_top_simpoint": "false: the simpoints are listed by cluster_id in simulations.",
        "top_simpoint": False,
        "_simulations": f"{picks}. {millions(a.warmup)} warmup + {millions(a.measured)} measured per simpoint.",
        "simulations": sims,
        "_configurations": (f"Marking flags on every config: {a.marking or 'none (REPL_MLP defaults)'}. "
                            "--inst_limit is warmup + measured; it overrides the run script's cap at the trace length."),
        "configurations": configs,
        "_collect_stats": "Stats collection settings for ./sci --collect-stats.",
        "collect_stats": {"configs": None},
        "_visualize": "./sci --visualize plots IPC per workload against lru; tools/mlp_oracle/report.py gives the bounds.",
        "visualize": {"baseline": "lru" if "lru" in configs else next(iter(configs)), "configs": list(configs), "counters": ["IPC"]},
        "_mem": "Memory settings (Slurm only).",
        "mem": {"collection_baseline": None, "fallback_mb": 16384},
        "_perf_analyze": "Settings for ./sci --perf-analyze.",
        "perf_analyze": {
            "baseline": "lru" if "lru" in configs else next(iter(configs)),
            "counters": ["IPC"],
            "stat_groups": ["core", "memory"],
            "compare_all_stats": False,
            "drift_top_workloads": 5,
            "drift_top_simpoints": 5,
            "prompt_budget_tokens": 12000,
            "threshold_pct": 2.0,
            "analyzer_cli_cmd": "codex",
        },
    }


def write(path, d):
    path.write_text(json.dumps(d, indent=2, separators=(",", ":")) + "\n")


def main():
    a = parse_args()
    exp, ref_exp = names(a)
    marking = a.marking or "REPL_MLP's default marking"
    ref_cfg = {"track": config(
        a, "LRU, tracking both sides, writing every MLC miss and its mark to mlp_oracle_misses.<core>.bin "
           "in the simpoint's directory, for the replay runs to read. Its MLP_RD* stats (report.py --reuse) "
           "count a line as marked once any of its fills was.",
        f"--mlp_oracle 1 --mlp_oracle_side 3 --mlp_oracle_record 1 {RD} {a.ref_params} {LRU}")}
    ref = descriptor(a, ref_exp, f"mlp_oracle reference for {exp} ({marking}).", ref_cfg, a.max_parallel)
    replay = descriptor(a, exp, f"mlp_oracle replay runs ({marking}); they read {ref_exp}/track's records, "
                                f"so run them after it completes.", replay_configs(a, ref_exp), a.max_parallel)
    out = INFRA / "json"
    for d in (ref, replay):
        write(out / f"{d['experiment']}.json", d)
    n_sims = len(replay["simulations"])
    n_cfg = len(replay["configurations"])
    print(f"json/{ref_exp}.json: 1 config x {n_sims} simpoints")
    print(f"json/{exp}.json: {n_cfg} configs x {n_sims} simpoints = {n_cfg * n_sims} sims")
    print(f"  {', '.join(replay['configurations'])}")
    print(f"Run: tools/mlp_oracle/run_study.sh {exp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
