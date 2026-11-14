"""Cross-workload averaging shared by ./sci --visualize, --perf-analyze and the plots.

Results are combined bottom up, one level at a time:

  simpoint -> input     stat_collector, before this module: cycle-weighted, so an
                        input's IPC is sum(w * insts) / sum(w * cycles)
  input    -> workload  the inputs of one benchmark (spec2006 gcc, gcc_2 ... gcc_9)
  workload -> suite     the workloads of one suite (spec2006)
  suite    -> Avg       the suites present

Every level above the simpoint uses the same mean: geometric for IPC and *_pct stats,
arithmetic for everything else. Each input therefore counts equally within its
workload, each workload equally within its suite, and each suite equally in Avg; a
benchmark with nine inputs no longer outvotes one with a single input.

For a geometric mean the ratio of two configs' means equals the mean of their ratios,
so a speedup read off any row is the nested geomean of the per-input speedups.

Inputs are recognized by name: within one suite/subsuite, workloads that differ only
by a trailing _<n> (gcc, gcc_2, gcc_3) are inputs of one workload. A name with no
sibling stays a workload of its own, so a lone "foo_1" is never renamed.
"""

import math
import re
from typing import Dict, Iterable, List, Mapping, Optional

_INPUT_SUFFIX = re.compile(r"^(?P<stem>.+?)_\d+$")

GEOMEAN_STATS = frozenset({"IPC", "IPC_total"})


def uses_geomean(stat: str) -> bool:
    """Geometric mean for IPC and percentage stats, arithmetic mean otherwise."""
    return stat in GEOMEAN_STATS or stat.split("_")[-1] == "pct"


def mean(values: Iterable[Optional[float]], geometric: bool) -> Optional[float]:
    """Mean of the values that are present.

    None means "not measured" and is skipped; NaN means "measured but failed" and
    makes the result NaN, so a failed simpoint is not silently averaged away.
    Returns None when nothing is present.
    """
    present = [float(v) for v in values if v is not None]
    if not present:
        return None
    if any(math.isnan(v) for v in present):
        return math.nan
    if not geometric:
        return sum(present) / len(present)
    if any(v < 0 for v in present):
        return math.nan
    if any(v == 0 for v in present):
        return 0.0
    return math.exp(sum(math.log(v) for v in present) / len(present))


def _split(label: str):
    """'suite/subsuite/name' -> ('suite/subsuite', 'name'); a bare name has no prefix."""
    prefix, _, name = label.rpartition("/")
    return prefix, name


def suite_of(label: str) -> str:
    return label.split("/", 1)[0]


class Hierarchy:
    """Groups input labels into workloads and workloads into suites."""

    def __init__(self, inputs: Iterable[str]):
        labels = list(dict.fromkeys(inputs))
        stems: Dict[tuple, List[str]] = {}
        for label in labels:
            prefix, name = _split(label)
            match = _INPUT_SUFFIX.match(name)
            stem = match.group("stem") if match else name
            stems.setdefault((prefix, stem), []).append(label)

        self.workloads: Dict[str, List[str]] = {}
        for label in labels:
            prefix, name = _split(label)
            match = _INPUT_SUFFIX.match(name)
            stem = match.group("stem") if match else name
            siblings = stems[(prefix, stem)]
            key = (f"{prefix}/{stem}" if prefix else stem) if len(siblings) > 1 else label
            self.workloads.setdefault(key, []).append(label)

        self.suites: Dict[str, List[str]] = {}
        for workload in self.workloads:
            self.suites.setdefault(suite_of(workload), []).append(workload)

    def aggregate(self, per_input: Mapping[str, Optional[float]], geometric: bool) -> Dict[str, object]:
        """Combine per-input values: {"workloads": {...}, "suites": {...}, "Avg": value}."""
        workloads = {
            workload: mean((per_input.get(label) for label in labels), geometric)
            for workload, labels in self.workloads.items()
        }
        suites = {
            suite: mean((workloads[w] for w in members), geometric)
            for suite, members in self.suites.items()
        }
        return {
            "workloads": workloads,
            "suites": suites,
            "Avg": mean(suites.values(), geometric),
        }

    def describe(self, geometric: bool) -> str:
        """One line saying how the rows above the inputs were formed."""
        kind = "geomean" if geometric else "arithmetic mean"
        parts = [f"workload = {kind} of its inputs", f"suite = {kind} of its workloads"]
        if len(self.suites) > 1:
            parts.append(f"Avg = {kind} of the {len(self.suites)} suites")
        else:
            parts.append("Avg = the suite")
        return "; ".join(parts) + "."

    def label(self, workload: str) -> str:
        """Display name: the workload, with its input count when it has several."""
        n = len(self.workloads.get(workload, ()))
        return f"{workload} ({n} inputs)" if n > 1 else workload

    def rows(self, per_config: Mapping[str, Mapping[str, Optional[float]]], geometric: bool,
             per_input: bool = False) -> List[Dict[str, object]]:
        """Rows for a table or bar chart, shared so the two cannot disagree.

        per_config maps config -> {input label: value}. Returns one dict per row with
        "level" (input / workload / suite / avg), "key", "label" and "values"
        ({config: value}): the workloads (or, with per_input, the inputs themselves),
        then one row per suite when there are several, then Avg.
        """
        aggregated = {cfg: self.aggregate(values, geometric) for cfg, values in per_config.items()}
        out: List[Dict[str, object]] = []
        if per_input:
            for labels in self.workloads.values():
                for label in labels:
                    out.append({"level": "input", "key": label, "label": label,
                                "values": {cfg: vals.get(label) for cfg, vals in per_config.items()}})
        else:
            for workload in self.workloads:
                out.append({"level": "workload", "key": workload, "label": self.label(workload),
                            "values": {cfg: agg["workloads"][workload] for cfg, agg in aggregated.items()}})
        if len(self.suites) > 1:
            for suite in self.suites:
                out.append({"level": "suite", "key": suite, "label": f"{suite} (suite)",
                            "values": {cfg: agg["suites"][suite] for cfg, agg in aggregated.items()}})
        out.append({"level": "avg", "key": "Avg", "label": "Avg",
                    "values": {cfg: agg["Avg"] for cfg, agg in aggregated.items()}})
        return out
