"""Tests for a configuration's per-workload params (`workload_params`).

Run with the scarabinfra python from the repo root: python scripts/test_workload_params.py (or pytest).
"""

import os
import sys

INFRA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, INFRA)

from scripts.utilities import (  # noqa: E402
    load_workloads_for_descriptor,
    merge_params,
    parse_workload_params,
    resolve_params,
    unselected_workload_params,
)

DB = {
    "spec2006": {
        "rate_int": {
            "gcc": {"simpoints": [{"cluster_id": 3, "weight": 0.5}, {"cluster_id": 7, "weight": 0.5}]},
            "gcc_2": {"simpoints": [{"cluster_id": 1, "weight": 1.0}]},
        },
        "rate_fp": {"wrf": {"simpoints": [{"cluster_id": 4, "weight": 1.0}]}},
    },
    "google": {"google": {"tahoe": {"simulation": {}}}},   # no simpoints: run whole, as cluster 0
}

BASE = "--mlc_cache_repl_policy 9 --mlp_lin_instr_lambda 96 --mlp_lin_data_lambda 48"


def raises(fn):
    try:
        fn()
    except RuntimeError:
        return True
    return False


def test_merge_replaces_repeated_flags():
    assert merge_params(BASE, "--mlp_lin_data_lambda 0") == \
        "--mlc_cache_repl_policy 9 --mlp_lin_instr_lambda 96 --mlp_lin_data_lambda 0"
    # --flag=value and --flag value name the same flag, in either direction.
    assert merge_params("--a=1 --b 2", "--a 3") == "--b 2 --a 3"
    assert merge_params("--a 1 --b 2", "--b=5") == "--a 1 --b=5"
    # Layers apply in order; a later layer wins.
    assert merge_params("--a 1", "--a 2 --c 1", "--a 3") == "--c 1 --a 3"
    # Every earlier copy of a repeated flag goes.
    assert merge_params("--a 1 --b 2 --a 9", "--a 3") == "--b 2 --a 3"
    # Negative values are values, not flags.
    assert merge_params("--a -1 --b 2", "--b -4") == "--a -1 --b -4"
    assert merge_params("", "--a 1") == "--a 1"
    assert merge_params(None, "--a 1") == "--a 1"
    assert merge_params("--a 1", "") == "--a 1"


def test_parse_orders_least_specific_first():
    entries = parse_workload_params("tuned", {
        "spec2006/rate_int/gcc:3": "--x 4",
        "spec2006": "--x 1",
        "spec2006/rate_int/gcc": "--x 3",
        "spec2006/rate_int": "--x 2",
    }, DB)
    assert [e[0] for e in entries] == [
        ("spec2006", None, None, None),
        ("spec2006", "rate_int", None, None),
        ("spec2006", "rate_int", "gcc", None),
        ("spec2006", "rate_int", "gcc", 3),
    ]
    assert parse_workload_params("tuned", None, DB) == []
    assert parse_workload_params("tuned", {}, DB) == []


def test_parse_rejects_bad_entries():
    bad = [
        ["--x 1"],                                   # not an object
        {"spec2006": 1},                             # params not a string
        {"spec2006": None},
        {"spec2006/rate_int/gcc/extra": "--x 1"},    # too deep
        {"spec2006:3": "--x 1"},                     # cluster needs a workload
        {"spec2006/rate_int/gcc:x": "--x 1"},        # cluster not an integer
        {"": "--x 1"},
        {"spec2017": "--x 1"},                       # unknown suite
        {"spec2006/rate_xx": "--x 1"},               # unknown subsuite
        {"spec2006/rate_int/mcf": "--x 1"},          # unknown workload
        {"spec2006/rate_fp/gcc": "--x 1"},           # workload in another subsuite
    ]
    for value in bad:
        assert raises(lambda: parse_workload_params("tuned", value, DB)), value
    # Without a DB, names are not checked.
    assert parse_workload_params("tuned", {"spec2017/a/b:2": "--x 1"}) == [(("spec2017", "a", "b", 2), "--x 1")]


def test_resolve_layers_matching_entries():
    entries = parse_workload_params("tuned", {
        "spec2006": "--mlp_lin_instr_lambda 0",
        "spec2006/rate_fp": "--mlp_lin_data_lambda 0",
        "spec2006/rate_int/gcc": "--mlp_lin_data_lambda 72",
        "spec2006/rate_int/gcc:7": "--mlp_lin_data_lambda 12 --mlp_lin_instr_lambda 24",
        "google/google/tahoe:0": "--mlp_lin_instr_lambda 144",
    }, DB)
    r = lambda *sim: resolve_params(BASE, entries, *sim)  # noqa: E731
    assert r("spec2006", "rate_int", "gcc", "3") == \
        "--mlc_cache_repl_policy 9 --mlp_lin_instr_lambda 0 --mlp_lin_data_lambda 72"
    # The simpoint entry beats the workload entry; cluster ids match as int or str.
    assert r("spec2006", "rate_int", "gcc", 7) == r("spec2006", "rate_int", "gcc", "7") == \
        "--mlc_cache_repl_policy 9 --mlp_lin_data_lambda 12 --mlp_lin_instr_lambda 24"
    # gcc_2 is another input: only the suite entry applies.
    assert r("spec2006", "rate_int", "gcc_2", "1") == \
        "--mlc_cache_repl_policy 9 --mlp_lin_data_lambda 48 --mlp_lin_instr_lambda 0"
    assert r("spec2006", "rate_fp", "wrf", "4") == \
        "--mlc_cache_repl_policy 9 --mlp_lin_instr_lambda 0 --mlp_lin_data_lambda 0"
    assert r("google", "google", "tahoe", "0") == \
        "--mlc_cache_repl_policy 9 --mlp_lin_data_lambda 48 --mlp_lin_instr_lambda 144"


def test_resolve_without_a_match_leaves_params_untouched():
    odd = "  --a 1   --a 2 "      # unusual spacing and a repeated flag stay exactly as written
    entries = parse_workload_params("tuned", {"google": "--a 3"}, DB)
    assert resolve_params(odd, entries, "spec2006", "rate_int", "gcc", "3") is odd
    assert resolve_params(odd, [], "spec2006", "rate_int", "gcc", "3") is odd
    assert resolve_params("", [], "spec2006", "rate_int", "gcc", "3") == ""
    assert resolve_params("", entries, "google", "google", "tahoe", "0") == "--a 3"


def test_unselected_simpoints():
    entries = parse_workload_params("tuned", {
        "spec2006/rate_int/gcc:3": "--x 1",
        "spec2006/rate_int/gcc:5": "--x 1",
        "spec2006/rate_int/gcc": "--x 1",
        "google/google/tahoe:0": "--x 1",
        "google/google/tahoe:2": "--x 1",
    }, DB)
    assert unselected_workload_params(entries, DB) == ["spec2006/rate_int/gcc:5", "google/google/tahoe:2"]


def test_real_db_names():
    db = load_workloads_for_descriptor({"top_simpoint": True}, INFRA, dbg_lvl=0)
    entries = parse_workload_params("tuned", {
        "spec2006/rate_int/gcc_2": "--x 1",
        "google/google/sierra.a.3": "--x 1",
        "spec2017/rate_int_v2": "--x 1",
    }, db)
    assert len(entries) == 3
    assert raises(lambda: parse_workload_params("tuned", {"spec2006/rate_int/gcc_99": "--x 1"}, db))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
