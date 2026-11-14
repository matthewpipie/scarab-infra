"""Tests for a descriptor's simpoint selection (top_simpoint, simpoint_coverage).

Run with the scarabinfra python from the repo root: python scripts/test_simpoint_selection.py (or pytest).
"""

import copy
import json
import math
import os
import sys
import tempfile

INFRA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, INFRA)

from scripts.utilities import load_workloads_for_descriptor, parse_simpoint_coverage  # noqa: E402
from workloads.extract_top_simpoints import process_simpoints_coverage  # noqa: E402


def sp(cid, w):
    return {"cluster_id": cid, "segment_id": cid, "weight": w}


def ids(simpoints):
    return [s["cluster_id"] for s in simpoints]


def close(a, b):
    return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


def test_coverage_picks_heaviest_until_reached():
    sps = [sp(1, 0.1), sp(2, 0.4), sp(3, 0.3), sp(4, 0.2)]
    assert ids(process_simpoints_coverage(copy.deepcopy(sps), 0.5)) == [2, 3]   # 0.4 < 0.5 <= 0.7
    assert ids(process_simpoints_coverage(copy.deepcopy(sps), 0.7)) == [2, 3]   # reached exactly
    assert ids(process_simpoints_coverage(copy.deepcopy(sps), 0.71)) == [2, 3, 4]
    assert ids(process_simpoints_coverage(copy.deepcopy(sps), 0.4)) == [2]
    assert ids(process_simpoints_coverage(copy.deepcopy(sps), 1.0)) == [2, 3, 4, 1]
    kept = process_simpoints_coverage(copy.deepcopy(sps), 0.9)
    assert close(sum(s["weight"] for s in kept), 1.0)
    assert close(kept[0]["weight"], 0.4 / 0.9)


def test_coverage_is_relative_to_the_workloads_own_total():
    # A workload whose weights sum to 0.99: coverage 1.0 still keeps everything, and
    # 0.5 means half of 0.99.
    sps = [sp(1, 0.33), sp(2, 0.33), sp(3, 0.33)]
    assert ids(process_simpoints_coverage(copy.deepcopy(sps), 1.0)) == [1, 2, 3]   # ties keep DB order
    assert ids(process_simpoints_coverage(copy.deepcopy(sps), 0.5)) == [1, 2]
    # Float sums that land a hair under the target still count as reaching it.
    tenths = [sp(i, 0.1) for i in range(10)]
    assert len(process_simpoints_coverage(copy.deepcopy(tenths), 0.3)) == 3
    assert len(process_simpoints_coverage(copy.deepcopy(tenths), 1.0)) == 10


def test_coverage_drops_null_weights():
    sps = [sp(1, None), sp(2, 0.6), sp(3, 0.4)]
    assert ids(process_simpoints_coverage(copy.deepcopy(sps), 1.0)) == [2, 3]


def test_parse():
    assert parse_simpoint_coverage(None) is None
    assert parse_simpoint_coverage(False) is None
    assert parse_simpoint_coverage(0.9) == {"default": 0.9}
    assert parse_simpoint_coverage(1) == {"default": 1.0}
    assert parse_simpoint_coverage({"default": 0.9, "google": None}) == {"default": 0.9, "google": None}
    for bad in (0, 1.5, -0.1, True, "0.9", {"spec2006": 2}):
        try:
            parse_simpoint_coverage(bad)
        except RuntimeError:
            continue
        raise AssertionError(f"accepted {bad!r}")


DB = {
    "a": {"sub": {"w": {"simpoints": [sp(1, 0.5), sp(2, 0.3), sp(3, 0.15), sp(4, 0.05)]}}},
    "b": {"sub": {"w": {"simpoints": [sp(1, 0.25), sp(2, 0.25), sp(3, 0.25), sp(4, 0.25)]}}},
}
# What workloads_top_simp.json would hold (its own entries, even if they differ from top 3 of DB).
TOP = {
    "a": {"sub": {"w": {"simpoints": [sp(9, 1.0)]}}},
    "b": {"sub": {"w": {"simpoints": [sp(8, 1.0)]}}},
}


def load(desc):
    with tempfile.TemporaryDirectory() as infra:
        os.makedirs(os.path.join(infra, "workloads"))
        for name, data in (("workloads_db.json", DB), ("workloads_top_simp.json", TOP)):
            with open(os.path.join(infra, "workloads", name), "w") as f:
                json.dump(data, f)
        db = load_workloads_for_descriptor(desc, infra, dbg_lvl=0)
    return {s: ids(db[s]["sub"]["w"]["simpoints"]) for s in db}


def test_descriptor_selection():
    # Without simpoint_coverage nothing changes.
    assert load({"top_simpoint": False}) == {"a": [1, 2, 3, 4], "b": [1, 2, 3, 4]}
    assert load({"top_simpoint": 2}) == {"a": [1, 2], "b": [1, 2]}
    assert load({"top_simpoint": True}) == {"a": [9], "b": [8]}
    # A number covers every suite and overrides top_simpoint.
    assert load({"top_simpoint": 1, "simpoint_coverage": 0.8}) == {"a": [1, 2], "b": [1, 2, 3, 4]}
    # A map: suites set to null (or left out without a default) keep top_simpoint.
    assert load({"top_simpoint": 2, "simpoint_coverage": {"default": 0.9, "b": None}}) == {"a": [1, 2, 3], "b": [1, 2]}
    assert load({"top_simpoint": False, "simpoint_coverage": {"a": 0.5}}) == {"a": [1], "b": [1, 2, 3, 4]}
    assert load({"top_simpoint": True, "simpoint_coverage": {"a": 0.5}}) == {"a": [1], "b": [8]}
    try:
        load({"top_simpoint": False, "simpoint_coverage": {"spec06": 0.9}})
    except RuntimeError as e:
        assert "spec06" in str(e)
    else:
        raise AssertionError("unknown suite accepted")


def test_real_db_non_google_90():
    """{"default": 0.9, "google": null} with top_simpoint 6: every other workload keeps the fewest
    heaviest simpoints that reach 90% of its weight; google is exactly its top-6 selection."""
    full = load_workloads_for_descriptor({"top_simpoint": False}, INFRA, dbg_lvl=0)
    top6 = load_workloads_for_descriptor({"top_simpoint": 6}, INFRA, dbg_lvl=0)
    cov = load_workloads_for_descriptor(
        {"top_simpoint": 6, "simpoint_coverage": {"default": 0.9, "google": None}}, INFRA, dbg_lvl=0)
    assert cov["google"] == top6["google"]
    checked = 0
    for suite in ("datacenter", "spec2006", "spec2017", "spec2026", "geekbench"):
        for sub, wls in full[suite].items():
            if not isinstance(wls, dict):
                continue
            for wl, info in wls.items():
                if not isinstance(info, dict) or not info.get("simpoints"):
                    continue
                orig = {s["cluster_id"]: s["weight"] for s in info["simpoints"] if s["weight"] is not None}
                kept = cov[suite][sub][wl]["simpoints"]
                total = sum(orig.values())
                covered = sum(orig[s["cluster_id"]] for s in kept)
                assert covered >= 0.9 * total * (1 - 1e-9), (suite, wl)
                assert covered - min(orig[s["cluster_id"]] for s in kept) < 0.9 * total, (suite, wl)
                assert close(sum(s["weight"] for s in kept), 1.0)
                checked += 1
    assert checked > 200


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
