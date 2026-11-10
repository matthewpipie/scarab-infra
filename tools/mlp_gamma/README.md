# REPL_MLP gamma sweep

Finds the best instruction and data gammas for Scarab's REPL_MLP L2 policy (`--mlc_cache_repl_policy 9 --mlp_boost_in_fills 1`): `--mlp_lin_instr_lambda` crossed with `--mlp_lin_data_lambda` over {0, 1, 4, 16, 64, 256}, on `golden_small` and `golden_small_incl`.

## Files

| File | What it does |
|---|---|
| `make_descriptors.py` | Writes `json/mlp_gamma_<suite>_<small\|small_incl>.json`, one per suite and architecture, each with the 36 configs `gi<I>_gd<D>`. `gi0_gd0` is plain LRU and is the baseline. `--binary scarab_<hash>` regenerates them for a new build; `--check` writes the smoke test. |
| `run_sweep.sh` | Builds the binary once, then runs `./sci --sim` and `./sci --collect-stats` for each descriptor in turn, so the machine runs one descriptor's `max_parallel` at a time. |
| `report.py` | Reads the collected stats and reports the best config per simpoint, per suite, and across suites. |

## Suites

| Suite | Workloads | Simpoints (top 6) | Instructions per simpoint |
|---|---|---|---|
| google | 12 | 63 | 10M warmup + 50M |
| datacenter | 7 (the memtrace ones) | 36 | 10M warmup + 10M |
| spec2006 | 55 | 294 | 10M warmup + 10M |
| spec2017 | 36 | 168 | 10M warmup + 10M |
| geekbench | 13 | 78 | 10M warmup + 10M |

That is 639 simpoints, or 23,004 simulations per architecture. spec2026 and dcperf have no traces on pakuchi (`./sci --list-traces`); add them to `SUITES` in `make_descriptors.py` once fetched.

## Running

```
python tools/mlp_gamma/make_descriptors.py --check
tools/mlp_gamma/run_sweep.sh --check
python tools/mlp_gamma/report.py --check --marks

tools/mlp_gamma/run_sweep.sh
python tools/mlp_gamma/report.py --all
```

Check idle cores before launching (`uptime` against `nproc`) and lower `max_parallel` in the descriptors if other runs are going.

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
