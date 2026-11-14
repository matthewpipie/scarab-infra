#!/usr/bin/env python3
"""Write subset copies of a full-sweep descriptor pair that run only the representative simpoints.

The representative set (simpoints.csv, see README.md) mixes Google with the 10M+10M suites, which need
different trace directories and instruction limits, so it is written as two descriptors:

    python tools/mlp_rep/make_descriptors.py NAME --google mlp_gamma_google_small \\
        --rest mlp_gamma_spec2006_small [--configs gi0_gd0,gi64_gd16] [--max-parallel 30]

writes json/NAME_google.json (Google simpoints; settings and configs from --google) and
json/NAME_rest.json (SPEC2006, SPEC2017, Geekbench, Datacenter; settings and configs from --rest).
Both keep the source's architecture, scarab_path, binaries and params; the experiment is the file
name. Google configs get --inst_limit 60000000 if they lack one; the others lose it.
Then: ./sci --sim NAME_google; ./sci --sim NAME_rest; collect stats; tools/mlp_rep/report.py NAME.
"""

import argparse
import csv
import json
import re
from pathlib import Path

INFRA = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
GOOGLE_LIMIT = "--inst_limit 60000000"


def load(name):
    with open(INFRA / "json" / f"{name}.json") as f:
        return json.load(f)


def simpoints():
    with open(HERE / "simpoints.csv") as f:
        return list(csv.DictReader(f))


def subset(src, name, rows, keep, google, max_parallel):
    d = json.loads(json.dumps(src))
    d["experiment"] = name
    d["_experiment"] = (f"Representative-subset copy of {src['experiment']} "
                        f"({len(rows)} simpoints from tools/mlp_rep/simpoints.csv).")
    d["top_simpoint"] = 6  # the cluster ids are from each workload's 6 heaviest simpoints
    d["simulations"] = [{"suite": r["suite"], "subsuite": r["subsuite"], "workload": r["workload"],
                         "cluster_id": int(r["cluster_id"]), "simulation_type": "memtrace",
                         "warmup": 10000000} for r in rows]
    configs = {}
    for cname, c in src["configurations"].items():
        if keep and cname not in keep:
            continue
        c = dict(c)
        params = re.sub(r"\s*--inst_limit[ =]\S+", "", c.get("params", "")).strip()
        c["params"] = f"{params} {GOOGLE_LIMIT}".strip() if google else params
        configs[cname] = c
    missing = sorted(set(keep or []) - set(configs))
    if missing:
        raise SystemExit(f"{src['experiment']} has no configs {missing}")
    d["configurations"] = configs
    if max_parallel:
        d["max_parallel"] = max_parallel
    vis = d.get("visualize")
    if isinstance(vis, dict) and "configs" in vis:
        vis["configs"] = [c for c in vis["configs"] if c in configs] or list(configs)
        if vis.get("baseline") not in configs:
            vis["baseline"] = next(iter(configs))
    pa = d.get("perf_analyze")
    if isinstance(pa, dict) and pa.get("baseline") not in configs:
        pa["baseline"] = next(iter(configs))
    out = INFRA / "json" / f"{name}.json"
    out.write_text(json.dumps(d, indent=2, separators=(",", ":")) + "\n")
    print(f"wrote {out.relative_to(INFRA)}: {len(rows)} simpoints x {len(configs)} configs")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="output prefix: json/NAME_google.json and json/NAME_rest.json")
    ap.add_argument("--google", required=True, help="full-sweep Google descriptor to copy settings and configs from")
    ap.add_argument("--rest", required=True, help="full-sweep descriptor (any 10M+10M suite) for the rest")
    ap.add_argument("--configs", help="comma-separated config names to keep (default: all)")
    ap.add_argument("--max-parallel", type=int, help="override max_parallel")
    a = ap.parse_args()
    keep = a.configs.split(",") if a.configs else None
    rows = simpoints()
    subset(load(a.google), f"{a.name}_google", [r for r in rows if r["suite"] == "google"], keep, True,
           a.max_parallel)
    subset(load(a.rest), f"{a.name}_rest", [r for r in rows if r["suite"] != "google"], keep, False,
           a.max_parallel)


if __name__ == "__main__":
    main()
