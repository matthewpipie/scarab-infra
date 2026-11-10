#!/usr/bin/env python3
"""Best REPL_MLP gammas from the mlp_gamma sweep: per simpoint, per suite, and across suites.

Reads each descriptor's collected_stats.csv, which ./sci --collect-stats <desc> writes
(tools/mlp_gamma/run_sweep.sh runs it after each --sim). Config names and gammas come from the
descriptor's params, so any descriptor with gi<I>_gd<D>-style configs works.

    python tools/mlp_gamma/report.py --all                     # every mlp_gamma_* sweep descriptor
    python tools/mlp_gamma/report.py --check --marks           # the smoke test
    python tools/mlp_gamma/report.py mlp_gamma_google_small    # named descriptors

Method (as sci --visualize): a simpoint's IPC is NODE_INST_COUNT / NODE_CYCLE over the measured
region; a workload's IPC is the weight-averaged IPC of its simpoints, weights renormalized over the
simpoints that completed under every config of the descriptor (others are dropped and counted);
a suite's gain is the geomean over its workloads of IPC / baseline IPC (baseline gi0_gd0, plain
LRU). Cross-suite scores weight every suite equally: the geomean of the suite geomeans.

Reported per architecture and suite (--group subsuite splits spec int/fp):
  - the full gamma grid of geomean gains, best cell in bold;
  - the best static config, its wins/losses and worst/best workload;
  - oracles: best config per workload, and per simpoint;
  - the cross-suite best config, and what it costs each suite against that suite's own best.
Files in --out: summary.md, per_simpoint.csv (best config and IPC of every simpoint, with the
suite-best and cross-suite-best configs' IPC), per_workload.csv, grid.csv, ipc_long.csv.
"""

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

import pandas as pd

INFRA = Path(__file__).resolve().parents[2]
META = ["Experiment", "Architecture", "Configuration", "Workload", "Cluster Id", "Weight", "Collect Failed"]
STATS = {
    "inst": "NODE_INST_COUNT_count",
    "cycles": "NODE_CYCLE_count",
    "total_inst": "NODE_INST_COUNT_total_count",
    "marked_fetch": "MLP_MARKED_FETCH_count",
    "marked_load": "MLP_MARKED_LOAD_count",
}
WIN = 0.005  # a workload wins/loses if it moves more than 0.5%


def flag_value(params, flag):
    m = re.search(rf"--{flag}[ =](\S+)", params)
    return m.group(1) if m else None


def classify(params):
    """('grid', gi, gd) for REPL_MLP configs, ('lru', None, None) for plain LRU, else None."""
    policy = flag_value(params, "mlc_cache_repl_policy")
    gi = flag_value(params, "mlp_lin_instr_lambda")
    gd = flag_value(params, "mlp_lin_data_lambda")
    if policy == "9":
        return "grid", float(gi or 0), float(gd or 0)
    if policy in (None, "0") and gi is None and gd is None:
        return "lru", None, None
    return None


def resolve(path_text):
    return Path(os.path.expandvars(path_text.replace("$USER", os.environ.get("USER", ""))))


def load_descriptor(name):
    path = Path(name) if name.endswith(".json") else INFRA / "json" / f"{name}.json"
    with open(path) as f:
        desc = json.load(f)
    desc["_name"] = path.stem
    return desc


def load_long(desc):
    """One row per (config, simpoint) of a descriptor's collected stats."""
    csv = resolve(desc["root_dir"]) / "simulations" / desc["experiment"] / "collected_stats.csv"
    if not csv.is_file():
        sys.exit(f"{desc['_name']}: no collected stats ({csv}). Run ./sci --collect-stats {desc['_name']}.")
    df = pd.read_csv(csv, low_memory=False).set_index("stats")
    df = df[~df.index.duplicated(keep="first")]
    df = df[[c for c in df.columns if c not in ("write_protect", "groups")]]
    missing = [s for s in META + [STATS["inst"], STATS["cycles"]] if s not in df.index]
    if missing:
        sys.exit(f"{desc['_name']}: {csv} has no {missing} rows")

    kinds = {name: classify(cfg.get("params", "")) for name, cfg in desc["configurations"].items()}
    t = df.loc[META + [s for s in STATS.values() if s in df.index]].T  # one row per simpoint column
    t = t[t["Configuration"].map(lambda c: kinds.get(c) is not None)]
    parts = t["Workload"].str.split("/", n=2, expand=True)
    long = pd.DataFrame({
        "desc": desc["_name"],
        "arch": t["Architecture"].values,
        "config": t["Configuration"].values,
        "kind": [kinds[c][0] for c in t["Configuration"]],
        "gi": [kinds[c][1] for c in t["Configuration"]],
        "gd": [kinds[c][2] for c in t["Configuration"]],
        "suite": parts[0].values,
        "subsuite": (parts[0] + "/" + parts[1]).values,
        "workload": t["Workload"].values,
        "cluster": t["Cluster Id"].astype(str).values,
        "weight": pd.to_numeric(t["Weight"], errors="coerce").values,
        "failed": t["Collect Failed"].astype(str).str.strip().str.lower().eq("true").values,
    })
    for key, stat in STATS.items():
        long[key] = pd.to_numeric(t[stat], errors="coerce").values if stat in t else math.nan
    long["ipc"] = long["inst"] / long["cycles"]
    long["ok"] = ~long["failed"] & long["ipc"].gt(0) & long["ipc"].apply(math.isfinite)
    return long


def geomean(values):
    values = list(values)
    return math.exp(sum(math.log(v) for v in values) / len(values)) if values else math.nan


def pct(x):
    return (x - 1.0) * 100.0


def fmt_gamma(g):
    return str(int(g)) if float(g).is_integer() else f"{g:g}"


def config_order(configs, gammas):
    """Sort key: higher score first, then smaller gammas (simpler config) on ties."""
    return lambda c: (gammas[c][0] + gammas[c][1], gammas[c])


def best_of(scores, gammas):
    top = max(scores.values())
    return min((c for c, s in scores.items() if s == top), key=config_order(scores, gammas))


def coverage_lookup():
    """cluster_id -> raw simpoint weight, from workloads_db.json, for the one-line run description."""
    try:
        db = json.load(open(INFRA / "workloads" / "workloads_db.json"))
    except OSError:
        return {}
    out = {}
    for suite, subs in db.items():
        for sub, wls in subs.items():
            for wl, info in wls.items():
                if isinstance(info, dict):
                    out[f"{suite}/{sub}/{wl}"] = {str(sp["cluster_id"]): sp["weight"] for sp in info.get("simpoints", [])}
    return out


class Report:
    def __init__(self, long, group, baseline, show_simpoints, marks):
        self.long, self.group, self.baseline = long, group, baseline
        self.show_simpoints, self.marks = show_simpoints, marks
        self.lines = []
        self.per_simpoint, self.per_workload, self.grid_rows = [], [], []
        self.coverage = coverage_lookup()

    def out(self, text=""):
        self.lines.append(text)

    def table(self, header, rows):
        self.out("| " + " | ".join(header) + " |")
        self.out("|" + "|".join("---" for _ in header) + "|")
        for r in rows:
            self.out("| " + " | ".join(str(x) for x in r) + " |")
        self.out()

    def complete(self, part):
        """Grid rows of simpoints that completed under every grid config of their descriptor."""
        grid = part[part["kind"] == "grid"]
        expected = grid.groupby("desc")["config"].nunique()
        good = grid[grid["ok"]].groupby(["desc", "workload", "cluster"])["config"].nunique()
        keep = good[good.values == expected.reindex(good.index.get_level_values("desc")).values].index
        all_sp = grid.groupby(["desc", "workload", "cluster"]).size().index
        dropped = [k for k in all_sp if k not in set(keep)]
        mask = grid.set_index(["desc", "workload", "cluster"]).index.isin(keep)
        return grid[mask], dropped

    def lru_check(self, part):
        both = part[part["ok"] & part["config"].isin(["lru", self.baseline])]
        piv = both.pivot_table(index=["workload", "cluster"], columns="config", values="cycles", aggfunc="first")
        if "lru" not in piv or self.baseline not in piv:
            return
        piv = piv.dropna()
        same = int((piv["lru"] == piv[self.baseline]).sum())
        ipc = both.pivot_table(index=["workload", "cluster"], columns="config", values="ipc", aggfunc="first").dropna()
        worst = (ipc[self.baseline] / ipc["lru"] - 1).abs().max() * 100 if len(ipc) else math.nan
        self.out(f"- Baseline check: {self.baseline} has the same cycle count as plain LRU on {same}/{len(piv)} "
                 f"simpoints (largest IPC difference {worst:.4f}%).")

    def describe(self, arch, name, grid):
        base = grid[grid["config"] == self.baseline]
        n_wl, n_sp = base["workload"].nunique(), len(base)
        measured = base["inst"].median() / 1e6
        warm = (base["total_inst"] - base["inst"]).median() / 1e6
        cov = []
        for wl, sps in base.groupby("workload")["cluster"]:
            raw = self.coverage.get(wl, {})
            if raw:
                cov.append(sum(raw.get(c, 0.0) for c in sps) / sum(raw.values()))
        cov_text = f", a median {100 * sorted(cov)[len(cov) // 2]:.0f}% of each workload's weight" if cov else ""
        per_wl = base.groupby("workload").size()
        return (f"{name}: {n_wl} workloads, {n_sp} simpoints (up to {per_wl.max()} per workload{cov_text}), "
                f"{warm:.0f}M warmup + {measured:.0f}M measured per simpoint, {arch}.")

    def analyze_group(self, arch, name, part):
        grid, dropped = self.complete(part)
        if grid.empty:
            self.out(f"### {arch} / {name}\n\nNo simpoint completed under every config.\n")
            return None
        gammas = grid.drop_duplicates("config").set_index("config")[["gi", "gd"]].apply(tuple, axis=1).to_dict()
        if self.baseline not in gammas:
            sys.exit(f"{arch}/{name}: no baseline config {self.baseline}")
        ipc = grid.pivot_table(index=["workload", "cluster"], columns="config", values="ipc", aggfunc="first")
        w = grid[grid["config"] == self.baseline].set_index(["workload", "cluster"])["weight"]
        w = w / w.groupby(level="workload").transform("sum")
        wl_ipc = ipc.mul(w, axis=0).groupby(level="workload").sum()
        speedup = wl_ipc.div(wl_ipc[self.baseline], axis=0)
        scores = {c: geomean(speedup[c]) for c in speedup.columns}
        best = best_of(scores, gammas)

        sp_best = ipc.apply(lambda r: best_of(r.to_dict(), gammas), axis=1)
        sp_oracle_ipc = ipc.max(axis=1).mul(w).groupby(level="workload").sum()
        sp_oracle = geomean(sp_oracle_ipc / wl_ipc[self.baseline])
        wl_oracle = geomean(speedup.max(axis=1))

        self.out(f"### {arch} / {name}\n")
        self.out(self.describe(arch, name, grid) + "\n")
        if dropped:
            self.out(f"- Dropped {len(dropped)} simpoints that failed or are missing under some config: "
                     + ", ".join(f"{wl.split('/')[-1]}:{c}" for _, wl, c in dropped[:12])
                     + (" ..." if len(dropped) > 12 else ""))
        self.lru_check(part)
        self.out()

        gis = sorted({g[0] for g in gammas.values()})
        gds = sorted({g[1] for g in gammas.values()})
        by_gamma = {g: c for c, g in gammas.items()}
        rows = []
        for gi in gis:
            row = [f"**γi {fmt_gamma(gi)}**"]
            for gd in gds:
                c = by_gamma.get((gi, gd))
                cell = "" if c is None else f"{pct(scores[c]):+.2f}"
                row.append(f"**{cell}**" if c == best else cell)
                if c is not None:
                    self.grid_rows.append({"arch": arch, "group": name, "config": c, "gamma_instr": gi,
                                           "gamma_data": gd, "geomean_gain_pct": pct(scores[c])})
            rows.append(row)
        self.out(f"Geomean IPC gain over {self.baseline} (%), rows instruction γ, columns data γ:\n")
        self.table(["", *[f"γd {fmt_gamma(gd)}" for gd in gds]], rows)

        s = speedup[best]
        wins, losses = int((s > 1 + WIN).sum()), int((s < 1 - WIN).sum())
        lo, hi = s.idxmin(), s.idxmax()
        self.out(f"- **Best static: {best}**, {pct(scores[best]):+.2f}% geomean; {wins} of {len(s)} workloads gain "
                 f">0.5%, {losses} lose >0.5%; worst {lo.split('/')[-1]} {pct(s[lo]):+.2f}%, "
                 f"best {hi.split('/')[-1]} {pct(s[hi]):+.2f}%.")
        self.out(f"- Oracles: best config per workload {pct(wl_oracle):+.2f}%, per simpoint {pct(sp_oracle):+.2f}%.")
        counts = sp_best.value_counts()
        self.out("- Per-simpoint best configs: " + ", ".join(f"{c} {n}" for c, n in counts.head(8).items())
                 + (f", {len(counts) - 8} others" if len(counts) > 8 else "") + f" (of {len(sp_best)}).")
        if self.marks:
            m = grid.groupby("config")[["marked_fetch", "marked_load"]].mean()
            self.out("\nMean lines marked per simpoint (measured region):\n")
            self.table(["config", "fetch", "load"],
                       [[c, f"{m.at[c, 'marked_fetch']:.0f}", f"{m.at[c, 'marked_load']:.0f}"]
                        for c in sorted(m.index, key=lambda c: gammas[c])])
        self.out()
        return {"arch": arch, "name": name, "scores": scores, "best": best, "gammas": gammas,
                "speedup": speedup, "wl_ipc": wl_ipc, "ipc": ipc, "w": w, "sp_best": sp_best,
                "wl_oracle": wl_oracle, "sp_oracle": sp_oracle}

    def cross(self, arch, results):
        common = set.intersection(*(set(r["scores"]) for r in results))
        gammas = results[0]["gammas"]
        score = {c: geomean(r["scores"][c] for r in results) for c in common}
        pooled_sp = pd.concat([r["speedup"][sorted(common)] for r in results])
        pooled = {c: geomean(pooled_sp[c]) for c in common}
        best = best_of(score, gammas)
        best_pooled = best_of(pooled, gammas)
        self.out(f"### {arch} / across suites\n")
        self.out(f"Suites weighted equally (geomean of suite geomeans): {', '.join(r['name'] for r in results)}.\n")
        if best_pooled == best:
            pooled_text = f"pooling all {len(pooled_sp)} workloads picks it too ({pct(pooled[best]):+.2f}%)"
        else:
            pooled_text = (f"pooling all {len(pooled_sp)} workloads instead picks {best_pooled} "
                           f"({pct(pooled[best_pooled]):+.2f}%, against {pct(pooled[best]):+.2f}% for {best})")
        self.out(f"- **Cross-suite best: {best}**, {pct(score[best]):+.2f}% with suites weighted equally; "
                 f"{pooled_text}.\n")
        rows = []
        for r in results:
            rb = r["best"]
            rows.append([r["name"], len(r["speedup"]), f"{rb} {pct(r['scores'][rb]):+.2f}",
                         f"{pct(r['scores'][best]):+.2f}", f"{pct(r['scores'][rb]) - pct(r['scores'][best]):.2f}",
                         f"{pct(r['wl_oracle']):+.2f}", f"{pct(r['sp_oracle']):+.2f}"])
        self.table(["suite", "workloads", "suite best (%)", f"{best} (%)", "lost vs suite best (pts)",
                    "per-workload oracle (%)", "per-simpoint oracle (%)"], rows)
        gis = sorted({gammas[c][0] for c in common})
        gds = sorted({gammas[c][1] for c in common})
        by_gamma = {gammas[c]: c for c in common}
        grid_rows = []
        for gi in gis:
            row = [f"**γi {fmt_gamma(gi)}**"]
            for gd in gds:
                c = by_gamma.get((gi, gd))
                cell = "" if c is None else f"{pct(score[c]):+.2f}"
                row.append(f"**{cell}**" if c == best else cell)
                if c is not None:
                    self.grid_rows.append({"arch": arch, "group": "ALL (equal-suite)", "config": c, "gamma_instr": gi,
                                           "gamma_data": gd, "geomean_gain_pct": pct(score[c])})
            grid_rows.append(row)
        self.out("Equal-suite geomean gain (%), rows instruction γ, columns data γ:\n")
        self.table(["", *[f"γd {fmt_gamma(gd)}" for gd in gds]], grid_rows)
        return best

    def tables(self, results, cross_best):
        for r in results:
            sb, cb, base = r["best"], cross_best.get(r["arch"]), self.baseline
            ipc, w = r["ipc"], r["w"]
            for (wl, cl), row in ipc.iterrows():
                bc = r["sp_best"][(wl, cl)]
                rec = {"arch": r["arch"], "group": r["name"], "workload": wl, "cluster": cl,
                       "weight": w[(wl, cl)], "baseline_ipc": row[base], "best_config": bc,
                       "best_gamma_instr": r["gammas"][bc][0], "best_gamma_data": r["gammas"][bc][1],
                       "best_ipc": row[bc], "best_gain_pct": pct(row[bc] / row[base]),
                       "suite_best_config": sb, "suite_best_ipc": row[sb]}
                if cb in row:
                    rec.update({"cross_best_config": cb, "cross_best_ipc": row[cb]})
                self.per_simpoint.append(rec)
            for wl, row in r["wl_ipc"].iterrows():
                spd = r["speedup"].loc[wl]
                wb = best_of(spd.to_dict(), r["gammas"])
                rec = {"arch": r["arch"], "group": r["name"], "workload": wl, "baseline_ipc": row[base],
                       "best_config": wb, "best_gain_pct": pct(spd[wb]),
                       "suite_best_config": sb, "suite_best_gain_pct": pct(spd[sb])}
                if cb in spd:
                    rec.update({"cross_best_config": cb, "cross_best_gain_pct": pct(spd[cb])})
                self.per_workload.append(rec)

    def run(self):
        cross_best = {}
        all_results = []
        for arch in sorted(self.long["arch"].unique()):
            results = []
            part_arch = self.long[self.long["arch"] == arch]
            self.out(f"## {arch}\n")
            for name in sorted(part_arch[self.group].unique()):
                r = self.analyze_group(arch, name, part_arch[part_arch[self.group] == name])
                if r:
                    results.append(r)
            if len(results) > 1:
                cross_best[arch] = self.cross(arch, results)
            all_results += results
        self.tables(all_results, cross_best)
        if self.show_simpoints and self.per_simpoint:
            self.out("## Best config per simpoint\n")
            self.table(["arch", "workload", "simpoint", "weight", "baseline IPC", "best", "best IPC", "gain (%)"],
                       [[p["arch"], p["workload"], p["cluster"], f"{p['weight']:.3f}", f"{p['baseline_ipc']:.4f}",
                         p["best_config"], f"{p['best_ipc']:.4f}", f"{p['best_gain_pct']:+.2f}"]
                        for p in self.per_simpoint])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("descriptors", nargs="*", help="descriptor names (json/<name>.json) or paths")
    ap.add_argument("--all", action="store_true", help="every json/mlp_gamma_*.json except the smoke test")
    ap.add_argument("--check", action="store_true", help="the smoke-test descriptors, json/mlp_gamma_check_*.json")
    ap.add_argument("--group", choices=["suite", "subsuite"], default="suite",
                    help="report per suite (default) or per suite/subsuite")
    ap.add_argument("--baseline", default="gi0_gd0", help="config gains are measured against (default gi0_gd0)")
    ap.add_argument("--out", default="mlp_gamma_report", help="directory for summary.md and the CSVs")
    ap.add_argument("--show-simpoints", action="store_true", help="also print the per-simpoint table")
    ap.add_argument("--marks", action="store_true", help="also print mean marked fetch/load lines per config")
    args = ap.parse_args()

    names = list(args.descriptors)
    if args.all:
        names += sorted(p.stem for p in (INFRA / "json").glob("mlp_gamma_*.json") if "_check_" not in p.stem)
    if args.check:
        names += sorted(p.stem for p in (INFRA / "json").glob("mlp_gamma_check_*.json"))
    if not names:
        ap.error("name descriptors, or pass --all or --check")

    long = pd.concat([load_long(load_descriptor(n)) for n in names], ignore_index=True)
    rep = Report(long, args.group, args.baseline, args.show_simpoints, args.marks)
    rep.out(f"# REPL_MLP gamma sweep: {', '.join(names)}\n")
    rep.run()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    text = "\n".join(rep.lines)
    (out / "summary.md").write_text(text + "\n")
    pd.DataFrame(rep.per_simpoint).to_csv(out / "per_simpoint.csv", index=False)
    pd.DataFrame(rep.per_workload).to_csv(out / "per_workload.csv", index=False)
    pd.DataFrame(rep.grid_rows).to_csv(out / "grid.csv", index=False)
    long.drop(columns=["ok"]).to_csv(out / "ipc_long.csv", index=False)
    print(text)
    print(f"\nWrote summary.md, per_simpoint.csv, per_workload.csv, grid.csv and ipc_long.csv to {out}/")


if __name__ == "__main__":
    main()
