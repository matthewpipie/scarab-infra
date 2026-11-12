# REPL_MLP oracle study

This study measures how much L2 (MLC) replacement could gain from REPL_MLP's marking, and how
well the marking picks the misses that matter. It runs on golden_small (LRU MLC), with the 12
Google workloads, 35 simpoints, and 50M warmup + 500M measured instructions per simpoint.

| Config | What it forces to hit | What it tells you |
|---|---|---|
| `lru` | nothing | the baseline |
| `ceiling_<s>` | every miss to a line the MLC has held before | the most any replacement policy could gain |
| `oracle_<s>_ro` | the misses the marking marked critical | what the marking would be worth if protecting those misses cost nothing |
| `ctrl_<s>_ro` | as many random misses, on as many lines | what forcing that many misses gains with no selection at all |
| `mlp<γi>_<γd>` | nothing (the real REPL_MLP policy) | how much of the oracle a real policy captures |
| `replay_check` | nothing, while replaying the reference | must equal `lru`; confirms the reference matches these runs |

`<s>` is the side: `i` instruction misses, `d` data misses, `b` both. Oracle and control never
force a line's first miss (`--mlp_oracle_reuse_only 1`). The MLC starts empty, so a first miss is
cold and no replacement policy can turn it into a hit. Forced hits reinstall the line in the MLC,
so the cache contents stay as they are under LRU and only the miss latency goes away.

A miss's mark is known only once it comes back, so the study runs in two passes. The reference
(`mlp_oracle_google_<tag>_ref`, one config named `track`) runs LRU and records every MLC miss and its
mark in `mlp_oracle_misses.0.bin` in each simpoint directory. The replay runs
(`mlp_oracle_google_<tag>`) read that record and force the chosen misses as they line up with it.
The marking flags therefore matter in the reference. The generator passes them to every config
anyway, so REPL_MLP runs with the same marking.

## Files

| File | What it does |
|---|---|
| `make_descriptors.py` | Writes `json/mlp_oracle_google_<tag>_ref.json` and `json/mlp_oracle_google_<tag>.json` for one marking. |
| `run_study.sh` | Builds the binary, runs the reference, checks that every simpoint recorded, runs the replay configs, and collects stats. Each step waits for room on the machine. |
| `report.py` | For each study: the marked share, the ceiling, oracle and control gains per side, REPL_MLP, the variants, and the checks. It also compares several studies. |
| `baseline/` | The descriptors of the default-marking study already run on pakuchi (`mlp_oracle_google_t3`), so you can report it and borrow its ceilings. |

## Rerunning with a different marking

### 1. Get a binary with the marking

- **The marking is a flag that already exists** (for example a different `--mlp_lin_data_thresh`):
  use the default binary, `scarab_c8bb06d`. It is the oracle code (matthewpipie/scarab PR #36,
  branch `mlp-oracle`) on main as of f1fce3c. Its data marking is the 0.7 ratio and its
  instruction marking is `--mlp_instr_mark 2 --mlp_instr_starve 2`.
- **The marking is new code** (for example scarab PR #35's ROB-head stall count): commit a branch
  that merges both, and pass `--binary scarab_<hash>`. For #35 that is
  `--marking "--mlp_data_bound 2 --mlp_data_bound_min 16 --mlp_lin_data_thresh 0"`. Once both are
  on main, any later main hash works.

`scarab_path` (`--scarab-path`, default `/home/$USER/carp/scarab-oracle/`) must be a private clone
that has the commit. sci checks hashes out in it in place, which would disturb a checkout you are
working in.

Pass every flag of the marking you want, not just the ones you change. PR #35 changes the default
data marking, so "the defaults" mean different things on different builds.

### 2. Smoke test (a few minutes)

```
python tools/mlp_oracle/make_descriptors.py --tag dthresh05 --marking "--mlp_lin_data_thresh 0.5" --check
tools/mlp_oracle/run_study.sh mlp_oracle_check_dthresh05
python tools/mlp_oracle/report.py mlp_oracle_check_dthresh05
```

`--check` runs every config on delta core 0 for 1M warmup + 3M measured instructions. At that
length most misses are cold, so the gains say nothing. Check these instead:
- `replay_check` equals lru;
- "Forced, control / oracle" is close to 1;
- the "Marked" share moved the way the new marking should move it.

### 3. The full study

```
python tools/mlp_oracle/make_descriptors.py --tag dthresh05 --marking "--mlp_lin_data_thresh 0.5" --sides d,b
tools/mlp_oracle/run_study.sh mlp_oracle_google_dthresh05
```

By default the replay descriptor holds `lru`, `replay_check`, REPL_MLP at γi 64 with γd 0 and 16,
and an oracle and a control for each side in `--sides`. That is 10 configs and 350 sims, plus the
35-sim reference.

- **Instruction side:** a data-only marking change leaves it as it was, so `--sides d,b` skips it
  (280 sims).
- **Ceilings:** they don't depend on the marking. Leave them out and borrow them from the baseline
  (step 4), or add `--ceilings`.
- **Variants:** `--variants` adds the no-reinstall oracle and control and the misses-only control.
- **Control seeds:** `--seeds N` adds N independent control draws. On the baseline's first runs,
  three seeds agreed to within 0.01%, so one is enough at this length.
- **Run length:** `--warmup` and `--measured` change it. The reference must be recorded at the
  same length, which the generator takes care of.

Run it in `tmux` or with `nohup`. A sim takes about 1.3 hours on a quiet machine and longer under
load, so the reference plus two waves of replay runs takes 4 to 8 hours. `run_study.sh` sizes `max_parallel` at each step from idle cores, free memory
(about 4 GB per sim with 300 GB kept free) and your other runs' scarab processes and pool
containers, never more than `MAX_SIMS` (256). It waits while fewer than `MIN_SLOTS` (32) are free.
On 11-11, pakuchi ran out of memory with about 500 sims across two users and stalled for hours,
so don't raise those limits without checking `free -g` first.

If a run is killed, rerun the same command. sci skips the simpoints that already have stats.

### 4. Report

```
python tools/mlp_oracle/report.py tools/mlp_oracle/baseline/mlp_oracle_google_t3.json mlp_oracle_google_dthresh05 \
    --ceilings-from tools/mlp_oracle/baseline/mlp_oracle_google_t3.json --workloads --out oracle_report/
```

Gains are geomeans over workloads of each workload's IPC over lru's. A workload's IPC is computed
the way sci does it, so per-workload numbers match `./sci --visualize`.

To compare markings, look at:
- **Marked:** the reference's marked misses, as a share of its non-cold misses. A marking that
  marks more gives the oracle more to force, so the oracle's gain alone favors marking everything.
- **Oracle − control:** what the marking's choice of misses is worth, since the control forces as
  many misses on as many lines. This is the number to compare.
- **Oracle against the ceiling:** how much of the possible gain the marked misses hold.
- **REPL_MLP against the oracle:** how much of it a real policy gets.

## Baseline: the default marking (`mlp_oracle_google_t3`)

Data marking ratio > 0.7, instruction `--mlp_instr_mark 2 --mlp_instr_starve 2`, binary c8bb06d
(and 5f06591 for lru, the ceilings and REPL_MLP, which are identical with the new flag off).
The numbers are geomean gains over LRU in %.

| Side | Marked | Ceiling | Oracle | Control | Oracle − control |
|---|---|---|---|---|---|
| instruction | 11.4% | +8.38 | +4.12 | +0.91 | +3.20 |
| data | 43.9% | +24.42 | +13.64 | +9.02 | +4.61 |
| both | 24.5% | +41.80 | +18.98 | +10.15 | +8.84 |

- **REPL_MLP:** γi 64, γd 0 gains +2.60; γi 64, γd 16 gains +2.78.
- **Variants, both sides:**
  - No-reinstall oracle: +18.26.
  - No-reinstall control: +11.68.
  - Control matched on misses only: +8.85.
- **Replay check:** `replay_check` equals lru on all 35 simpoints.

On the data side the marking selects weakly: random reuse misses already get two thirds of the
oracle's gain. That is what a different data marking would try to improve.

The descriptor also holds 11 superseded configs, run before `--mlp_oracle_reuse_only` existed,
which forced cold misses as well. `report.py` skips them.

## Things that go wrong

- **Oracle above the ceiling:** a sign the oracle forces cold misses. Every oracle and control
  config needs `--mlp_oracle_reuse_only 1`. `report.py` flags any workload where the oracle beats
  the ceiling.
- **`replay_check` differs from lru:** the reference was recorded with a different binary, marking
  or length. Regenerate both descriptors together and rerun the reference.
- **`./sci --status` after a rerun:** it counts only the latest run's job logs, so finished
  simpoints from earlier runs show as non-existent. `report.py` and the simpoint directories are
  the truth.
- **`./sci --visualize`'s Avg row:** it is neither a geomean nor a plain average. sci scales each
  workload's IPC by the summed weight of the simpoints that ran, so whiskey, with both of its
  cores in the run, counts double against workloads with 3 of 6. Use `report.py` for summaries.
- **Short traces:** Google simpoints are whole cores, and a few cores' traces are shorter than
  560M instructions (charlie 0 and 1, merced 2). The generator swaps them for the next cores. A
  longer run needs that list (`GOOGLE_PICKS`) rechecked.
- **Disk:** each simpoint's reference record is 35 to 260 MB, about 4 GB per study, on top of
  about 3 MB per sim.
