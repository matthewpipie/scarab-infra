#!/usr/bin/env python3
"""Bounds from mlp_oracle studies: per side, the share of misses the marking marks, and the gain
over LRU of the ceiling, the oracle (marked misses forced) and the control (as many random misses
forced), plus REPL_MLP and any variants.

Reads each descriptor's collected_stats.csv (./sci --collect-stats <desc>, which run_study.sh runs).
Configs are told apart by their flags, not their names, so any descriptor with mlp_oracle configs
works, including hand-written ones. Oracle and control configs without --mlp_oracle_reuse_only
also force cold misses, which no replacement policy can save; they are skipped unless
--include-cold.

    python tools/mlp_oracle/report.py mlp_oracle_google_t3
    python tools/mlp_oracle/report.py mlp_oracle_google_t3 mlp_oracle_google_dmin8 --ceilings-from mlp_oracle_google_t3
    python tools/mlp_oracle/report.py mlp_oracle_google_dmin8 --workloads --out oracle_report/

Method, as ./sci --visualize: a workload's IPC is its simpoints' weighted measured instructions
over their weighted measured cycles (Periodic_Instructions / Periodic_Cycles), over the simpoints
that finished under every config reported for that descriptor (others are dropped and counted).
Per-workload gains match --visualize's. Its Avg row does not match the geomeans here: it averages
workload IPCs that sci scales by the summed weight of the simpoints that ran (a workload with half
its simpoints counts half), so it weighs workloads by IPC and by how many of their simpoints ran.
"""

import argparse
import io
import json
import math
import re
import sys
from pathlib import Path

import pandas as pd

INFRA = Path(__file__).resolve().parents[2]
META = ["Configuration", "Workload", "Cluster Id", "Weight", "Collect Failed"]
STATS = {"inst": "Periodic_Instructions", "cycles": "Periodic_Cycles"}
for _s in "ID":
    for _f in ("REF_MISSES", "REF_MARKED", "REF_COLD", "REF_COLD_MARKED"):
        STATS[f"{_s}_{_f}"] = f"MLP_ORACLE_{_s}_{_f}_total_count"
    STATS[f"{_s}_FORCED"] = f"MLP_ORACLE_{_s}_FORCED_count"
SIDES = {1: "i", 2: "d", 3: "b"}
SIDE_NAME = {"i": "instruction", "d": "data", "b": "both"}
# Flags that set up the study rather than the marking.
STUDY_FLAGS = re.compile(r"--(mlp_oracle\w*|mlc_cache_repl_policy|inst_limit|mlp_boost_in_fills|mlp_lin_instr_lambda|"
                         r"mlp_lin_data_lambda)[ =]\S+")


def flag(params, name, default=None):
    found = re.findall(rf"--{name}[ =](\S+)", params)
    return found[-1] if found else default  # the last one wins, as in Scarab


def on(value):
    return str(value).lower() in ("1", "true")


def classify(params):
    """The role a config plays, e.g. 'oracle_d', 'control_b+noinst', 'ceiling_i', 'mlp64_16', or None."""
    policy = flag(params, "mlc_cache_repl_policy")
    mode = int(flag(params, "mlp_oracle", "0"))
    if policy == "9" and mode == 0:
        return f"mlp{flag(params, 'mlp_lin_instr_lambda', '0')}_{flag(params, 'mlp_lin_data_lambda', '0')}"
    if policy != "0":
        return None
    if mode == 0:
        return "lru"
    if mode == 1:
        return "record" if on(flag(params, "mlp_oracle_record", "0")) else (
            "check" if flag(params, "mlp_oracle_ref") else None)
    side = SIDES.get(int(flag(params, "mlp_oracle_side", "1")))
    if mode == 4:
        return f"ceiling_{side}"
    role = f"{'oracle' if mode == 2 else 'control'}_{side}"
    if not on(flag(params, "mlp_oracle_install", "1")):
        role += "+noinst"
    if mode == 3 and not on(flag(params, "mlp_oracle_ctrl_lines", "1")):
        role += "+misses"
    seed = int(flag(params, "mlp_oracle_seed", "0"))
    if seed:
        role += f"+seed{seed}"
    if not on(flag(params, "mlp_oracle_reuse_only", "0")):
        role = "cold:" + role
    return role


def load_descriptor(name):
    path = Path(name) if name.endswith(".json") else INFRA / "json" / f"{name}.json"
    d = json.loads(path.read_text())
    d["_name"] = path.stem
    return d


def load_long(desc):
    """One row per (config, simpoint) of a descriptor's collected stats."""
    csv = Path(desc["root_dir"]) / "simulations" / desc["experiment"] / "collected_stats.csv"
    if not csv.is_file():
        sys.exit(f"{desc['_name']}: no {csv}. Run ./sci --collect-stats {desc['_name']}.")
    wanted = {s + "," for s in META + list(STATS.values())}
    with open(csv) as f:  # parse only the header and the rows used here
        lines = [next(f)] + [line for line in f if line[:line.find(",") + 1] in wanted]
    df = pd.read_csv(io.StringIO("".join(lines)), low_memory=False).set_index("stats")
    df = df[~df.index.duplicated(keep="first")]
    df = df[[c for c in df.columns if c not in ("write_protect", "groups")]]
    t = df.T
    long = pd.DataFrame({
        "config": t["Configuration"].values,
        "workload": t["Workload"].str.split("/").str[-1].values,
        "cluster": t["Cluster Id"].astype(str).values,
        "weight": pd.to_numeric(t["Weight"], errors="coerce").fillna(1.0).values,
        "failed": t["Collect Failed"].astype(str).str.strip().str.lower().eq("true").values,
    })
    for key, stat in STATS.items():
        long[key] = pd.to_numeric(t[stat], errors="coerce").values if stat in t else math.nan
    long["ipc"] = long["inst"] / long["cycles"]
    long["ok"] = ~long["failed"] & long["ipc"].gt(0) & long["ipc"].apply(math.isfinite)
    return long


def geomean(values):
    values = [v for v in values if v > 0 and math.isfinite(v)]
    return math.exp(sum(map(math.log, values)) / len(values)) if values else math.nan


def pct(x):
    return (x - 1.0) * 100.0


def sgn(x, digits=2):
    return "n/a" if not math.isfinite(x) else f"{x:+.{digits}f}"


class Study:
    """One descriptor's results: per-workload IPC gains over lru for every config with a role."""

    def __init__(self, name, include_cold):
        self.desc = load_descriptor(name)
        self.name = self.desc["_name"]
        cfgs = self.desc["configurations"]
        self.roles = {}
        self.skipped = []
        for c, cfg in cfgs.items():
            role = classify(cfg.get("params", ""))
            if role is None or role == "record":
                continue
            if role.startswith("cold:") and not include_cold:
                self.skipped.append(c)
                continue
            self.roles.setdefault(role, c)  # the first config with a role stands for it
        if "lru" not in self.roles:
            sys.exit(f"{self.name}: no LRU config (--mlc_cache_repl_policy 0 and no --mlp_oracle) to compare against")
        self.long = load_long(self.desc)
        used = set(self.roles.values())
        rows = self.long[self.long["config"].isin(used)]
        ok = rows[rows["ok"]].groupby(["workload", "cluster"])["config"].nunique()
        common = set(ok[ok == len(used)].index)
        every = set(map(tuple, rows[["workload", "cluster"]].drop_duplicates().values))
        self.dropped = sorted(every - common)
        keep = pd.Series([(w, c) in common for w, c in zip(rows["workload"], rows["cluster"])], index=rows.index)
        self.rows = rows.loc[keep].copy()
        self.n_sp = len(common)
        self.workloads = sorted(self.rows["workload"].unique())
        self.ipc = {}
        for c, g in self.rows.groupby("config"):
            by = g["workload"]
            self.ipc[c] = (g["inst"] * g["weight"]).groupby(by).sum() / (g["cycles"] * g["weight"]).groupby(by).sum()
        base = self.ipc[self.roles["lru"]]
        self.gain = {role: (self.ipc[c] / base) for role, c in self.roles.items() if c in self.ipc}
        self.marking = self._marking()
        sims = self.desc["simulations"]
        limit = flag(cfgs[self.roles["lru"]].get("params", ""), "inst_limit")
        warm = sims[0].get("warmup") or 0
        self.length = (f"{warm / 1e6:g}M warmup + {(int(limit) - warm) / 1e6:g}M measured"
                       if limit else f"{warm / 1e6:g}M warmup")
        self.arch = self.desc.get("architecture", "?")

    def _marking(self):
        for role, c in self.roles.items():
            if role.startswith("oracle_"):
                rest = STUDY_FLAGS.sub("", self.desc["configurations"][c]["params"]).split()
                return " ".join(rest) or "REPL_MLP defaults"
        return "n/a"

    def geo(self, role):
        return pct(geomean(self.gain[role].values)) if role in self.gain else math.nan

    def cell(self, role):
        return sgn(self.geo(role)) if role in self.gain else "–"

    def ref_shares(self, side):
        """(marked share of reuse misses, cold share of marked misses) from the reference's counts."""
        cfg = next((c for r, c in self.roles.items() if r.startswith(("oracle_", "control_"))), None)
        if cfg is None:
            return math.nan, math.nan
        g = self.rows[self.rows["config"] == cfg]
        sides = "ID" if side == "b" else side.upper()
        tot = {k: sum(g[f"{s}_{k}"].sum() for s in sides) for k in ("REF_MISSES", "REF_MARKED", "REF_COLD_MARKED")}
        marked = tot["REF_MARKED"] / tot["REF_MISSES"] if tot["REF_MISSES"] else math.nan
        all_marked = tot["REF_MARKED"] + tot["REF_COLD_MARKED"]
        cold = tot["REF_COLD_MARKED"] / all_marked if all_marked else math.nan
        return marked, cold

    def forced(self, role, side):
        if role not in self.roles:
            return math.nan
        g = self.rows[self.rows["config"] == self.roles[role]]
        return sum(g[f"{s}_FORCED"].sum() for s in ("ID" if side == "b" else side.upper()))


def borrow_ceilings(study, source):
    """Ceilings do not depend on the marking: take them from another study of the same simpoints."""
    if source.name == study.name:
        return
    same = set(map(tuple, study.rows[["workload", "cluster"]].drop_duplicates().values)) == \
        set(map(tuple, source.rows[["workload", "cluster"]].drop_duplicates().values))
    for s in "idb":
        role = f"ceiling_{s}"
        if role in source.gain and role not in study.gain:
            study.gain[role] = source.gain[role]
            study.roles[role] = f"{source.name}/{source.roles[role]}"
    study.borrowed = (source.name, same)


def summary(st):
    out = [f"## {st.name}", ""]
    out.append(f"{st.arch} · {len(st.workloads)} workloads, {st.n_sp} simpoints · {st.length} · marking: `{st.marking}`")
    if st.dropped:
        out.append(f"Dropped {len(st.dropped)} simpoints that did not finish under every config: "
                   + ", ".join(f"{w}/{c}" for w, c in st.dropped[:8]) + (" …" if len(st.dropped) > 8 else ""))
    if getattr(st, "borrowed", None):
        src, same = st.borrowed
        out.append(f"Ceilings from {src}" + ("" if same else " (WARNING: different simpoints)"))
    out += ["", "Gain over lru in %, geomean over workloads.", "",
            "| Side | Marked | Marked misses that are cold | Ceiling | Oracle | Control | Oracle − control | Forced, control / oracle |",
            "|---|---|---|---|---|---|---|---|"]
    for s in "idb":
        o, c = f"oracle_{s}", f"control_{s}"
        if o not in st.gain and c not in st.gain:
            continue
        marked, cold = st.ref_shares(s)
        diff = st.geo(o) - st.geo(c)
        fo, fc = st.forced(o, s), st.forced(c, s)
        ratio = f"{fc / fo:.3f}" if fo and math.isfinite(fo) and math.isfinite(fc) else "n/a"
        out.append(f"| {SIDE_NAME[s]} | {marked:.1%} | {cold:.1%} | {st.cell(f'ceiling_{s}')} | {st.cell(o)} | "
                   f"{st.cell(c)} | {sgn(diff)} | {ratio} |")
    out += ["", "Marked: the reference's marked misses as a share of its non-cold misses. A threshold that marks "
                "more gives the oracle more to force; the control forces as many, so oracle − control is what "
                "the marking's choice of misses is worth."]
    mlp = sorted(r for r in st.gain if r.startswith("mlp"))
    if mlp:
        out += ["", "REPL_MLP with this marking: " + "; ".join(
            f"γi {r[3:].split('_')[0]}, γd {r.split('_')[1]} ({st.roles[r]}) {st.cell(r)}" for r in mlp)]
    variants = sorted(r for r in st.gain if "+" in r)
    if variants:
        out += ["", "Variants:"]
        for r in variants:
            base = r.split("+")[0]
            out.append(f"- {st.roles[r]}: {st.cell(r)}, {sgn(st.geo(r) - st.geo(base))} vs {st.roles.get(base, base)}")
    checks = []
    if "check" in st.roles:
        g = st.rows[st.rows["config"].isin([st.roles["check"], st.roles["lru"]])]
        cyc = g.pivot_table(index=["workload", "cluster"], columns="config", values="cycles")
        same = int((cyc[st.roles["check"]] == cyc[st.roles["lru"]]).sum())
        checks.append(f"{st.roles['check']} equals lru in cycles on {same}/{len(cyc)} simpoints"
                      + ("" if same == len(cyc) else " (MISMATCH: the reference does not match these runs)"))
    for s in "idb":
        o, cl = f"oracle_{s}", f"ceiling_{s}"
        if o in st.gain and cl in st.gain:
            over = [w for w in st.workloads if st.gain[o].get(w, 0) > st.gain[cl].get(w, math.inf)]
            if over:
                checks.append(f"oracle_{s} beats its ceiling on {', '.join(over)} (expected only within noise)")
    if checks:
        out += ["", "Checks: " + "; ".join(checks) + "."]
    if st.skipped:
        out += ["", f"Skipped (force cold misses): {', '.join(st.skipped)}."]
    return out


def per_workload(studies):
    out = []
    for s in "idb":
        cols = [(st, r) for st in studies for r in (f"ceiling_{s}", f"oracle_{s}", f"control_{s}") if r in st.gain]
        if not any(r.startswith("oracle") for _, r in cols):
            continue
        multi = len(studies) > 1
        head = [f"{st.name if multi else ''} {r.split('_')[0]}".strip() for st, r in cols]
        out += ["", f"### Per workload, {SIDE_NAME[s]}", "", "| Workload | " + " | ".join(head) + " |",
                "|---" * (len(cols) + 1) + "|"]
        workloads = sorted(set().union(*(st.workloads for st in studies)))
        for w in workloads:
            vals = [sgn(pct(st.gain[r].get(w, math.nan))) for st, r in cols]
            out.append(f"| {w} | " + " | ".join(vals) + " |")
        out.append("| geomean | " + " | ".join(sgn(st.geo(r)) for st, r in cols) + " |")
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("descriptors", nargs="+", help="descriptor names (json/<name>.json) or paths")
    p.add_argument("--ceilings-from", help="take missing ceilings from this descriptor (same simpoints and lengths)")
    p.add_argument("--workloads", action="store_true", help="also print per-workload tables")
    p.add_argument("--include-cold", action="store_true", help="also report oracle/control configs that force cold misses")
    p.add_argument("--out", help="directory for per_workload.csv (every role's gain per workload) and ipc.csv")
    a = p.parse_args()

    studies = [Study(n, a.include_cold) for n in a.descriptors]
    if a.ceilings_from:
        src = next((st for st in studies if st.name == Path(a.ceilings_from).stem), None) or Study(a.ceilings_from, False)
        for st in studies:
            borrow_ceilings(st, src)
    out = []
    for st in studies:
        out += summary(st) + [""]
    if a.workloads:
        out += per_workload(studies)
    print("\n".join(out))
    if a.out:
        Path(a.out).mkdir(parents=True, exist_ok=True)
        rows = [{"study": st.name, "role": r, "config": st.roles[r], "workload": w, "gain_pct": pct(v)}
                for st in studies for r, ser in st.gain.items() for w, v in ser.items()]
        pd.DataFrame(rows).to_csv(Path(a.out) / "per_workload.csv", index=False)
        ipc = [{"study": st.name, "config": c, "workload": w, "ipc": v}
               for st in studies for c, ser in st.ipc.items() for w, v in ser.items()]
        pd.DataFrame(ipc).to_csv(Path(a.out) / "ipc.csv", index=False)
        print(f"\nWrote per_workload.csv and ipc.csv in {a.out}")


if __name__ == "__main__":
    main()
