#!/usr/bin/env python3
"""Representative-subset estimate of the 5-suite equal-suite geomean gain.

    python tools/mlp_rep/report.py NAME [--baseline gi0_gd0] [--by-suite]

Reads json/NAME_google.json and json/NAME_rest.json (from make_descriptors.py) and their
collected_stats.csv (./sci --collect-stats). A simpoint's IPC is NODE_INST_COUNT / NODE_CYCLE over the
measured region. The estimate for each config is the plain geomean over the representative simpoints of
IPC / baseline IPC: every simpoint counts once, with no simpoint or suite weights (see README.md for why
that tracks the full sweep). Simpoints missing under any config are dropped and listed.
"""

import argparse
import csv
import io
import json
import math
import os
from pathlib import Path

import pandas as pd

INFRA = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
META = ["Configuration", "Workload", "Cluster Id", "Collect Failed"]
STATS = ["NODE_INST_COUNT_count", "NODE_CYCLE_count"]


def load(name):
    with open(INFRA / "json" / f"{name}.json") as f:
        desc = json.load(f)
    root = Path(os.path.expandvars(desc["root_dir"].replace("$USER", os.environ.get("USER", ""))))
    path = root / "simulations" / desc["experiment"] / "collected_stats.csv"
    if not path.is_file():
        raise SystemExit(f"{name}: no {path}. Run ./sci --collect-stats {name}.")
    wanted = {s + "," for s in META + STATS}
    with open(path) as f:
        lines = [next(f)] + [line for line in f if line[: line.find(",") + 1] in wanted]
    df = pd.read_csv(io.StringIO("".join(lines)), low_memory=False).set_index("stats")
    df = df[~df.index.duplicated(keep="first")]
    t = df.loc[META + STATS].T
    t = t[t["Configuration"].isin(desc["configurations"].keys())]
    out = pd.DataFrame({"config": t["Configuration"].values, "workload": t["Workload"].values,
                        "cluster": t["Cluster Id"].astype(str).values,
                        "failed": t["Collect Failed"].astype(str).str.strip().str.lower().eq("true").values})
    out["ipc"] = pd.to_numeric(t[STATS[0]], errors="coerce").values / pd.to_numeric(t[STATS[1]], errors="coerce").values
    out.loc[out["failed"] | ~(out["ipc"] > 0), "ipc"] = math.nan
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name")
    ap.add_argument("--baseline", default="gi0_gd0")
    ap.add_argument("--by-suite", action="store_true", help="also print each suite's part of the subset")
    a = ap.parse_args()
    with open(HERE / "simpoints.csv") as f:
        want = {(f"{r['suite']}/{r['subsuite']}/{r['workload']}", r["cluster_id"]) for r in csv.DictReader(f)}
    df = pd.concat([load(f"{a.name}_google"), load(f"{a.name}_rest")])
    ipc = df.pivot_table(index=["workload", "cluster"], columns="config", values="ipc", aggfunc="first")
    ipc = ipc[[k in want for k in ipc.index]]
    if a.baseline not in ipc:
        raise SystemExit(f"no baseline config {a.baseline}; configs: {list(ipc.columns)}")
    complete = ipc.dropna()
    missing = sorted(want - set(complete.index))
    gain = (complete.div(complete[a.baseline], axis=0)).apply(lambda c: c.map(math.log))
    est = (gain.mean().map(math.exp) - 1) * 100
    print(f"{a.name}: {len(complete)}/{len(want)} representative simpoints complete under every config"
          + (f"; missing {missing}" if missing else ""))
    print(f"Estimated 5-suite equal-suite geomean gain over {a.baseline} (%):")
    table = est.drop(a.baseline).sort_values(ascending=False).to_frame("estimate")
    if a.by_suite:
        suite = pd.Series([w.split("/")[0] for w, _ in gain.index], index=gain.index)
        for s, part in gain.groupby(suite):
            table[f"{s} ({len(part)})"] = (part.mean().map(math.exp) - 1).drop(a.baseline) * 100
    print(table.round(2).to_string())


if __name__ == "__main__":
    main()
