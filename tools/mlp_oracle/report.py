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

--reuse reads a reference's MLP_RD1_* / MLP_RDN_* stats, which need a Scarab with the IPRF and
control kinds (scarab f17e3f5 on); references from before them (c8bb06d) are not supported.

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


REUSE_CLASSES = {"MARKED": "marked", "UNMARKED": "unmarked", "CTRL": "control"}
# Scarab's MLP_RD kinds behind each side's classes (memory.c, mlp_rd_fill): unmarked lines are those
# a demand waited on unmarked and, on the instruction side, those an instruction prefetch brought in
# with no demand waiting (IPRF); on the data side, stores, writebacks and data prefetches (OTHER).
REUSE_KINDS = {
    "I": {"MARKED": ["IMARK"], "UNMARKED": ["INOMARK", "IPRF"], "CTRL": ["ICTRL"]},
    "D": {"MARKED": ["DMARK"], "UNMARKED": ["DNOMARK", "OTHER"], "CTRL": ["DCTRL"]},
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
    the reuse and together cover every reuse; CTRL is the control's lines, counted in one of the two as
    well. How lines are classed comes from the reference's flags (make_descriptors.py sets
    --mlp_rd_sticky 1 --mlp_rd_ctrl 8). Each workload weighs the same: its counts are divided by its total
    reuses (marked + unmarked) of that side, then averaged over workloads."""

    def __init__(self, name):
        self.desc = load_descriptor(name)
        self.name = self.desc["_name"]
        self.arch = self.desc.get("architecture", "?")
        params = " ".join(c.get("params", "") for c in self.desc["configurations"].values())
        self.sticky = on(flag(params, "mlp_rd_sticky", "0"))
        self.ctrl = int(flag(params, "mlp_rd_ctrl", "0") or 0) > 0
        self.iprf_reuse = on(flag(params, "mlp_rd_iprf_reuse", "0"))
        kinds = sorted({k for side in REUSE_KINDS.values() for ks in side.values() for k in ks})
        stats = {}
        for k in kinds:
            for rd in ("RD1", "RDN"):
                stats[f"{rd}_{k}_HIT"] = f"MLP_{rd}_{k}_HIT_count"
                for b in range(REUSE_BUCKETS):
                    stats[f"{rd}_{k}_{b}"] = f"MLP_{rd}_{k}_LOG2_{b}_count"
        csv = Path(self.desc["root_dir"]) / "simulations" / self.desc["experiment"] / "collected_stats.csv"
        if not csv.is_file():
            sys.exit(f"{self.name}: no {csv}. Run ./sci --collect-stats {self.name}.")
        wanted = {s + "," for s in META + list(stats.values())}
        with open(csv) as f:
            lines = [next(f)] + [line for line in f if line[:line.find(",") + 1] in wanted]
        t = pd.read_csv(io.StringIO("".join(lines)), low_memory=False).set_index("stats")
        t = t[~t.index.duplicated(keep="first")]
        t = t[[c for c in t.columns if c not in ("write_protect", "groups")]].T
        missing = [v for v in stats.values() if v not in t]
        if missing:
            sys.exit(f"{self.name}: no {missing[0]} (…): run with a Scarab that has the IPRF and control kinds")
        raw = pd.DataFrame({k: pd.to_numeric(t[v], errors="coerce").fillna(0).values for k, v in stats.items()})
        cols = {}
        for s in "ID":  # sum RD1 and RDN, and the kinds behind each class
            for cls, ks in REUSE_KINDS[s].items():
                cols[f"{s}_{cls}_HIT"] = sum(raw[f"{rd}_{k}_HIT"] for rd in ("RD1", "RDN") for k in ks)
                for b in range(REUSE_BUCKETS):
                    cols[f"{s}_{cls}_{b}"] = sum(raw[f"{rd}_{k}_{b}"] for rd in ("RD1", "RDN") for k in ks)
                cols[f"{s}_{cls}_N"] = sum(cols[f"{s}_{cls}_{b}"] for b in range(REUSE_BUCKETS))
        cols["workload"] = t["Workload"].str.split("/").str[-1].values
        cols["weight"] = pd.to_numeric(t["Weight"], errors="coerce").fillna(1.0).values
        cols["failed"] = t["Collect Failed"].astype(str).str.strip().str.lower().eq("true").values
        df = pd.DataFrame(cols)
        df = df[~df["failed"]]
        self.n_sp = len(df)
        num = [c for c in df.columns if c not in ("workload", "weight", "failed")]
        self.per_wl = df[num].mul(df["weight"], axis=0).groupby(df["workload"]).sum()
        self.workloads = list(self.per_wl.index)
        self.classes = [c for c in REUSE_CLASSES if c != "CTRL" or self.ctrl]
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
        ctrl_rule = ("; control lines are counted in one of the two as well" if self.ctrl
                     else "; no control (--mlp_rd_ctrl 0)")
        out = [f"## Reuse distance, {self.name}", "",
               f"{self.arch} · {len(self.workloads)} workloads, {self.n_sp} simpoints · plain LRU (the reference run)", "",
               f"A reuse is {reuse_rule} at the MLC of a line filled before. Its distance is the fills into the "
               "line's set since its previous reuse (since its first fill, for the first); a 16-way LRU set has "
               f"evicted an untouched line after 16. Marked and unmarked are the line's class at the reuse ({cls_rule})"
               f"{ctrl_rule}. Unmarked instruction lines include those an instruction prefetch brought in with no "
               "demand waiting, and unmarked data lines those a store, writeback or data prefetch did. Columns give "
               "each class's share of its own reuses; the last two compare marked with unmarked lines in each bucket. "
               "Every workload weighs the same.", ""]
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


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("descriptors", nargs="*", help="descriptor names (json/<name>.json) or paths")
    p.add_argument("--reuse", nargs="+", default=[], metavar="REF",
                   help="reference descriptors (mlp_oracle_google_<tag>_ref) whose reuse-distance histograms to report")
    p.add_argument("--ceilings-from", help="take missing ceilings from this descriptor (same simpoints and lengths)")
    p.add_argument("--workloads", action="store_true", help="also print per-workload tables")
    p.add_argument("--include-cold", action="store_true", help="also report oracle/control configs that force cold misses")
    p.add_argument("--out", help="directory for per_workload.csv (every role's gain per workload) and ipc.csv")
    a = p.parse_args()
    if not a.descriptors and not a.reuse:
        p.error("give a study descriptor, or --reuse with a reference descriptor")

    studies = [Study(n, a.include_cold) for n in a.descriptors]
    reuses = [Reuse(n) for n in a.reuse]
    if a.ceilings_from:
        src = next((st for st in studies if st.name == Path(a.ceilings_from).stem), None) or Study(a.ceilings_from, False)
        for st in studies:
            borrow_ceilings(st, src)
    out = []
    for st in studies:
        out += summary(st) + [""]
    if a.workloads:
        out += per_workload(studies)
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
