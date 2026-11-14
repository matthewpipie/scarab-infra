# REPL_MLP gamma sweep

Finds the best instruction and data gammas for Scarab's REPL_MLP L2 policy (`--mlc_cache_repl_policy 9 --mlp_boost_in_fills 1`): `--mlp_lin_instr_lambda` crossed with `--mlp_lin_data_lambda` over {0, 1, 4, 16, 64, 256}, on `golden_small` and `golden_small_incl`.

## Files

| File | What it does |
|---|---|
| `make_descriptors.py` | Writes `json/mlp_gamma_<suite>_<small\|small_incl>.json`, one per suite and architecture, each with the 36 configs `gi<I>_gd<D>`. `gi0_gd0` is plain LRU and is the baseline. `--binary scarab_<hash>` regenerates them for a new build; `--check` writes the smoke test. |
| `run_sweep.sh` | Builds the binary once, then runs `./sci --sim` and `./sci --collect-stats` for each descriptor in turn, so the machine runs one descriptor's `max_parallel` at a time. |
| `report.py` | Reads the collected stats and reports the best config per simpoint, per suite, and across suites. |
| `prune.py` | Splits a sweep so that simpoints instruction γ does not move run only γi=0 and one probe config; `report.py` fills in their other γi>0 cells. See [Skipping γi runs](#skipping-γi-runs-prunepy). |

## Suites

| Suite | Workloads | Simpoints (top 6) | Instructions per simpoint |
|---|---|---|---|
| google | 12 | 63 | 10M warmup + 50M |
| datacenter | 7 (the memtrace ones) | 36 | 10M warmup + 10M |
| spec2006 | 55 | 294 | 10M warmup + 10M |
| spec2017 | 36 | 168 | 10M warmup + 10M |
| geekbench | 13 | 78 | 10M warmup + 10M |
| spec2026 | 92 | 532 | 10M warmup + 10M |

The first five suites are 639 simpoints, or 23,004 simulations per architecture; spec2026 adds 19,152 more. spec2026's traces came from `trace_home=/mnt/storage/traces/scarab ./sci --fetch-traces spec2026`. dcperf is also downloadable but not in the sweep; add it to `SUITES` in `make_descriptors.py` after fetching it the same way.

Output goes to `/mnt/storage/mgiordan/simulations/<experiment>` (`root_dir` in the descriptors, `--root-dir` in `make_descriptors.py`). The sweep writes about 3 MB per simulation plus a copy of the binary per experiment, about 150 GB for the first five suites, which the root disk holding `/home` cannot spare.

## Running

```
python tools/mlp_gamma/make_descriptors.py --check
tools/mlp_gamma/run_sweep.sh --check
python tools/mlp_gamma/report.py --check --marks

tools/mlp_gamma/run_sweep.sh
tools/mlp_gamma/run_sweep.sh mlp_gamma_spec2026_small mlp_gamma_spec2026_small_incl
python tools/mlp_gamma/report.py --all
```

`run_sweep.sh` with no arguments runs the first five suites.

Check idle cores before launching (`uptime` against `nproc`) and lower `max_parallel` in the descriptors if other runs are going.

The descriptors set `container_reuse`, so each of the 256 slots runs its sims in one long-lived container. Without it, sci creates and removes a container per sim, and on pakuchi those docker operations queue on the storage drive: google_small took 130 minutes for 71 minutes' worth of simulation at 256 slots. It needs a scarab-infra with `container_reuse` support; an older one ignores the key.

## Report

For each architecture and suite (`--group subsuite` splits SPEC int and fp), `report.py` prints:

- the 6x6 grid of geomean IPC gain over `gi0_gd0`, best cell in bold;
- the best static config, how many workloads it helps or hurts by more than 0.5%, and its worst and best workload;
- two oracles: the best config chosen per workload, and per simpoint;
- how often each config is the best for a simpoint.

Across suites, it weights every suite equally (the geomean of suite geomeans), reports the best config, and shows what that config costs each suite against the suite's own best. It also reports which config wins if all workloads are pooled instead.

A simpoint's IPC is measured instructions over measured cycles. A workload's IPC is the weighted average of its simpoints' IPC, with weights renormalized over the simpoints that completed under every config; the rest are dropped and listed. Gains are geomeans over workloads of IPC over the baseline's IPC.

`--out` (default `mlp_gamma_report/`) gets `summary.md` and four CSVs:
- `per_simpoint.csv`: each simpoint's best config and IPC, next to the baseline's and the suite-best and cross-suite-best configs' IPC;
- `per_workload.csv`: the same per workload, as gains;
- `grid.csv`: every grid cell;
- `ipc_long.csv`: every config and simpoint.

`--show-simpoints` prints the per-simpoint table; `--marks` prints the mean number of lines marked per config.

## Skipping γi runs (`prune.py`)

Most SPEC2006, SPEC2017 and Geekbench simpoints barely respond to instruction γ: they miss in the L2 on few instruction lines. `prune.py` splits a sweep descriptor `SRC` in two:

- **`SRC_gi0`:** every simpoint, under the γi=0 configs, any non-REPL_MLP config such as `lru`, and one probe config with γi>0. The default probe is the grid's corner: the largest γi, then the largest γd.
- **`SRC_gi`:** only the simpoints whose IPC the probe moved by more than `--tol` percent (default 0.2), under the other γi>0 configs.

`report.py SRC_gi` reads both as one sweep. For a simpoint that `SRC_gi` leaves out, each of its γi>0 cells is taken from the γi=0 config with the same params otherwise: the same γd, binary and other flags. Every γi>0 config needs such a partner, or `prune.py` refuses. The report notes how many simpoints were filled this way. With `--all`, it uses the pruned pair in place of `SRC`.

```
python tools/mlp_gamma/prune.py column mlp_gamma_spec2006_small      # writes json/mlp_gamma_spec2006_small_gi0.json
tools/mlp_gamma/run_sweep.sh mlp_gamma_spec2006_small_gi0
python tools/mlp_gamma/prune.py rest mlp_gamma_spec2006_small        # writes ..._gi.json from the probe's stats
tools/mlp_gamma/run_sweep.sh mlp_gamma_spec2006_small_gi
python tools/mlp_gamma/report.py mlp_gamma_spec2006_small_gi
```

- **`--list`:** `rest --list` prints each simpoint's probe change and whether it runs.
- **`--like`:** `column --probe none` with `rest --like REF ... --tol 0.1` decides from an earlier sweep instead. A simpoint is left out when its IPC moved by at most `--tol` percent between γi=0 and every γi>0 at the same γd there; simpoints the references lack are run.
  - Both halves can launch at once.
  - **Caveat:** it cannot see what a new binary changes. sphinx3 simpoint 311317 moved at most 0.05% across γi in the fill-counted sweep, but its gi64_gd16 run took 7.7% fewer cycles than gi0_gd16 with countdown aging (`mlp_revive_sphinx3_sweep`, `scarab_dc523f4`).

### Accuracy and savings (golden_small, `scarab_f1fce3c`)

These are end-to-end runs of `prune.py` and `report.py` on the existing sweep's stats, compared with the full report.

| Rule | Simulations skipped | Instructions skipped | Equal-suite grid error, max | Any suite, max | Best config |
|---|---|---|---|---|---|
| corner probe (gi256_gd256), `--tol 0.2` | 66% | 55% | 0.004 pt | 0.009 pt | same across suites and in each suite, except SPEC2006, whose two top cells swap (gi4_gd16 +0.192%, gi1_gd16 +0.190%) |
| `--like` golden_small_incl, `--tol 0.1` | 53% | 45% | 0.002 pt | 0.007 pt | same across suites and in each suite |

- **Where the skips come from:** the probe rule leaves out 282 of 294 SPEC2006 simpoints, 148 of 168 SPEC2017, 75 of 78 Geekbench, 14 of 36 Datacenter and 2 of 63 Google.
- **Instructions vs simulations:** Google runs 60M instructions per simulation against 20M elsewhere, so the instruction saving is smaller than the simulation saving.
- **Other grids:** the corner probe was also tried on sub-grids of the sweep: {0,4,16,64}, {0,16,64}, {0,1,4,16} and {0,64,256}, on both architectures. At `--tol 0.2`, it kept the equal-suite grid within 0.012 pt and every suite within 0.035 pt, with the same best config.
- **Smaller grids save less:** each skipped simpoint saves fewer γi>0 configs. On {0,16,64}, it skips 46% of simulations.
- **Probe choice:** a probe at γd 16 or γd 0 predicted the grid much worse, with errors up to 0.10 pt on the equal-suite grid and 0.26 pt in a suite. Adding gi256_gd0 as a second probe gained almost nothing.

### Why not count instruction-marked evictions

A simpoint whose γi=0 runs never evict an instruction-marked line would run identically at any γi, since γi only raises those lines' priority. That would be an exact rule. Scarab has had the stat for it (`MLP_EVICT_IMARK`) since 19097a1.

A check run tested it. Its descriptors, `mlp_prune_verify_{google,rest}`, are not committed.
- **What ran:** the 30 `tools/mlp_rep` simpoints plus sphinx3:311317, on `scarab_d91e24d`, under gi0/gi64 at γd 0 and gi0/gi64/gi256 at γd 16, with countdown and fill-counted aging.
- **No simpoint qualified:** every one evicted at least 0.2 instruction-marked lines per million instructions at γi=0.
- **The count doesn't separate them either, below about 100 per million:** simpoints at 0.2–100 per million moved by at most 0.4% at γi>0, with mixed signs, and some were cycle-identical anyway. The real responders were all above 150 per million: the six Google simpoints moved 0.3–4.3%, verilator 47%.
