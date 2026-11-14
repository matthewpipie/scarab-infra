"""Tests for scarab_stats/aggregation.py. Run: python scarab_stats/test_aggregation.py (or pytest)."""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aggregation import Hierarchy, mean, uses_geomean  # noqa: E402

LABELS = [
    "spec2006/rate_int/gcc", "spec2006/rate_int/gcc_2", "spec2006/rate_int/gcc_3",
    "spec2006/rate_int/mcf",
    "spec2006/rate_fp/lbm",
    "spec2017/rate_int_v2/gcc_r", "spec2017/rate_int_v2/gcc_r_2",
    "spec2017/rate_int_v2/xz_r_1",          # a lone suffixed name stays as it is
    "google/google/sierra.a.3",              # dots are not input suffixes
    "google/google/sierra.a.4",
]


def close(a, b):
    return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


def gm(values):
    return math.exp(sum(math.log(v) for v in values) / len(values))


def test_grouping():
    h = Hierarchy(LABELS)
    assert h.workloads == {
        "spec2006/rate_int/gcc": LABELS[0:3],
        "spec2006/rate_int/mcf": [LABELS[3]],
        "spec2006/rate_fp/lbm": [LABELS[4]],
        "spec2017/rate_int_v2/gcc_r": LABELS[5:7],
        "spec2017/rate_int_v2/xz_r_1": [LABELS[7]],
        "google/google/sierra.a.3": [LABELS[8]],
        "google/google/sierra.a.4": [LABELS[9]],
    }
    assert list(h.suites) == ["spec2006", "spec2017", "google"]
    assert h.suites["spec2006"] == ["spec2006/rate_int/gcc", "spec2006/rate_int/mcf", "spec2006/rate_fp/lbm"]
    assert h.label("spec2006/rate_int/gcc") == "spec2006/rate_int/gcc (3 inputs)"
    assert h.label("spec2006/rate_int/mcf") == "spec2006/rate_int/mcf"


def test_inputs_split_across_subsuites_stay_apart():
    h = Hierarchy(["s/a/foo", "s/b/foo_2"])
    assert h.workloads == {"s/a/foo": ["s/a/foo"], "s/b/foo_2": ["s/b/foo_2"]}


def test_nested_geomean_and_ratio_identity():
    h = Hierarchy(LABELS)
    base = {label: 1.0 + 0.1 * i for i, label in enumerate(LABELS)}
    new = {label: base[label] * (1.0 + 0.01 * (i % 4)) for i, label in enumerate(LABELS)}
    b, n = h.aggregate(base, True), h.aggregate(new, True)

    assert close(b["workloads"]["spec2006/rate_int/gcc"], gm([base[x] for x in LABELS[0:3]]))
    assert close(b["suites"]["spec2006"],
                 gm([gm([base[x] for x in LABELS[0:3]]), base[LABELS[3]], base[LABELS[4]]]))
    assert close(b["Avg"], gm(list(b["suites"].values())))

    # Ratio of nested geomeans == nested geomean of per-input ratios, at every level.
    ratios = h.aggregate({x: new[x] / base[x] for x in LABELS}, True)
    assert close(n["Avg"] / b["Avg"], ratios["Avg"])
    for suite in h.suites:
        assert close(n["suites"][suite] / b["suites"][suite], ratios["suites"][suite])


def test_each_workload_counts_once():
    # gcc has three inputs at +10%, mcf one at 0%: flat would give +7.1%, nested +4.9%.
    h = Hierarchy(LABELS[0:4])
    ratios = {x: 1.1 for x in LABELS[0:3]}
    ratios[LABELS[3]] = 1.0
    assert close(h.aggregate(ratios, True)["Avg"], math.sqrt(1.1))


def test_arithmetic_and_missing_values():
    h = Hierarchy(LABELS[0:4])
    vals = {LABELS[0]: 1.0, LABELS[1]: 3.0, LABELS[2]: None, LABELS[3]: 10.0}
    agg = h.aggregate(vals, False)
    assert agg["workloads"]["spec2006/rate_int/gcc"] == 2.0      # None skipped
    assert agg["Avg"] == 6.0
    vals[LABELS[2]] = math.nan                                    # NaN propagates
    assert math.isnan(h.aggregate(vals, False)["Avg"])
    assert h.aggregate({}, True)["Avg"] is None


def test_mean_edge_cases():
    assert mean([], True) is None
    assert mean([2.0, 0.0], True) == 0.0
    assert math.isnan(mean([2.0, -1.0], True))
    assert close(mean([2.0, 8.0], True), 4.0)


def test_uses_geomean():
    assert uses_geomean("IPC") and uses_geomean("IPC_total") and uses_geomean("TOTAL_MEM_LATENCY_pct")
    assert not uses_geomean("ICACHE_MISS_count") and not uses_geomean("Cumulative_Instructions")


def test_rows():
    h = Hierarchy(LABELS[0:5])
    per_config = {"a": {x: 1.0 for x in LABELS[0:5]}, "b": {x: 2.0 for x in LABELS[0:5]}}
    rows = h.rows(per_config, True)
    assert [r["level"] for r in rows] == ["workload"] * 3 + ["avg"]   # one suite: no suite rows
    assert rows[0]["label"] == "spec2006/rate_int/gcc (3 inputs)"
    assert close(rows[-1]["values"]["b"], 2.0)
    rows = Hierarchy(LABELS).rows({"a": {x: 1.0 for x in LABELS}}, True, per_input=True)
    assert [r["level"] for r in rows].count("input") == len(LABELS)
    assert [r["key"] for r in rows if r["level"] == "suite"] == ["spec2006", "spec2017", "google"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
