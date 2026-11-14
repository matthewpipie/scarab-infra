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
    python tools/mlp_oracle/report.py --reuse mlp_oracle_google_dmin8_ref
    python tools/mlp_oracle/report.py mlp_oracle_google_dmin8 --miss-reuse

--reuse reads a reference's MLP_RD1_* / MLP_RDN_* stats: reuse distances of marked and unmarked
lines. --miss-reuse reads a study's record check (replay_check), whose MLP_RDMISS_* are the reuse
distances of exactly the misses the oracle and the control force. --reuse needs scarab f17e3f5 or
later (the IPRF kind), --miss-reuse b30d6c1 or later (MLP_RDMISS_*); runs from before the merge with
main (c8bb06d) are not supported.

Method, as ./sci --visualize: a workload's IPC is its simpoints' weighted measured instructions
over their weighted measured cycles (Periodic_Instructions / Periodic_Cycles), over the simpoints
that finished under every config reported for that descriptor (others are dropped and counted).
Per-workload gains match --visualize's. Its Avg row does not match the geomeans here: it divides
the mean of a config's workload IPCs by the mean of the baseline's, so it weighs workloads by IPC,
and before scarab-infra#10 also by the share of their simpoint weight that ran.
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
    for _f in ("REF_MISSES", "REF_MARKED", "REF_COLD", "REF_COLD_MARKED", "REF_MARKED_LINES", "REF_CTRL",
               "REF_CTRL_LINES", "REF_CTRL_MARKED", "FORCED", "FORCED_REF_HIT"):
        STATS[f"{_s}_{_f}_ALL"] = f"MLP_ORACLE_{_s}_{_f}_total_count"
    for _f in ("REF_MISSES", "REF_MARKED", "REF_COLD", "REF_COLD_MARKED", "REF_MARKED_LINES", "REF_CTRL",
               "REF_CTRL_LINES", "REF_CTRL_MARKED"):
        STATS[f"{_s}_{_f}"] = f"MLP_ORACLE_{_s}_{_f}_total_count"
    for _f in ("FORCED", "FORCED_REF_HIT"):
        STATS[f"{_s}_{_f}"] = f"MLP_ORACLE_{_s}_{_f}_count"
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


def read_stats(desc, stats):
    """A descriptor's collected stats, one row per (config, simpoint), with META and the given stats as
    columns (those the CSV has)."""
    csv = Path(desc["root_dir"]) / "simulations" / desc["experiment"] / "collected_stats.csv"
    if not csv.is_file():
        sys.exit(f"{desc['_name']}: no {csv}. Run ./sci --collect-stats {desc['_name']}.")
    wanted = {s + "," for s in META + list(stats)}
    with open(csv) as f:  # parse only the header and the rows used here
        lines = [next(f)] + [line for line in f if line[:line.find(",") + 1] in wanted]
    df = pd.read_csv(io.StringIO("".join(lines)), low_memory=False).set_index("stats")
    df = df[~df.index.duplicated(keep="first")]
    df = df[[c for c in df.columns if c not in ("write_protect", "groups")]]
    return df.T


def load_long(desc):
    """One row per (config, simpoint) of a descriptor's collected stats."""
    t = read_stats(desc, STATS.values())
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


    def match(self, role, side):
        """How closely control config `role` matched its oracle (the one with the same install mode), per
        simpoint: the draw (the reference's eligible marked misses and their lines against the control's,
        over the whole reference) and the misses the replays forced over the measured region (leaving out
        the ones mlp_oracle_install 0 forces because they hit in the reference). Returns
        {name: (control total, oracle total, per-simpoint ratios)}."""
        o = self.roles.get(role.replace("control", "oracle").replace("+misses", ""))
        c = self.roles.get(role)
        if o is None or c is None:
            return {}
        sides = "ID" if side == "b" else side.upper()
        key = ["workload", "cluster"]
        g = {cfg: self.rows[self.rows["config"] == cfg].set_index(key) for cfg in (o, c)}

        def tot(cfg, k):
            return sum(g[cfg][f"{s}_{k}"] for s in sides)

        pairs = {
            "misses drawn": (tot(c, "REF_CTRL"), tot(c, "REF_MARKED")),
            "lines drawn": (tot(c, "REF_CTRL_LINES"), tot(c, "REF_MARKED_LINES")),
            "forced, whole run": (tot(c, "FORCED_ALL") - tot(c, "FORCED_REF_HIT_ALL"),
                                  tot(o, "FORCED_ALL") - tot(o, "FORCED_REF_HIT_ALL")),
            "misses forced": (tot(c, "FORCED") - tot(c, "FORCED_REF_HIT"), tot(o, "FORCED") - tot(o, "FORCED_REF_HIT")),
        }
        out = {}
        for name, (cv, ov) in pairs.items():
            cv, ov = cv.align(ov, join="inner")
            ok = ov > 0
            out[name] = (float(cv.sum()), float(ov.sum()), (cv[ok] / ov[ok]))
        drawn = tot(c, "REF_CTRL")
        out["_marked_share"] = float(tot(c, "REF_CTRL_MARKED").sum() / drawn.sum()) if drawn.sum() else math.nan
        return out


def match_table(st):
    out = [f"### Control against oracle, {st.name}", "",
           "Control / oracle, summed over simpoints, and the range of the per-simpoint ratios. Drawn: over the "
           "whole reference, the control's draw against the marked misses (with mlp_oracle_reuse_only, non-cold "
           "ones) and the lines they fall on. Forced: misses the replay forced, over the whole run and over its "
           "measured region, against the oracle with the same install mode. The draw is matched over the whole "
           "reference, warmup included, so the measured region can hold more or fewer of the control's misses "
           "than of the oracle's. No stat counts the distinct lines a replay forced.", "",
           "| Config | Side | Misses drawn | Lines drawn | Forced, whole run | Forced, measured | Lowest measured | Draws that were marked |",
           "|---|---|---|---|---|---|---|---|"]
    for s in "idb":
        for role in sorted(r for r in st.roles if r.startswith(f"control_{s}") and "+seed" not in r):
            m = st.match(role, s)
            if not m:
                continue
            cells = []
            for name in ("misses drawn", "lines drawn", "forced, whole run", "misses forced"):
                cv, ov, r = m[name]
                cells.append(f"{cv / ov:.4f} ({r.min():.3f}–{r.max():.3f})" if ov else "n/a")
            r = m["misses forced"][2]
            cells.append(f"{r.idxmin()[0]}/{r.idxmin()[1]}" if len(r) else "–")
            out.append(f"| {st.roles[role]} | {SIDE_NAME[s]} | " + " | ".join(cells) + f" | {m['_marked_share']:.1%} |")
        m = st.match(f"control_{s}", s)
        if m:
            out.append(f"| *oracle_{s} totals* | | {m['misses drawn'][1]:,.0f} | {m['lines drawn'][1]:,.0f} | "
                       f"{m['forced, whole run'][1]:,.0f} | {m['misses forced'][1]:,.0f} | | |")
    return out


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


REUSE_CLASSES = {"MARKED": "marked", "UNMARKED": "unmarked"}
# Scarab's MLP_RD kinds behind each side's classes (memory.c, mlp_rd_fill): unmarked lines are those
# a demand waited on unmarked and, on the instruction side, those an instruction prefetch brought in
# with no demand waiting (IPRF); on the data side, stores, writebacks and data prefetches (OTHER).
REUSE_KINDS = {
    "I": {"MARKED": ["IMARK"], "UNMARKED": ["INOMARK", "IPRF"]},
    "D": {"MARKED": ["DMARK"], "UNMARKED": ["DNOMARK", "OTHER"]},
}
REUSE_BUCKETS = 16  # MLP_RD*_LOG2_k: k 0 is distance 0, k covers [2^(k-1), 2^k - 1], and 15 is 2^14 and up
LRU_WAYS = 16


def bucket_label(b):
    if b == 0:
        return "0"
    lo, hi = 2 ** (b - 1), 2 ** b - 1
    if b == REUSE_BUCKETS - 1:
        return f"≥{lo:,}"
    return f"{lo:,}" if lo == hi else f"{lo:,}–{hi:,}"


class Reuse:
    """A reference run's reuse distances (Scarab's MLP_RD1_* and MLP_RDN_*, kept under plain LRU): per side
    and class, the distance of each reuse of a line, in fills into its MLC set since its previous reuse (or
    since its first fill, for the first), as a log2 histogram. MARKED and UNMARKED are the line's class at
    the reuse and together cover every reuse. How lines are classed and what counts as a reuse come from
    the reference's flags (make_descriptors.py sets --mlp_rd_sticky 1 --mlp_rd_iprf_reuse 1). Each workload
    weighs the same: its counts are divided by its total reuses (marked + unmarked) of that side, then
    averaged over workloads. There is no control here: lines can't be matched to the marked ones by
    reuses without bias, so the control's distances are per miss, in MissReuse."""

    def __init__(self, name):
        self.desc = load_descriptor(name)
        self.name = self.desc["_name"]
        self.arch = self.desc.get("architecture", "?")
        params = " ".join(c.get("params", "") for c in self.desc["configurations"].values())
        self.sticky = on(flag(params, "mlp_rd_sticky", "0"))
        self.iprf_reuse = on(flag(params, "mlp_rd_iprf_reuse", "0"))
        kinds = sorted({k for side in REUSE_KINDS.values() for ks in side.values() for k in ks})
        stats = {}
        for k in kinds:
            for rd in ("RD1", "RDN"):
                stats[f"{rd}_{k}_HIT"] = f"MLP_{rd}_{k}_HIT_count"
                for b in range(REUSE_BUCKETS):
                    stats[f"{rd}_{k}_{b}"] = f"MLP_{rd}_{k}_LOG2_{b}_count"
        t = read_stats(self.desc, stats.values())
        missing = [v for v in stats.values() if v not in t]
        if missing:
            sys.exit(f"{self.name}: no {missing[0]} (…): run with a Scarab that has the IPRF kind (f17e3f5 on)")
        raw = pd.DataFrame({k: pd.to_numeric(t[v], errors="coerce").fillna(0).values for k, v in stats.items()})
        cols = {}
        for s in "ID":  # sum RD1 and RDN, and the kinds behind each class
            for cls, ks in REUSE_KINDS[s].items():
                cols[f"{s}_{cls}_HIT"] = sum(raw[f"{rd}_{k}_HIT"] for rd in ("RD1", "RDN") for k in ks)
                for b in range(REUSE_BUCKETS):
                    cols[f"{s}_{cls}_{b}"] = sum(raw[f"{rd}_{k}_{b}"] for rd in ("RD1", "RDN") for k in ks)
                cols[f"{s}_{cls}_N"] = sum(cols[f"{s}_{cls}_{b}"] for b in range(REUSE_BUCKETS))
        cols["cluster"] = t["Cluster Id"].astype(str).values
        cols["workload"] = t["Workload"].str.split("/").str[-1].values
        cols["weight"] = pd.to_numeric(t["Weight"], errors="coerce").fillna(1.0).values
        cols["failed"] = t["Collect Failed"].astype(str).str.strip().str.lower().eq("true").values
        df = pd.DataFrame(cols)
        df = df[~df["failed"]]
        self.n_sp = len(df)
        num = [c for c in df.columns if c not in ("workload", "cluster", "weight", "failed")]
        self.per_wl = df[num].mul(df["weight"], axis=0).groupby(df["workload"]).sum()
        self.workloads = list(self.per_wl.index)
        self.classes = list(REUSE_CLASSES)
        self.frac = {}  # side -> DataFrame, index = class, columns = bucket, average workload's share of all reuses
        for s in "ID":
            total = self.per_wl[f"{s}_MARKED_N"] + self.per_wl[f"{s}_UNMARKED_N"]
            ok = total > 0
            rows = {}
            for cls in self.classes:
                cols = [f"{s}_{cls}_{b}" for b in range(REUSE_BUCKETS)]
                share = self.per_wl.loc[ok, cols].div(total[ok], axis=0).mean()
                share.index = range(REUSE_BUCKETS)
                rows[cls] = share
            self.frac[s] = pd.DataFrame(rows).T

    def hit_rate(self, s, cls, w=None):
        g = self.per_wl if w is None else self.per_wl.loc[[w]]
        n = g[f"{s}_{cls}_N"]
        return ((g[f"{s}_{cls}_HIT"] / n)[n > 0]).mean() if w is None else float((g[f"{s}_{cls}_HIT"] / n).iloc[0])

    def side_summary(self, s):
        """The histogram of one side as plain numbers: buckets (the last ones folded into one), each
        class's share of its own reuses per bucket, marked's share of all reuses per bucket, and totals."""
        fr = self.frac[s]
        dist = fr.div(fr.sum(axis=1), axis=0)  # each class's own distribution
        last = max(b for b in range(REUSE_BUCKETS) if (dist[b] >= 0.0005).any())
        buckets = []
        for b in range(last + 1):
            tail = b == last and last < REUSE_BUCKETS - 1
            cols = list(range(b, REUSE_BUCKETS)) if tail else [b]
            m, u = fr.loc["MARKED", cols].sum(), fr.loc["UNMARKED", cols].sum()
            buckets.append({
                "label": f"≥{2 ** (b - 1):,}" if tail and b else bucket_label(b),
                "from": 0 if b == 0 else 2 ** (b - 1),
                **{REUSE_CLASSES[c]: float(dist.loc[c, cols].sum()) for c in self.classes},
                "marked_share": float(m / (m + u)) if m + u > 0 else None,
            })
        return {
            "buckets": buckets,
            "within15": {REUSE_CLASSES[c]: float(dist.loc[c, :4].sum()) for c in self.classes},
            "hit_rate": {REUSE_CLASSES[c]: float(self.hit_rate(s, c)) for c in self.classes},
            "median": {REUSE_CLASSES[c]: self.median(dist.loc[c]) for c in self.classes},
            "marked_share": float(fr.loc["MARKED"].sum() / (fr.loc["MARKED"].sum() + fr.loc["UNMARKED"].sum())),
        }

    def table(self, s):
        sm = self.side_summary(s)
        side = SIDE_NAME[s.lower()]
        names = [REUSE_CLASSES[c] for c in self.classes]
        head = ["Fills since previous reuse"] + names + ["marked / unmarked", "marked share of reuses"]
        out = [f"#### {side.capitalize()}", "", "| " + " | ".join(head) + " |", "|---" * len(head) + "|"]
        for bk in sm["buckets"]:
            ratio = f"{bk['marked'] / bk['unmarked']:.2f}" if bk["unmarked"] > 0 else "–"
            share = f"{bk['marked_share']:.1%}" if bk["marked_share"] is not None else "–"
            out.append(f"| {bk['label']} | " + " | ".join(f"{bk[c]:.1%}" for c in names) + f" | {ratio} | {share} |")
            if bk["from"] == 8:  # 8–15: the last bucket a 16-way LRU set can still hit in
                out.append("| *within 15 fills* | " + " | ".join(f"*{sm['within15'][c]:.1%}*" for c in names) + " | | |")
        out.append("| *LRU hit rate* | " + " | ".join(f"*{sm['hit_rate'][c]:.1%}*" for c in names) + " | | |")
        out.append("| *median* | " + " | ".join(sm["median"][c] for c in names) + " | | |")
        out += ["", f"Marked lines take {sm['marked_share']:.1%} of all {side} reuses."]
        return out

    @staticmethod
    def median(dist):
        c = dist.cumsum()
        b = int((c >= 0.5).idxmax())
        return bucket_label(b)

    def summary(self):
        cls_rule = ("marked once any of its fills was marked" if self.sticky
                    else "classed by its first fill for the whole run")
        reuse_rule = ("a demand or instruction-prefetch lookup" if self.iprf_reuse else "a demand lookup")
        out = [f"## Reuse distance, {self.name}", "",
               f"{self.arch} · {len(self.workloads)} workloads, {self.n_sp} simpoints · plain LRU (the reference run)", "",
               f"A reuse is {reuse_rule} at the MLC of a line filled before. Its distance is the fills into the "
               "line's set since its previous reuse (since its first fill, for the first); a 16-way LRU set has "
               f"evicted an untouched line after 16. Marked and unmarked are the line's class at the reuse ({cls_rule})."
               " Unmarked instruction lines include those an instruction prefetch brought in with no "
               "demand waiting, and unmarked data lines those a store, writeback or data prefetch did. Columns give "
               "each class's share of its own reuses; the last two compare marked with unmarked lines in each bucket. "
               "Every workload weighs the same. For the control, see --miss-reuse.", ""]
        for s in "ID":
            out += self.table(s) + [""]
        return out

    def per_workload(self):
        out = ["### Reuse per workload: share within 15 fills, and LRU hit rate", "",
               "| Workload | I marked | I unmarked | D marked | D unmarked |", "|---|---|---|---|---|"]
        for w in self.workloads:
            cells = []
            for s in "ID":
                for cls in ("MARKED", "UNMARKED"):
                    row = self.per_wl.loc[w]
                    n = sum(row[f"{s}_{cls}_{b}"] for b in range(REUSE_BUCKETS))
                    near = sum(row[f"{s}_{cls}_{b}"] for b in range(5))
                    cells.append(f"{near / n:.0%} / {self.hit_rate(s, cls, w):.0%}" if n else "–")
            out.append(f"| {w} | " + " | ".join(cells) + " |")
        return out

    def rows(self):
        return [{"study": self.name, "workload": w, "side": s, "class": REUSE_CLASSES[cls], "bucket": b,
                 "fills_from": 0 if b == 0 else 2 ** (b - 1), "count": self.per_wl.loc[w, f"{s}_{cls}_{b}"]}
                for w in self.workloads for s in "ID" for cls in self.classes for b in range(REUSE_BUCKETS)]


MISS_CLASSES = {"ALL": "all non-cold", "MARK": "marked", "CTRL": "control"}
# What the record check must share with the control runs to draw the same control (mlp_oracle.cc, draw_ctrl).
DRAW_FLAGS = {"mlp_oracle_reuse_only": "0", "mlp_oracle_seed": "0", "mlp_oracle_ctrl_lines": "1",
              "mlp_oracle_ctrl_choices": "8"}


class MissReuse:
    """The reuse distances of the misses a study's bounds force (Scarab's MLP_RDMISS_*), from its record
    check (replay_check: mode 1 replaying the reference, forcing nothing, so it sees the reference's own
    misses). It draws mode 3's control as the control runs do when it has their draw flags
    (--mlp_oracle_reuse_only above all), so per side: every non-cold reference miss (all), the marked ones
    the oracle forces (marked), and the control's draw (control), which matches the marked ones in misses
    and lines on every simpoint. A miss's distance is the fills into its line's MLC set since the line's
    last reuse (or its first fill), as the reference's MLP_RD* count them. Whole run, warmup included, as
    the draw and the forcing are; each workload weighs the same."""

    def __init__(self, st):
        self.st, self.name = st, st.name
        self.cfg = st.roles.get("check")
        if self.cfg is None:
            sys.exit(f"{st.name}: --miss-reuse needs a record check (mlp_oracle 1 with --mlp_oracle_ref)")
        cfgs = st.desc["configurations"]
        params = cfgs[self.cfg]["params"]
        self.iprf_reuse = on(flag(params, "mlp_rd_iprf_reuse", "0"))
        self.ctrl_cfg = next((c for r, c in st.roles.items() if r.startswith("control_") and "+" not in r), None)
        self.draw_diff = []
        if self.ctrl_cfg:
            cp = cfgs[self.ctrl_cfg]["params"]
            self.draw_diff = [n for n, d in DRAW_FLAGS.items() if flag(params, n, d) != flag(cp, n, d)]
        stats = {}
        for s in "DI":
            for cls in MISS_CLASSES:
                for b in range(REUSE_BUCKETS):
                    for when, suffix in (("all", "total_count"), ("meas", "count")):
                        stats[f"{when}_{s}_{cls}_{b}"] = f"MLP_RDMISS_{s}{cls}_LOG2_{b}_{suffix}"
        stats["no_line"] = "MLP_RDMISS_NO_LINE_total_count"
        t = read_stats(st.desc, stats.values())
        t = t[t["Configuration"] == self.cfg]
        missing = [v for v in stats.values() if v not in t]
        if missing:
            sys.exit(f"{st.name}: no {missing[0]} (…): the record check needs scarab b30d6c1 or later")
        df = pd.DataFrame({k: pd.to_numeric(t[v], errors="coerce").fillna(0).values for k, v in stats.items()})
        df["workload"] = t["Workload"].str.split("/").str[-1].values
        df["cluster"] = t["Cluster Id"].astype(str).values
        df = df.set_index(["workload", "cluster"])
        keep = set(map(tuple, st.rows[["workload", "cluster"]].values))  # simpoints every config finished
        df = df.loc[[k for k in df.index if k in keep]]
        self.n_sp = len(df)
        for when in ("all", "meas"):
            for s in "DI":
                for cls in MISS_CLASSES:
                    df[f"{when}_{s}_{cls}_N"] = sum(df[f"{when}_{s}_{cls}_{b}"] for b in range(REUSE_BUCKETS))
        self.sp = df
        self.per_wl = df.groupby(level="workload").sum()
        self.workloads = list(self.per_wl.index)

    def dist(self, s, cls, w=None):
        """The class's distance histogram, as shares of its misses: of one workload, or averaged over workloads."""
        cols = [f"all_{s}_{cls}_{b}" for b in range(REUSE_BUCKETS)]
        g = self.per_wl if w is None else self.per_wl.loc[[w]]
        n = g[f"all_{s}_{cls}_N"]
        share = g.loc[n > 0, cols].div(n[n > 0], axis=0).mean()
        share.index = range(REUSE_BUCKETS)
        return share

    def checks(self):
        """The record check against the reference's draw: misses filed against the misses drawn, per simpoint."""
        rows = self.st.rows[self.st.rows["config"] == self.cfg].set_index(["workload", "cluster"])
        out = []
        for s in "ID":
            for cls, ref in (("MARK", "REF_MARKED"), ("CTRL", "REF_CTRL")):
                got, want = self.sp[f"all_{s}_{cls}_N"].align(rows[f"{s}_{ref}"], join="inner")
                out.append((s, cls, int((got == want).sum()), len(got), float(got.sum()), float(want.sum())))
        return out

    def same_draw(self):
        """Simpoints where the record check drew the same control as the control run, by the draw's counts on
        both sides (misses, lines, marked ones), and how many simpoints were compared."""
        if not self.ctrl_cfg:
            return None
        key = ["workload", "cluster"]
        a = self.st.rows[self.st.rows["config"] == self.cfg].set_index(key)
        b = self.st.rows[self.st.rows["config"] == self.ctrl_cfg].set_index(key)
        cols = [f"{s}_{k}" for s in "ID" for k in ("REF_CTRL", "REF_CTRL_LINES", "REF_CTRL_MARKED", "REF_MARKED")]
        a, b = a[cols].align(b[cols], join="inner")
        return int((a == b).all(axis=1).sum()), len(a)

    def table(self, s):
        side = SIDE_NAME[s.lower()]
        d = {cls: self.dist(s, cls) for cls in MISS_CLASSES}
        last = max(b for b in range(REUSE_BUCKETS) if any(d[c][b] >= 0.0005 for c in d))
        first = min(b for b in range(REUSE_BUCKETS) if any(d[c][b] >= 0.0005 for c in d))
        names = [MISS_CLASSES[c] for c in MISS_CLASSES]
        head = ["Fills since the line's last reuse"] + names + ["marked − control"]
        out = [f"#### {side.capitalize()}", "", "| " + " | ".join(head) + " |", "|---" * len(head) + "|"]
        for b in range(first, last + 1):
            lo = b == first and first > 0
            tail = b == last and last < REUSE_BUCKETS - 1
            cols = list(range(0, b + 1)) if lo else list(range(b, REUSE_BUCKETS)) if tail else [b]
            label = (f"≤{2 ** b - 1:,}" if lo else f"≥{2 ** (b - 1):,}" if tail and b else bucket_label(b))
            v = {c: float(d[c][cols].sum()) for c in d}
            out.append(f"| {label} | " + " | ".join(f"{v[c]:.1%}" for c in MISS_CLASSES)
                       + f" | {100 * (v['MARK'] - v['CTRL']):+.1f} pts |")
        out.append("| *median* | " + " | ".join(Reuse.median(d[c]) for c in MISS_CLASSES) + " | |")
        tot = {c: self.sp[f"all_{s}_{c}_N"].sum() for c in MISS_CLASSES}
        meas = {c: self.sp[f"meas_{s}_{c}_N"].sum() for c in MISS_CLASSES}
        out.append("| *misses, whole run* | " + " | ".join(f"{tot[c]:,.0f}" for c in MISS_CLASSES) + " | |")
        out.append("| *misses, measured region* | " + " | ".join(f"{meas[c]:,.0f}" for c in MISS_CLASSES) + " | |")
        rows = self.st.rows[self.st.rows["config"] == self.cfg]
        drawn = rows[f"{s}_REF_CTRL"].sum()
        if drawn:
            out += ["", f"{rows[f'{s}_REF_CTRL_MARKED'].sum() / drawn:.1%} of the control's misses are marked ones."]
        return out

    def summary(self):
        st = self.st
        reuse_rule = "a demand or instruction-prefetch lookup" if self.iprf_reuse else "a demand lookup"
        out = [f"## Reuse distance of the forced misses, {self.name}", "",
               f"{st.arch} · {len(self.workloads)} workloads, {self.n_sp} simpoints · {st.length} · "
               f"marking: `{st.marking}` · from {self.cfg}", "",
               "The misses the oracle forces (marked) and the misses the control forces (control), with every "
               "non-cold miss for scale, by the fills into the line's MLC set since its last reuse "
               f"(a reuse is {reuse_rule}). Every one of them is an LRU miss, so most are past a 16-way set's reach; "
               "the marked − control column is how the oracle's misses differ from a random sample of as many, "
               "on as many lines. Shares of each class's own misses, whole run (warmup included, as the forcing), "
               "every workload weighing the same.", ""]
        if self.draw_diff:
            out += [f"WARNING: {self.cfg} and {self.ctrl_cfg} differ in {', '.join(self.draw_diff)}, so its control "
                    "is not the one the control runs force.", ""]
        for s in "ID":
            out += self.table(s) + [""]
        cells = [f"{SIDE_NAME[s.lower()]} {'marked' if c == 'MARK' else 'control'} {ok}/{n}"
                 + ("" if got == want else f" ({got:,.0f} filed vs {want:,.0f} drawn)")
                 for s, c, ok, n, got, want in self.checks()]
        no_line = int(self.sp["no_line"].sum())
        same = self.same_draw()
        out.append("Checks: misses filed equal the reference's draw on " + ", ".join(cells) + " simpoints"
                   + (f"; the draw equals {self.ctrl_cfg}'s on {same[0]}/{same[1]} simpoints" if same else "")
                   + (f"; {no_line:,} lined-up misses on lines never filled" if no_line else "") + ".")
        return out

    def per_workload(self):
        out = ["", f"### Forced misses per workload, {self.name}: share under 32 fills, and median", "",
               "| Workload | I marked | I control | D marked | D control |", "|---|---|---|---|---|"]
        for w in self.workloads:
            cells = []
            for s in "ID":
                for cls in ("MARK", "CTRL"):
                    if not self.per_wl.loc[w, f"all_{s}_{cls}_N"]:
                        cells.append("–")
                        continue
                    d = self.dist(s, cls, w)
                    cells.append(f"{d.iloc[:6].sum():.0%} / {Reuse.median(d)}")
            out.append(f"| {w} | " + " | ".join(cells) + " |")
        return out

    def rows(self):
        return [{"study": self.name, "workload": w, "side": s, "class": MISS_CLASSES[cls], "bucket": b,
                 "fills_from": 0 if b == 0 else 2 ** (b - 1), "count": self.per_wl.loc[w, f"all_{s}_{cls}_{b}"]}
                for w in self.workloads for s in "ID" for cls in MISS_CLASSES for b in range(REUSE_BUCKETS)]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("descriptors", nargs="*", help="descriptor names (json/<name>.json) or paths")
    p.add_argument("--reuse", nargs="+", default=[], metavar="REF",
                   help="reference descriptors (mlp_oracle_google_<tag>_ref) whose reuse-distance histograms to report")
    p.add_argument("--ceilings-from", help="take missing ceilings from this descriptor (same simpoints and lengths)")
    p.add_argument("--workloads", action="store_true", help="also print per-workload tables")
    p.add_argument("--match", action="store_true",
                   help="also print how closely each control matched the oracle in misses and lines")
    p.add_argument("--miss-reuse", action="store_true",
                   help="also print the reuse distances of the misses the oracle and the control force, from each "
                        "study's record check (replay_check)")
    p.add_argument("--include-cold", action="store_true", help="also report oracle/control configs that force cold misses")
    p.add_argument("--out", help="directory for per_workload.csv (every role's gain per workload) and ipc.csv")
    a = p.parse_args()
    if not a.descriptors and not a.reuse:
        p.error("give a study descriptor, or --reuse with a reference descriptor")

    studies = [Study(n, a.include_cold) for n in a.descriptors]
    reuses = [Reuse(n) for n in a.reuse]
    miss_reuses = [MissReuse(st) for st in studies] if a.miss_reuse else []
    if a.ceilings_from:
        src = next((st for st in studies if st.name == Path(a.ceilings_from).stem), None) or Study(a.ceilings_from, False)
        for st in studies:
            borrow_ceilings(st, src)
    out = []
    for st in studies:
        out += summary(st) + [""]
    if a.workloads:
        out += per_workload(studies)
    if a.match:
        for st in studies:
            out += [""] + match_table(st)
    for r in miss_reuses:
        out += [""] + r.summary() + (r.per_workload() if a.workloads else [])
    for r in reuses:
        out += [""] + r.summary() + (r.per_workload() if a.workloads else [])
    print("\n".join(out))
    if a.out:
        Path(a.out).mkdir(parents=True, exist_ok=True)
        if reuses:
            pd.DataFrame([row for r in reuses for row in r.rows()]).to_csv(Path(a.out) / "reuse.csv", index=False)
            summ = {r.name: {"architecture": r.arch, "workloads": len(r.workloads), "simpoints": r.n_sp,
                             **{SIDE_NAME[s.lower()]: r.side_summary(s) for s in "ID"}} for r in reuses}
            (Path(a.out) / "reuse_summary.json").write_text(json.dumps(summ, indent=1, ensure_ascii=False) + "\n")
            print(f"\nWrote reuse.csv (per workload and bucket) and reuse_summary.json in {a.out}")
        if miss_reuses:
            pd.DataFrame([row for r in miss_reuses for row in r.rows()]).to_csv(Path(a.out) / "miss_reuse.csv",
                                                                               index=False)
            print(f"\nWrote miss_reuse.csv (per workload and bucket, whole run) in {a.out}")
        if not studies:
            return
        rows = [{"study": st.name, "role": r, "config": st.roles[r], "workload": w, "gain_pct": pct(v)}
                for st in studies for r, ser in st.gain.items() for w, v in ser.items()]
        pd.DataFrame(rows).to_csv(Path(a.out) / "per_workload.csv", index=False)
        ipc = [{"study": st.name, "config": c, "workload": w, "ipc": v}
               for st in studies for c, ser in st.ipc.items() for w, v in ser.items()]
        pd.DataFrame(ipc).to_csv(Path(a.out) / "ipc.csv", index=False)
        print(f"\nWrote per_workload.csv and ipc.csv in {a.out}")


if __name__ == "__main__":
    main()
