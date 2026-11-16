# tools/fullsweep

Full-length runs of a finished gamma search (tools/mlp_gsearch) next to the comparison policies. The tool writes
the descriptors and hands them to `cloudhost sweep`, which splits, places, retries, pulls and checks the shards
across the pool. Everything lands in one experiment for `./sci --collect-stats` / `--visualize`.

Run it with the scarabinfra python. The script re-executes itself under that interpreter if needed.

```
tools/fullsweep/fullsweep.py gen cd1/small               # write the descriptors; print sim counts + estimate
tools/fullsweep/fullsweep.py gen cd1/small --dry-run     # the same, writing nothing
tools/fullsweep/fullsweep.py estimate fs_cd1_small --pool h4d=2,n4d=6
tools/fullsweep/fullsweep.py run fs_cd1_small            # the cloudhost commands it would run; --yes runs them
tools/fullsweep/fullsweep.py status fs_cd1_small         # the sweeps and every setup's progress
tools/fullsweep/fullsweep.py collect fs_cd1_small        # one ./sci --collect-stats over everything
tools/fullsweep/fullsweep.py calibrate cd1 dr1           # refresh calibration.json from gsearch runs
tools/fullsweep/plot.py fs_cd1_small                     # paper figure: geomean IPC gain per suite and policy
```

It needs:

- a scarab-infra checkout with sci's multi-runner fix (scarab-infra#22), since every shard writes the one
  experiment
- a cloudhost with `sweep` (`$CLOUDHOST`, default `/home/mgiordan/carp/cloudhost/cloudhost`). The tool also
  imports cloudhost's `split_shards`, so its estimates use the same shards.

## What gen makes

`gen STUDY/VARIANT` reads the gsearch result in `<gsearch_dir>/<study>`: its `study.json`, its templates and
`report/best_*.csv` for that variant. It takes the architecture (`small` → golden_small) and the REPL_MLP base
params (cd1 `--mlc_cache_repl_policy 9`, dr1 `... 18 --mlp_rrip_boost 2`) from the study's templates.
`plans/full_sweep.json` supplies the rest:

| setup | params |
| --- | --- |
| `lru` | baseline, `--mlc_cache_repl_policy 0` |
| `mlp_core` | REPL_MLP at the core unit `all` (one static pair for every suite) |
| `mlp_gd` | core unit `gi=0` (best γd, γi = 0) |
| `mlp_gi` | core unit `gd=0` (best γi, γd = 0) |
| `mlp_sp` | each simpoint's own best pair where gsearch searched it, else its input's best (fallback `input` > `workload` > `core:all`), through `workload_params` (scarab-infra #17) |
| `mlp_wl` | each workload's (benchmark's) best pair for all its inputs; off by default |
| `icarus`, `emissary`, `emissary_r1`, `lacs`, `clip` | policies 21, 24, 24 + `--emissary_rand_denom 1`, 20, 25 with defaults |
| `drrip` | policy 18, off by default (the baseline of DRRIP-boost studies like dr1) |

- **Turning setups on and off:** set `"enabled": false` in the plan, or use `--only lru,mlp_core` / `--skip clip`.
- **Choosing suites:** a suite's `"enabled"` in the plan, or `--suites google` / `--skip-suites spec2006`. Without
  `--name`, a selection other than the plan's default adds the suites to the name (`fs_cd1_small_google`). gen
  refuses to reuse a sweep name that holds a different set of suites (`--force` overrides it until something has
  launched).
- **Adding setups later:** rerun gen with them selected, then `run`. It starts sweeps of just the new setups
  (`json/<name>_<group>_b2.json`). A setup that already ran can't change what it runs (params, binary, simpoints);
  gen refuses, so give the changed sweep a new `--name`.
- **mlp gamma sources:** `core:all`, `core:gi=0`, `core:gd=0`, `simpoint`, `input`, `workload`, `suite`, or a
  literal `[gi, gd]`.
  - **Fallback:** a unit gsearch never searched takes the first level of its `fallback` list that has one. The
    list ends in a core unit. Default: `input, workload, core:all` for `simpoint`, `core:all` otherwise.
  - **Per-simpoint coverage:** gsearch searched google's top 6 and the other suites' top 3 simpoints. The sweep
    runs google's top 24 and 90% coverage elsewhere, so only 612 of cd1/small's 1,638 sweep simpoints have their
    own optimum. They hold 67% of an input's weight on average. The other 1,026 run their input's best.
  - **What gen prints:** how many simpoints each level supplied.
- **Which gsearch point:** `pick` is `best` (the converged incumbent, the report's headline) or `max_point` (best
  measured).
- **Run settings (FULL_SWEEP.txt):**
  - google: top 24 simpoints, 50M warmup + 200M measured (`--inst_limit 250000000` through a `google`
    workload_params entry).
  - datacenter (memtrace inputs) and SPEC 2006/2017/2026: 90% `simpoint_coverage` (#16), with the trace's own
    warmup (10M / 50M) + 10M.
- **Binary:** `HEAD` of `scarab_path`, pinned to `scarab_<hash>` at gen time.

Output (every descriptor has experiment `<name>`):

- **One descriptor per suite group:** `json/<name>_google.json` and `json/<name>_rest.json`, each with every setup
  on that group's suites. Each runs as one `cloudhost sweep`. The groups differ in `traces_dir` and memory per sim,
  which are per sweep in cloudhost.
- **Merged descriptor:** `json/<name>.json`, with every setup on every suite. It is for `--collect-stats` /
  `--visualize` / `--perf-analyze` only. **Never `--sim` it.**
- **State:** `<state_dir>/<name>/state.json` records the setups, the sweeps started and what each setup ran.
  `logs/` holds the build and cloudhost output.

## Groups and shards

Each group in the plan sets:

- `traces_dir`
- `shard_size`: cloudhost's `--shard-size`. cloudhost sorts each suite's simpoints by estimated runtime and cuts
  them into shards of every setup × (shard_size // #setups) simpoints, starting a new shard where the cost halves.
  So 128 with 10 setups gives shards of 10 × 12 = 120 sims, and fewer at the cost breaks.
- `mem_per_sim_gb`: `--mem-per-sim`, one reservation for every sim of the group's sweep. Both groups leave it null:
  cloudhost then sizes each shard from `sim_cost.json`, at its workloads' measured peak +15% and at least 3.8 GB.
  It scales those peaks by 0.58 for binaries that contain scarab PR #61 (138ae8d), so every shard of a 138ae8d+
  sweep gets 3.8 GB (185 slots on an H4D).
  - **PR #61** cut peak RSS ~43%. At 60M, old → new: merced 3.18 → 1.80 GB, sierra.a.4 3.43 → 1.97, whiskey
    2.52 → 1.44, xgboost 3311 4.47 → 2.57.
  - **Before PR #61** (`ebc4e03`, VmHWM of running sims, 11-16): google at 150M had charlie up to 5.2 GB; merced,
    sierra.a.3, sierra.a.4 and tahoe 4.3–4.5; the rest under 3.1. merced and sierra.a.4 reached 4.7–4.8 GB at 250M.
    RSS grows through a run. SPEC median 0.07 GB, max 0.54 (gcc_9); datacenter xgboost about 4.5.
  - **Scaled to 250M on PR #61**, the heaviest (charlie) should be about 3.3 GB, under the 3.8 floor. This is
    an extrapolation; no 250M google sim has run on 138ae8d yet.
  - Set `mem_per_sim_gb` only to override cloudhost, e.g. for a binary older than PR #61 that `sim_cost.json`
    gets wrong.
- `where`: google runs on the cloud only.
- `slow`: the group's shards go first and prefer the fastest machines (`--slow-suites`).
- `max_parallel`: used only if a shard runs on pakuchi. A cloud shard runs as many sims as its slice.

cd1/small has 16,380 sims:

- google: 1,920 sims in 17 shards of 10–120. One 120-sim shard fills most of an H4D's 185 slots.
- rest: 14,460 sims in 84 shards of 20–180.

## Plotting

`plot.py <name>` reads the collected experiment and writes a paper figure, plus a `.csv` of every plotted value. The
figure shows the nested geomean IPC gain over LRU, as sci computes it, per suite and policy. Policies are named and
ordered as the paper does (`NAMES`, `DEFAULT_CONFIGS`): Catalyst (REPL_MLP), Catalyst per-SP, D-only, I-only, then DRRIP, SHiP++,
Mockingjay, LACS, CLIP, EMISSARY and ICARUS. Mockingjay is `mockingjay_p1`, with no prefetch penalty.

```
tools/fullsweep/plot.py fs_cd1_small --by suite --ymax 6 --out gain_by_suite.png   # a group per suite, a bar per policy
tools/fullsweep/plot.py fs_cd1_small --out gain_by_policy.png                      # a group per policy, a bar per suite
```

- **Styles:** Catalyst is the dark solid bar. Every other policy has a texture on a pale tint. Textures repeat 7
  bars apart. Each policy and suite keeps one tint in every figure (`TINT_OF`): Catalyst's ablations share its blue,
  and variants of one policy share its tint. Bars that share a texture differ in color.
- **`--ymax`** cuts the y axis. A cut bar gets a break mark and its value standing on top, and the value also goes to stderr
  and to the CSV's `cut` column.
- **Output:** run details go to stderr and the gain table to stdout.

## Counts and estimates

gen (and `estimate`) prints:

- simpoints per suite, from sci's own selection code
- simulations per setup (simpoints × setups)
- H4D core-hours
- cloudhost's shards per group
- an end-to-end estimate on `--pool`: hours, VM-hours and dollars

The pool is assumed to be ours alone. Pass the machines you can actually get, e.g. `--pool h4d=2,n4d=6`.
`pakuchi=N` (cores) takes only shards of `pakuchi_suites` (traces on pakuchi: google, datacenter), and google is
cloud-only.

**How the estimate works.** It is an event model of cloudhost:

- Every group's sweep runs at once, slow ones first.
- Each sweep has at most 4 launches in flight, 2 min each (gsearch's launches took 1.5–3 min).
- Shards are cloudhost's own (`split_shards`, with its per-shard memory and long-shard marks). They are placed
  where cloudhost's `place` would put them. Each takes a slice of min(sims, the machine's slots) cores, by cores
  and by the shard's memory per sim, or a partial slice of at least 32 cores.
- Sims start 3 min after placement, longest first.
- Each core goes back to the machine as its last sim ends (tail release).

The cost model is `calibration.json`, written by `calibrate` from `SIM_HOST_WALL_SECONDS` of every finished
gsearch cd1/dr1 run:

- **Per input:** mean H4D wall minutes per million instructions at full load.
- **Per suite:** N4D and pakuchi slowdowns.
- **Inputs without data:** the fallback per-suite rates in the script.

It predates scarab #58 (8–10% less wall time), so it runs a little high on newer binaries.

**The shard sizes** are the best of a grid (google 64–190 × rest 64–185) under 1, 2 and 3 min launches, on
5 H4D + 10 N4D and on 2 H4D + 6 N4D. The grid uses cloudhost's cost-aware split (cloudhost 55ad159) and
PR #61 memory (scarab_138ae8d). The table shows the worst and mean end-to-end time over those six cases, as a
multiple of the estimate's own floor (perfect packing, no launch or tail: 1.0 h and 2.1 h):

| google | rest | worst | mean | $ |
| --- | --- | --- | --- | --- |
| 128 | 185 | 1.78× | 1.63× | 134–152 |
| 64 | 185 | 1.84× | 1.67× | 135–151 |
| 128 | 128 | 1.93× | 1.67× | 137–169 |
| 64 | 96 | 2.37× | 1.78× | 141–174 |
| any | 64 | ≥ 3.19× | ≥ 2.12× | |

google 190 (19 simpoints × 10 setups) does about as well (1.77×), but 190 sims don't fit an H4D's 185 slots.

On the full pool the estimate is about 1.8 h and $151; on 2 H4D + 6 N4D, 3.2 h and $135.

- **The floor is core-bound.** With PR #61 every sim reserves 3.8 GB, so an H4D's memory no longer limits it.
- **Small rest shards are launch-bound.** Each sweep launches at most 4 shards at a time, so 163 shards at 3 min
  a launch take about 2 h on their own.
- **The rest of the gap** is the model's tail and cloudhost's partial slices.

Recalibrate `LAUNCH_MIN` and `START_OVERHEAD_MIN` from the first real sweep's `driver.log`.

## run

`run NAME` prints the cloudhost commands and the estimate. `run NAME --yes` runs them:

1. **Build once.** `./sci --build-scarab` builds each binary here, so cloud machines get the cached build instead
   of each building it.
2. **One sweep per group**, slow groups first:

   ```
   cloudhost sweep start <name>_<group> --infra <this checkout> --shard-size N --where W
       --who fullsweep-<name> --detach [--mem-per-sim G] --slow-suites [suites]
   ```

   The sweep id goes into `state.json`. From then on cloudhost's driver runs the sweep: placement, retries
   (3 attempts, back on the same host), pulls, `./sci --status` per shard and a final check.
3. **Rerunning `run --yes`:**
   - It starts sweeps for setups added since.
   - It resumes sweeps whose driver is gone short of `complete` (`cloudhost sweep resume <id> --detach`).
     `--retry-failed` gives failed shards new attempts.
   - Running and complete sweeps are left alone.

`status NAME` reads cloudhost's sweep state:

- each sweep's state, its shard counts and its driver
- the hosts its running shards are on
- sims done per setup

Use `cloudhost sweep status <id>` for every shard. Driver logs are in
`/mnt/storage/mgiordan/cloudhost/sweeps/<id>/driver.log`.

**collect.** Each sweep collects its own group when it finishes. Both groups write the experiment's one
`collected_stats.csv`, so the last one to finish overwrites it. Once every sweep is complete, `collect NAME` runs
`./sci --collect-stats NAME` with the merged descriptor, which covers every setup and suite. `--partial` collects
just the setups whose sims are all done (through `collect_stats.configs`).

## Tests

`~/miniconda3/envs/scarabinfra/bin/python tools/fullsweep/test_fullsweep.py` (offline, about 12 s). They check:

- **Counts:** cd1/small expands to 10 × 1,638 sims with sci's selection code.
- **Group descriptors:** they partition the merged descriptor and share its experiment.
- **cloudhost's view:** cloudhost's `plan_run.py` accepts both descriptors and lists exactly the simpoints the
  estimate uses. Its `split_shards` covers every sim once.
- **Params** (through sci's `resolve_params`): every sim's run length, policy and gammas. mlp_sp gammas match
  `best_simpoint.csv`, or `best_input.csv` for the 1,026 fallbacks, and mlp_wl gammas match `best_workload.csv`.
- **Suite selection and toggles.**
- **Frozen setups:** a setup that already ran can't change; an added setup gets its own sweep.
- **The event model.**
- **run / status / collect** against stub cloudhost and sci scripts:
  - sweep commands and flags
  - no relaunch
  - batches of added setups
  - resume of a dead driver
  - collect waits for every setup, and `--partial` works
