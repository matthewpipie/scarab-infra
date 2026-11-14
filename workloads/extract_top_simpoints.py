#!/usr/bin/python3

# 04/23/2025 | Surim Oh | extract_top_simpoints.py
# A script that extracts top simpoints from the source workloads_db.json and write the recomputed weights of top simpoints to the destination workloads_top_simp.json

import json
import argparse
import copy

# Number of simpoints kept per workload in workloads_top_simp.json, which is what
# a descriptor's `"top_simpoint": true` selects.
DEFAULT_TOP_N = 3

def normalize_weights(simpoints):
    total = sum(sp['weight'] for sp in simpoints)
    if total == 0:  # Avoid division by zero
        return simpoints
    for sp in simpoints:
        sp['weight'] = sp['weight'] / total
    return simpoints

def process_simpoints(simpoints, n=DEFAULT_TOP_N):
    # Remove simpoints with null weight
    simpoints = [sp for sp in simpoints if sp.get('weight') is not None]
    # Sort and take top n by weight
    simpoints = sorted(simpoints, key=lambda x: x['weight'], reverse=True)[:n]
    return normalize_weights(simpoints)

def process_simpoints_coverage(simpoints, coverage):
    """Keep the heaviest simpoints until they hold at least `coverage` (0-1] of the
    workload's total weight, then renormalize the kept weights to sum to 1.

    The total is the sum of the workload's own weights (some sum to 0.99, not 1), so
    coverage 1.0 keeps every simpoint. Ties keep workloads_db.json order, as top-N does.
    """
    simpoints = [sp for sp in simpoints if sp.get('weight') is not None]
    simpoints = sorted(simpoints, key=lambda x: x['weight'], reverse=True)
    # The relative slack absorbs float rounding in the running sum.
    target = coverage * sum(sp['weight'] for sp in simpoints) * (1 - 1e-9)
    kept, covered = [], 0.0
    for sp in simpoints:
        kept.append(sp)
        covered += sp['weight']
        if covered >= target:
            break
    return normalize_weights(kept)

def _map_simpoints(d, fn):
    if isinstance(d, dict):
        if 'simpoints' in d and isinstance(d['simpoints'], list):
            d['simpoints'] = fn(d['simpoints'])
        for v in d.values():
            _map_simpoints(v, fn)
    elif isinstance(d, list):
        for item in d:
            _map_simpoints(item, fn)

def modify_simpoints_in_place(d, n=DEFAULT_TOP_N):
    _map_simpoints(d, lambda simpoints: process_simpoints(simpoints, n))

def modify_simpoints_coverage_in_place(d, coverage):
    _map_simpoints(d, lambda simpoints: process_simpoints_coverage(simpoints, coverage))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Extract the top simpoints and recompute their weights')

    parser.add_argument('-s', '--src_descriptor_name', required=True, help='Source workloads_db.json descriptor name.')
    parser.add_argument('-d', '--dest_descriptor_name', required=True, help='Destination workloads_top_simp.json descriptor name.')
    parser.add_argument('-n', '--num_simpoints', type=int, default=DEFAULT_TOP_N, help=f'Number of top simpoints to keep per workload (default: {DEFAULT_TOP_N}).')
    args = parser.parse_args()

    with open(args.src_descriptor_name) as f_in:
        data = json.load(f_in)
        # Deep copy to avoid modifying input if needed elsewhere
        modified_data = copy.deepcopy(data)
        modify_simpoints_in_place(modified_data, args.num_simpoints)

        with open(args.dest_descriptor_name, 'w') as f_out:
            json.dump(modified_data, f_out, indent=2, separators=(",", ":"))

