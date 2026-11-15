#!/usr/bin/env python3
"""Adaptive REPL_MLP gamma search over cloudhost: best (gamma_i, gamma_d) per simpoint, workload and suite.

    python tools/mlp_gsearch/gsearch.py plan   STUDY    # what the next round would run (writes nothing)
    python tools/mlp_gsearch/gsearch.py step   STUDY    # run one round: launch, wait, collect, decide
    python tools/mlp_gsearch/gsearch.py run    STUDY [--yes]   # rounds until converged or max_rounds
    python tools/mlp_gsearch/gsearch.py status STUDY
    python tools/mlp_gsearch/gsearch.py report STUDY
    python tools/mlp_gsearch/gsearch.py pull   STUDY    # copy the cloud shards' full results to pakuchi

STUDY is tools/mlp_gsearch/studies/<name>.json or a path. It names template descriptors (ordinary sci
descriptors in json/, one configuration each: the binary and base params) and the search settings; see
README.md. Each round's sims are split into shard descriptors (json/gs_<study>_r<k>_*.json, copies of the
templates with their own simulations and gi<I>_gd<D> configurations), launched with
`cloudhost run any <shard> --wait`, and collected with ./sci --collect-stats (on the cloud machine, fetching
only collected_stats.csv). The decision rules are in search.py. State lives in <state_dir>/<study>/ and a
rerun resumes where the last one stopped.
"""

import argparse
import copy
import dataclasses
import gzip
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
INFRA = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(INFRA / "tools" / "mlp_gamma"))
sys.path.insert(0, str(INFRA / "workloads"))
sys.path.insert(0, str(INFRA / "scarab_stats"))
import search as S  # noqa: E402
import report as R  # noqa: E402
import prune as P  # noqa: E402
import aggregation as AGG  # noqa: E402  sci's input -> workload grouping (./sci --visualize, plots)

CLOUDHOST = Path(os.environ.get("CLOUDHOST", "/home/mgiordan/carp/cloudhost/cloudhost"))
LOCAL = os.environ.get("CLOUDHOST_LOCAL", "pakuchi")
H4D_MIN_PER_MINSTR = 0.125   # 11-11 benchmark: 60M-instruction google sims took ~7.5 min each on an H4D
VM_CORES, VM_DOLLARS_HR = 192, 7.85
POLL_S = 120
MIN_GROUP_SIMS = 64
SHARD_OVERHEAD_CORE_MIN = 192 * 10   # what one more shard costs: a launch, and a machine leased whole for its tail
SHARD_RETRIES = 5            # relaunches of a shard that came back mostly empty (a job-level failure, e.g. a VM whose
                             # data disk filled up), before its runs count as failed points
# sci's stat collector can hang: its parallel CSV writer's pool workers attach to a SharedMemory segment by
# name, and when the segment is already gone (seen with Python 3.12, no `track=False`) each worker's
# initializer dies, multiprocessing.Pool respawns it forever and the job log grows by GBs. So collection
# runs under a timeout and falls back to one process (--jobs 1, no shared memory), and a collector that a
# finished `sci --sim` left running this long is killed so the job, and its lease, can end.
COLLECT_TIMEOUT_S = 900
MAX_LAUNCH_FAILURES = 10    # launches that fail for a reason other than "no host" or an unreachable machine
TRANSIENT = ("Connection timed out", "Connection refused", "Connection reset", "No route to host",
             "ssh_exchange_identification", "kex_exchange_identification", "Temporary failure in name resolution",
             "503 ", "502 ", "Internal error", "rateLimitExceeded")
LOCAL_SMALL_SIMS = 24       # a shard this small may run on pakuchi even when its tag prefers the cloud
LOCAL_MIN_PARALLEL = 48      # smallest pakuchi slice a shard takes when the cloud machines are busy (16 left two
                             # 168- and 483-sim shards at 16 and 27 wide for over an hour after the VMs freed)
GAMMA_FLAGS = ("--mlp_lin_instr_lambda", "--mlp_lin_data_lambda")


def now():
    return time.strftime("%F %T")


def log(msg):
    print(f"{now()} {msg}", flush=True)


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, separators=(",", ":")) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------- study, templates, simpoints
class Study:
    def __init__(self, arg):
        path = Path(arg)
        if not path.suffix:
            path = HERE / "studies" / f"{arg}.json"
        self.path = path
        spec = json.loads(path.read_text())
        self.name = spec["name"]
        if not re.fullmatch(r"[A-Za-z0-9]+", self.name):
            raise SystemExit("study name: letters and digits only (it is part of every shard's name)")
        self.dir = Path(spec.get("state_dir", "/mnt/storage/mgiordan/gsearch")) / self.name
        frozen = self.dir / "study.json"
        if frozen.is_file():
            old = json.loads(frozen.read_text())
            if old != spec:
                log(f"note: {path} differs from the frozen copy {frozen}; using the frozen copy "
                    "(delete the state dir to start over)")
            spec = old
        self.spec = spec
        self.settings = S.Settings.from_study(spec)
        self.max_rounds = int(spec.get("max_rounds", 5))
        # Target shard length in hours of one machine running max_parallel sims; a float or {tag: float}.
        self.shard_vm_hours = spec.get("shard_vm_hours", 1.0)
        self.where = spec.get("where", "any")
        # Sims at once per shard (sci max_parallel, cloudhost --max-parallel), an int or {tag: int}. Google at
        # 50M+200M reaches ~5 GB RSS per sim (merced), so 192 at once OOM-killed sims on a 708 GB H4D.
        self.max_parallel = spec.get("max_parallel", VM_CORES)
        # GB of RAM one sim needs, a float or {tag: float}: cloudhost then caps a cloud run's sims at once by the
        # chosen VM's memory (cloudhost run --mem-per-sim), so a mixed pool of H4D and n4d machines each run as
        # many as fit (google at 50M+200M: ~5.5 GB, 125 on an H4D, 65 on an n4d-96).
        self.mem_per_sim_gb = spec.get("mem_per_sim_gb", 0)
        self.extra_configs = spec.get("extra_configs", {})
        self.variants = {}       # variant -> {label: template desc}
        for var, tmpls in spec["variants"].items():
            if not re.fullmatch(r"[A-Za-z0-9_]+", var):
                raise SystemExit(f"variant {var!r}: letters, digits and _ only")
            self.variants[var] = {lab: self.template(name) for lab, name in tmpls.items()}
        self.members = {var: self.expand(var) for var in self.variants}

    def template(self, name):
        frozen = self.dir / "templates" / f"{name}.json"
        src = frozen if frozen.is_file() else INFRA / "json" / f"{name}.json"
        d = json.loads(src.read_text())
        d["_name"] = name
        confs = d.get("configurations", {})
        if len(confs) != 1:
            raise SystemExit(f"template {name}: needs exactly one configuration (the base), has {list(confs)}")
        base = next(iter(confs.values()))
        params = base.get("params", "")
        # Any base policy the gammas mean something on: REPL_MLP (--mlc_cache_repl_policy 9), or an RRIP policy
        # with --mlp_rrip_boost. gi0_gd0 is then that policy with no boost.
        if any(f in params for f in GAMMA_FLAGS):
            raise SystemExit(f"template {name}: base params must not set the gammas; the search adds them")
        if not re.fullmatch(r"scarab_[0-9a-f]{7,40}", base.get("binary", "")):
            raise SystemExit(f"template {name}: binary must be scarab_<githash>, got {base.get('binary')!r}")
        return d

    def freeze(self):
        if (self.dir / "study.json").is_file():
            return
        write_json(self.dir / "study.json", self.spec)
        for tmpls in self.variants.values():
            for d in tmpls.values():
                out = {k: v for k, v in d.items() if k != "_name"}
                write_json(self.dir / "templates" / f"{d['_name']}.json", out)

    def expand(self, var):
        """Every simpoint of a variant: {key: (Simpoint, label, simulation entry)}."""
        out = {}
        for lab, d in self.variants[var].items():
            db = P.workloads_db(d)
            for (suite, sub, wl, cl), sim in P.simpoints(d):
                entry = db[suite][sub][wl]
                w = 1.0
                for sp in entry.get("simpoints", []):
                    if int(sp["cluster_id"]) == cl:
                        w = float(sp["weight"])
                key = (f"{suite}/{sub}/{wl}", str(cl))
                if key in out:
                    raise SystemExit(f"variant {var}: {key} is in two templates")
                out[key] = (S.Simpoint(suite, key[0], key[1], w, tag=lab), lab, sim, entry)
        # Group inputs into workloads exactly as sci does (gcc, gcc_2 ... gcc_9 -> gcc).
        hier = AGG.Hierarchy(sorted({k[0] for k in out}))
        bench = {label: wk for wk, labels in hier.workloads.items() for label in labels}
        return {k: (dataclasses.replace(v[0], bench=bench[k[0]]), *v[1:]) for k, v in out.items()}

    def shard_hours(self, lab):
        h = self.shard_vm_hours
        return float(h.get(lab, h.get("default", 1.0)) if isinstance(h, dict) else h)

    def mem_per_sim(self, lab):
        m = self.mem_per_sim_gb
        return float(m.get(lab, m.get("default", 0)) if isinstance(m, dict) else m or 0)

    def parallel(self, lab):
        mp = self.max_parallel
        return int(mp.get(lab, mp.get("default", VM_CORES)) if isinstance(mp, dict) else mp)

    def base(self, var, lab):
        return next(iter(self.variants[var][lab]["configurations"].values()))

    def sim_minutes(self, var, lab, entry, sim):
        """Estimated H4D minutes for one sim: inst_limit if the base params set one, else warmup + segment."""
        limit = R.flag_value(self.base(var, lab).get("params", ""), "inst_limit")
        if limit:
            instr = int(limit)
        else:
            mt = entry.get("simulation", {}).get("memtrace", {})
            warm = sim.get("warmup")
            warm = (mt.get("warmup") or 0) if warm is None else warm
            instr = warm + (mt.get("segment_size") or 10_000_000)
        return instr / 1e6 * H4D_MIN_PER_MINSTR


# ---------------------------------------------------------------- state
class State:
    def __init__(self, study):
        self.study = study
        self.path = study.dir / "state.json"
        self.lock = threading.RLock()
        self.data = json.loads(self.path.read_text()) if self.path.is_file() else {"rounds": [], "built": []}

    def save(self):
        with self.lock:
            write_json(self.path, self.data)

    @property
    def rounds(self):
        return self.data["rounds"]

    def current(self):
        return self.rounds[-1] if self.rounds else None

    def runs(self, var):
        """Every ingested run of a variant, as search.decide wants them."""
        out = []
        obs = self.study.dir / "obs"
        for rnd in self.rounds:
            for name, sh in rnd["shards"].items():
                if sh["variant"] != var or sh["status"] != "ingested":
                    continue
                f = obs / f"{name}.json"
                for r in json.loads(f.read_text()):
                    if "cycles" in r:
                        out.append((r["workload"], r["cluster"], r["gi"], r["gd"], r["inst"], r["cycles"]))
                    else:
                        out.append((r["workload"], r["cluster"], r["gi"], r["gd"], r["ipc"]))
        return out


# ---------------------------------------------------------------- planning
def plan_round(study, state, k):
    """Decide the next round and lay it out as shard descriptors (not written)."""
    shards, summary = {}, {}
    for var, members in study.members.items():
        sps = [m[0] for m in members.values()]
        needs, units, model = S.decide(sps, state.runs(var), study.settings)
        summary[var] = {"needs": {f"{w}:{c}": [list(p) for p in ps] for (w, c), ps in needs.items()},
                        "units": unit_rows(units), "dropped": {f"{w}:{c}": ps for (w, c), ps in model.dropped.items()}}
        by_lab = defaultdict(dict)
        for key, pts in needs.items():
            by_lab[members[key][1]][key] = frozenset(pts)
        spare = spare_points(model, units, study.settings)
        for lab, kneeds in sorted(by_lab.items()):
            for i, it in enumerate(layout(study, var, lab, kneeds, spare)):
                name = f"gs_{study.name}_r{k}_{var}_{lab}_{i}"
                shards[name] = shard_desc(study, var, lab, name, it["keys"], it["pts"], extra=(k == 1),
                                          slots=it["slots"])
    return shards, summary


def unit_rows(units):
    rows = []
    for u in units:
        rows.append({"level": u.level, "name": u.name, "suite": u.suite, "members": u.members,
                     "axes": "+".join(u.axes), "pending": u.pending, "converged": u.converged,
                     "at_cap": u.at_cap, "best": list(u.best) if u.best else None,
                     "gain_pct": None if u.best is None else round(u.gain, 4),
                     "max_point": list(u.max_point) if u.max_point else None,
                     "max_gain_pct": None if u.max_point is None else round(u.max_gain, 4),
                     "smallest_within": list(u.smallest_within) if u.smallest_within else None,
                     "start": list(u.start) if u.start else None, "defined": u.defined,
                     "needs": [list(p) for p in u.needs]})
    return rows


def spare_points(model, units, st):
    """Per simpoint, unmeasured points near its incumbent that a slot shard can run in a slot it would otherwise
    fill with a duplicate: the diagonal neighbors (both gammas +-1 step; gamma_i-sensitive simpoints only),
    then 2 steps along each searched axis. They are extra data in idle cores of a single-wave shard, and they
    check the corners an axis-by-axis climb never visits."""
    out = {}
    si, sd = st.steps
    hi = st.max_gamma
    for u in units:
        if u.level != "simpoint" or u.best is None:
            continue
        wl, cl = u.name.rsplit(":", 1)
        key = (wl, cl)
        gi, gd = u.best
        cand = []
        if "gi" in u.axes:
            cand += [(gi + a * si, gd + b * sd) for a in (-1, 1) for b in (-1, 1)]
            cand += [(gi + 2 * si, gd), (gi - 2 * si, gd)]
        cand += [(gi, gd + 2 * sd), (gi, gd - 2 * sd)]
        out[key] = [p for p in cand if 0 <= p[0] <= hi[0] and 0 <= p[1] <= hi[1] and model.value(key, p) is None]
    return out


def layout(study, var, lab, kneeds, spare=None):
    """Lay out one template's needed (simpoint, point) pairs as shards. Returns [{"keys", "pts", "slots"}].

    sci runs every config of a descriptor on every simulation in it. Simpoints that need the same point set
    (and enough of them to fill a group) get ordinary named configs gi<I>_gd<D> (slots None). Every other
    simpoint goes into slot shards: configs s0..s<K-1> whose workload_params (scarab-infra #17) give each
    simpoint its own point per slot, so it runs exactly the points it needs. Slot groups are formed by K (points
    per simpoint); a small group is padded up to the next K (repeating a point, a duplicate run) only while the
    padding costs less than a shard's overhead. Each group is cut into shards of about shard_vm_hours."""
    members = study.members[var]
    mins = {k: study.sim_minutes(var, lab, members[k][3], members[k][2]) for k in kneeds}
    by_set = defaultdict(list)
    for key, pts in kneeds.items():
        by_set[pts].append(key)
    named, loose = [], []
    for pts, keys in by_set.items():
        if len(keys) >= 2 and len(keys) * len(pts) >= MIN_GROUP_SIMS:
            named.append((sorted(pts), sorted(keys)))
        else:
            loose += keys
    by_k = defaultdict(list)
    for key in loose:
        by_k[len(kneeds[key])].append(key)
    ks = sorted(by_k)
    groups = [[k, by_k[k]] for k in ks]                 # [K, keys], ascending K
    i = 0
    while i < len(groups) - 1:
        k_small, keys_small = groups[i]
        k_next, keys_next = groups[i + 1]
        pad = sum(mins[k] for k in keys_small) * (k_next - k_small)
        # Pad only while the merged group still runs in one wave: then the padding fills cores a separate small
        # shard would leave idle anyway; past a wave it would add whole waves of repeats.
        one_wave = (len(keys_small) + len(keys_next)) * k_next <= min(VM_CORES, study.parallel(lab))
        if pad <= SHARD_OVERHEAD_CORE_MIN and one_wave:
            groups[i + 1] = [k_next, keys_next + keys_small]
            groups.pop(i)
        else:
            i += 1
    items = [{"pts": pts, "keys": keys, "slots": None, "k": len(pts)} for pts, keys in named]
    for k_slots, keys in groups:
        slots = {}
        for key in keys:
            own = sorted(kneeds[key])
            extra = [p for p in (spare or {}).get(key, []) if p not in own][:k_slots - len(own)]
            slots[key] = own + extra + [own[-1]] * (k_slots - len(own) - len(extra))
        items.append({"pts": sorted({p for v in slots.values() for p in v}), "keys": sorted(keys), "slots": slots,
                      "k": k_slots})
    out = []
    # H4D core-minutes per shard: shard_vm_hours of a machine running this tag's max_parallel sims at once.
    cap = study.shard_hours(lab) * min(VM_CORES, study.parallel(lab)) * 60
    for it in items:
        per_wl = defaultdict(list)
        for key in it["keys"]:
            per_wl[key[0]].append(key)
        total = sum(mins[key] for key in it["keys"]) * it["k"]
        n = max(1, math.ceil(total / cap))
        target = total / n                              # even shards rather than full ones and a stub
        cur, cur_cost = [], 0.0

        def emit(keys):
            sl = {key: it["slots"][key] for key in keys} if it["slots"] else None
            pts = sorted({p for v in sl.values() for p in v}) if sl else it["pts"]
            out.append({"keys": keys, "pts": pts, "slots": sl})

        for wl, wks in sorted(per_wl.items()):
            c = sum(mins[key] for key in wks) * it["k"]
            if cur and cur_cost + c > target * 1.05:
                emit(cur)
                cur, cur_cost = [], 0.0
            cur += wks
            cur_cost += c
        if cur:
            emit(cur)
    return out


def replan(study, state):
    """Re-lay out the current round's shards that have not launched (e.g. after a layout change), keeping the
    ones that did. A planned shard that holds a live cloudhost lease (its launch was in flight) counts as
    launched."""
    rnd = state.current()
    if rnd is None or rnd["status"] == "done":
        raise SystemExit("no round in progress")
    k = rnd["round"]
    p = pool()
    for n, s in rnd["shards"].items():
        if s["status"] != "planned":
            continue
        host = None
        if any(l.get("desc") == n for l in p.get(LOCAL, {}).get("leases", [])):
            host = LOCAL
        for h, v in p.get("cloud", {}).items():
            if v.get("lease") and v["lease"].get("desc") == n:
                host = h
        if host:
            s.update(status="launched", host=host, launched=now(), adopted=True)
            log(f"{n}: has a live lease on {host}; counted as launched")
    todo = [n for n, s in rnd["shards"].items() if s["status"] == "planned"]
    if not todo:
        log("every shard of the round has launched; nothing to re-lay out")
        state.save()
        return
    pairs = defaultdict(lambda: defaultdict(set))      # (var, lab) -> key -> points
    for n in todo:
        d = json.loads((INFRA / "json" / f"{n}.json").read_text())
        g = d["gsearch"]
        for sim in d["simulations"]:
            key = (f"{sim['suite']}/{sim['subsuite']}/{sim['workload']}", str(sim["cluster_id"]))
            own = g["slots"][f"{key[0]}:{key[1]}"] if g.get("slots") else g["points"]
            pairs[(g["variant"], g["label"])][key] |= {tuple(x) for x in own}
    old_sims = sum(rnd["shards"][n]["sims"] for n in todo)
    for n in todo:
        del rnd["shards"][n]
        (INFRA / "json" / f"{n}.json").unlink(missing_ok=True)
    rdir = study.dir / "rounds" / f"r{k}"
    new = {}
    for (var, lab), kneeds in sorted(pairs.items()):
        used = [int(n.rsplit("_", 1)[1]) for n, s in rnd["shards"].items()
                if s["variant"] == var and s["label"] == lab]
        start = max(used, default=-1) + 1
        for i, it in enumerate(layout(study, var, lab, {kk: frozenset(v) for kk, v in kneeds.items()})):
            name = f"gs_{study.name}_r{k}_{var}_{lab}_{start + i}"
            new[name] = shard_desc(study, var, lab, name, it["keys"], it["pts"], slots=it["slots"])
    for name, d in new.items():
        write_json(INFRA / "json" / f"{name}.json", d)
        write_json(rdir / "shards" / f"{name}.json", d)
        rnd["shards"][name] = {"variant": d["gsearch"]["variant"], "label": d["gsearch"]["label"], "status": "planned",
                               "sims": d["gsearch"]["sims"], "h4d_core_minutes": d["gsearch"]["h4d_core_minutes"]}
    state.save()
    log(f"re-laid out {len(todo)} unlaunched shards ({old_sims} sims) as {len(new)} ({sum(d['gsearch']['sims'] for d in new.values())} sims):\n"
        + describe_plan(new))


def topup(study, state, write):
    """Add to the round in progress what a fresh decision wants that none of the round's shards already runs (e.g.
    after units were added to the study), as new shards of the same round, so it need not wait for the next round."""
    rnd = state.current()
    if rnd is None or rnd["status"] == "done":
        raise SystemExit("no round in progress")
    k = rnd["round"]
    in_round = defaultdict(set)          # (var, key) -> points some shard of this round runs
    for n, sh_ in rnd["shards"].items():
        if sh_["status"] == "abandoned":
            continue
        f = INFRA / "json" / f"{n}.json"
        if not f.is_file():
            f = study.dir / "rounds" / f"r{k}" / "shards" / f"{n}.json"
        d = json.loads(f.read_text())
        g = d["gsearch"]
        for sim in d["simulations"]:
            key = (f"{sim['suite']}/{sim['subsuite']}/{sim['workload']}", str(sim["cluster_id"]))
            own = g["slots"][f"{key[0]}:{key[1]}"] if g.get("slots") else g["points"]
            in_round[(g["variant"], key)] |= {tuple(x) for x in own}
    new = {}
    for var, members in study.members.items():
        sps = [m[0] for m in members.values()]
        needs, units, model = S.decide(sps, state.runs(var), study.settings)
        by_lab = defaultdict(dict)
        for key, pts in needs.items():
            extra = frozenset(p for p in pts if p not in in_round[(var, key)])
            if extra:
                by_lab[members[key][1]][key] = extra
        for lab, kneeds in sorted(by_lab.items()):
            used = [int(n.rsplit("_", 1)[1]) for n, s_ in rnd["shards"].items()
                    if s_["variant"] == var and s_["label"] == lab]
            start = max(used, default=-1) + 1
            for i, it in enumerate(layout(study, var, lab, kneeds)):
                name = f"gs_{study.name}_r{k}_{var}_{lab}_{start + i}"
                new[name] = shard_desc(study, var, lab, name, it["keys"], it["pts"], slots=it["slots"])
    if not new:
        log("top-up: the round already runs everything a fresh decision wants")
        return
    log(f"top-up of round {k}{'' if write else ' (dry run; --yes writes it)'}:\n" + describe_plan(new))
    if not write:
        return
    rdir = study.dir / "rounds" / f"r{k}"
    for name, d in new.items():
        write_json(INFRA / "json" / f"{name}.json", d)
        write_json(rdir / "shards" / f"{name}.json", d)
        rnd["shards"][name] = {"variant": d["gsearch"]["variant"], "label": d["gsearch"]["label"], "status": "planned",
                               "sims": d["gsearch"]["sims"], "h4d_core_minutes": d["gsearch"]["h4d_core_minutes"]}
    state.save()


def shard_desc(study, var, lab, name, keys, pts, extra=False, slots=None):
    tmpl = study.variants[var][lab]
    d = copy.deepcopy({k: v for k, v in tmpl.items() if k != "_name"})
    base = next(iter(tmpl["configurations"].values()))
    members = study.members[var]
    d["experiment"] = name
    d["_experiment"] = (f"Round of the gamma search study {study.name} (tools/mlp_gsearch), variant {var}, from "
                        f"template {tmpl['_name']}. Written by gsearch.py; do not edit.")
    sims = []
    for k in keys:
        sp, _, sim, _ = members[k]
        suite, sub, wl = k[0].split("/", 2)
        sims.append({"suite": suite, "subsuite": sub, "workload": wl, "cluster_id": int(k[1]),
                     "simulation_type": sim.get("simulation_type") or "memtrace", "warmup": sim.get("warmup")})
    d["simulations"] = sims
    confs = {}
    if slots:
        # Slot j of every simpoint is its j-th point, set per simpoint by workload_params (selector
        # suite/subsuite/workload:cluster). Every simulation is selected, so the base gammas are never used.
        k_slots = len(next(iter(slots.values())))
        for j in range(k_slots):
            c = copy.deepcopy(base)
            c["params"] = f"{base.get('params', '').strip()} {GAMMA_FLAGS[0]} 0 {GAMMA_FLAGS[1]} 0".strip()
            c["workload_params"] = {f"{key[0]}:{key[1]}": f"{GAMMA_FLAGS[0]} {slots[key][j][0]} {GAMMA_FLAGS[1]} {slots[key][j][1]}"
                                    for key in keys}
            c["_params"] = (f"Slot {j}: template base params plus each simpoint's own gammas (workload_params); "
                            "gsearch.slots maps simpoint and slot to (gamma_i, gamma_d).")
            confs[f"s{j}"] = c
    else:
        for gi, gd in pts:
            c = copy.deepcopy(base)
            c["params"] = f"{base.get('params', '').strip()} {GAMMA_FLAGS[0]} {gi} {GAMMA_FLAGS[1]} {gd}".strip()
            c["_params"] = f"Template base params plus instruction gamma {gi}, data gamma {gd}."
            confs[S.config_name((gi, gd))] = c
    if extra:
        limit = R.flag_value(base.get("params", ""), "inst_limit")
        for cname, params in study.extra_configs.items():
            c = copy.deepcopy(base)
            c["params"] = (params + (f" --inst_limit {limit}" if limit and "inst_limit" not in params else "")).strip()
            confs[cname] = c
    d["configurations"] = confs
    n = len(sims) * len(confs)
    d["max_parallel"] = min(n, VM_CORES, study.parallel(lab))
    d["container_reuse"] = True
    vis = d.get("visualize")
    if isinstance(vis, dict):
        vis["baseline"] = "gi0_gd0" if "gi0_gd0" in confs else next(iter(confs))
        vis["configs"] = list(confs)[:4]
    pa = d.get("perf_analyze")
    if isinstance(pa, dict):
        pa["baseline"] = "gi0_gd0" if "gi0_gd0" in confs else next(iter(confs))
    mins = sum(study.sim_minutes(var, lab, members[k][3], members[k][2]) for k in keys) * len(confs)
    d["_gsearch"] = "Bookkeeping for gsearch.py."
    d["gsearch"] = {"study": study.name, "variant": var, "template": tmpl["_name"], "label": lab,
                    "points": [list(p) for p in pts], "sims": n, "h4d_core_minutes": round(mins, 1)}
    if slots:
        d["gsearch"]["slots"] = {f"{key[0]}:{key[1]}": [list(p) for p in slots[key]] for key in keys}
        d["gsearch"]["needed"] = sum(len(set(v)) for v in slots.values())
    return d


def describe_plan(shards):
    tot_sims = sum(d["gsearch"]["sims"] for d in shards.values())
    tot_h = sum(d["gsearch"]["h4d_core_minutes"] for d in shards.values()) / 60
    lines = [f"{len(shards)} shards, {tot_sims} sims, ~{tot_h:.0f} H4D core-hours "
             f"(~{tot_h / VM_CORES:.1f} VM-hours, ~${tot_h / VM_CORES * VM_DOLLARS_HR:.0f} if all on cloud)"]
    for name, d in shards.items():
        g = d["gsearch"]
        if g.get("slots"):
            pts = f"each simpoint its own points ({g['needed']} needed, {len(g['points'])} distinct)"
        else:
            pts = " ".join(S.config_name(tuple(p)) for p in g["points"])
        lines.append(f"  {name}: {len(d['simulations'])} simpoints x {len(d['configurations'])} "
                     f"{'slots' if g.get('slots') else 'configs'} = {g['sims']} sims, "
                     f"~{g['h4d_core_minutes'] / 60:.1f} core-h; {pts}")
    return "\n".join(lines)


def describe_units(summary):
    lines = []
    for var, s in summary.items():
        rows = s["units"]
        for lv in S.LEVELS:
            us = [u for u in rows if u["level"] == lv]
            conv = sum(u["converged"] for u in us)
            pend = sum(u["pending"] for u in us)
            cap = sum(u["at_cap"] for u in us)
            lines.append(f"  {var} {lv}: {conv}/{len(us)} converged" + (f", {pend} pending" if pend else "")
                         + (f", {cap} at max_gamma" if cap else ""))
        for u in rows:
            if u["level"] in ("suite", "core") and u["best"]:
                lines.append(f"    {var} {'core ' if u['level'] == 'core' else ''}{u['name']}: {u['axes']} best gi{u['best'][0]}_gd{u['best'][1]} "
                             f"{u['gain_pct']:+.3f}% (measured max gi{u['max_point'][0]}_gd{u['max_point'][1]} "
                             f"{u['max_gain_pct']:+.3f}%), {'converged' if u['converged'] else 'needs ' + str(u['needs'])}")
        if s["dropped"]:
            lines.append(f"    {var} dropped (failed {study_max_attempts} times): {list(s['dropped'])}")
    return "\n".join(lines)


study_max_attempts = 2


# ---------------------------------------------------------------- running
def sh(cmd, logf=None, **kw):
    """Run a command (list), output to logf (append) or captured."""
    if logf:
        with open(logf, "a") as f:
            f.write(f"\n=== {now()} {' '.join(shlex.quote(str(c)) for c in cmd)}\n")
            f.flush()
            return subprocess.run([str(c) for c in cmd], stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  **kw)
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, stdin=subprocess.DEVNULL, **kw)


def point_of(params):
    """(gamma_i, gamma_d) of a config whose params set both gamma flags (the last occurrence wins, as in Scarab's
    parser), on any base policy; None for any other config (e.g. extra_configs like plain LRU)."""
    gi = re.findall(r"--mlp_lin_instr_lambda[ =](\S+)", params)
    gd = re.findall(r"--mlp_lin_data_lambda[ =](\S+)", params)
    if not gi or not gd:
        return None
    return int(float(gi[-1])), int(float(gd[-1]))


PERIODIC = ("Periodic_Instructions", "Periodic_Cycles")
CSV_META = ("Configuration", "Workload", "Cluster Id", "Collect Failed")


def load_periodic(desc):
    """(config, input, cluster, insts, cycles) per run in a shard's collected_stats.csv, from the measured-region
    Periodic_Instructions / Periodic_Cycles rows that sci's IPC row uses; insts/cycles None if the run failed.
    None when there is no CSV."""
    import io
    import pandas as pd
    csv = R.resolve(desc["root_dir"]) / "simulations" / desc["experiment"] / "collected_stats.csv"
    if not csv.is_file():
        return None
    wanted = {s + "," for s in CSV_META + PERIODIC}
    with open(csv) as f:
        lines = [next(f)] + [line for line in f if line[:line.find(",") + 1] in wanted]
    df = pd.read_csv(io.StringIO("".join(lines)), low_memory=False).set_index("stats")
    df = df[~df.index.duplicated(keep="first")]
    df = df[[c for c in df.columns if c not in ("write_protect", "groups")]]
    if any(r not in df.index for r in CSV_META + PERIODIC):
        return None
    out = []
    for _, col in df.T.iterrows():
        failed = str(col["Collect Failed"]).strip().lower() == "true"
        ins = pd.to_numeric(col[PERIODIC[0]], errors="coerce")
        cyc = pd.to_numeric(col[PERIODIC[1]], errors="coerce")
        good = not failed and ins > 0 and cyc > 0 and math.isfinite(ins) and math.isfinite(cyc)
        out.append((col["Configuration"], col["Workload"], str(col["Cluster Id"]),
                    float(ins) if good else None, float(cyc) if good else None))
    return out


def ingest_shard(name):
    """Rows for obs/<shard>.json: every (simpoint, point) the shard was to run, with its Periodic insts and cycles
    (None if it failed or is missing); the rows of extra (non-grid) configs; and the fraction that ran."""
    d = R.load_descriptor(str(INFRA / "json" / f"{name}.json"))
    kinds = {c: point_of(cfg.get("params", "")) for c, cfg in d["configurations"].items()}
    got = load_periodic(d)
    if got is None:
        log(f"{name}: no collected_stats.csv with Periodic_Instructions/Periodic_Cycles")
        got = []
    slots = d["gsearch"].get("slots")
    vals, extra = {}, []
    for cfg, wl, cl, ins, cyc in got:
        if cfg not in kinds:
            continue
        if slots:
            # Slot config s<j>: this simpoint's j-th point. A padded slot repeats a point; the runs are identical.
            own = slots.get(f"{wl}:{cl}")
            m = re.fullmatch(r"s(\d+)", cfg)
            if own is None or m is None or int(m.group(1)) >= len(own):
                continue
            p = tuple(own[int(m.group(1))])
            if (wl, cl, p[0], p[1]) in vals and vals[(wl, cl, p[0], p[1])][1] is not None and cyc is None:
                continue                    # keep a padded duplicate's good run over its failed twin
            vals[(wl, cl, p[0], p[1])] = (ins, cyc)
            continue
        p = kinds[cfg]
        if p is not None:
            vals[(wl, cl, p[0], p[1])] = (ins, cyc)
        else:
            extra.append({"config": cfg, "workload": wl, "cluster": cl, "inst": ins, "cycles": cyc,
                          "ipc": ins / cyc if ins and cyc else None})
    rows = []
    for sim in d["simulations"]:
        wl = f"{sim['suite']}/{sim['subsuite']}/{sim['workload']}"
        pts = (sorted({tuple(p) for p in slots[f"{wl}:{sim['cluster_id']}"]}) if slots
               else [tuple(p) for p in d["gsearch"]["points"]])
        for gi, gd in pts:
            ins, cyc = vals.get((wl, str(sim["cluster_id"]), gi, gd), (None, None))
            rows.append({"workload": wl, "cluster": str(sim["cluster_id"]), "gi": gi, "gd": gd,
                         "inst": ins, "cycles": cyc, "ipc": ins / cyc if ins and cyc else None})
    ok = sum(r["ipc"] is not None for r in rows) / max(1, len(rows))
    return rows, extra, ok


def collect_script(name, csv):
    """Shell: ./sci --collect-stats under a timeout; if that fails, kill what it left and run sci's collector
    with one process. Run in the infra checkout; the CSV lands at `csv`."""
    py = "$HOME/miniconda3/envs/scarabinfra/bin/python"
    tmp = csv.parent / "tmp"
    return (f"timeout {COLLECT_TIMEOUT_S} ./sci --collect-stats {name} || "
            f"{{ echo 'gsearch: ./sci --collect-stats failed or timed out; collecting with --jobs 1'; "
            f"pkill -f 'stat_collector[.]py -d [^ ]*{name}[.]json'; sleep 2; mkdir -p {tmp}; "
            f"TMPDIR={tmp} PYTHONPATH=$PWD timeout {3 * COLLECT_TIMEOUT_S} {py} scarab_stats/stat_collector.py "
            f"-d $PWD/json/{name}.json -o {csv} --postprocess --jobs 1; }}")


def pool():
    r = sh([CLOUDHOST, "ls", "--json"])
    if r.returncode != 0:
        raise RuntimeError(f"cloudhost ls --json failed: {r.stderr.strip()[-500:]}")
    return json.loads(r.stdout)


def lease_alive(p, host, shard):
    if host == LOCAL:
        return any(l.get("desc") == shard for l in p.get(LOCAL, {}).get("leases", []))
    l = p.get("cloud", {}).get(host, {}).get("lease")
    return bool(l and l.get("desc") == shard and l.get("kind") == "run")


class Runner:
    def __init__(self, study, state, where, logdir):
        self.study, self.state, self.where = study, state, where
        self.logdir = logdir
        logdir.mkdir(parents=True, exist_ok=True)
        self.env = dict(os.environ, CLOUDHOST_WHO=f"gsearch-{study.name}")

    def build_once(self, rnd):
        need = {}
        for name, shd in rnd["shards"].items():
            d = json.loads((INFRA / "json" / f"{name}.json").read_text())
            for c in d["configurations"].values():
                need.setdefault(c["binary"], name)
        for binary, name in sorted(need.items()):
            if binary in self.state.data["built"] and list((INFRA / "scarab_builds").glob(f"{binary}.*")):
                continue
            log(f"building {binary} once with ./sci --build-scarab {name}")
            r = sh(["./sci", "--build-scarab", name], logf=self.logdir / "build.log", cwd=INFRA)
            if r.returncode != 0 or not list((INFRA / "scarab_builds").glob(f"{binary}.*")):
                raise SystemExit(f"build of {binary} failed; see {self.logdir / 'build.log'}")
            self.state.data["built"].append(binary)
            self.state.save()

    def where_for(self, shd):
        """cloudhost --where for a shard: --where on the command line, else the study's `where`, which may map
        template tags to places (e.g. {"google": "cloud", "rest": "any"}: long sims run faster on an H4D). A shard's
        own `where` in the state (set by hand, e.g. to move a shard off an overloaded pakuchi) wins."""
        if shd.get("where"):
            return shd["where"]
        w = self.where
        if isinstance(w, dict):
            w = w.get(shd.get("label"), w.get("default", "any"))
        if w == "cloud" and shd.get("sims", 10**9) <= LOCAL_SMALL_SIMS:
            w = "any"      # a few sims need neither the faster machine nor a whole one
        return w

    def fit_local(self, name, shd):
        """Size a shard's max_parallel to pakuchi's free cores (at least LOCAL_MIN_PARALLEL), so it can run there
        when other runs hold the cloud machines. cloudhost asks pakuchi for min(sims, max_parallel) cores; on a
        cloud machine --max-parallel sets it instead."""
        try:
            free = int(pool().get(LOCAL, {}).get("free", 0))
        except (RuntimeError, json.JSONDecodeError, ValueError):
            return
        path = INFRA / "json" / f"{name}.json"
        d = json.loads(path.read_text())
        want = min(d["gsearch"]["sims"], self.study.parallel(shd.get("label")), max(LOCAL_MIN_PARALLEL, free))
        if d.get("max_parallel") != want:
            d["max_parallel"] = want
            write_json(path, d)

    def pinned_waiter_on_free_vm(self):
        """Is someone's `cloudhost run <vm> ...` (pinned to one machine, e.g. a replay that needs that machine's
        earlier output) waiting for a VM that is free right now? `run any` would take it first."""
        r = subprocess.run(["pgrep", "-af", r"cloudhost run cloudhost-[0-9]+ "], capture_output=True, text=True)
        pinned = set(re.findall(r"cloudhost run (cloudhost-\d+) ", r.stdout))
        if not pinned:
            return False
        try:
            p = pool()
        except (RuntimeError, json.JSONDecodeError):
            return False
        free = [h for h in pinned if not p.get("cloud", {}).get(h, {}).get("lease")]
        if free:
            log(f"leaving {', '.join(sorted(free))} to a run pinned to it")
        return bool(free)

    def cleanup_previous(self, name, shd):
        """Before a shard runs again, remove what its last attempt left on that host: job scripts
        (<container>_tmp_run.sh, which cloudhost's rsync never deletes), pool containers and the partial
        experiment dir. sci treats a killed attempt's sim dirs as done and its live-looking scripts as running,
        so a relaunch on the same host would skip those sims."""
        host = shd.get("host")
        if not host:
            return True
        if host != LOCAL:
            try:
                status = pool().get("cloud", {}).get(host, {}).get("status")
            except (RuntimeError, json.JSONDecodeError):
                status = None
            if status != "RUNNING":
                self.rename_shard(name, shd, f"{host} is {status or 'unreachable'}, so its leftovers can't be removed")
                return False
        d = json.loads((INFRA / "json" / f"{name}.json").read_text())
        exp_dir = f"{R.resolve(d['root_dir']).as_posix().rstrip('/')}/simulations/{d['experiment']}"
        script = (f"if systemctl is-active -q cloudhost-job-sim-{name} 2>/dev/null; then echo 'job still active'; exit 3; fi; "
                  f"cd {INFRA} && rm -f ./*_{name}_*_tmp_run.sh; "
                  f"docker ps -aq --filter name=_{name}_ | xargs -r docker rm -f >/dev/null; rm -rf {exp_dir}")
        cmd = ["bash", "-c", script] if host == LOCAL else [str(CLOUDHOST), "ssh", host, script]
        r = sh(cmd, logf=self.logdir / f"{name}.launch.log")
        if r.returncode != 0:
            log(f"{name}: cleanup of its last attempt on {host} failed (exit {r.returncode}); not relaunching yet")
            return False
        log(f"{name}: removed its last attempt's scripts, containers and sim dirs on {host}")
        with self.state.lock:
            shd["previous_hosts"] = shd.get("previous_hosts", []) + [host]
            for k in ("host", "lease", "launched", "fetched"):
                shd.pop(k, None)
            self.state.save()
        return True

    def rename_shard(self, name, shd, why):
        """Relaunch a shard under a new name (a fresh experiment, so nothing a previous attempt left on any host's
        disk can make sci skip its sims) and abandon the old name."""
        with self.state.lock:
            rnd = self.state.current()
            var, lab = shd["variant"], shd["label"]
            used = [int(n.rsplit("_", 1)[1]) for n, s in rnd["shards"].items() if s["variant"] == var and s["label"] == lab]
            new = f"gs_{self.study.name}_r{rnd['round']}_{var}_{lab}_{max(used) + 1}"
            d = json.loads((INFRA / "json" / f"{name}.json").read_text())
            d["experiment"] = new
            d["gsearch"]["renamed_from"] = name
            write_json(INFRA / "json" / f"{new}.json", d)
            write_json(self.study.dir / "rounds" / f"r{rnd['round']}" / "shards" / f"{new}.json", d)
            rnd["shards"][new] = {k: shd[k] for k in ("variant", "label", "sims", "h4d_core_minutes")}
            rnd["shards"][new].update(status="planned", renamed_from=name, relaunches=shd.get("relaunches", 0))
            shd["status"] = "abandoned"
            shd["abandoned_for"] = new
            self.state.save()
        log(f"{name}: relaunching as {new} ({why})")

    def launch(self, name, shd):
        if not self.cleanup_previous(name, shd):
            time.sleep(60)
            return False
        logf = self.logdir / f"{name}.launch.log"
        if self.where_for(shd) != "cloud":
            self.fit_local(name, shd)
        # No --wait: a shard that finds no host returns at once, so the launcher can try the next one (a
        # google shard waiting for a cloud machine must not hold back a SPEC shard pakuchi could take).
        cmd = [CLOUDHOST, "run", "any", name, "--infra", INFRA, "--where", self.where_for(shd),
               "--max-parallel", str(self.study.parallel(shd.get("label")))]
        mem = self.study.mem_per_sim(shd.get("label"))
        if mem:
            cmd += ["--mem-per-sim", f"{mem:g}"]     # cloudhost caps sims at once by the chosen VM's RAM
        start = logf.stat().st_size if logf.exists() else 0
        r = sh(cmd, logf=logf, env=self.env)
        text = logf.read_text()[start:]
        if r.returncode != 0 and "no host available" in text:
            return "nohost"
        if r.returncode != 0 and any(s in text for s in TRANSIENT):
            # cloudhost drops the lease of a launch that failed; an unreachable machine is not the shard's fault.
            log(f"launch of {name} hit a transient error (ssh/gcloud); trying again later")
            return "nohost"
        m = re.findall(rf"{re.escape(name)}: \d+ sims -> (\S+) \(lease (\w+)", text)
        with self.state.lock:
            if r.returncode == 0 and m:
                shd.update(status="launched", host=m[-1][0], lease=m[-1][1], launched=now())
                log(f"launched {name} on {m[-1][0]}")
            else:
                shd["launch_failures"] = shd.get("launch_failures", 0) + 1
                shd["status"] = "planned"
                log(f"launch of {name} failed (exit {r.returncode}); see {logf}")
            self.state.save()
        return shd["status"] == "launched"

    def fetch(self, name, shd):
        """Collect a finished shard's stats into the local root_dir. True when collected_stats.csv is here."""
        d = json.loads((INFRA / "json" / f"{name}.json").read_text())
        root = R.resolve(d["root_dir"]).as_posix().rstrip("/")
        csv = Path(f"{root}/simulations/{d['experiment']}/collected_stats.csv")
        logf = self.logdir / f"{name}.collect.log"
        host = shd["host"]
        if host == LOCAL:
            sh(["bash", "-c", collect_script(name, csv)], logf=logf, cwd=INFRA)
            return csv.is_file()
        status = pool().get("cloud", {}).get(host, {}).get("status")
        if status == "RUNNING":
            script = f"cd {INFRA} && {{ {collect_script(name, csv)}; }} >&2 && gzip -c {csv}"
            tmp = csv.with_name("collected_stats.csv.gz.part")
            csv.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as out, open(logf, "a") as err:
                err.write(f"\n=== {now()} remote collect on {host}\n")
                err.flush()
                r = subprocess.run([str(CLOUDHOST), "ssh", host, f"bash -lc {shlex.quote(script)}"], stdout=out,
                                   stderr=err, stdin=subprocess.DEVNULL)
            if r.returncode == 0:
                try:
                    with gzip.open(tmp, "rb") as src, open(csv.with_suffix(".csv.part"), "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    csv.with_suffix(".csv.part").replace(csv)
                    tmp.unlink()
                    shd["fetched"] = "remote-collect"
                    return True
                except (OSError, EOFError) as e:
                    log(f"{name}: bad collected_stats.csv.gz from {host}: {e}")
            log(f"{name}: remote collect on {host} failed (exit {r.returncode}); pulling instead")
        r = sh([CLOUDHOST, "pull", host, name, "--infra", INFRA], logf=logf, env=self.env)
        if r.returncode != 0:
            return False
        sh(["bash", "-c", collect_script(name, csv)], logf=logf, cwd=INFRA)
        shd["fetched"] = "pull"
        return csv.is_file()

    def reap_stuck_collectors(self, launched, shards):
        """Kill stat collectors of still-leased shards that have run longer than COLLECT_TIMEOUT_S."""
        by_host = defaultdict(list)
        for n in launched:
            by_host[shards[n]["host"]].append(n)
        for host, names in by_host.items():
            pat = "|".join(re.escape(n) for n in names)
            script = (f"ps -eo pid=,etimes=,args= | awk '/stat_collector[.]py -d [^ ]*({pat})[.]json/ "
                      f"&& $2 > {COLLECT_TIMEOUT_S} {{print $1, $0}}' | while read pid rest; do "
                      "echo \"killing stuck collector: $rest\"; pkill -P $pid; kill $pid; done")
            cmd = ["bash", "-c", script] if host == LOCAL else [str(CLOUDHOST), "ssh", host, script]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
            except subprocess.TimeoutExpired:
                continue
            for line in r.stdout.splitlines():
                log(f"{host}: {line.strip()[:200]}")

    def ingest(self, name, shd=None):
        return ingest_shard(name)

    def finish(self, name, shd):
        if not self.fetch(name, shd):
            log(f"{name}: no collected stats")
            rows, extra, ok = [], [], 0.0
        else:
            rows, extra, ok = self.ingest(name, shd)
        if ok < 0.5 and shd.get("relaunches", 0) < SHARD_RETRIES:
            shd["relaunches"] = shd.get("relaunches", 0) + 1
            shd["status"] = "planned"
            log(f"{name}: only {ok:.0%} of its sims ran; relaunching ({shd['relaunches']}/{SHARD_RETRIES})")
            return
        obs = self.study.dir / "obs"
        write_json(obs / f"{name}.json", rows)
        if extra:
            write_json(obs / f"{name}.extra.json", extra)
        shd.update(status="ingested", ok_fraction=round(ok, 4), finished=now())
        log(f"{name}: ingested {len(rows)} runs, {ok:.1%} ok")

    def run_round(self, rnd):
        self.build_once(rnd)
        shards = rnd["shards"]
        # Longest wall time first: waves (sims / max_parallel) times minutes per sim, so single-wave 250M google
        # shards go out before bigger but shorter SPEC ones.
        def wall(n):
            s = shards[n]
            par = min(VM_CORES, self.study.parallel(s.get("label")))
            return math.ceil(s["sims"] / par) * s["h4d_core_minutes"] / max(1, s["sims"])
        def order():
            """The round's live shards, longest first (shards can be added mid-round by a relaunch under a new
            name; an abandoned one is not waited for)."""
            with self.state.lock:
                names = [n for n in list(shards) if shards[n]["status"] != "abandoned"]
            return sorted(names, key=lambda n: -wall(n))
        stop = threading.Event()

        def launcher():
            while not stop.is_set():
                todo = [n for n in order() if shards[n]["status"] == "planned"
                        and shards[n].get("launch_failures", 0) < MAX_LAUNCH_FAILURES]
                if not todo:
                    if all(shards[n]["status"] != "planned" for n in order()):
                        return
                    time.sleep(30)
                    continue
                if self.pinned_waiter_on_free_vm():
                    time.sleep(90)               # let a run pinned to that VM (its own `--wait` polls every 60 s) take it
                    continue
                placed = False
                for n in todo:                   # longest first; skip the ones no host can take right now
                    if stop.is_set():
                        return
                    if self.launch(n, shards[n]) is True:
                        placed = True
                if not placed:
                    time.sleep(60)

        th = threading.Thread(target=launcher, daemon=True)
        th.start()
        try:
            while True:
                with self.state.lock:
                    pending = [n for n in order() if shards[n]["status"] != "ingested"]
                if not pending:
                    break
                stuck = [n for n in pending if shards[n]["status"] == "planned" and shards[n].get("launch_failures", 0) >= MAX_LAUNCH_FAILURES]
                if stuck:
                    raise SystemExit(f"shards failed to launch {MAX_LAUNCH_FAILURES} times: {stuck}; see {self.logdir}")
                launched = [n for n in pending if shards[n]["status"] == "launched"]
                self.reap_stuck_collectors(launched, shards)
                if launched:
                    try:
                        p = pool()
                    except (RuntimeError, json.JSONDecodeError) as e:
                        log(f"pool poll failed: {e}")
                        p = None
                    for n in launched:
                        if p is not None and not lease_alive(p, shards[n]["host"], n):
                            log(f"{n} finished on {shards[n]['host']}; collecting")
                            with self.state.lock:
                                shards[n]["status"] = "collecting"
                            self.finish(n, shards[n])
                            self.state.save()
                            if shards[n]["status"] == "planned" and not th.is_alive():
                                th = threading.Thread(target=launcher, daemon=True)
                                th.start()
                for n in pending:
                    if shards[n]["status"] == "collecting":       # interrupted mid-collect last time
                        self.finish(n, shards[n])
                        self.state.save()
                if not th.is_alive() and any(shards[n]["status"] == "planned" for n in order()):
                    th = threading.Thread(target=launcher, daemon=True)
                    th.start()
                if all(shards[n]["status"] == "ingested" for n in order()):
                    continue
                time.sleep(POLL_S)
        finally:
            stop.set()
        rnd["status"] = "done"
        rnd["finished"] = now()
        self.state.save()


# ---------------------------------------------------------------- commands
def next_round(study, state):
    k = len(state.rounds) + 1
    shards, summary = plan_round(study, state, k)
    return k, shards, summary


def cmd_plan(study, state, a):
    cur = state.current()
    if cur and cur["status"] != "done":
        print(f"round {cur['round']} is still {cur['status']}:")
        print(describe_plan({n: json.loads((INFRA / 'json' / f'{n}.json').read_text()) for n in cur["shards"]}))
        return
    k, shards, summary = next_round(study, state)
    print(f"study {study.name}, round {k} (max_rounds {study.max_rounds}):")
    print(describe_units(summary))
    print(describe_plan(shards) if shards else "nothing left to run: every unit converged")


def start_round(study, state, k, shards, summary):
    study.freeze()
    rdir = study.dir / "rounds" / f"r{k}"
    write_json(rdir / "decision.json", summary)
    for name, d in shards.items():
        write_json(INFRA / "json" / f"{name}.json", d)
        write_json(rdir / "shards" / f"{name}.json", d)
    rnd = {"round": k, "status": "running", "planned": now(),
           "shards": {n: {"variant": d["gsearch"]["variant"], "label": d["gsearch"]["label"], "status": "planned",
                          "sims": d["gsearch"]["sims"], "h4d_core_minutes": d["gsearch"]["h4d_core_minutes"]}
                      for n, d in shards.items()}}
    state.rounds.append(rnd)
    state.save()
    return rnd


def step(study, state, a):
    """Run (or finish) one round. Returns False when there is nothing left to do."""
    cur = state.current()
    if cur is None or cur["status"] == "done":
        k, shards, summary = next_round(study, state)
        log(f"study {study.name}, round {k}:\n{describe_units(summary)}")
        if not shards:
            log("every unit converged; nothing to run")
            return False
        if k > study.max_rounds:
            log(f"max_rounds {study.max_rounds} reached; {sum(d['gsearch']['sims'] for d in shards.values())} sims "
                "still wanted (see `report`)")
            return False
        log(describe_plan(shards))
        if not a.yes:
            if input("launch this round? [y/N] ").strip().lower() != "y":
                return False
        cur = start_round(study, state, k, shards, summary)
    else:
        log(f"resuming round {cur['round']} ({sum(s['status'] == 'ingested' for s in cur['shards'].values())}/"
            f"{len(cur['shards'])} shards ingested)")
    Runner(study, state, a.where or study.where, study.dir / "logs").run_round(cur)
    log(f"round {cur['round']} done")
    write_report(study, state, quiet=True)
    return True


def cmd_step(study, state, a):
    step(study, state, a)


def cmd_run(study, state, a):
    while step(study, state, a):
        pass
    write_report(study, state)


def cmd_status(study, state, a):
    try:
        p = pool()
    except RuntimeError as e:
        p = None
        print(e)
    for rnd in state.rounds:
        counts = defaultdict(int)
        for s in rnd["shards"].values():
            counts[s["status"]] += 1
        print(f"round {rnd['round']}: {rnd['status']} (planned {rnd['planned']}"
              + (f", finished {rnd['finished']}" if rnd.get("finished") else "") + f"); {dict(counts)}")
        if rnd["status"] != "done":
            for n, s in rnd["shards"].items():
                alive = p is not None and s.get("host") and lease_alive(p, s["host"], n)
                print(f"  {n}: {s['status']}" + (f" on {s['host']} since {s.get('launched')}" if s.get("host") else "")
                      + (" (running)" if alive else "") + f", {s['sims']} sims")
    dec = study.dir / "rounds" / f"r{len(state.rounds)}" / "decision.json"
    if dec.is_file():
        print(f"decision before round {len(state.rounds)}:")
        print(describe_units(json.loads(dec.read_text())))


def write_report(study, state, quiet=False):
    import pandas as pd
    out = study.dir / "report"
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"# Gamma search {study.name}\n", f"Rounds run: {len(state.rounds)}. Settings: step {study.settings.step}, "
             f"init {study.settings.init}, min_gain_pp {study.settings.min_gain_pp}, gi_sensitive_pct "
             f"{study.settings.gi_sensitive_pct}, max_gamma {study.settings.max_gamma}, lookahead "
             f"{study.settings.lookahead}.\n"]
    all_rows, obs_rows, extra_rows = [], [], []
    for var, members in study.members.items():
        sps = [m[0] for m in members.values()]
        runs = state.runs(var)
        needs, units, model = S.decide(sps, runs, study.settings)
        for r in unit_rows(units):
            r["variant"] = var
            all_rows.append(r)
        for key, pts in model.ipc.items():
            sp = model.sps[key]
            for p, v in pts.items():
                obs_rows.append({"variant": var, "arch": study.variants[var][members[key][1]]["architecture"],
                                 "suite": sp.suite, "subsuite": "/".join(key[0].split("/")[:2]),
                                 "sci_workload": sp.group, "input": key[0], "cluster": key[1], "weight": sp.weight,
                                 "gi": p[0], "gd": p[1], "inst": model.ic[key][p][0], "cycles": model.ic[key][p][1],
                                 "ipc": v, "gain_pct": (v / pts[(0, 0)] - 1) * 100 if (0, 0) in pts else None,
                                 "gi_sensitivity": model.sens[key][0]})
        for rnd in state.rounds:                 # reference configs (extra_configs, e.g. plain LRU / DRRIP)
            for n, sh in rnd["shards"].items():
                f = study.dir / "obs" / f"{n}.extra.json"
                if sh["variant"] == var and sh["status"] == "ingested" and f.is_file():
                    for r in json.loads(f.read_text()):
                        k = (r["workload"], str(r["cluster"]))
                        extra_rows.append({"variant": var, "config": r["config"], "input": r["workload"],
                                           "cluster": r["cluster"], "weight": members[k][0].weight if k in members else None,
                                           "inst": r.get("inst"), "cycles": r.get("cycles"), "ipc": r.get("ipc")})
        lines.append(f"## {var}\n")
        lines.append(f"{len(sps)} simpoints, {len(runs)} runs ingested; "
                     f"{sum(1 for u in units if u.converged)}/{len(units)} units converged; "
                     f"{len(needs)} simpoints still want runs; dropped {len(model.dropped)}.\n")
        cores = [u for u in units if u.level == "core"]
        if cores:
            snames = sorted({model.sps[k].suite for k in model.active()})
            by_s = {s: [k for k in model.active() if model.sps[k].suite == s] for s in snames}
            lines.append("Core units: sci's Avg (geomean of the suites) at one static setting; `all` searches both "
                         "gammas, `gd=0` gamma_i alone, `gi=0` gamma_d alone. Suite columns: each suite's gain at the "
                         "core incumbent.\n")
            lines.append("| core unit | axes | best (incumbent) | Avg gain % | best measured | gain % | "
                         + " | ".join(snames) + " | converged | at cap |")
            lines.append("|---|---|---|---|---|---|" + "---|" * len(snames) + "---|---|")
            for u in cores:
                if u.best is None:
                    lines.append(f"| {u.name} | | pending |")
                    continue
                per = [model.unit_value("suite", by_s[s], u.best) for s in snames]
                lines.append(f"| {u.name} | {'+'.join(u.axes)} | {S.config_name(u.best)} | {u.gain:+.3f} | "
                             f"{S.config_name(u.max_point)} | {u.max_gain:+.3f} | "
                             + " | ".join("" if v is None else f"{v:+.2f}" for v in per)
                             + f" | {u.converged} | {u.at_cap} |")
            lines.append("")
        lines.append("| suite | axes | best (incumbent) | gain % | best measured | gain % | per-workload oracle % "
                     "| per-simpoint oracle % | converged | at cap |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        suites = [u for u in units if u.level == "suite"]
        for u in suites:
            if u.best is None:
                lines.append(f"| {u.name} | | pending | | | | | | | |")
                continue
            wls = [w for w in units if w.level == "workload" and w.suite == u.name and w.best is not None]
            sp_units = {w.name: w for w in units if w.level == "simpoint" and w.suite == u.name and w.best}
            # Oracles, aggregated as the suite is: each workload at its own best point; each simpoint at its own.
            wl_or = (math.exp(sum(math.log(1 + w.max_gain / 100) for w in wls) / len(wls)) - 1) * 100 if wls else math.nan
            members_u = [k for k in model.active() if model.sps[k].suite == u.name]
            sp_best = {k: sp_units[f"{k[0]}:{k[1]}"].max_point if f"{k[0]}:{k[1]}" in sp_units else (0, 0)
                       for k in members_u}
            sp_or = model.aggregate(members_u, lambda k: model.value_ic(k, sp_best[k]),
                                    lambda k: model.value_ic(k, (0, 0)))
            sp_or = math.nan if sp_or is None else sp_or
            lines.append(f"| {u.name} | {'+'.join(u.axes)} | {S.config_name(u.best)} | {u.gain:+.3f} | "
                         f"{S.config_name(u.max_point)} | {u.max_gain:+.3f} | {wl_or:+.3f} | {sp_or:+.3f} | "
                         f"{u.converged} | {u.at_cap} |")
        lines.append("")
        for u in suites:
            if u.best is None:
                continue
            members_u = [k for k in model.active() if model.sps[k].suite == u.name]
            ran = set().union(*(model.ipc[k].keys() for k in members_u))
            pts = sorted(p for p in model.candidates(members_u, ("gi", "gd"))
                         if p in ran and model.unit_value("suite", members_u, p) is not None)
            gis = sorted({p[0] for p in pts})
            gds = sorted({p[1] for p in pts})
            lines.append(f"{var} / {u.name}: suite gain % at every point all its simpoints have "
                         f"(rows γi, columns γd; imputed cells for γi-insensitive simpoints):\n")
            lines.append("| | " + " | ".join(f"γd {g}" for g in gds) + " |")
            lines.append("|---|" + "---|" * len(gds))
            for gi in gis:
                cells = []
                for gd in gds:
                    v = model.unit_value("suite", members_u, (gi, gd)) if (gi, gd) in pts else None
                    c = "" if v is None else f"{v:+.2f}"
                    cells.append(f"**{c}**" if (gi, gd) == u.best else c)
                lines.append(f"| **γi {gi}** | " + " | ".join(cells) + " |")
            lines.append("")
        sp_units = [u for u in units if u.level == "simpoint" and u.best]
        if sp_units:
            from collections import Counter
            c = Counter((u.suite, S.config_name(u.best)) for u in sp_units)
            lines.append("Per-simpoint incumbents, most common per suite: " + "; ".join(
                f"{s}: " + ", ".join(f"{cfg} {n}" for (s2, cfg), n in c.most_common() if s2 == s)[:200]
                for s in sorted({u.suite for u in sp_units})) + "\n")
    pd.DataFrame(all_rows).to_csv(out / "units.csv", index=False)
    for lv in S.LEVELS:
        pd.DataFrame([r for r in all_rows if r["level"] == lv]).to_csv(out / f"best_{lv}.csv", index=False)
    pd.DataFrame(obs_rows).to_csv(out / "obs.csv", index=False)
    pd.DataFrame(extra_rows).to_csv(out / "extra.csv", index=False)
    text = "\n".join(lines)
    (out / "summary.md").write_text(text + "\n")
    if not quiet:
        print(text)
        print(f"\nwrote summary.md, units.csv, best_{{simpoint,input,workload,suite}}.csv, obs.csv to {out}/")


def cmd_reingest(study, state, a):
    """Rewrite every ingested shard's obs/<shard>.json from its collected_stats.csv (e.g. after a metric change)."""
    obs = study.dir / "obs"
    for rnd in state.rounds:
        for n, sh in rnd["shards"].items():
            if sh["status"] != "ingested":
                continue
            old = json.loads((obs / f"{n}.json").read_text())
            rows, extra, ok = ingest_shard(n)
            old_ok = sum(r.get("ipc") is not None for r in old)
            new_ok = sum(r["ipc"] is not None for r in rows)
            if len(rows) != len(old) or new_ok < old_ok:
                raise SystemExit(f"{n}: reingest gives {new_ok}/{len(rows)} runs, had {old_ok}/{len(old)}; not writing")
            write_json(obs / f"{n}.json", rows)
            if extra:
                write_json(obs / f"{n}.extra.json", extra)
            log(f"{n}: reingested {new_ok}/{len(rows)} runs with Periodic insts and cycles")


def cmd_check_metric(study, state, a):
    """Check the search's input and suite values against sci's own on ingested shards: each input's value
    against the IPC row stat_collector writes (cycle-weighted over the input's simpoints), and each suite's
    gain over gi0_gd0 against aggregation.Hierarchy applied to those IPC rows."""
    import io
    import pandas as pd
    names = a.shards or [n for rnd in state.rounds[:1] for n, sh in rnd["shards"].items() if sh["status"] == "ingested"]
    worst_in = worst_suite = 0.0
    for n in names:
        d = R.load_descriptor(str(INFRA / "json" / f"{n}.json"))
        if d["gsearch"].get("slots"):
            print(f"{n}: slot shard (per-simpoint gammas), skipped; check a shard with named configs")
            continue
        var = d["gsearch"]["variant"]
        csv = R.resolve(d["root_dir"]) / "simulations" / d["experiment"] / "collected_stats.csv"
        wanted = {s + "," for s in CSV_META + PERIODIC + ("IPC",)}
        with open(csv) as f:
            lines = [next(f)] + [line for line in f if line[:line.find(",") + 1] in wanted]
        df = pd.read_csv(io.StringIO("".join(lines)), low_memory=False).set_index("stats")
        df = df[~df.index.duplicated(keep="first")]
        t = df[[c for c in df.columns if c not in ("write_protect", "groups")]].T
        t = t[t["Configuration"] == t["Configuration"]]
        sci_ipc = {(r["Configuration"], r["Workload"]): float(r["IPC"]) for _, r in t.iterrows()}
        inputs = sorted({w for _, w in sci_ipc})
        hier = AGG.Hierarchy(inputs)
        bench = {label: wk for wk, labels in hier.workloads.items() for label in labels}
        members = study.members[var]
        keys = [k for k in members if k[0] in set(inputs)
                and any(s["cluster_id"] == int(k[1]) and f"{s['suite']}/{s['subsuite']}/{s['workload']}" == k[0]
                        for s in d["simulations"])]
        sps = [dataclasses.replace(members[k][0], bench=bench[k[0]]) for k in keys]
        rows, _, _ = ingest_shard(n)
        m = S.Model(sps, [(r["workload"], r["cluster"], r["gi"], r["gd"], r["inst"], r["cycles"]) for r in rows],
                    study.settings)
        for cfg, c in d["configurations"].items():
            p = point_of(c.get("params", ""))
            if p is None:
                continue
            # Raw runs only: the search imputes a gamma_i-insensitive simpoint's gamma_i>0 cells, sci does not.
            raw = lambda k, q: m.ic[k].get(q)
            for wl in inputs:
                ks = [k for k in keys if k[0] == wl]
                vals = [raw(k, p) for k in ks]
                if any(v is None for v in vals) or (cfg, wl) not in sci_ipc:
                    continue
                ours = sum(m.sps[k].weight * v[0] for k, v in zip(ks, vals)) / sum(m.sps[k].weight * v[1] for k, v in zip(ks, vals))
                worst_in = max(worst_in, abs(ours / sci_ipc[(cfg, wl)] - 1))
            for suite in hier.suites:
                ks = [k for k in keys if k[0].split("/")[0] == suite]
                ours = m.aggregate(ks, lambda k: raw(k, p), lambda k: raw(k, (0, 0)))
                agg = hier.aggregate({wl: sci_ipc.get((cfg, wl)) for wl in inputs}, geometric=True)["suites"][suite]
                base = hier.aggregate({wl: sci_ipc.get(("gi0_gd0", wl)) for wl in inputs}, geometric=True)["suites"][suite]
                if ours is not None and agg and base:
                    worst_suite = max(worst_suite, abs(ours - (agg / base - 1) * 100))
        print(f"{n}: checked {len(inputs)} inputs x {len(d['configurations'])} configs")
    print(f"largest input difference from sci's IPC row: {worst_in:.2e} (relative); "
          f"largest suite-gain difference from sci's nested geomean: {worst_suite:.2e} pp")


def cmd_replan(study, state, a):
    replan(study, state)


def cmd_top_up(study, state, a):
    topup(study, state, a.yes)


def cmd_report(study, state, a):
    write_report(study, state)


def cmd_pull(study, state, a):
    env = dict(os.environ, CLOUDHOST_WHO=f"gsearch-{study.name}")
    for rnd in state.rounds:
        for n, s in rnd["shards"].items():
            if s.get("host") and s["host"] != LOCAL and s.get("status") == "ingested" and not s.get("pulled"):
                log(f"pulling {n} from {s['host']}")
                r = sh([CLOUDHOST, "pull", s["host"], n, "--infra", INFRA], logf=study.dir / "logs" / f"{n}.pull.log",
                       env=env)
                if r.returncode == 0:
                    s["pulled"] = now()
                    state.save()


def new_study(a):
    """Write a new study (templates, study file, smoke study) from an existing one: same suites, run lengths and
    search settings, with the architectures, base params, binary and search settings the flags change."""
    src_path = Path(a.source) if Path(a.source).suffix else HERE / "studies" / f"{a.source}.json"
    src = json.loads(src_path.read_text())
    name = a.study
    if not re.fullmatch(r"[A-Za-z0-9]+", name):
        raise SystemExit("study name: letters and digits only (it is part of every shard's name)")
    out_study = HERE / "studies" / f"{name}.json"
    if out_study.exists() and not a.force:
        raise SystemExit(f"{out_study} exists (--force to overwrite)")
    # One source variant's templates serve as the pattern for every new architecture.
    pattern = next(iter(src["variants"].values()))
    tmpls = {lab: json.loads((INFRA / "json" / f"{t}.json").read_text()) for lab, t in pattern.items()}
    first = next(iter(next(iter(tmpls.values()))["configurations"].values()))
    archs = a.arch or sorted({t["architecture"] for t in tmpls.values()})
    scarab_path = a.scarab_path or next(iter(tmpls.values()))["scarab_path"]
    binary = a.binary or first["binary"]
    if binary == "head":
        h = subprocess.run(["git", "-C", scarab_path, "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True)
        binary = f"scarab_{h.stdout.strip()}"
    head = subprocess.run(["git", "-C", scarab_path, "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True).stdout.strip()
    if binary != f"scarab_{head}" and not a.scarab_path:
        raise SystemExit(f"{binary} is not the HEAD ({head}) of {scarab_path}: sci would check it out there to build it, "
                         "under any study shipping from that clone. Give the study its own clone (--scarab-path).")
    for arch in archs:
        if not (Path(scarab_path) / "src" / f"PARAMS.{arch}").is_file():
            raise SystemExit(f"no PARAMS.{arch} in {scarab_path}/src")
    if not list((INFRA / "scarab_builds").glob(f"{binary}.*")):
        log(f"note: {binary} is not built yet; the first round builds it once before launching")

    def tag(arch):
        return re.sub(r"[^A-Za-z0-9]+", "_", arch.removeprefix("golden_"))

    def write_tmpl(tname, t):
        t = copy.deepcopy(t)
        t["experiment"] = tname
        write_json(INFRA / "json" / f"{tname}.json", t)
        return tname

    def retarget(t, arch, base_params, why):
        t = copy.deepcopy(t)
        t["architecture"] = arch
        t["scarab_path"] = scarab_path
        b = next(iter(t["configurations"].values()))
        b["binary"] = binary
        if base_params is not None:
            limit = R.flag_value(b.get("params", ""), "inst_limit")
            b["params"] = (base_params + (f" --inst_limit {limit}" if limit and "inst_limit" not in base_params else "")).strip()
            b["_params"] = f"Base params of study {name}" + (" (run length kept from the source template)." if limit else ".")
        t["_experiment"] = f"Template for tools/mlp_gsearch study {name} ({why}), from {src['name']}'s; written by gsearch.py new-study."
        return t

    variants, smoke_variants = {}, {}
    for arch in archs:
        variants[tag(arch)], smoke_variants[tag(arch)] = {}, {}
        for lab, t in tmpls.items():
            nt = retarget(t, arch, a.params, f"{arch}, {lab}")
            variants[tag(arch)][lab] = write_tmpl(f"gs_tmpl_{name}_{lab}_{tag(arch)}", nt)
            # Smoke: one workload per suite, top simpoint; google shortened to 50M warmup + 20M measured.
            st = copy.deepcopy(nt)
            st["top_simpoint"] = 1
            picked, sims = set(), []
            for (suite, sub, wl, cl), sim in P.simpoints(nt):
                if suite in picked:
                    continue
                picked.add(suite)
                sims.append({"suite": suite, "subsuite": sub, "workload": wl, "cluster_id": None,
                             "simulation_type": sim.get("simulation_type") or "memtrace", "warmup": sim.get("warmup")})
            st["simulations"] = sims
            st["_simulations"] = "Smoke test: the first workload of each suite, top simpoint."
            b = next(iter(st["configurations"].values()))
            if R.flag_value(b["params"], "inst_limit"):
                warm = int(sims[0]["warmup"] or 0)
                b["params"] = re.sub(r"--inst_limit[ =]\S+", f"--inst_limit {warm + 20_000_000}", b["params"])
            smoke_variants[tag(arch)][lab] = write_tmpl(f"gs_tmpl_{name}smoke_{lab}_{tag(arch)}", st)
    spec = {k: v for k, v in src.items() if k not in ("variants",)}
    spec["_doc"] = (a.doc or f"Gamma search {name}: {', '.join(archs)}"
                    + (f", base params {a.params!r}" if a.params else "") + f"; written by gsearch.py new-study from {src['name']}.")
    spec["name"] = name
    spec["variants"] = variants
    for key in ("step", "init", "max_gamma", "max_rounds", "init_extra"):
        v = getattr(a, key.replace("-", "_"))
        if v is not None:
            spec[key] = json.loads(v) if isinstance(v, str) else v
    if a.extra:
        spec["extra_configs"] = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in a.extra}
    S.Settings.from_study(spec)          # validate init/step/max_gamma now, not at launch
    write_json(out_study, spec)
    smoke = {"_doc": f"Smoke test of study {name}: one workload per suite, top simpoint, one round, on pakuchi.",
             "name": f"{name}smoke", "state_dir": spec.get("state_dir", "/mnt/storage/mgiordan/gsearch"),
             "variants": smoke_variants, "max_rounds": 1, "where": "local",
             "extra_configs": dict(spec.get("extra_configs", {}), lru=spec.get("extra_configs", {}).get("lru", "--mlc_cache_repl_policy 0"))}
    for key in ("step", "init", "init_extra", "max_gamma", "min_gain_pp", "gi_sensitive_pct", "lookahead", "partial_gain", "max_parallel"):
        if key in spec:
            smoke[key] = spec[key]
    write_json(HERE / "studies" / f"{name}_smoke.json", smoke)
    print(f"wrote {out_study.relative_to(INFRA)} ({', '.join(variants)}), studies/{name}_smoke.json and "
          f"{2 * sum(len(v) for v in variants.values())} templates in json/")
    print(f"next: gsearch.py run {name}_smoke --yes   (one round on pakuchi; check gi0_gd0 against the extra configs)\n"
          f"      gsearch.py plan {name}   then   setsid nohup python -u tools/mlp_gsearch/gsearch.py run {name} --yes "
          f"> <state_dir>/{name}/run.log 2>&1 < /dev/null &")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "new-study":
        ap = argparse.ArgumentParser(prog="gsearch.py new-study", description=new_study.__doc__)
        ap.add_argument("cmd")
        ap.add_argument("study", help="new study name (letters and digits)")
        ap.add_argument("--from", dest="source", default="cd1", help="study to copy suites, run lengths and settings from")
        ap.add_argument("--arch", action="append", help="PARAMS.<arch> to search (repeat for several; default: the source's)")
        ap.add_argument("--params", help="base params replacing the templates' (their --inst_limit is kept)")
        ap.add_argument("--binary", help="scarab_<githash>, or 'head' for scarab_path's HEAD (default: the source's)")
        ap.add_argument("--scarab-path", help="scarab checkout to build and ship (default: the source's)")
        ap.add_argument("--step", help="JSON: 12 or [gamma_i step, gamma_d step]")
        ap.add_argument("--init", help='JSON: round 1 points as rows keyed by gamma_i, e.g. {"0": [0, 1, 2], "16": [0]}')
        ap.add_argument("--max-gamma", dest="max_gamma", help="JSON: [gamma_i cap, gamma_d cap]")
        ap.add_argument("--max-rounds", dest="max_rounds", type=int)
        ap.add_argument("--init-extra", dest="init_extra", help='JSON: extra round-1 points per template tag, e.g. {"google": {"48": [12, 36], "144": [12, 36]}}')
        ap.add_argument("--extra", action="append", help="NAME=PARAMS config added to round 1 (e.g. drrip='--mlc_cache_repl_policy 18')")
        ap.add_argument("--doc", help="one-line description for the study file")
        ap.add_argument("--force", action="store_true")
        new_study(ap.parse_args())
        return
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["plan", "step", "run", "status", "report", "pull", "replan", "top-up",
                                    "reingest", "check-metric"])
    ap.add_argument("study")
    ap.add_argument("shards", nargs="*", help="check-metric: shard names (default: round 1's)")
    ap.add_argument("--yes", action="store_true", help="launch rounds without asking")
    ap.add_argument("--where", choices=["any", "local", "cloud"], help="override the study's cloudhost --where")
    a = ap.parse_args()
    study = Study(a.study)
    global study_max_attempts
    study_max_attempts = study.settings.max_attempts
    state = State(study)
    globals()[f"cmd_{a.cmd.replace('-', '_')}"](study, state, a)


if __name__ == "__main__":
    main()
