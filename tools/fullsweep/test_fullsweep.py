"""Offline tests for tools/fullsweep: no simulation runs and the real cloudhost is never run (its code is imported
for its shard split, as fullsweep.py does).

    ~/miniconda3/envs/scarabinfra/bin/python tools/fullsweep/test_fullsweep.py

The gen tests read the gsearch cd1 report (plan gsearch_dir) and are skipped without it. Descriptors are checked
with sci's own code: simpoint selection (load_workloads_for_descriptor, as ./sci --sim and cloudhost's plan_run.py
expand them) and per-sim params (parse_workload_params + resolve_params, as sci writes PARAMS).
"""
import argparse
import copy
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

os.environ["FULLSWEEP_NO_REEXEC"] = "1"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fullsweep as F  # noqa: E402

U = F.U
PLAN = F.load_json(F.DEFAULT_PLAN)
GSEARCH = Path(PLAN["gsearch_dir"])
HAVE_CD1 = (GSEARCH / "cd1" / "report" / "best_core.csv").exists()
PLAN_RUN = Path(F.CLOUDHOST).parent / "plan_run.py"


def expand_desc(d):
    """[(config, suite, subsuite, workload, cluster_id, params)] for every sim sci would run for descriptor d."""
    db = U.load_workloads_for_descriptor(d, str(F.INFRA), dbg_lvl=0)
    out = []
    for sim in U.normalize_simulations(d["simulations"]):
        suite, sub, wl = sim["suite"], sim["subsuite"], sim["workload"]
        for cid in U.get_simpoints(db[suite][sub][wl], sim["simulation_type"], 0):
            for cname, c in d["configurations"].items():
                entries = U.parse_workload_params(cname, c.get("workload_params"), db)
                out.append((cname, suite, sub, wl, int(cid),
                            U.resolve_params(c["params"], entries, suite, sub, wl, cid)))
    return out


def gammas(params):
    return (F.flag_value(params, "mlp_lin_instr_lambda"), F.flag_value(params, "mlp_lin_data_lambda"))


@unittest.skipUnless(HAVE_CD1, "no gsearch cd1 report")
class GenCd1Small(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        plan = copy.deepcopy(PLAN)
        plan["state_dir"] = cls.tmp.name
        cls.plan = plan
        cls.out = F.build_sweep(F.DEFAULT_PLAN, "cd1/small", name="fs_test", plan=plan)
        cls.master = expand_desc(cls.out["master"])
        cls.groups = {g: cls.out["files"][gi["desc"]] for g, gi in cls.out["st"]["groups"].items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_counts(self):
        per_cfg = Counter(r[0] for r in self.master)
        self.assertEqual(set(per_cfg), {"lru", "mlp_core", "mlp_gd", "mlp_gi", "mlp_sp", "icarus", "emissary",
                                        "emissary_r1", "lacs", "clip", "drrip", "mockingjay", "mockingjay_p1", "shippp"})
        self.assertEqual(set(per_cfg.values()), {1638})
        lru = Counter(r[1] for r in self.master if r[0] == "lru")
        self.assertEqual(lru, {"google": 192, "datacenter": 71, "spec2006": 433, "spec2017": 243, "spec2026": 699})
        self.assertEqual(sum(g["sims"] for g in self.out["st"]["groups"].values()), len(self.master))

    def test_groups_partition_the_master(self):
        """Every sim of the merged descriptor is in exactly one group's descriptor, with the same params; every
        descriptor writes the one experiment."""
        seen = Counter()
        for g, d in self.groups.items():
            rows = expand_desc(d)
            gi = self.out["st"]["groups"][g]
            self.assertEqual(len(rows), gi["sims"], g)
            self.assertEqual({r[1] for r in rows}, set(gi["suites"]))
            self.assertEqual(d["traces_dir"], self.plan["groups"][g]["traces_dir"])
            self.assertEqual(d["experiment"], "fs_test")
            seen.update(rows)
        self.assertEqual(seen, Counter(self.master))
        self.assertEqual(self.out["master"]["experiment"], "fs_test")
        self.assertEqual(set(self.out["files"]), {"fs_test_google", "fs_test_rest"})

    def test_pinned_simpoints_match_cloudhost(self):
        """fullsweep's pinned list (the estimate's input) is exactly what cloudhost's plan_run.py gives
        `sweep start`, which also validates each descriptor with sci (validate_simulation)."""
        if not PLAN_RUN.exists():
            self.skipTest(f"no {PLAN_RUN}")
        for g, d in self.groups.items():
            with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
                json.dump(d, f)
                f.flush()
                r = subprocess.run([sys.executable, str(PLAN_RUN), str(F.INFRA), f.name], capture_output=True,
                                   text=True)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            p = json.loads(r.stdout.strip().splitlines()[-1])
            self.assertEqual(p["simpoints"], F.pinned_simpoints(d), g)
            self.assertEqual(p["sims"], self.out["st"]["groups"][g]["sims"])

    def test_cloudhost_split_covers_every_sim_once(self):
        st = self.out["st"]
        for g, d in self.groups.items():
            shards = F.group_shards(st, g, d)
            sims = Counter((c, lab) for s in shards for c in s["configs"] for lab in s["sims"])
            want = Counter((r[0], f"{r[1]}/{r[2]}/{r[3]}") for r in expand_desc(d))
            self.assertEqual(sims, want, g)
            size = self.plan["groups"][g]["shard_size"]
            self.assertTrue(all(len(s["configs"]) * len(s["sims"]) <= max(size, 71) for s in shards))
            if self.plan["groups"][g].get("slow"):       # else cloudhost marks its costliest shards long itself
                self.assertTrue(all(s["slow"] for s in shards), g)
            self.assertTrue(all(s["gb"] >= 2.0 for s in shards), g)     # cloudhost's floor for measured workloads
        # only these setups (setups added after the first sweeps)
        shards = F.group_shards(st, "google", self.groups["google"], ["lru", "clip"])
        self.assertEqual(sum(len(s["configs"]) * len(s["sims"]) for s in shards), 2 * 192)

    def test_run_lengths(self):
        for cfg, suite, sub, wl, cid, params in self.master:
            limit = F.flag_value(params, "inst_limit")
            self.assertEqual(limit, "250000000" if suite == "google" else None, (cfg, suite, wl))
        sims = {(s["suite"], s["workload"]): s["warmup"] for s in self.out["master"]["simulations"]}
        self.assertEqual({w for (suite, _), w in sims.items() if suite == "google"}, {50000000})
        self.assertEqual({w for (suite, _), w in sims.items() if suite != "google"}, {None})

    def test_policy_params(self):
        want = {"lru": "0", "mlp_core": "9", "mlp_gd": "9", "mlp_gi": "9", "mlp_sp": "9", "icarus": "21",
                "emissary": "24", "emissary_r1": "24", "lacs": "20", "clip": "25", "drrip": "18", "mockingjay": "22",
                "mockingjay_p1": "22", "shippp": "26"}
        for cfg, suite, sub, wl, cid, params in self.master:
            self.assertEqual(F.flag_value(params, "mlc_cache_repl_policy"), want[cfg], cfg)
            if cfg == "emissary_r1":
                self.assertEqual(F.flag_value(params, "emissary_rand_denom"), "1")
            if cfg.startswith("mockingjay"):
                self.assertEqual(F.flag_value(params, "mockingjay_bypass"), "0", cfg)
                self.assertEqual(F.flag_value(params, "mockingjay_pf_penalty"), "1" if cfg == "mockingjay_p1" else None)
            if not cfg.startswith("mlp"):
                self.assertEqual(gammas(params), (None, None), cfg)
        static = {"mlp_core": ("96", "48"), "mlp_gd": ("0", "48"), "mlp_gi": ("96", "0")}
        for cfg, suite, sub, wl, cid, params in self.master:
            if cfg in static:
                self.assertEqual(gammas(params), static[cfg], (cfg, wl))

    @staticmethod
    def report(level):
        with open(GSEARCH / "cd1" / "report" / f"best_{level}.csv") as f:
            return {r["name"]: json.loads(r["best"]) for r in csv.DictReader(f) if r["variant"] == "small"}

    def test_per_simpoint_gammas_match_the_report(self):
        """mlp_sp: a simpoint gsearch searched runs its own best pair (best_simpoint.csv, read independently);
        every other simpoint runs its input's best (best_input.csv). All 200 inputs were searched, so the
        workload and core fallbacks are never reached here."""
        sp, inp = self.report("simpoint"), self.report("input")
        own = fell = 0
        for cfg, suite, sub, wl, cid, params in self.master:
            if cfg != "mlp_sp":
                continue
            key = f"{suite}/{sub}/{wl}"
            gi, gd = sp[f"{key}:{cid}"] if f"{key}:{cid}" in sp else inp[key]
            self.assertEqual(gammas(params), (str(gi), str(gd)), (key, cid))
            own += f"{key}:{cid}" in sp
            fell += f"{key}:{cid}" not in sp
        self.assertEqual((own, fell), (612, 1026))
        self.assertEqual(self.out["st"]["policies"]["mlp_sp"]["levels_used"], {"simpoint": 612, "input": 1026})
        # by name: xalancbmk's searched simpoints (spec2006 small)
        xal = {r[4]: gammas(r[5]) for r in self.master if r[0] == "mlp_sp" and r[3] == "xalancbmk"}
        for cid, g in xal.items():
            name = f"spec2006/rate_int/xalancbmk:{cid}"
            want = sp.get(name, inp["spec2006/rate_int/xalancbmk"])
            self.assertEqual(g, (str(want[0]), str(want[1])))

    def test_per_workload_fallback_chain(self):
        """mlp_wl (off by default): every input gets its benchmark's best pair from best_workload.csv."""
        best = self.report("workload")
        out = F.build_sweep(F.DEFAULT_PLAN, "cd1/small", name="fs_test_wl", plan=self.plan, only=["mlp_wl"])
        rows = expand_desc(out["master"])
        labels = sorted({f"{r[1]}/{r[2]}/{r[3]}" for r in rows})
        of = {lab: w for w, labs in F.AGG.Hierarchy(labels).workloads.items() for lab in labs}
        self.assertEqual(set(of.values()), set(best))        # same 97 workloads as the search
        for cfg, suite, sub, wl, cid, params in rows:
            gi, gd = best[of[f"{suite}/{sub}/{wl}"]]
            self.assertEqual(gammas(params), (str(gi), str(gd)), wl)
        self.assertEqual(len(rows), 1638)

    def test_binary_is_scarab_path_head(self):
        head = F.resolve_binary(self.plan)
        self.assertEqual({c["binary"] for c in self.out["master"]["configurations"].values()}, {head})

    def test_toggles(self):
        out = F.build_sweep(F.DEFAULT_PLAN, "cd1/small", name="fs_test2", plan=self.plan, skip=["clip", "lacs"])
        self.assertNotIn("clip", out["master"]["configurations"])
        for g, gi in out["st"]["groups"].items():
            self.assertEqual(gi["configs"], list(out["master"]["configurations"]))
            self.assertEqual(list(out["files"][gi["desc"]]["configurations"]), gi["configs"])
        with self.assertRaises(SystemExit):     # no gsearch result and no plan architecture
            F.build_sweep(F.DEFAULT_PLAN, None, name="fs_test3", plan=self.plan, only=["lru", "icarus"])
        plan = dict(self.plan, architecture="golden_small")
        out = F.build_sweep(F.DEFAULT_PLAN, None, name="fs_test3", plan=plan, only=["lru", "icarus"])
        self.assertEqual(list(out["master"]["configurations"]), ["lru", "icarus"])
        with self.assertRaises(SystemExit):     # REPL_MLP setups without a gsearch result
            F.build_sweep(F.DEFAULT_PLAN, None, name="fs_test4", plan=plan, only=["mlp_core"])

    def test_suites(self):
        out = F.build_sweep(F.DEFAULT_PLAN, "cd1/small", plan=self.plan, suites=["google"])
        self.assertEqual(out["st"]["name"], "fs_cd1_small_google")
        rows = expand_desc(out["master"])
        self.assertEqual({r[1] for r in rows}, {"google"})
        self.assertEqual(len(rows), len(out["master"]["configurations"]) * 192)
        self.assertEqual(list(out["st"]["groups"]), ["google"])
        self.assertEqual(out["master"]["traces_dir"], self.plan["groups"]["google"]["traces_dir"])
        out = F.build_sweep(F.DEFAULT_PLAN, "cd1/small", plan=self.plan, skip_suites=["google", "spec2006"],
                            only=["lru"])
        self.assertEqual(list(out["st"]["groups"]), ["rest"])
        self.assertEqual(Counter(r[1] for r in expand_desc(out["master"])),
                         {"datacenter": 71, "spec2017": 243, "spec2026": 699})
        with self.assertRaises(SystemExit):
            F.build_sweep(F.DEFAULT_PLAN, "cd1/small", plan=self.plan, suites=["nosuch"])

    def test_launched_setups_are_frozen(self):
        """After a sweep ran a setup, gen keeps the record and refuses to change what that setup runs; adding a
        setup is fine (run starts a sweep of just the new one)."""
        name = "fs_test_launched"
        base = F.build_sweep(F.DEFAULT_PLAN, "cd1/small", name=name, plan=self.plan, only=["lru"])
        st = base["st"]
        rec = {"id": "fs_test_launched-x", "group": "google", "desc": f"{name}_google", "configs": ["lru"],
               "digests": {"lru": st["groups"]["google"]["digests"]["lru"]}, "started": F.now()}
        st["sweeps"] = [rec]
        F.write_json(base["st_path"], st)
        out = F.build_sweep(F.DEFAULT_PLAN, "cd1/small", name=name, plan=self.plan, only=["lru", "clip"])
        self.assertEqual(out["st"]["sweeps"], [rec])
        launches = dict(F.plan_launches(out["st"]))
        self.assertEqual(launches, {"google": ["clip"], "rest": ["lru", "clip"]})
        rec["digests"]["lru"] = "0" * 12          # as if lru had run with other params
        F.write_json(base["st_path"], st)
        with self.assertRaises(SystemExit):
            F.build_sweep(F.DEFAULT_PLAN, "cd1/small", name=name, plan=self.plan, only=["lru"])
        with self.assertRaises(SystemExit):       # --force can't rename suites under launched sweeps
            F.build_sweep(F.DEFAULT_PLAN, "cd1/small", name=name, plan=self.plan, only=["lru"], force=True,
                          suites=["google"])

    def test_launched_sweep_keeps_its_binary(self):
        """A launched sweep stays on the binary HEAD named at launch after scarab_path moves on, so a setup added
        later runs it too; an explicit scarab_<hash> in the plan still wins (and trips the frozen digests)."""
        name = "fs_test_binary"
        old_plan = dict(self.plan, binary="scarab_1234567")        # stands in for HEAD at launch
        base = F.build_sweep(F.DEFAULT_PLAN, "cd1/small", name=name, plan=old_plan, only=["lru"])
        st = base["st"]
        st["sweeps"] = [{"id": f"{name}-x", "group": "google", "desc": f"{name}_google", "configs": ["lru"],
                         "digests": {"lru": st["groups"]["google"]["digests"]["lru"]}, "started": F.now()}]
        F.write_json(base["st_path"], st)
        out = F.build_sweep(F.DEFAULT_PLAN, "cd1/small", name=name, plan=self.plan, only=["lru", "clip"])
        self.assertEqual(out["st"]["binary"], "scarab_1234567")
        self.assertEqual({c["binary"] for c in out["master"]["configurations"].values()}, {"scarab_1234567"})
        with self.assertRaises(SystemExit):
            F.build_sweep(F.DEFAULT_PLAN, "cd1/small", name=name, plan=dict(self.plan, binary="scarab_7654321"),
                          only=["lru"])

    def test_estimate_runs(self):
        for pool in ("h4d=5,n4d=10", "n4d=2,pakuchi=128"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                F.print_estimate(self.out["st"], self.groups, pool)
            self.assertIn("h end to end", buf.getvalue(), pool)
        buf = io.StringIO()
        with redirect_stdout(buf):
            F.print_estimate(self.out["st"], self.groups, "pakuchi=64")      # google is cloud-only
        self.assertIn("no machine it may run on", buf.getvalue())


class StaleFiles(unittest.TestCase):
    def test_only_gens_own_descriptors(self):
        """gen's cleanup removes <name>_<group>[_b<k>] it no longer makes, never cloudhost's copies of running
        shards (same fullsweep tag) or another sweep whose name starts with this one's."""
        with tempfile.TemporaryDirectory() as tmp:
            old, F.JSON_DIR = F.JSON_DIR, Path(tmp)
            try:
                tag = lambda n: {"fullsweep": {"sweep": n}}
                for stem, sweep in [("fs_x_google", "fs_x"), ("fs_x_rest_b3", "fs_x"), ("fs_x_rest_b2", "fs_x"),
                                    ("fs_x_rest_b2_0-b8d2_s002", "fs_x"), ("fs_x_incl_google", "fs_x_incl")]:
                    F.write_json(Path(tmp) / f"{stem}.json", tag(sweep))
                stale = F.stale_files("fs_x", {"fs_x_google", "fs_x_rest_b2"}, tmp, ["google", "rest"])
                self.assertEqual([p.stem for p in stale], ["fs_x_rest_b3"])
            finally:
                F.JSON_DIR = old


class Simulate(unittest.TestCase):
    def sweep(self, name, n_shards, sims, minutes, slow=False, gb=3.8, where="any"):
        return {"name": name, "slow": slow, "where": where,
                "shards": [{"local": False, "slow": slow, "gb": gb, "durs": [(minutes, "spec2017")] * sims}
                           for _ in range(n_shards)]}

    def test_one_machine(self):
        F.LAUNCH_MIN, F.START_OVERHEAD_MIN = 2.0, 3.0
        cost = F.Cost("golden_small")
        ms = F.simulate([self.sweep("a", 1, 192, 10.0)], {"h4d": 1}, cost)
        # 720 - 16 GB at 3.8 GB: 185 at once, so two waves; started 3 min after placement
        self.assertEqual(ms[0]["shards"], 1)
        self.assertAlmostEqual(ms[0]["last"], 3.0 + 20.0)

    def test_tail_release_lets_the_next_shard_in(self):
        """Two 96-sim shards on one N4D (96 slots): the second starts as the first's sims end."""
        F.LAUNCH_MIN, F.START_OVERHEAD_MIN = 2.0, 3.0
        cost = F.Cost("golden_small")
        ms = F.simulate([self.sweep("a", 2, 96, 10.0)], {"n4d": 1}, cost)
        per = 10.0 * cost.slowdown("n4d", "spec2017")
        self.assertAlmostEqual(ms[0]["last"], 3.0 + per + 3.0 + per)

    def test_where_and_memory(self):
        cost = F.Cost("golden_small")
        self.assertIsNone(F.simulate([self.sweep("g", 1, 64, 20.0, gb=5.5, where="cloud")], {"pakuchi": 128}, cost))
        ms = F.simulate([self.sweep("g", 1, 200, 20.0, gb=5.5, slow=True)], {"h4d": 1, "n4d": 1}, cost)
        # 200 sims want more than an H4D's 128 google slots: a partial slice on the roomiest (fastest) machine
        self.assertEqual([m["shards"] for m in ms], [1, 0])


STUB_CLOUDHOST = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
stub = Path(os.environ["STUB_DIR"])
(stub / "calls.log").open("a").write(" ".join(a) + "\n")
if a[:2] == ["sweep", "start"]:
    desc = a[2]
    d = json.loads((Path(os.environ["STUB_JSON"]) / f"{desc}.json").read_text())
    n = len(list((stub / "sweeps").glob("*"))) + 1
    sid = f"{d['experiment']}-stub{n}"
    root = stub / "sweeps" / sid
    (root / "shards").mkdir(parents=True)
    shards = []
    for k, c in enumerate(d["configurations"]):
        p = root / "shards" / f"{desc}_s{k}.json"
        p.write_text(json.dumps({"configurations": {c: d["configurations"][c]}}))
        shards.append({"name": p.stem, "path": str(p), "sims": 2, "state": "running", "host": "cloudhost-1",
                       "attempts": 0})
    st = {"id": sid, "state": "running", "sims": 2 * len(shards), "shards": shards,
          "driver_pid": int(os.environ["STUB_PID"]), "driver_host": os.uname().nodename}
    (root / "state.json").write_text(json.dumps(st))
    print(f"sweep {sid}: {st['sims']} sims of {d['experiment']} in {len(shards)} shards")
    print(f"sweep {sid} running in the background (pid 1); log x")
'''

STUB_SCI = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
(Path(os.environ["STUB_DIR"]) / "calls.log").open("a").write("sci " + " ".join(sys.argv[1:]) + "\n")
'''


class SweepFlow(unittest.TestCase):
    """run starts one cloudhost sweep per group (slow first), records it, starts only setups added later, resumes
    sweeps that stopped short, and collect waits for every setup."""

    def setUp(self):
        self.tmpd = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpd.name)
        self.stub, self.jdir, sdir = tmp / "stub", tmp / "json", tmp / "state"
        (self.stub / "sweeps").mkdir(parents=True)
        self.jdir.mkdir()
        for name, text in (("cloudhost", STUB_CLOUDHOST), ("sci", STUB_SCI)):
            (self.stub / name).write_text(text)
            (self.stub / name).chmod(0o755)
        os.environ.update(STUB_DIR=str(self.stub), STUB_JSON=str(self.jdir), STUB_PID=str(os.getpid()))
        F.ch()                   # the real cloudhost's code, loaded before CLOUDHOST points at the stub
        self.saved = (F.CLOUDHOST, F.SCI, F.JSON_DIR, F.SWEEPS_DIR, F.build_once, F.print_estimate)
        F.CLOUDHOST, F.SCI, F.JSON_DIR, F.SWEEPS_DIR = (str(self.stub / "cloudhost"), str(self.stub / "sci"),
                                                       self.jdir, str(self.stub / "sweeps"))
        F.build_once = lambda st, logdir: None
        F.print_estimate = lambda *a, **k: None
        groups = {"google": {"desc": "fs_stub_google", "configs": ["a", "b"], "suites": ["google"],
                             "simpoints": 2, "sims": 4, "digests": {"a": "1", "b": "2", "c": "3"}},
                  "rest": {"desc": "fs_stub_rest", "configs": ["a", "b"], "suites": ["spec2017"],
                           "simpoints": 2, "sims": 4, "digests": {"a": "4", "b": "5", "c": "6"}}}
        plan = {"root_dir": str(tmp / "root"), "state_dir": str(sdir),
                "groups": {"rest": {"shard_size": 96, "where": "any"},            # listed first: run sorts slow first
                           "google": {"shard_size": 64, "where": "cloud", "mem_per_sim_gb": 5.5, "slow": True}}}
        self.data = {"name": "fs_stub", "arch": "golden_small", "plan": plan, "groups": groups, "sweeps": [],
                     "policies": {"a": {}, "b": {}}}
        self.write_group_descs("ab")
        F.write_json(self.jdir / "fs_stub.json", {"experiment": "fs_stub", "configurations": {},
                                                   "fullsweep": {"sweep": "fs_stub", "state_dir": str(sdir)}})
        self.st = F.State("fs_stub", sdir)
        self.st.data = self.data
        self.st.save()

    def write_group_descs(self, configs):
        for g, gi in self.data["groups"].items():
            F.write_json(self.jdir / f"{gi['desc']}.json",
                         {"experiment": "fs_stub", "configurations": {c: {"params": c} for c in configs},
                          "fullsweep": {"sweep": "fs_stub", "group": g, "state_dir": self.data["plan"]["state_dir"]}})

    def tearDown(self):
        F.CLOUDHOST, F.SCI, F.JSON_DIR, F.SWEEPS_DIR, F.build_once, F.print_estimate = self.saved
        self.tmpd.cleanup()

    def args(self, **kw):
        return argparse.Namespace(**{"name": "fs_stub", "yes": False, "retry_failed": False, "pool": "h4d=1",
                                     "partial": False, **kw})

    def calls(self):
        f = self.stub / "calls.log"
        return f.read_text().splitlines() if f.exists() else []

    def sweep_files(self):
        return {p.parent.name: p for p in (self.stub / "sweeps").glob("*/state.json")}

    def set_sweeps(self, **changes):
        for p in self.sweep_files().values():
            s = json.loads(p.read_text())
            s.update(changes)
            for sh in s["shards"]:
                sh["state"] = "done" if changes.get("state") == "complete" else sh["state"]
            p.write_text(json.dumps(s))

    def test_flow(self):
        with redirect_stdout(io.StringIO()):
            F.cmd_run(self.args())
        self.assertEqual(self.calls(), [])                 # without --yes nothing starts
        with redirect_stdout(io.StringIO()):
            F.cmd_run(self.args(yes=True))
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].startswith("sweep start fs_stub_google "))
        self.assertIn("--mem-per-sim 5.5", calls[0])
        self.assertIn("--where cloud", calls[0])
        self.assertTrue(calls[0].endswith("--slow-suites google"))
        self.assertTrue(calls[1].startswith("sweep start fs_stub_rest "))
        self.assertTrue(calls[1].endswith("--slow-suites"))   # no slow suites
        self.assertNotIn("--mem-per-sim", calls[1])
        self.assertIn("--who fullsweep-fs_stub", calls[1])
        data = F.State("fs_stub", self.data["plan"]["state_dir"]).data
        self.assertEqual([(s["group"], s["desc"], s["configs"]) for s in data["sweeps"]],
                         [("google", "fs_stub_google", ["a", "b"]), ("rest", "fs_stub_rest", ["a", "b"])])

        with redirect_stdout(io.StringIO()):               # running drivers: nothing to do
            F.cmd_run(self.args(yes=True))
        self.assertEqual(len(self.calls()), 2)

        st = F.State("fs_stub", self.data["plan"]["state_dir"])
        for gi in st.data["groups"].values():              # gen added setup c
            gi["configs"].append("c")
        st.data["policies"]["c"] = {}
        st.save()
        self.write_group_descs("abc")
        with redirect_stdout(io.StringIO()):
            F.cmd_run(self.args(yes=True))
        new = self.calls()[2:]
        self.assertEqual([c.split()[2] for c in new], ["fs_stub_google_b2", "fs_stub_rest_b2"])
        self.assertEqual(list(F.load_json(self.jdir / "fs_stub_rest_b2.json")["configurations"]), ["c"])

        # a sweep whose driver died: run --yes resumes it
        sid, p = sorted(self.sweep_files().items())[0]
        s = json.loads(p.read_text())
        s.update(state="failed", driver_pid=None)
        p.write_text(json.dumps(s))
        with redirect_stdout(io.StringIO()):
            F.cmd_run(self.args(yes=True, retry_failed=True))
        self.assertEqual(self.calls()[-1], f"sweep resume {sid} --detach --retry-failed")

        # collect: refused until every setup is done, then one ./sci --collect-stats over the merged descriptor
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            F.cmd_collect(self.args())
        self.assertIn("not every setup", str(e.exception))
        buf = io.StringIO()
        with redirect_stdout(buf):
            F.cmd_status(self.args())
        self.assertIn("0/4", buf.getvalue())
        self.set_sweeps(state="complete", driver_pid=None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            F.cmd_status(self.args())
        self.assertIn("every sweep is complete", buf.getvalue())
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            F.cmd_collect(self.args())
        self.assertEqual(e.exception.code, 0)
        self.assertEqual(self.calls()[-1], "sci --collect-stats fs_stub")
        self.assertIsNone(F.load_json(self.jdir / "fs_stub.json")["collect_stats"]["configs"])

    def test_partial_collect(self):
        with redirect_stdout(io.StringIO()):
            F.cmd_run(self.args(yes=True))
        for p in self.sweep_files().values():              # setup a done everywhere, b still running
            s = json.loads(p.read_text())
            for sh in s["shards"]:
                if "a" in F.load_json(sh["path"])["configurations"]:
                    sh["state"] = "done"
            p.write_text(json.dumps(s))
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            F.cmd_collect(self.args())
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            F.cmd_collect(self.args(partial=True))
        self.assertEqual(e.exception.code, 0)
        self.assertEqual(F.load_json(self.jdir / "fs_stub.json")["collect_stats"]["configs"], ["a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
