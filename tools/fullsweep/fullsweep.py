#!/usr/bin/env python3
"""Full-length runs of a gsearch result next to the comparison policies, run as cloudhost sweeps.

    fullsweep.py gen RESULT [--plan PLAN] [--name NAME] [--only A,B] [--skip A,B] [--suites S,..]
                            [--skip-suites S,..] [--pick best|max_point] [--pool h4d=5,n4d=10] [--dry-run] [--force]
    fullsweep.py estimate NAME [--pool SPEC]    simulations, core-hours, cloudhost's shards, an end-to-end estimate
    fullsweep.py run NAME [--yes] [--retry-failed]
                                                build once, then one `cloudhost sweep` per suite group
    fullsweep.py status NAME                    the sweeps' progress, per setup
    fullsweep.py collect NAME [--partial]       ./sci --collect-stats NAME once every sweep is complete
    fullsweep.py calibrate [STUDY...]           per-input sim cost from finished gsearch runs -> calibration.json

RESULT is a gsearch study and variant, e.g. cd1/small. See README.md.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import heapq
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
INFRA = Path(os.environ.get("FULLSWEEP_INFRA", HERE.parent.parent)).resolve()
SCARABINFRA_PY = Path.home() / "miniconda3" / "envs" / "scarabinfra" / "bin" / "python"
if (SCARABINFRA_PY.exists() and Path(sys.prefix).resolve() != SCARABINFRA_PY.parent.parent.resolve()
        and not os.environ.get("FULLSWEEP_NO_REEXEC")):
    # sci's own modules need the scarabinfra env (Python 3.12).
    os.execv(str(SCARABINFRA_PY), [str(SCARABINFRA_PY), str(Path(__file__).resolve()), *sys.argv[1:]])

sys.path.insert(0, str(INFRA))
sys.path.insert(0, str(INFRA / "scarab_stats"))
from scripts import utilities as U  # noqa: E402  sci's simpoint selection, exactly as ./sci --sim does it
import aggregation as AGG  # noqa: E402  sci's input -> workload grouping

CLOUDHOST = os.environ.get("CLOUDHOST", "/home/mgiordan/carp/cloudhost/cloudhost")
SCI = os.environ.get("FULLSWEEP_SCI", "./sci")
SWEEPS_DIR = None             # cloudhost's sweep state (<shared>/sweeps); None: as the cloudhost module says
JSON_DIR = INFRA / "json"     # where sci (and cloudhost) look for descriptors
DEFAULT_PLAN = HERE / "plans" / "full_sweep.json"
CALIBRATION = HERE / "calibration.json"
GAMMA_FLAGS = ("--mlp_lin_instr_lambda", "--mlp_lin_data_lambda")

# Machines a pool estimate can use: cores, RAM, $/h while running, and cloudhost's placement speed
# (FAMILY_SPEED, LOCAL_SPEED: slow suites prefer faster machines).
MACHINES = {"h4d": {"cores": 192, "mem_gb": 720, "usd_hr": 7.85, "speed": 2.0},
            "n4d": {"cores": 96, "mem_gb": 384, "usd_hr": 4.07, "speed": 1.0},
            "pakuchi": {"cores": 256, "mem_gb": None, "usd_hr": 0.0, "speed": 1.25}}
IDLE_TAIL_MIN = 20       # a cloud machine bills 20 idle minutes before it powers itself off
# A cloudhost launch ships the infra checkout, scarab_path, the build and the descriptor to the machine: 1.5-3 min
# each in gsearch's back-to-back launches (11-14), and a sweep launches at most SWEEP_MAX_LAUNCHING at once.
LAUNCH_MIN = 2.0
# From placement to a shard's first sims: the launch plus sci's preparation. gsearch's shards spent 2.8-4.7 min
# (median) beyond their sims, launch to lease end, including the end-of-run collection that no longer holds cores.
START_OVERHEAD_MIN = 3.0
# Fallback cost model, used where calibration.json has no entry. H4D minutes per million simulated instructions
# (warmup included) at full load, and other machines' per-sim slowdown, from SIM_HOST_WALL_SECONDS of the gsearch
# cd1 + dr1 runs on golden_small (11-14..15).
FALLBACK_RATE = {"google": 0.0816, "datacenter": 0.212, "spec2006": 0.066, "spec2017": 0.066, "spec2026": 0.0737,
                 "default": 0.075}
FALLBACK_SLOWDOWN = {"h4d": {"default": 1.0},
                     "n4d": {"google": 1.47, "datacenter": 1.68, "default": 1.9},
                     "pakuchi": {"google": 1.65, "datacenter": 1.71, "default": 1.7}}
DONE_SWEEP_STATES = ("complete",)


def die(msg):
    raise SystemExit(f"fullsweep: {msg}")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def load_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, obj):
    """sci's canonical descriptor formatting (json/ CI checks it on the files it covers)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, separators=(",", ":")) + "\n")
    tmp.replace(path)


def flag_groups(params):
    """'--a 1 --b 2' -> [('a', '--a 1'), ('b', '--b 2')]."""
    out = []
    for tok in (params or "").split():
        if tok.startswith("--"):
            out.append([tok[2:].split("=", 1)[0], [tok]])
        elif out:
            out[-1][1].append(tok)
    return [(f, " ".join(t)) for f, t in out]


def flag_value(params, flag):
    for f, text in flag_groups(params):
        if f == flag:
            parts = text.replace("=", " ").split()
            return parts[1] if len(parts) > 1 else None
    return None


def strip_flag(params, flag):
    return " ".join(text for f, text in flag_groups(params) if f != flag)


def gnum(x):
    x = float(x)
    return str(int(x)) if x.is_integer() else f"{x:g}"


def gname(pt):
    return f"gi{gnum(pt[0])}_gd{gnum(pt[1])}"


def gflags(pt):
    return f"{GAMMA_FLAGS[0]} {gnum(pt[0])} {GAMMA_FLAGS[1]} {gnum(pt[1])}"


def fmt_n(n):
    return f"{n:,}"


# ---------------------------------------------------------------- gsearch results
class Gsearch:
    """One variant of a finished gsearch study (tools/mlp_gsearch): its run settings and best points."""

    def __init__(self, gsearch_dir, result, pick):
        study, sep, var = result.partition("/")
        if not sep or not study or not var:
            die(f"RESULT is STUDY/VARIANT, e.g. cd1/small (got {result!r})")
        self.dir = Path(gsearch_dir) / study
        self.result, self.study, self.variant, self.pick = result, study, var, pick
        sj = load_json(self.dir / "study.json")
        if var not in sj["variants"]:
            die(f"study {study} has variants {sorted(sj['variants'])}, not {var!r}")
        tmpls = {lab: load_json(self.dir / "templates" / f"{t}.json") for lab, t in sj["variants"][var].items()}
        bases = {lab: next(iter(t["configurations"].values())) for lab, t in tmpls.items()}
        archs = {t["architecture"] for t in tmpls.values()}
        params = {strip_flag(b.get("params", ""), "inst_limit") for b in bases.values()}
        if len(archs) != 1 or len(params) != 1:
            die(f"{result}: templates disagree on architecture {archs} or base params {params}")
        self.arch = archs.pop()
        self.base_params = params.pop()
        self.search_binaries = sorted({b["binary"] for b in bases.values()})
        self.tables = {}

    def table(self, level):
        if level not in self.tables:
            path = self.dir / "report" / f"best_{level}.csv"
            with open(path) as f:
                self.tables[level] = {r["name"]: r for r in csv.DictReader(f) if r["variant"] == self.variant}
            if not self.tables[level]:
                die(f"{path} has no rows for variant {self.variant}")
        return self.tables[level]

    def row(self, level, name):
        r = self.table(level).get(name)
        if r is None:
            die(f"{self.result}: no {level} unit {name!r} (have {sorted(self.table(level))[:8]}...)")
        return r

    def point(self, row, pick=None):
        g = json.loads(row[pick or self.pick])
        return float(g[0]), float(g[1])


# ---------------------------------------------------------------- calibration (cost model)
class Cost:
    """H4D minutes per sim of an input, and other machines' slowdown, from calibration.json or the fallback."""

    def __init__(self, arch, path=CALIBRATION):
        cal = load_json(path) if Path(path).exists() else {}
        self.arch = arch
        self.inputs = cal.get("h4d_min_per_minstr", {}).get(arch, {})
        self.suites = {**FALLBACK_RATE, **cal.get("suite_h4d_min_per_minstr", {}).get(arch, {})}
        self.slow = copy.deepcopy(FALLBACK_SLOWDOWN)
        for m, by_suite in cal.get("slowdown", {}).get(arch, {}).items():
            self.slow.setdefault(m, {"default": 1.0}).update(by_suite)
        self.source = f"{path.name} ({len(self.inputs)} inputs)" if self.inputs else "fallback per-suite rates"

    def h4d_min(self, label, suite, instr):
        rate = self.inputs.get(label) or self.suites.get(suite) or self.suites["default"]
        return rate * instr / 1e6

    def slowdown(self, machine, suite):
        s = self.slow.get(machine, {"default": 1.0})
        return s.get(suite, s.get("default", 1.0))


# ---------------------------------------------------------------- the sweep
class Input:
    """One workload input the sweep runs, with every selected simpoint."""

    def __init__(self, suite, subsuite, workload, group, cids, instr, warmup, sim_type, weights=None):
        self.suite, self.subsuite, self.workload, self.group = suite, subsuite, workload, group
        self.cids, self.instr, self.warmup, self.sim_type = cids, instr, warmup, sim_type
        self.weights = weights or {c: 1.0 / len(cids) for c in cids}   # renormalized by sci's selection
        self.label = f"{suite}/{subsuite}/{workload}"
        self.n = len(cids)
        self.cost = 0.0          # H4D minutes per sim

    def sim_entry(self):
        return {"suite": self.suite, "subsuite": self.subsuite, "workload": self.workload, "cluster_id": None,
                "simulation_type": self.sim_type, "warmup": self.warmup}


def selection_fields(plan):
    """Descriptor-level simpoint selection for every suite of the plan (one descriptor can hold them all)."""
    tops, cov = set(), {}
    for suite, sc in plan["suites"].items():
        has_top, has_cov = sc.get("top_simpoint") is not None, sc.get("simpoint_coverage") is not None
        if has_top == has_cov:
            die(f"suite {suite}: give exactly one of top_simpoint and simpoint_coverage")
        if has_top:
            tops.add(sc["top_simpoint"])
        cov[suite] = sc.get("simpoint_coverage")
    if len(tops) > 1:
        die(f"suites use different top_simpoint values {sorted(tops)}; the merged descriptor can hold only one")
    return {"top_simpoint": tops.pop() if tops else None,
            "simpoint_coverage": cov if any(v is not None for v in cov.values()) else None}


def expand(plan, cost):
    """Every input of the sweep with its selected simpoints, through sci's own selection code."""
    sel = selection_fields(plan)
    db = U.load_workloads_for_descriptor(sel, str(INFRA), dbg_lvl=0)
    if db is None:
        die(f"cannot read {INFRA}/workloads/workloads_db.json")
    inputs = []
    for suite, sc in plan["suites"].items():
        if suite not in db:
            die(f"suite {suite} is not in the workload DB")
        if sc["group"] not in plan["groups"]:
            die(f"suite {suite}: unknown group {sc['group']!r}")
        mode = sc.get("simulation_type") or "memtrace"
        subs = sc.get("subsuites") or [s for s, ws in db[suite].items() if isinstance(ws, dict)]
        found = 0
        for sub in subs:
            for wl, node in sorted(db[suite][sub].items()):
                if not isinstance(node, dict) or mode not in node.get("simulation", {}):
                    continue
                if sc.get("workloads") and wl not in sc["workloads"]:
                    continue
                sp = U.get_simpoints(node, mode, 0) if "simpoints" in node else {"0": 1.0}
                weights = {int(c): float(w) for c, w in sp.items()}
                cids = list(weights)      # sci's order, as plan_run.py lists them
                if not cids:
                    continue
                sim = node["simulation"][mode]
                limit = flag_value(sc.get("params", ""), "inst_limit")
                db_warm = sim.get("warmup") or 0
                warm = sc.get("warmup")
                if warm is not None and warm > db_warm:
                    die(f"{suite}/{sub}/{wl}: warmup {warm} is more than the trace has ({db_warm})")
                instr = int(limit) if limit else (db_warm if warm is None else warm) + (sim.get("segment_size") or 0)
                inp = Input(suite, sub, wl, sc["group"], cids, instr, warm, mode, weights)
                inp.cost = cost.h4d_min(inp.label, suite, instr)
                inputs.append(inp)
                found += 1
        if not found:
            die(f"suite {suite}: no inputs with simulation type {mode}")
    return inputs, sel


def resolve_binary(plan, policy=None):
    b = (policy or {}).get("binary") or plan.get("binary") or "HEAD"
    if b.startswith("scarab_"):
        return b
    r = subprocess.run(["git", "-C", plan["scarab_path"], "rev-parse", "--short=7", b], capture_output=True, text=True)
    if r.returncode != 0:
        die(f"cannot resolve binary {b!r} in {plan['scarab_path']}: {r.stderr.strip()}")
    return f"scarab_{r.stdout.strip()}"


def choose_policies(plan, only, skip):
    pol = plan["policies"]
    bad = [p for p in (only or []) + (skip or []) if p not in pol]
    if bad:
        die(f"unknown policies {bad}; the plan has {list(pol)}")
    if only:
        names = [p for p in pol if p in only]
    else:
        names = [p for p in pol if pol[p].get("enabled", True)]
    names = [p for p in names if p not in (skip or [])]
    if not names:
        die("no policies selected")
    return names


def mlp_config(name, p, gs, inputs):
    """REPL_MLP params (+ per-input workload_params) and a description of where the gammas came from."""
    if gs is None:
        die(f"policy {name} needs a gsearch RESULT")
    pick = p.get("pick", gs.pick)
    extra = p.get("params", "")
    spec = p["mlp"]
    base = " ".join(x for x in (gs.base_params, extra) if x)
    if isinstance(spec, list):
        pt = (float(spec[0]), float(spec[1]))
        return f"{base} {gflags(pt)}", {}, {"gammas": gname(pt), "source": "explicit"}
    kind, _, unit = spec.partition(":")
    if kind == "core":
        r = gs.row("core", unit or "all")
        pt = gs.point(r, pick)
        info = {"gammas": gname(pt), "source": f"{gs.result} core {unit or 'all'} ({pick})",
                "gsearch_gain_pct": float(r["gain_pct"] if pick == "best" else r["max_gain_pct"]),
                "converged": r["converged"] == "True"}
        return f"{base} {gflags(pt)}", {}, info
    if kind not in UNIT_LEVELS:
        die(f"policy {name}: mlp must be core[:unit], {', '.join(UNIT_LEVELS)} or [gi, gd] (got {spec!r})")
    chain = fallback_chain(name, p, kind)
    core_unit = chain[-1].partition(":")[2] or "all"
    core_pt = gs.point(gs.row("core", core_unit), pick)
    hier = AGG.Hierarchy([i.label for i in inputs])
    workload_of = {lab: w for w, labs in hier.workloads.items() for lab in labs}

    def unit(level, i, cid=None):
        return {"simpoint": f"{i.label}:{cid}", "input": i.label, "workload": workload_of[i.label],
                "suite": i.suite}[level]

    unconverged = set()

    def resolve(levels, i):
        """The first level of `levels` that gsearch has a unit for: (point, level)."""
        for lev in levels:
            if lev.startswith("core"):
                return core_pt, "core"
            r = gs.table(lev).get(unit(lev, i))
            if r is not None:
                if r["converged"] != "True":
                    unconverged.add(unit(lev, i))
                return gs.point(r, pick), lev
        return core_pt, "core"

    wp, used, levels, own_weight = {}, Counter(), Counter(), []
    for i in inputs:
        if kind == "simpoint":
            # One entry per input from the fallback chain, then one per searched simpoint: sci layers the more
            # specific selector last, so a searched simpoint runs its own optimum, every other its input's fallback.
            fb_pt, fb_lev = resolve(chain, i)
            wp[i.label] = gflags(fb_pt)
            tab = gs.table("simpoint")
            w_own = 0.0
            for cid in i.cids:
                r = tab.get(unit("simpoint", i, cid))
                if r is None:
                    used[gname(fb_pt)] += 1
                    levels[fb_lev] += 1
                    continue
                if r["converged"] != "True":
                    unconverged.add(unit("simpoint", i, cid))
                pt = gs.point(r, pick)
                wp[f"{i.label}:{cid}"] = gflags(pt)
                used[gname(pt)] += 1
                levels["simpoint"] += 1
                w_own += i.weights[cid]
            own_weight.append(w_own)
        else:
            pt, lev = resolve([kind] + chain, i)
            wp[i.label] = gflags(pt)
            used[gname(pt)] += i.n
            levels[lev] += i.n
    n_sp = sum(i.n for i in inputs)
    if kind == "simpoint":
        gammas = (f"per simpoint where searched: {levels['simpoint']} of {n_sp} simpoints, "
                  f"{sum(own_weight) / len(own_weight):.0%} of an input's weight on average")
    else:
        units = {unit(kind, i) for i in inputs}
        gammas = f"per {kind} ({len(units)} units)"
    info = {"gammas": f"{gammas}; {len(used)} distinct points",
            "source": f"{gs.result} best_{kind}.csv ({pick}); fallback {' > '.join(chain)} ({gname(core_pt)})",
            "levels_used": dict(levels),
            "unconverged_units": sorted(unconverged), "points_by_simpoints": dict(used.most_common())}
    return f"{base} {gflags(core_pt)}", wp, info


UNIT_LEVELS = ("simpoint", "input", "workload", "suite")


def fallback_chain(name, p, kind):
    """Where a unit gsearch never searched gets its gammas: a list of coarser levels ending in a core unit.
    Default for simpoint: input > workload > core:all (the most specific searched unit); else core:all.
    A bare string is a core unit name (the old form)."""
    fb = p.get("fallback")
    if fb is None:
        fb = ["input", "workload", "core:all"] if kind == "simpoint" else ["core:all"]
    if isinstance(fb, str):
        fb = [fb if ":" in fb or fb in UNIT_LEVELS else f"core:{fb}"]
    fb = [x for x in fb if x != "simpoint"]
    bad = [x for x in fb if x not in UNIT_LEVELS and not x.startswith("core")]
    if bad:
        die(f"policy {name}: fallback levels must be {', '.join(UNIT_LEVELS[1:])} or core:<unit> (got {bad})")
    if not fb or not fb[-1].startswith("core"):
        fb = fb + ["core:all"]
    return fb


def make_configs(plan, gs, inputs, names, binary):
    suite_wp = {s: sc["params"] for s, sc in plan["suites"].items() if sc.get("params")}
    db = U.read_descriptor_from_json(str(INFRA / "workloads" / "workloads_db.json"), 0)
    configs, info = {}, {}
    for name in names:
        p = plan["policies"][name]
        wp = dict(suite_wp)
        if "mlp" in p:
            params, extra, info[name] = mlp_config(name, p, gs, inputs)
            for k, v in extra.items():
                wp[k] = f"{wp[k]} {v}" if k in wp else v
        else:
            params, info[name] = p.get("params", "").strip(), {"gammas": "", "source": "params"}
        c = {"_params": p.get("_doc", ""), "params": params.strip(), "binary": resolve_binary(plan, p) if p.get("binary") else binary,
             "slurm_options": "", "memory_overhead_mb": 64}
        if wp:
            c["_workload_params"] = ("Per-suite params (run length) and, for per-unit REPL_MLP, each input's own "
                                     "gammas, layered on params by sci (most specific last).")
            c["workload_params"] = wp
        configs[name] = c
        try:
            U.parse_workload_params(name, wp, db)     # sci's own check: every selector names a real input
        except RuntimeError as e:
            die(str(e))
    return configs, info


def descriptor(plan, experiment, inputs, configs, sel, arch, traces_dir, max_parallel, note, meta):
    d = load_json(INFRA / "json" / "exp.json")
    d["architecture"] = arch
    d["_experiment"] = note
    d["experiment"] = experiment
    d["workload_manager"] = "manual"
    d["max_parallel"] = max_parallel
    d["container_reuse"] = True
    d["root_dir"] = plan["root_dir"]
    d["application_dir"] = plan["application_dir"]
    d["scarab_path"] = plan["scarab_path"]
    d["scarab_build"] = plan.get("scarab_build", "opt")
    d["traces_dir"] = traces_dir
    d["top_simpoint"] = sel["top_simpoint"]
    d["simpoint_coverage"] = sel["simpoint_coverage"]
    d["_simulations"] = "Whole inputs (every selected simpoint), longest first. Written by tools/fullsweep."
    # Longest inputs first: cloudhost's shards keep this order, so a shard's tail is short sims.
    d["simulations"] = [i.sim_entry() for i in sorted(inputs, key=lambda i: -i.cost)]
    d["configurations"] = configs
    d["collect_stats"] = {"configs": None}
    base = plan.get("baseline") if plan.get("baseline") in configs else next(iter(configs))
    d["visualize"] = {"baseline": base, "configs": [c for c in configs if c != base] or [base], "counters": ["IPC"]}
    d["perf_analyze"]["baseline"] = base
    d["_fullsweep"] = "Bookkeeping for tools/fullsweep/fullsweep.py."
    d["fullsweep"] = meta
    return d


def config_digest(d, config):
    """What a setup's results depend on: its config and the sims it runs."""
    keep = {k: d[k] for k in ("architecture", "simulations", "top_simpoint", "simpoint_coverage")}
    keep["configuration"] = d["configurations"][config]
    return hashlib.sha1(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:12]


def pinned_simpoints(d):
    """Every simpoint of descriptor d, pinned as cloudhost's plan_run.py lists them for `sweep start` to split
    (through sci's own selection; the tests check the two agree)."""
    db = U.load_workloads_for_descriptor(d, str(INFRA), dbg_lvl=0)
    out = []
    for sim in U.normalize_simulations(d["simulations"]):
        suite, sub, wl = sim["suite"], sim["subsuite"], sim["workload"]
        info = db[suite][sub][wl]
        mode = sim.get("simulation_type") or info["simulation"]["prioritized_mode"]
        ids = list(U.get_simpoints(info, mode, 0)) if "simpoints" in info else ["0"]
        out += [{"suite": suite, "subsuite": sub, "workload": wl, "cluster_id": int(c) if str(c).isdigit() else c,
                 "simulation_type": mode, "warmup": sim.get("warmup")} for c in ids]
    return out


# ---------------------------------------------------------------- cloudhost
_CH = None


def ch():
    """cloudhost's own code (its shard split, constants and sweep state paths), loaded from CLOUDHOST."""
    global _CH
    if _CH is None:
        loader = SourceFileLoader("cloudhost_tool", CLOUDHOST)
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("cloudhost_tool", loader))
        loader.exec_module(mod)
        for need in ("split_shards", "SWEEPS", "SWEEP_MAX_LAUNCHING", "MIN_SLICE", "MEM_RESERVE_GB"):
            if not hasattr(mod, need):
                die(f"{CLOUDHOST} has no {need}: it predates `cloudhost sweep`")
        _CH = mod
    return _CH


def sweeps_dir():
    return Path(SWEEPS_DIR) if SWEEPS_DIR else Path(ch().SWEEPS)


def sweep_state(sid):
    f = sweeps_dir() / sid / "state.json"
    return load_json(f) if f.exists() else None


def driver_alive(sw):
    pid = sw.get("driver_pid")
    return bool(pid) and sw.get("driver_host") == os.uname().nodename and Path(f"/proc/{pid}").exists()


def multi_runner_safe(infra=INFRA):
    try:
        return "MULTI_RUNNER_SAFE = 1" in (Path(infra) / "scripts" / "utilities.py").read_text()
    except OSError:
        return False


def group_shards(st, group, d, configs=None):
    """The shards `cloudhost sweep start` will cut group's descriptor d into (cloudhost's own split_shards)."""
    g = st["plan"]["groups"][group]
    if configs is not None:
        d = dict(d, configurations={c: v for c, v in d["configurations"].items() if c in configs})
    pins = pinned_simpoints(d)
    p = {"simpoints": pins, "sims": len(pins) * len(d["configurations"])}
    slow = set(st["groups"][group]["suites"]) if g.get("slow") else set()
    shards = ch().split_shards(st["groups"][group]["desc"], "estimate", d, p, g["shard_size"], slow)
    return [{"configs": list(sh["desc"]["configurations"]), "suite": sh["suite"], "slow": sh["slow"],
             "sims": [f"{s['suite']}/{s['subsuite']}/{s['workload']}" for s in sh["desc"]["simulations"]]}
            for sh in shards]


# ---------------------------------------------------------------- estimates
def parse_pool(spec):
    pool = {}
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        k, _, v = part.partition("=")
        if k not in MACHINES or not v.isdigit():
            die(f"--pool is like h4d=5,n4d=10,pakuchi=128 (pakuchi in cores); got {part!r}")
        pool[k] = int(v)
    return pool


def _choose(sw, sh, machines, min_slice):
    """cloudhost's placement of one shard (place + cloud_fit), on a pool we have to ourselves: (machine, cores)."""
    n, gb, slow = len(sh["durs"]), sw["gb"], sw["slow"]
    full, partial = [], []
    for mi, m in enumerate(machines):
        local = m["type"] == "pakuchi"
        if (local and (sw["where"] == "cloud" or not sh["local"])) or (not local and sw["where"] == "local"):
            continue
        slots = min(m["cores"], 192) if local else min(m["cores"], int(m["mem"] // gb))
        want = min(n, slots)
        grant = min(want, m["free_c"] if local else min(m["free_c"], int(m["free_m"] // gb)))
        in_use = m["free_c"] < m["cores"]
        speed = MACHINES[m["type"]]["speed"]
        if grant == want and grant > 0:
            # Slow shards: the fastest machine first (H4D > pakuchi > N4D); others: pakuchi, then machines already
            # running something, H4D before N4D, tightest fit.
            key = (-speed, not in_use, slots - grant) if slow else (not local, not in_use, -speed, slots - grant)
            full.append((key, mi, grant))
        elif not local and n > grant >= min_slice:
            partial.append(((-speed if slow else not in_use), -grant, mi, grant))
    if full:
        _, mi, grant = min(full)
        return mi, grant
    if partial:
        *_, mi, grant = min(partial)
        return mi, grant
    return None


def simulate(sweeps, pool, cost):
    """Event-driven model of cloudhost sweeps running at once on `pool`. Each sweep places its shards in order
    (slow sweeps first), at most SWEEP_MAX_LAUNCHING launching at a time, each where cloudhost would put it, on a
    slice of min(sims, the machine's slots) cores (or a partial slice of at least MIN_SLICE). A shard's sims start
    START_OVERHEAD_MIN after placement, longest first on the first free core of its slice, and every core goes back
    to the machine as its last sim ends (cloudhost's tail release). Returns the machines, or None if a shard fits
    nowhere."""
    c = ch()
    machines = []
    for t, cnt in pool.items():
        for k in range(1 if t == "pakuchi" else cnt):
            m = MACHINES[t]
            cores = cnt if t == "pakuchi" else m["cores"]
            mem = m["mem_gb"] - c.MEM_RESERVE_GB if m["mem_gb"] else None
            machines.append({"type": t, "cores": cores, "mem": mem, "free_c": cores, "free_m": mem,
                             "first": None, "last": 0.0, "shards": 0})
    if not machines:
        return None
    releases = []                                     # (time, machine, GB of the sim that held the core)
    pending = {sw["name"]: list(sw["shards"]) for sw in sweeps}
    launch = {sw["name"]: [0.0] * c.SWEEP_MAX_LAUNCHING for sw in sweeps}
    order = sorted(sweeps, key=lambda sw: not sw["slow"])
    t = 0.0
    while True:
        placed = True
        while placed:
            placed = False
            for sw in order:
                q, slots = pending[sw["name"]], launch[sw["name"]]
                k = min(range(len(slots)), key=slots.__getitem__)
                if not q or slots[k] > t + 1e-9:
                    continue
                got = _choose(sw, q[0], machines, c.MIN_SLICE)
                if got is None:
                    continue
                mi, grant = got
                m, sh = machines[mi], q.pop(0)
                m["free_c"] -= grant
                if m["mem"] is not None:
                    m["free_m"] -= grant * sw["gb"]
                slots[k] = t + LAUNCH_MIN
                cores = [t + START_OVERHEAD_MIN] * grant
                for dur in sorted((h4d * cost.slowdown(m["type"], suite) for h4d, suite in sh["durs"]), reverse=True):
                    heapq.heapreplace(cores, cores[0] + dur)
                for end in cores:
                    heapq.heappush(releases, (end, mi, sw["gb"]))
                m["first"] = t if m["first"] is None else m["first"]
                m["last"] = max(m["last"], max(cores))
                m["shards"] += 1
                placed = True
        if not any(pending.values()):
            return machines
        nxt = [releases[0][0]] if releases else []
        nxt += [min(launch[n]) for n, q in pending.items() if q and min(launch[n]) > t + 1e-9]
        if not nxt:
            return None
        t = max(t, min(nxt))
        while releases and releases[0][0] <= t + 1e-9:
            _, mi, gb = heapq.heappop(releases)
            machines[mi]["free_c"] += 1
            if machines[mi]["mem"] is not None:
                machines[mi]["free_m"] += gb


def print_estimate(st, descs, pool_spec, configs=None):
    """Counts, H4D core-hours, cloudhost's shards per group and an end-to-end estimate. descs: group -> descriptor;
    configs: only these setups (e.g. setups added after the first sweeps)."""
    plan = st["plan"]
    cost = Cost(st["arch"])
    pols = [p for p in st["policies"] if configs is None or p in configs]
    n_sp = sum(v["simpoints"] for v in st["suites"].values())
    print(f"\n{st['name']}: {len(pols)} setups x {fmt_n(n_sp)} simpoints = {fmt_n(len(pols) * n_sp)} simulations"
          + ("" if configs is None else f" ({', '.join(pols)})"))
    print(f"  {st['arch']}, {st['binary']}" + (f", REPL_MLP gammas from gsearch {st['result']} ({plan.get('pick', 'best')})"
                                             if st.get("result") else ""))
    print(f"  cost model: {cost.source}; H4D minutes per sim at full load\n")
    print(f"  {'suite':11s} {'group':7s} {'inputs':>6s} {'simpoints':>9s} {'selection':>12s} {'instr/sim':>9s} {'H4D min/sim':>11s}")
    for suite, v in st["suites"].items():
        print(f"  {suite:11s} {v['group']:7s} {v['inputs']:6d} {v['simpoints']:9d} {v['selection']:>12s} "
              f"{v['instr'] / 1e6:8.0f}M {v['h4d_min_per_sim']:11.1f}")
    per_setup = sum(v["simpoints"] * v["h4d_min_per_sim"] for v in st["suites"].values()) / 60
    print(f"\n  {fmt_n(round(per_setup))} H4D core-hours per setup, {fmt_n(round(per_setup * len(pols)))} in all")

    sweeps = []
    local_suites = set(plan.get("pakuchi_suites", ["google", "datacenter"]))
    for group, d in descs.items():
        g = plan["groups"][group]
        shards = group_shards(st, group, d, configs)
        if not shards:
            continue
        sizes = [len(s["configs"]) * len(s["sims"]) for s in shards]
        print(f"  {group}: {fmt_n(sum(sizes))} sims in {len(shards)} shards of {min(sizes)}-{max(sizes)} "
              f"(cloudhost sweep --shard-size {g['shard_size']}"
              + (f", --mem-per-sim {g['mem_per_sim_gb']:g}" if g.get("mem_per_sim_gb") else "")
              + f", --where {g.get('where', 'any')}{', slow: first and on the fastest machines' if g.get('slow') else ''})")
        sweeps.append({"name": group, "slow": bool(g.get("slow")), "where": g.get("where", "any"),
                       "gb": g.get("mem_per_sim_gb") or ch().DEFAULT_MEM_PER_SIM_GB,
                       "shards": [{"local": s["suite"] in local_suites,
                                   "durs": [(st["input_cost"][lab], lab.split("/")[0])
                                            for lab in s["sims"] for _ in s["configs"]]} for s in shards]})
    pool = parse_pool(pool_spec)
    machines = simulate(sweeps, pool, cost)
    if machines is None:
        print(f"\n  pool {pool_spec}: some shard has no machine it may run on")
        return
    used = [m for m in machines if m["shards"]]
    makespan = max(m["last"] for m in used)
    floor = 0.0
    for res in ("cores", "mem"):       # no packing, launch or tail limits: the busier of cores and memory
        need = sum(h * (1 if res == "cores" else sw["gb"]) for sw in sweeps for s in sw["shards"] for h, _ in s["durs"])
        have = sum((m["cores"] if res == "cores" or m["mem"] is None else m["mem"])
                   / min(cost.slowdown(m["type"], suite) for suite in st["suites"]) for m in machines)
        floor = max(floor, need / have)
    vm_hours = defaultdict(float)
    for m in used:
        if m["type"] != "pakuchi":
            vm_hours[m["type"]] += (m["last"] - m["first"] + IDLE_TAIL_MIN) / 60
    usd = sum(h * MACHINES[t]["usd_hr"] for t, h in vm_hours.items())
    print(f"\n  On pool {pool_spec} (ours alone; every group's sweep at once, slow ones first):")
    print(f"    ~{makespan / 60:.1f} h end to end (with perfect packing and no tail: {floor / 60:.1f} h); "
          f"{len(used)} machines used, up to {max(m['shards'] for m in used)} shards on one")
    print(f"    ~{sum(vm_hours.values()):.0f} VM-hours ({', '.join(f'{t} {h:.0f}' for t, h in sorted(vm_hours.items()))}), "
          f"~${usd:,.0f} incl. {IDLE_TAIL_MIN} idle min per machine")


# ---------------------------------------------------------------- state
def master_desc_path(name):
    return JSON_DIR / f"{name}.json"


def state_path(name, state_dir=None):
    if state_dir is None:
        p = master_desc_path(name)
        if not p.exists():
            die(f"no sweep {name}: {p} is missing (run gen first)")
        state_dir = load_json(p)["fullsweep"]["state_dir"]
    return Path(state_dir) / name / "state.json"


class State:
    def __init__(self, name, state_dir=None):
        self.path = state_path(name, state_dir)
        self.data = load_json(self.path) if self.path.exists() else None
        if self.data is not None and "groups" not in self.data:
            die(f"{self.path} was written by the old shard-based fullsweep; rerun gen")

    def save(self):
        write_json(self.path, self.data)


def group_descs(st):
    return {g: load_json(JSON_DIR / f"{gi['desc']}.json") for g, gi in st["groups"].items()}


# ---------------------------------------------------------------- gen
def choose_suites(plan, only, skip):
    """The suites to run: the plan's enabled ones, or exactly --suites, minus --skip-suites."""
    su = plan["suites"]
    bad = [x for x in (only or []) + (skip or []) if x not in su]
    if bad:
        die(f"unknown suites {bad}; the plan has {list(su)}")
    names = [x for x in su if (x in only if only else su[x].get("enabled", True)) and x not in (skip or [])]
    if not names:
        die("no suites selected")
    return names


def build_sweep(plan_path, result=None, only=None, skip=None, pick=None, name=None, force=False, plan=None,
                suites=None, skip_suites=None):
    """Everything gen writes, without writing it: {"st", "files" (descriptor name -> descriptor), "master",
    "st_path", "inputs"}. `plan` (a dict) overrides reading plan_path."""
    plan_path = Path(plan_path).resolve()
    plan = copy.deepcopy(plan) if plan is not None else load_json(plan_path)
    if pick:
        plan["pick"] = pick
    default_suites = choose_suites(plan, None, None)
    suite_names = choose_suites(plan, suites, skip_suites)
    plan["suites"] = {x: plan["suites"][x] for x in suite_names}
    names = choose_policies(plan, only, skip)
    needs_gs = any("mlp" in plan["policies"][p] for p in names)
    gs = Gsearch(plan["gsearch_dir"], result, plan.get("pick", "best")) if result else None
    if needs_gs and gs is None:
        die("REPL_MLP setups need a gsearch RESULT (e.g. cd1/small)")
    arch = plan.get("architecture") or (gs.arch if gs else None)
    if not arch:
        die("no architecture: give a RESULT or set plan.architecture")
    if gs and plan.get("architecture") and plan["architecture"] != gs.arch:
        die(f"plan architecture {plan['architecture']} != gsearch {result}'s {gs.arch}")
    if not name:
        name = f"fs_{gs.study}_{gs.variant}" if gs else f"fs_{arch}"
        if suite_names != default_suites:
            name += "_" + "_".join(suite_names)          # e.g. fs_cd1_small_google
    binary = resolve_binary(plan)
    cost = Cost(arch)
    inputs, sel = expand(plan, cost)
    configs, info = make_configs(plan, gs, inputs, names, binary)

    st_path = Path(plan["state_dir"]) / name / "state.json"
    old = load_json(st_path) if st_path.exists() else None
    launched = (old or {}).get("sweeps", [])
    why = "; it has launched sweeps, so use a new --name" if launched else "; pass --name, or --force to replace it"
    if old and old.get("result") != result and (launched or not force):
        die(f"sweep {name} exists for gsearch {old.get('result')}{why}")
    if old and list(old.get("suites", {})) != suite_names and (launched or not force):
        die(f"sweep {name} exists with suites {list(old.get('suites', {}))}, not {suite_names}{why}")

    suite_order = list(plan["suites"])
    inputs.sort(key=lambda i: (suite_order.index(i.suite), i.label))
    files, groups = {}, {}
    for gname_, g in plan["groups"].items():
        g_inputs = [i for i in inputs if i.group == gname_]
        if not g_inputs:
            continue
        missing = [k for k in ("traces_dir", "shard_size") if not g.get(k)]
        if missing:
            die(f"group {gname_} needs {missing} (see the plan's _groups)")
        dname = f"{name}_{gname_}"
        g_suites = [s for s in suite_order if any(i.suite == s for i in g_inputs)]
        meta = {"sweep": name, "group": gname_, "state_dir": plan["state_dir"]}
        note = (f"The {gname_} suites ({', '.join(g_suites)}) of full sweep {name} (tools/fullsweep), run as one "
                f"`cloudhost sweep`; every group writes experiment {name}. Written by fullsweep.py gen; do not edit.")
        d = descriptor(plan, name, g_inputs, configs, sel, arch, g["traces_dir"], g.get("max_parallel"), note, meta)
        digests = {c: config_digest(d, c) for c in configs}
        for sw in launched:
            if sw["group"] != gname_:
                continue
            for c in sw["configs"]:
                if c in digests and sw["digests"].get(c) != digests[c]:
                    die(f"setup {c} already ran on the {gname_} suites (sweep {sw['id']}) with different params or "
                        f"simpoints; give the changed sweep a new --name")
        files[dname] = d
        n_sp = sum(i.n for i in g_inputs)
        groups[gname_] = {"desc": dname, "configs": list(configs), "suites": g_suites, "simpoints": n_sp,
                          "sims": n_sp * len(configs), "digests": digests}

    by_suite = {}
    for suite in suite_order:
        si = [i for i in inputs if i.suite == suite]
        sc = plan["suites"][suite]
        sel_txt = (f"top {sc['top_simpoint']}" if sc.get("top_simpoint") is not None
                   else f"{sc['simpoint_coverage']:.0%} weight")
        by_suite[suite] = {"group": sc["group"], "inputs": len(si), "simpoints": sum(i.n for i in si),
                           "selection": sel_txt, "instr": si[0].instr if si else 0,
                           "h4d_min_per_sim": sum(i.n * i.cost for i in si) / max(1, sum(i.n for i in si))}
    st = {"name": name, "result": result, "plan_file": str(plan_path), "plan": plan, "arch": arch,
          "binary": binary, "search_binaries": gs.search_binaries if gs else [], "created": (old or {}).get("created", now()),
          "generated": now(), "policies": {p: info[p] for p in names}, "suites": by_suite, "groups": groups,
          "input_cost": {i.label: round(i.cost, 4) for i in inputs}, "sweeps": launched}

    # Master: every selected setup on every input, for collection; each group's sweep runs its own descriptor.
    meta = {"sweep": name, "master": True, "state_dir": plan["state_dir"], "gsearch": result}
    note = (f"Full sweep {name} (tools/fullsweep): every setup on every suite, for ./sci --collect-stats / "
            f"--visualize / --perf-analyze. Do not --sim it: fullsweep.py run sweeps json/{name}_<group>.json.")
    first_g = next(iter(groups))
    master = descriptor(plan, name, inputs, configs, sel, arch, plan["groups"][first_g]["traces_dir"], None, note, meta)
    master["_traces_dir"] = "Unused: this descriptor is only collected. Each group's descriptor has its traces_dir."
    return {"st": st, "files": files, "master": master, "st_path": st_path, "inputs": inputs}


def stale_files(name, keep, state_dir):
    """Descriptors an earlier gen of sweep `name` wrote that it no longer makes (e.g. the old per-setup shards)."""
    out = []
    for p in sorted(JSON_DIR.glob(f"{name}_*.json")) + sorted((Path(state_dir) / name / "shards").glob("*.json")):
        if p.parent == JSON_DIR and p.stem in keep:
            continue
        try:
            if load_json(p).get("fullsweep", {}).get("sweep") == name:
                out.append(p)
        except (OSError, json.JSONDecodeError):
            continue
    return out


def cmd_gen(a):
    out = build_sweep(a.plan, a.result, a.only, a.skip, a.pick, a.name, a.force, suites=a.suites,
                      skip_suites=a.skip_suites)
    st, files, master, st_path, name = out["st"], out["files"], out["master"], out["st_path"], out["st"]["name"]
    if a.dry_run:
        print_policies(st)
        print_estimate(st, {g: files[gi["desc"]] for g, gi in st["groups"].items()}, a.pool)
        print("\n(dry run: nothing written)")
        return
    for desc_name, d in files.items():
        write_json(JSON_DIR / f"{desc_name}.json", d)
    write_json(master_desc_path(name), master)
    write_json(st_path, st)
    keep = set(files) | {sw["desc"] for sw in st["sweeps"]}
    stale = stale_files(name, keep, st["plan"]["state_dir"])
    for p in stale:
        p.unlink()
    shards_dir = st_path.parent / "shards"
    if shards_dir.is_dir() and not any(shards_dir.iterdir()):
        shards_dir.rmdir()
    if stale:
        log(f"removed {len(stale)} descriptors an earlier gen of {name} wrote and this one no longer makes")
    print_policies(st)
    print_estimate(st, {g: files[gi["desc"]] for g, gi in st["groups"].items()}, a.pool)
    print(f"\nwrote {', '.join(f'json/{f}.json' for f in files)} (one cloudhost sweep each) + json/{name}.json "
          f"(collection) in {INFRA}; state {st_path}")
    print(f"next: fullsweep.py run {name}   (shows what it would launch; --yes launches)")


def print_policies(st):
    print(f"\nSetups of {st['name']}:")
    for p, info in st["policies"].items():
        line = f"  {p:12s} {info.get('gammas', '')}"
        if info.get("gsearch_gain_pct") is not None:
            line += f"  (gsearch Avg {info['gsearch_gain_pct']:+.2f}%{'' if info.get('converged') else ', NOT converged'})"
        print(line)
        if len(info.get("levels_used", {})) > 1:
            print(f"  {'':12s} simpoints by gamma source: "
                  + ", ".join(f"{lev} {fmt_n(n)}" for lev, n in info["levels_used"].items()))
        if info.get("unconverged_units"):
            print(f"  {'':12s} unconverged gsearch units: {', '.join(info['unconverged_units'])}")
    if st.get("search_binaries") and st["binary"] not in st["search_binaries"]:
        print(f"  note: the gammas were searched on {', '.join(st['search_binaries'])}; this sweep runs {st['binary']}")


def cmd_estimate(a):
    st = State(a.name).data
    print_policies(st)
    print_estimate(st, group_descs(st), a.pool)


# ---------------------------------------------------------------- running
def run_logged(cmd, logf, **kw):
    """Run cmd, appending its output to logf; returns (exit code, this run's output)."""
    Path(logf).parent.mkdir(parents=True, exist_ok=True)
    with open(logf, "a") as f:
        f.write(f"\n=== {now()} $ {' '.join(map(str, cmd))}\n")
        f.flush()
        start = f.tell()
    with open(logf, "a") as f:
        r = subprocess.run([str(c) for c in cmd], stdout=f, stderr=subprocess.STDOUT, text=True,
                           stdin=subprocess.DEVNULL, **kw)
    with open(logf) as f:
        f.seek(start)
        return r.returncode, f.read()


def build_once(st, logdir):
    """Build each binary here once, so cloudhost ships the cached build instead of every machine building it."""
    done = set()
    for gi in st["groups"].values():
        d = load_json(JSON_DIR / f"{gi['desc']}.json")
        for c in d["configurations"].values():
            b = c["binary"]
            if b in done or list((INFRA / "scarab_builds").glob(f"{b}.*")):
                done.add(b)
                continue
            log(f"building {b} once: ./sci --build-scarab {gi['desc']}")
            rc, _ = run_logged([SCI, "--build-scarab", gi["desc"]], logdir / "build.log", cwd=INFRA)
            if rc != 0 or not list((INFRA / "scarab_builds").glob(f"{b}.*")):
                die(f"build of {b} failed; see {logdir / 'build.log'}")
            done.add(b)


def sweep_cmd(st, group, desc):
    g = st["plan"]["groups"][group]
    cmd = [CLOUDHOST, "sweep", "start", desc, "--infra", str(INFRA), "--shard-size", str(g["shard_size"]),
           "--where", g.get("where", "any"), "--who", f"fullsweep-{st['name']}", "--detach"]
    if g.get("mem_per_sim_gb"):
        cmd += ["--mem-per-sim", f"{g['mem_per_sim_gb']:g}"]
    # Last: nargs="*", so an empty list is a bare flag (no slow suites).
    return cmd + ["--slow-suites", *(st["groups"][group]["suites"] if g.get("slow") else [])]


def plan_launches(st):
    """(group, setups) not yet in any sweep of that group, slow groups first."""
    out = []
    for group, gi in st["groups"].items():
        have = {c for sw in st["sweeps"] if sw["group"] == group for c in sw["configs"]}
        new = [c for c in gi["configs"] if c not in have]
        if new:
            out.append((group, new))
    return sorted(out, key=lambda x: not st["plan"]["groups"][x[0]].get("slow"))


def to_resume(st):
    """Sweeps of this sweep that stopped short (stopped, failed, incomplete, driver gone): (record, cloudhost state)."""
    out = []
    for sw in st["sweeps"]:
        cs = sweep_state(sw["id"])
        if cs is not None and cs["state"] not in DONE_SWEEP_STATES and not driver_alive(cs):
            out.append((sw, cs))
    return out


def cmd_run(a):
    st = State(a.name)
    data = st.data
    if not multi_runner_safe():
        die(f"{INFRA} lacks sci's multi-runner fix (scarab-infra#22): `cloudhost sweep` refuses it; merge main")
    todo, resume = plan_launches(data), to_resume(data)
    if not todo and not resume:
        print_status(data)
        print("nothing to launch or resume")
        return
    logdir = st.path.parent / "logs"
    print(f"{a.name}: experiment {data['plan']['root_dir'].rstrip('/')}/simulations/{a.name}")
    batch = {}
    for group, new in todo:
        gi = data["groups"][group]
        desc = gi["desc"] if new == gi["configs"] else f"{gi['desc']}_b{1 + sum(sw['group'] == group for sw in data['sweeps'])}"
        batch[group] = desc
        sims = gi["simpoints"] * len(new)
        print(f"  start {group} ({', '.join(gi['suites'])}): {len(new)} setups, {fmt_n(sims)} sims"
              + ("" if desc == gi["desc"] else f" (setups added since: {', '.join(new)})"))
        print("    " + " ".join(map(str, sweep_cmd(data, group, desc))))
    for sw, cs in resume:
        print(f"  resume {sw['group']} sweep {sw['id']} ({cs['state']}, driver not running)")
        print(f"    {CLOUDHOST} sweep resume {sw['id']} --detach" + (" --retry-failed" if a.retry_failed else ""))
    if not a.yes:
        if todo:
            new_cfgs = {c for _, new in todo for c in new}
            print_estimate(data, {g: load_json(JSON_DIR / f"{data['groups'][g]['desc']}.json") for g, _ in todo},
                           a.pool, None if new_cfgs == set(data["policies"]) else new_cfgs)
        print("\nnot launched: pass --yes to build the binary and start these sweeps (check `cloudhost ls` first)")
        return
    build_once(data, logdir)
    for sw, cs in resume:
        cmd = [CLOUDHOST, "sweep", "resume", sw["id"], "--detach"] + (["--retry-failed"] if a.retry_failed else [])
        rc, out = run_logged(cmd, logdir / "sweeps.log", cwd=INFRA)
        log(f"resumed {sw['id']}" if rc == 0 else f"resume of {sw['id']} failed (exit {rc}); see {logdir / 'sweeps.log'}")
    for group, new in todo:
        gi = data["groups"][group]
        desc = batch[group]
        if desc != gi["desc"]:
            d = load_json(JSON_DIR / f"{gi['desc']}.json")
            d["configurations"] = {c: d["configurations"][c] for c in new}
            d["fullsweep"] = dict(d["fullsweep"], batch=desc)
            write_json(JSON_DIR / f"{desc}.json", d)
        rc, out = run_logged(sweep_cmd(data, group, desc), logdir / "sweeps.log", cwd=INFRA)
        m = re.search(r"^sweep (\S+): ", out, re.M)
        if rc != 0 or not m:
            die(f"cloudhost sweep start {desc} failed (exit {rc}):\n{out.strip()[-1500:]}")
        data["sweeps"].append({"id": m.group(1), "group": group, "desc": desc, "configs": new,
                               "digests": {c: gi["digests"][c] for c in new}, "started": now()})
        st.save()
        log(f"started sweep {m.group(1)} for {group}: {out.strip().splitlines()[0]}")
    print(f"\nfollow it: fullsweep.py status {a.name}   (or cloudhost sweep status <id>; driver logs in "
          f"{sweeps_dir()}/<id>/driver.log)")


def setup_progress(data):
    """{setup: [sims done, sims launched]} over every shard of every sweep, and the sweeps' cloudhost states."""
    prog = defaultdict(lambda: [0, 0])
    states = {}
    for sw in data["sweeps"]:
        cs = sweep_state(sw["id"])
        states[sw["id"]] = cs
        if cs is None:
            continue
        for sh in cs["shards"]:
            try:
                cfgs = list(load_json(sh["path"])["configurations"])
            except (OSError, KeyError, json.JSONDecodeError):
                cfgs = sw["configs"]
            per = sh["sims"] // max(1, len(cfgs))
            for c in cfgs:
                prog[c][1] += per
                if sh["state"] == "done":
                    prog[c][0] += per
    return prog, states


def print_status(data):
    prog, states = setup_progress(data)
    print(f"\n{data['name']}: experiment {data['plan']['root_dir'].rstrip('/')}/simulations/{data['name']}")
    for sw in data["sweeps"]:
        cs = states[sw["id"]]
        if cs is None:
            print(f"  {sw['group']:7s} {sw['id']}: no cloudhost state in {sweeps_dir()}")
            continue
        c = Counter(sh["state"] for sh in cs["shards"])
        done = sum(sh["sims"] for sh in cs["shards"] if sh["state"] == "done")
        print(f"  {sw['group']:7s} {sw['id']}: {cs['state']}, shards " + ", ".join(f"{v} {k}" for k, v in sorted(c.items()))
              + f"; {fmt_n(done)}/{fmt_n(cs['sims'])} sims; driver {'running' if driver_alive(cs) else 'not running'}")
        hosts = Counter(sh.get("host") for sh in cs["shards"] if sh["state"] == "running")
        if hosts:
            print(f"  {'':7s} running on " + ", ".join(f"{h} ({n})" for h, n in sorted(hosts.items())))
    print(f"\n  {'setup':12s} {'sims done':>15s}")
    for p in data["policies"]:
        total = sum(gi["simpoints"] for gi in data["groups"].values() if p in gi["configs"])
        done, launched = prog.get(p, (0, 0))
        print(f"  {p:12s} {fmt_n(done):>7s}/{fmt_n(total):<7s}" + ("" if launched == total else
                                                                  f"  ({fmt_n(total - launched)} not launched)"))
    if plan_launches(data):
        print(f"\n  not launched yet: " + "; ".join(f"{g}: {', '.join(n)}" for g, n in plan_launches(data))
              + f"  (fullsweep.py run {data['name']})")
    elif data["sweeps"] and all(cs and cs["state"] in DONE_SWEEP_STATES for cs in states.values()):
        print(f"\n  every sweep is complete: fullsweep.py collect {data['name']}")


def cmd_status(a):
    print_status(State(a.name).data)


def cmd_collect(a):
    """One ./sci --collect-stats over every group: each sweep's own collection covered only its group."""
    data = State(a.name).data
    prog, states = setup_progress(data)
    totals = {p: sum(gi["simpoints"] for gi in data["groups"].values() if p in gi["configs"]) for p in data["policies"]}
    complete = [p for p in data["policies"] if prog.get(p, (0, 0))[0] == totals[p]]
    busy = [sid for sid, cs in states.items() if cs and cs["state"] not in DONE_SWEEP_STATES + ("failed", "incomplete")
            and driver_alive(cs)]
    if len(complete) < len(data["policies"]):
        print(f"complete setups: {', '.join(complete) or 'none'}")
        if not a.partial or not complete:
            die("not every setup is complete (fullsweep.py status); --partial collects just the complete ones")
    if busy:
        log(f"note: sweeps {', '.join(busy)} are still running; each collects its own group into the same "
            f"collected_stats.csv when it ends, so run collect again after that")
    path = master_desc_path(a.name)
    d = load_json(path)
    d["collect_stats"] = {"configs": None if len(complete) == len(data["policies"]) else complete}
    write_json(path, d)
    r = subprocess.run([SCI, "--collect-stats", a.name], cwd=INFRA)
    raise SystemExit(r.returncode)


# ---------------------------------------------------------------- calibrate
def cmd_calibrate(a):
    """Per-input H4D minutes per million instructions from finished gsearch shards (SIM_HOST_WALL_SECONDS in
    sci's collected_stats.csv), by architecture; other machines' per-suite slowdown from the same runs."""
    csv.field_size_limit(10 ** 9)
    gdir = Path(a.gsearch_dir)
    db = load_json(INFRA / "workloads" / "workloads_db.json")
    samples = defaultdict(list)          # (arch, machine, label) -> [min per Minstr]
    for study in a.studies:
        sdir = gdir / study
        descs = {p.stem: p for p in sdir.glob("rounds/r*/shards/*.json")}
        stj = load_json(sdir / "state.json")
        n_sh = 0
        for rnd in stj["rounds"]:
            for name, s in rnd["shards"].items():
                if s.get("status") != "ingested" or not s.get("host") or name not in descs:
                    continue
                d = load_json(descs[name])
                path = Path(d["root_dir"]) / "simulations" / d["experiment"] / "collected_stats.csv"
                if not path.exists():
                    continue
                machine = LOCAL if s["host"] == LOCAL else "n4d" if s["host"].startswith("cloudhost-n") else "h4d"
                limit = flag_value(next(iter(d["configurations"].values())).get("params", ""), "inst_limit")
                warm = {f"{x['suite']}/{x['subsuite']}/{x['workload']}": x.get("warmup") for x in d["simulations"]}
                with open(path) as f:
                    rd = csv.reader(f)
                    hdr = next(rd)
                    row = next((r for r in rd if r and r[0] == "SIM_HOST_WALL_SECONDS_value"), None)
                if row is None:
                    continue
                n_sh += 1
                for col, v in zip(hdr[3:], row[3:]):
                    try:
                        sec = float(v)
                    except ValueError:
                        continue
                    if not sec > 0:
                        continue
                    _, label, _ = col.split(" ")
                    suite, sub, wl = label.split("/")
                    if limit:
                        instr = int(limit)
                    else:
                        mt = db[suite][sub][wl]["simulation"]["memtrace"]
                        w = warm.get(label)
                        instr = ((mt.get("warmup") or 0) if w is None else w) + (mt.get("segment_size") or 0)
                    samples[(d["architecture"], machine, label)].append(sec / 60 / (instr / 1e6))
        log(f"{study}: {n_sh} shards read")
    mean = lambda v: sum(v) / len(v)  # noqa: E731
    out = {"_doc": ("Sim cost model for fullsweep.py estimates and shard sizing: mean H4D wall minutes per million "
                    "simulated instructions (warmup included) per input, at full machine load, and other machines' "
                    "per-suite slowdown vs an H4D. Written by `fullsweep.py calibrate` from SIM_HOST_WALL_SECONDS "
                    "of finished gsearch runs."),
           "source": {"studies": a.studies, "written": now()},
           "h4d_min_per_minstr": {}, "suite_h4d_min_per_minstr": {}, "slowdown": {}}
    archs = sorted({k[0] for k in samples})
    for arch in archs:
        per = defaultdict(dict)          # machine -> label -> mean
        for (ar, m, label), v in samples.items():
            if ar == arch:
                per[m][label] = mean(v)
        h4d = per.get("h4d", {})
        suites = sorted({lab.split("/")[0] for m in per.values() for lab in m})
        suite_rate, slow = {}, defaultdict(dict)
        for suite in suites:
            hv = [x for lab, x in h4d.items() if lab.startswith(suite + "/")]
            if hv:
                suite_rate[suite] = round(mean(hv), 5)
            for m, labs in per.items():
                if m == "h4d":
                    continue
                pairs = [(labs[lab], h4d[lab]) for lab in labs if lab in h4d and lab.startswith(suite + "/")]
                if len(pairs) >= 3:
                    slow[m][suite] = round(sum(p for p, _ in pairs) / sum(q for _, q in pairs), 3)
        rates = {}
        for lab in sorted({lab for m in per.values() for lab in m}):
            suite = lab.split("/")[0]
            if lab in h4d:
                rates[lab] = round(h4d[lab], 5)
            else:
                for m, labs in per.items():
                    if lab in labs and suite in slow.get(m, {}):
                        rates[lab] = round(labs[lab] / slow[m][suite], 5)
                        break
        out["h4d_min_per_minstr"][arch] = rates
        out["suite_h4d_min_per_minstr"][arch] = suite_rate
        out["slowdown"][arch] = dict(slow)
        log(f"{arch}: {len(rates)} inputs; suites {suite_rate}; slowdown {dict(slow)}")
    write_json(a.out, out)
    print(f"wrote {a.out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pool_default = "h4d=5,n4d=10"
    g = sub.add_parser("gen", help="write each group's sweep descriptor and the merged descriptor")
    g.add_argument("result", nargs="?", help="gsearch STUDY/VARIANT, e.g. cd1/small")
    g.add_argument("--plan", default=str(DEFAULT_PLAN))
    g.add_argument("--name", help="sweep (and experiment) name; default fs_<study>_<variant>")
    g.add_argument("--only", type=lambda s: [x for x in s.split(",") if x], help="run just these setups")
    g.add_argument("--skip", type=lambda s: [x for x in s.split(",") if x], help="leave these setups out")
    g.add_argument("--suites", type=lambda s: [x for x in s.split(",") if x], help="run just these suites")
    g.add_argument("--skip-suites", type=lambda s: [x for x in s.split(",") if x], help="leave these suites out")
    g.add_argument("--pick", choices=["best", "max_point"], help="override the plan's pick")
    g.add_argument("--pool", default=pool_default, help=f"machines for the estimate (default {pool_default})")
    g.add_argument("--dry-run", action="store_true", help="print the counts and estimate; write nothing")
    g.add_argument("--force", action="store_true",
                   help="reuse a name generated for another result or suite set (only before anything launched)")
    e = sub.add_parser("estimate")
    e.add_argument("name")
    e.add_argument("--pool", default=pool_default)
    r = sub.add_parser("run")
    r.add_argument("name")
    r.add_argument("--yes", action="store_true", help="really build and start the sweeps")
    r.add_argument("--retry-failed", action="store_true", help="resumed sweeps give failed shards new attempts")
    r.add_argument("--pool", default=pool_default)
    s = sub.add_parser("status")
    s.add_argument("name")
    m = sub.add_parser("collect")
    m.add_argument("name")
    m.add_argument("--partial", action="store_true", help="collect just the setups whose sims are all done")
    c = sub.add_parser("calibrate")
    c.add_argument("studies", nargs="*", default=["cd1", "dr1"])
    c.add_argument("--gsearch-dir", default=load_json(DEFAULT_PLAN)["gsearch_dir"])
    c.add_argument("--out", default=str(CALIBRATION))
    a = ap.parse_args()
    {"gen": cmd_gen, "estimate": cmd_estimate, "run": cmd_run, "status": cmd_status, "collect": cmd_collect,
     "calibrate": cmd_calibrate}[a.cmd](a)


if __name__ == "__main__":
    main()
