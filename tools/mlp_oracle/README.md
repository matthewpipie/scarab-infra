# REPL_MLP oracle study

This study measures how much L2 (MLC) replacement could gain from REPL_MLP's marking, and how
well the marking picks the misses that matter. It runs the 12 Google workloads, 35 simpoints, with
50M warmup + 500M measured instructions per simpoint, on golden_small with an LRU MLC unless you
pick another core. The marking, the scarab commit and the core are all yours to choose (see
[Running a study](#running-a-study)).

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
| `report.py` | For each study: the marked share, the ceiling, oracle and control gains per side, REPL_MLP, the variants, and the checks. It also compares several studies. `--match` shows how closely each control matched its oracle, `--miss-reuse` the reuse distances of the misses the oracle and the control force, and `--reuse` those of marked and unmarked lines. |
| `baseline/` | The descriptors of the ratio-marking study already run on pakuchi (`mlp_oracle_google_t3`), so you can report it and borrow its ceilings. |

## Running a study

A study is one scarab commit, one marking and one core. `make_descriptors.py` turns those into the
two descriptors, and checks every flag against the commit before writing anything.

### 1. Pick the scarab commit

`--binary` takes a git ref of the scarab clone, or a `scarab_<hash>`. The descriptors pin the hash
it resolves to, so a study stays reproducible after main moves on.

- **The default, `origin/main`:** the generator fetches the clone's `origin` and uses its newest
  `main`. Once matthewpipie/scarab#36 is merged, that is the oracle with main's current marking, so
  a new study measures the marking REPL_MLP uses today.
- **Until #36 is merged:** main has no oracle and the generator says so. Pass
  `--binary origin/mlp-oracle` (#36's branch: main 03c737d plus the oracle and the reuse stats,
  b30d6c1 as of 11-14).
- **A marking that is new code:** see [New marking code](#new-marking-code).

The commit must have `src/memory/mlp_oracle.cc` and the reuse flags (`--mlp_rd_sticky`,
`--mlp_rd_iprf_reuse`, from b30d6c1 on). The generator checks both and prints the commit it pinned.

`--scarab-path` (default `/home/$USER/carp/scarab-oracle/`) must be a private clone of
matthewpipie/scarab. sci checks the hash out in it in place, which would disturb a checkout you are
working in. sci builds from the clone, so a local branch works without pushing.

### 2. Pick the marking

The reference records, for every MLC miss, the mark the MLC gives its fill: the same mark REPL_MLP
boosts, which the MLC computes under any replacement policy (`mlp_fill_mark` in
`src/memory/memory.c`). Seven flags decide it. `--marking` sets any of them, and the rest keep the
commit's defaults. The generator writes all seven into every config, the reference and REPL_MLP
included, and prints them with the ones you changed. A study's marking is therefore explicit, and
`report.py` prints it, whatever main's defaults become. The defaults below are main 03c737d's (#35
and #42); the generator prints the ones your commit has.

**Data side:** a fill some load waited on is marked if the count `--mlp_data_bound` picks reached
`--mlp_data_bound_min`. Each count runs from when a load merges into the request until the MLC fill.

| Flag | Values | Default |
|---|---|---|
| `--mlp_data_bound` | which count: **0** cycles with no functional unit busy and the ROB not empty (`fu_wait`), once per load waiting on the request; **1** those cycles once per request; **2** cycles nothing retired because the ROB head was a load waiting on this request; **3** those cycles in which no op issued either | 3 |
| `--mlp_data_bound_min` | the count must reach this | 16 |
| `--mlp_lin_data_thresh` | above 0, the load-bound / window ratio must also clear it | 0 |

**Instruction side:** only a fill a demand fetch reached can be marked: one a demand fetch created,
or a prefetch a demand fetch merged into before the fill.

| Flag | Values | Default |
|---|---|---|
| `--mlp_instr_mark` | **0** those whose frontend-bound / window ratio clears `--mlp_lin_instr_thresh`; **1** all of them; **2** all but those fetch was redirected away from while it waited on them | 2 |
| `--mlp_lin_instr_thresh` | mode 0's ratio | 0.7 |
| `--mlp_instr_starve` | which cycles fetch waited count: **0** all; **1** the backend was starved (the ROB had room but got no ops, rename not stalled); **2** as 1, and the ROB head is not a load waiting on memory; **3** the issue queue is empty (ICARUS's critical fetch); **4** the ROB is empty; **5** at most `topdown_fu_exec_few` functional units are busy | 2 |
| `--mlp_instr_starve_min` | fetch must have waited at least this many such cycles | 1 |

Some markings, as `--marking`:

| Marking | `--marking` |
|---|---|
| main's default | (empty) |
| ROB-head stalls whether or not anything issued | `--mlp_data_bound 2` |
| a lower data threshold | `--mlp_data_bound_min 8` |
| every fill a load waited on | `--mlp_data_bound_min 0` |
| the old 0.7 ratio (the baseline study's) | `--mlp_data_bound 0 --mlp_data_bound_min 0 --mlp_lin_data_thresh 0.7` |
| every demand-fetched instruction fill | `--mlp_instr_mark 1 --mlp_instr_starve 0` |
| ICARUS-style critical fetch | `--mlp_instr_starve 3` |

The old 0.7 ratio on b30d6c1 reproduces the baseline's c8bb06d reference byte for byte (all 35
simpoints at 1M + 10M), and lru, the oracle and the control at the same IPC. The ceiling matched on
f17e3f5, which differs from b30d6c1 only in stats.

A marking that changes one side leaves the other side's oracle and control as they were, so a
data-only change can skip the instruction side with `--sides d,b`, and an instruction-only one the
data side with `--sides i,b`. To compare markings, run one study per marking on the same commit and
core, and report them together (step 6).

#### New marking code

The oracle records whatever `mlp_fill_mark` returns, so a marking written there is bounded with no
change to the oracle.
1. Add its flag to a `*.param.def` and use it in `mlp_fill_mark`. Commit it on a branch of the clone
   that has the oracle: main once #36 is in, `mlp-oracle` before.
2. Add the flag to `MARKING_PARAMS` in `make_descriptors.py` and `MARKING_FLAGS` in `report.py`.
   Without that, `--marking` still passes it through, but only as you wrote it, and the report lists
   it with the machine flags.
3. Generate with `--binary <branch>` and `--marking "<new flag> <value>"`.

### 3. Pick the core

The reference and the replays must run the same machine, so everything here reaches every config.

- **`--architecture <preset>`** (default `golden_small`) picks `src/PARAMS.<preset>`. sci reads it
  from the pinned commit, and also wants it in the clone's checkout. Presets on main 03c737d:
  `golden_small` (and its `_2lvl`, `_berti`, `_ic16k`, `_ic64k`, `_incl`, `_incl_1x`, `_nofdip`,
  `_pdip` variants), `golden_large`, `golden_cove`, `sunny_cove`, `kaby_lake`, `cortex_a76`,
  `cortex_m55`. The study has been run on `golden_small` (an exclusive LLC that L2 victims fill) and
  `golden_small_incl` (a non-exclusive LLC that every miss fills). The oracle's forced hits reinstall
  the line through the LLC's own fill path, so on any other preset run the smoke test first.
- **`--params "<flags>"`** adds machine flags, for example `--mlc_size 1048576 --mlc_assoc 8` for a
  1 MB 8-way L2. In Scarab, `mlc_*` is the L2, `l1_*` is the LLC, and `dcache_*` / `icache_*` are
  the L1s. Marking flags belong in `--marking`, and the generator refuses them here.
- **The MLC policy** is LRU in the baseline, the reference, the oracle and the control, and REPL_MLP
  in the `mlp*` configs, whatever the preset says.
- **Ceilings:** a new core changes lru and the ceilings, so pass `--ceilings`. `report.py` warns when
  `--ceilings-from` takes ceilings from a study whose lru IPC differs.
- **L2 associativity** sets how many fills a line survives untouched under LRU, which is the scale
  `--miss-reuse` reads against (16 on golden_small).

### 4. Smoke test (a few minutes)

```
python tools/mlp_oracle/make_descriptors.py --tag dmin8 --marking "--mlp_data_bound_min 8" --check
tools/mlp_oracle/run_study.sh mlp_oracle_check_dmin8
python tools/mlp_oracle/report.py mlp_oracle_check_dmin8
```

Until #36 is merged, add `--binary origin/mlp-oracle` to these and the commands below.

`--check` runs every config on delta core 0 for 1M warmup + 3M measured instructions. At that
length most misses are cold, so the gains say nothing. Check these instead:
- `replay_check` equals lru;
- "Forced, control / oracle" is close to 1;
- the "Marked" share moved the way the new marking should move it.

### 5. The full study

```
python tools/mlp_oracle/make_descriptors.py --tag dmin8 --marking "--mlp_data_bound_min 8" --sides d,b
tools/mlp_oracle/run_study.sh mlp_oracle_google_dmin8
```

By default the replay descriptor holds `lru`, `replay_check`, REPL_MLP at γi 64 with γd 0 and 16,
and an oracle and a control for each side in `--sides`. That is 10 configs and 350 sims, plus the
35-sim reference.

- **Instruction side:** a data-only marking change leaves it as it was, so `--sides d,b` skips it
  (280 sims).
- **Ceilings:** they don't depend on the marking. Leave them out and borrow them from a study on the
  same commit and core (step 6), or add `--ceilings`.
- **Variants:** `--variants` adds the no-reinstall oracle and control and the misses-only control.
- **Control seeds:** `--seeds N` adds N independent control draws. On the baseline's first runs,
  three seeds agreed to within 0.01%, so one is enough at this length.
- **Run length:** `--warmup` and `--measured` change it. The reference must be recorded at the
  same length, which the generator takes care of. Google cores shorter than warmup + measured plus
  some slack are swapped out in `GOOGLE_PICKS`; a longer run needs that list rechecked.

Run it in `tmux` or with `nohup`. A sim takes about 1.3 hours on a quiet machine and longer under
load, so the reference plus two waves of replay runs takes 4 to 8 hours. `run_study.sh` sizes `max_parallel` at each step from idle cores, free memory
(about 4 GB per sim with 300 GB kept free) and your other runs' scarab processes and pool
containers, never more than `MAX_SIMS` (256). It waits while fewer than `MIN_SLOTS` (32) are free.
On 11-11, pakuchi ran out of memory with about 500 sims across two users and stalled for hours,
so don't raise those limits without checking `free -g` first.

If a run is killed, rerun the same command. sci skips the simpoints that already have stats.

### 6. Report

```
python tools/mlp_oracle/report.py tools/mlp_oracle/baseline/mlp_oracle_google_t3.json mlp_oracle_google_dmin8 \
    --ceilings-from tools/mlp_oracle/baseline/mlp_oracle_google_t3.json --workloads --out oracle_report/
```

Gains are geomeans over workloads of each workload's IPC over lru's. A workload's IPC is computed
the way sci does it, so per-workload gains match `./sci --visualize`.

To compare markings, look at:
- **Marked:** the reference's marked misses, as a share of its non-cold misses. A marking that
  marks more gives the oracle more to force, so the oracle's gain alone favors marking everything.
- **Oracle − control:** what the marking's choice of misses is worth, since the control forces as
  many misses on as many lines. This is the number to compare.
- **Oracle against the ceiling:** how much of the possible gain the marked misses hold.
- **REPL_MLP against the oracle:** how much of it a real policy gets.

`--match` adds how closely each control matched its oracle, per simpoint: misses and lines drawn
(exact by construction), and misses forced over the whole run and over the measured region. The
draw is matched over the whole reference, warmup included, so the measured region can hold more or
fewer of the control's misses than of the oracle's.

### Reuse distances

```
python tools/mlp_oracle/report.py mlp_oracle_google_dmin8 --miss-reuse --workloads --out oracle_report/
python tools/mlp_oracle/report.py --reuse mlp_oracle_google_dmin8_ref --workloads --out oracle_report/
```

`--miss-reuse` compares the misses the oracle forces with the misses the control forces. It reads
the record check (`replay_check`), whose `MLP_RDMISS_*` file every miss that lines up with a
non-cold reference miss by its distance: the fills into the line's set since the line's last reuse.
Misses are filed under all, marked (what the oracle forces), and control. The record check replays
the reference forcing nothing, so it sees the reference's own misses. It also draws the control the
`ctrl_*_ro` runs force, since `make_descriptors.py` gives it their `--mlp_oracle_reuse_only 1`. So
the two classes match in misses and lines on every simpoint, by construction, and the report checks
that. Every one of these misses is an LRU miss, so the table shows how far past a 16-way set's reach
each class's misses are. It covers the whole run, warmup included, as the draw and the forcing do.

`--reuse` reads the reference's `MLP_RD1_*` / `MLP_RDN_*`, per side, as log2 histograms of every
reuse's distance (hits included), for marked and unmarked lines. There is no control at this level.
Unmarked instruction lines include the ones FDIP prefetched in time (Scarab's `IPRF` kind).
Unmarked data lines include the ones a store or writeback brought in (`OTHER`).

`make_descriptors.py` gives the reference and the record check
`--mlp_rd_sticky 1 --mlp_rd_iprf_reuse 1`:
- A line counts as marked once any of its fills was, as the oracle forces marked misses.
- An instruction prefetch's lookup is a reuse too, since it renews the line in LRU; without it, FDIP
  lookups make instruction lines look colder than LRU sees them.

Override either flag with `--ref-params`. Each table's header says which definitions the run used.
`--out` also writes `miss_reuse.csv`, `reuse.csv` (per workload and bucket) and
`reuse_summary.json`.

## Baseline: the ratio marking (`mlp_oracle_google_t3`)

Data marking ratio > 0.7 (the default before scarab #35 and #42), instruction
`--mlp_instr_mark 2 --mlp_instr_starve 2`, binary c8bb06d (and 5f06591 for lru, the ceilings and
REPL_MLP, which are identical with the new flag off). It has not been rerun on the current marking.
Its lru and ceilings come from those binaries. Its ceilings apply to a newer study only while
lru is unchanged: `report.py --ceilings-from` compares the two studies' lru IPCs and warns when they
differ. b30d6c1 still matched c8bb06d's lru exactly at 1M + 10M. The numbers are geomean gains over LRU in %.

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

On the data side the ratio marking selected weakly: random reuse misses already got two thirds of
the oracle's gain. That is what the current data marking, and any later one, should be measured
against.

The descriptor also holds 11 superseded configs, run before `--mlp_oracle_reuse_only` existed,
which forced cold misses as well. `report.py` skips them.

## Things that go wrong

- **Oracle above the ceiling:** a sign the oracle forces cold misses. Every oracle and control
  config needs `--mlp_oracle_reuse_only 1`. `report.py` flags any workload where the oracle beats
  the ceiling.
- **`replay_check` differs from lru:** the reference was recorded with a different binary, marking,
  core or length. Regenerate both descriptors together and rerun the reference.
- **`./sci --status` after a rerun:** it counts only the latest run's job logs, so finished
  simpoints from earlier runs show as non-existent. `report.py` and the simpoint directories are
  the truth.
- **`./sci --visualize`'s Avg row:** it is not an average of the per-workload gains. It divides
  the mean of a config's workload IPCs by the mean of lru's, so workloads count by IPC. Before
  matthewpipie/scarab-infra#10, sci also printed each workload's IPC times the summed weight of
  the simpoints that ran. A Google core weighs 1/N, with N from 2 (whiskey) to 89 (merced), so with
  3 cores per workload whiskey, arizona, bravo.a and delta held 79% of the Avg and merced under 1%.
  Per-workload gains are right either way. Use `report.py` for summaries.
- **Short traces:** Google simpoints are whole cores, and a few cores' traces are shorter than
  560M instructions (charlie 0 and 1, merced 2). The generator swaps them for the next cores. A
  longer run needs that list (`GOOGLE_PICKS`) rechecked.
- **Disk:** each simpoint's reference record is 35 to 260 MB, about 4 GB per study, on top of
  about 3 MB per sim.
