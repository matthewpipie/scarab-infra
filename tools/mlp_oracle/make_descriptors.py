#!/usr/bin/env python3
"""Write the two descriptors of one mlp_oracle study.

The study bounds what cost-sensitive L2 (MLC) replacement can gain from REPL_MLP's marking. A
reference run (json/mlp_oracle_google_<tag>_ref.json) runs LRU and records every MLC miss with
the mark REPL_MLP's marking gives it. The replay runs (json/mlp_oracle_google_<tag>.json) read that
record and force misses to hit: the marked ones (the oracle), or as many random ones on as many
lines (the control), per side. Both leave each line's first miss alone (--mlp_oracle_reuse_only),
since no replacement policy can save a cold miss. LRU is the baseline, and REPL_MLP itself and the
ceilings (every non-cold miss forced) can run alongside.

--binary is a commit of a private scarab clone (--scarab-path) that has the oracle (scarab #36): a git
ref such as origin/main (the default, fetched first) or a scarab_<hash>. The descriptors pin its hash.
The marking is that commit's, with --marking on top: every marking flag (MARKING_PARAMS) is written out
into every config, at the commit's default unless --marking sets it, so a study's marking is explicit
whatever main's defaults become. --architecture picks the core (src/PARAMS.<architecture>) and --params
adds machine flags; both reach every config too, as the reference and the replays must run the same
machine. Every flag is checked against the commit's *.param.def before anything is written.

    python tools/mlp_oracle/make_descriptors.py --tag base
    python tools/mlp_oracle/make_descriptors.py --tag dmin8 --marking "--mlp_data_bound_min 8" --sides d,b
    python tools/mlp_oracle/make_descriptors.py --tag dmin8 --marking "--mlp_data_bound_min 8" --check
    python tools/mlp_oracle/make_descriptors.py --tag incl --architecture golden_small_incl --ceilings
    python tools/mlp_oracle/make_descriptors.py --tag l2_1m --params "--mlc_size 1048576" --ceilings

Then run both with tools/mlp_oracle/run_study.sh mlp_oracle_google_<tag> and read the results with
tools/mlp_oracle/report.py. See tools/mlp_oracle/README.md.
"""

import argparse
import getpass
import json
import re
import subprocess
import sys
from pathlib import Path

INFRA = Path(__file__).resolve().parents[2]
USER = getpass.getuser()

# The newest main of the scarab clone: once scarab #36 is merged, it has the oracle and main's current
# marking. Until then, origin/mlp-oracle (#36's branch, b30d6c1: main 03c737d plus the oracle and the
# reuse stats) is the commit to pass. The baseline study (baseline/mlp_oracle_google_t3) ran on
# c8bb06d, with the 0.7 ratio data marking, before the oracle used main's marking.
DEFAULT_BINARY = "origin/main"

# The flags that decide which fills are marked (scarab src/memory/memory.param.def), data side, then
# instruction side. The oracle records the mark the MLC gives a fill under any replacement policy,
# the one REPL_MLP boosts, so these are what a study's oracle bounds. A marking flag Scarab adds later
# belongs here; until then, --marking passes it through as it is.
MARKING_PARAMS = ["mlp_data_bound", "mlp_data_bound_min", "mlp_lin_data_thresh",
                  "mlp_instr_mark", "mlp_lin_instr_thresh", "mlp_instr_starve", "mlp_instr_starve_min"]
# Flags the generator sets per config, so neither --marking nor --params may.
STUDY_FLAGS = re.compile(r"--(mlp_oracle\w*|mlc_cache_repl_policy|inst_limit|mlp_lin_data_lambda|mlp_lin_instr_lambda|"
                         r"mlp_boost_in_fills)\b")

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
RD_FLAGS = re.findall(r"--(\w+)", RD)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tag", required=True, help="names the study: mlp_oracle_google_<tag> and mlp_oracle_google_<tag>_ref")
    p.add_argument("--marking", default="", help='Scarab flags that change the marking, e.g. "--mlp_data_bound_min 8"; '
                   "every other marking flag takes the binary's default")
    p.add_argument("--params", default="",
                   help='Scarab flags that change the machine, for every config, e.g. "--mlc_size 1048576"')
    p.add_argument("--binary", default=DEFAULT_BINARY,
                   help="a git ref in --scarab-path (origin/... is fetched first) or scarab_<hash>, for every config "
                        f"(default {DEFAULT_BINARY})")
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
    a.marking_flags = flag_pairs(p, "--marking", a.marking)
    a.params_flags = flag_pairs(p, "--params", a.params)
    a.ref_flags = flag_pairs(p, "--ref-params", a.ref_params)
    for opt, flags in (("--marking", a.marking_flags), ("--params", a.params_flags), ("--ref-params", a.ref_flags)):
        bad = [f for f in flags if STUDY_FLAGS.fullmatch(f"--{f}")]
        if bad:
            p.error(f"{opt} must not set {', '.join(bad)}: the generator sets those per config")
    misplaced = [f for f in a.params_flags if f in MARKING_PARAMS]
    if misplaced:
        p.error(f"--params sets marking flags ({', '.join(misplaced)}): put them in --marking")
    a.pairs = []
    for pair in filter(None, (s.strip() for s in a.repl_mlp.split(","))):
        gi, _, gd = pair.partition(":")
        if not (gi.isdigit() and gd.isdigit()):
            p.error(f"--repl-mlp: {pair!r} is not gamma_i:gamma_d")
        a.pairs.append((int(gi), int(gd)))
    if a.check:
        a.warmup, a.measured = 1_000_000, 3_000_000
    resolve_binary(p, a)
    return a


def flag_pairs(p, opt, text):
    """Scarab flags as {name: value}, in order; each flag takes one value, and a later one wins."""
    toks = text.split()
    if len(toks) % 2 or any(not t.startswith("--") for t in toks[::2]):
        p.error(f'{opt} takes Scarab flags with a value each, e.g. "--mlp_data_bound_min 8"')
    return {toks[i][2:]: toks[i + 1] for i in range(0, len(toks), 2)}


def git(a, *args):
    return subprocess.run(["git", "-C", a.scarab_path, *args], capture_output=True, text=True)


def resolve_binary(p, a):
    """Pin --binary to a commit of --scarab-path, check that it has what the study needs and that every
    flag exists there, and write the marking out in full at its defaults (a.marking_full)."""
    m = re.fullmatch(r"scarab_([0-9a-f]{7,40})", a.binary)
    ref = m.group(1) if m else a.binary
    if not m and ref.startswith("origin/"):
        fetched = git(a, "fetch", "--quiet", "origin")
        if fetched.returncode:
            print(f"warning: git fetch origin failed in {a.scarab_path}; using the {ref} it has", file=sys.stderr)
    r = git(a, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if r.returncode:
        p.error(f"--binary: {ref} is not a commit in {a.scarab_path}")
    a.commit = r.stdout.strip()
    a.binary = a.binary if m else f"scarab_{git(a, 'rev-parse', '--short=7', a.commit).stdout.strip()}"
    a.commit_desc = git(a, "log", "-1", "--format=%h %cs %s", a.commit).stdout.strip()
    if git(a, "cat-file", "-e", f"{a.commit}:src/memory/mlp_oracle.cc").returncode:
        p.error(f"--binary: {a.commit_desc} has no mlp_oracle (scarab #36). Until #36 is on main, pass "
                "--binary origin/mlp-oracle")
    names = set(re.findall(r"DEF_PARAM\(\s*(\w+)", git(a, "grep", "-h", "DEF_PARAM(", a.commit, "--", "*.param.def").stdout))
    unknown = [f for f in (*RD_FLAGS, *a.marking_flags, *a.params_flags, *a.ref_flags) if f not in names]
    if unknown:
        p.error(f"--binary: {a.commit_desc} has no flag {', '.join(sorted(set(unknown)))}")
    preset = f"src/PARAMS.{a.architecture}"
    if git(a, "cat-file", "-e", f"{a.commit}:{preset}").returncode:
        p.error(f"--architecture: {a.commit_desc} has no {preset}")
    if not (Path(a.scarab_path) / preset).is_file():
        p.error(f"--architecture: sci also wants {preset} in the checkout of {a.scarab_path}")
    # The marking at this commit: memory.param.def's defaults, under the preset's (none set any today).
    marking = {}
    pdef = git(a, "show", f"{a.commit}:src/memory/memory.param.def").stdout
    for name, value in re.findall(r"DEF_PARAM\(\s*(\w+)\s*,\s*\w+\s*,\s*[\w*]+\s*,\s*\w+\s*,\s*([^,]+?)\s*,", pdef):
        if name in MARKING_PARAMS:
            marking[name] = value
    for name, value in re.findall(r"^--(\w+)\s+(\S+)", git(a, "show", f"{a.commit}:{preset}").stdout, re.M):
        if name in MARKING_PARAMS:
            marking[name] = value
    a.marking_defaults = dict(marking)
    marking.update(a.marking_flags)
    order = [n for n in MARKING_PARAMS if n in marking] + [n for n in marking if n not in MARKING_PARAMS]
    a.marking_full = " ".join(f"--{n} {marking[n]}" for n in order)


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
    params = " ".join(f"{params} {a.marking_full} {a.params} --inst_limit {a.warmup + a.measured}".split())
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
        "_architecture": "src/PARAMS.<architecture> at the binary's commit (sci also wants it in scarab_path's checkout).",
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
        "_configurations": (f"Binary: {a.commit_desc}. Marking on every config: {a.marking_full} "
                            f"(set by --marking: {a.marking or 'none'}). Machine flags on every config: "
                            f"{a.params or 'none'}. --inst_limit is warmup + measured; it overrides the run "
                            "script's cap at the trace length."),
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
    marking = f"{a.marking or 'the binary’s default marking'} on {a.architecture}" + (f", {a.params}" if a.params else "")
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
    print(f"binary {a.binary}: {a.commit_desc}")
    print(f"marking: {a.marking_full}")
    changed = {n: v for n, v in a.marking_flags.items() if a.marking_defaults.get(n) != v}
    if changed:
        print("  changed from the binary's defaults: " + ", ".join(
            f"{n} {a.marking_defaults.get(n, '(new)')} -> {v}" for n, v in changed.items()))
    print(f"architecture: {a.architecture}" + (f", machine flags: {a.params}" if a.params else ""))
    print(f"json/{ref_exp}.json: 1 config x {n_sims} simpoints")
    print(f"json/{exp}.json: {n_cfg} configs x {n_sims} simpoints = {n_cfg * n_sims} sims")
    print(f"  {', '.join(replay['configurations'])}")
    print(f"Run: tools/mlp_oracle/run_study.sh {exp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
