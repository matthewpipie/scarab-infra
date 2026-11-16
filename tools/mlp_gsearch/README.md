# Adaptive REPL_MLP γ search

`gsearch.py` finds the best instruction and data gammas (`--mlp_lin_instr_lambda`, `--mlp_lin_data_lambda`) for each simpoint, and keeps enough runs to give the best for each workload and suite too; or, with `levels: ["core"]`, only the best static settings for sci's `Avg`. It runs a few rounds of sims through the cloudhost pool (as `cloudhost sweep`s) instead of a full grid. Each round's runs are decided from the previous rounds' results.

## Files

| File | What it does |
|---|---|
| `gsearch.py` | The CLI: plans rounds, writes round descriptors, runs each as a `cloudhost sweep`, ingests the stats, reports. |
| `search.py` | The decision rules. No I/O; `test_search.py` runs them against fake IPC surfaces. |
| `studies/*.json` | Study files: search settings and the template descriptors to use. |
| `test_search.py` | Offline tests. `--replay DIR/ipc_long.csv` also replays a real sweep (`tools/mlp_gamma/report.py --out DIR`). |

## Inputs

**Template descriptors** (`json/gs_tmpl_*.json`) hold every run setting. Each is an ordinary sci descriptor with exactly one configuration, the base, which holds the binary (`scarab_<githash>`) and the base params. The base params pick the policy the gammas apply to, and leave the gammas unset:
- REPL_MLP: `--mlc_cache_repl_policy 9`. γ is in fills (countdown) or stack positions.
- An RRIP policy with `--mlp_rrip_boost`: e.g. `--mlc_cache_repl_policy 18 --mlp_rrip_boost 2`, DRRIP with γ in aging steps.

gi0_gd0 is the base policy with no boost. A config is a search point if its params set both `--mlp_lin_instr_lambda` and `--mlp_lin_data_lambda`. The template also sets:
- `architecture`, `scarab_path`, `root_dir` and `traces_dir`
- `top_simpoint`
- `simulations`, which pick suites and workloads, with their warmup
- in the base params, any instruction limit (Google: `--inst_limit 250000000` with warmup 50000000 is 50M warmup + 200M measured; `dr2` uses 150000000, 50M + 100M)

To search a new architecture, suite, run length or flag set, write a template. Templates whose suites need different `traces_dir`, `top_simpoint` or instruction limits must be separate, as in sci.

**The study file** (`studies/<name>.json`) holds only the search:

| Key | Default | Meaning |
|---|---|---|
| `name` | | Letters and digits; part of every round descriptor's name. |
| `variants` | | `{label: {tag: template}}`. Each variant is an independent search: its own optima, and its own cross-suite view. `cd1.json` has `small` and `small_incl`. |
| `step` | 12 | Grid step for both gammas, or `[γi step, γd step]`. The right step depends on γ's unit: `dr1` (DRRIP, aging steps) uses `[4, 1]`. |
| `init` | `{"0": [0,12,24,36,48], "96": [0,24,48]}` | Round 1's points, as rows keyed by γi. The largest γi row doubles as the γi probe. |
| `init_extra` | `{}` | Extra round-1 points for one template's simpoints: `{tag: {γi: [γd, ...]}}`, added to `init`. A wide coarse grid for a suite whose optimum lies far from the default start saves the rounds a step-by-step climb would take, and costs only that suite. It can also find an optimum the default probe misses, by making a simpoint look γi-insensitive. `dr1` gives google `{"48": [8, 16], "64": [4, 8, 12, 16], "96": [8, 16], "128": [8, 16]}`, because round 1 put 50 of 63 google simpoints at the (32, 8) corner. |
| `lookahead` | 2 | Steps to run in a direction the search is still climbing. |
| `max_lookahead` | 8 | The horizon doubles while a unit keeps climbing the same way, from `lookahead` to 2×, 4× … up to this many steps. Every step out to the horizon runs, so nothing on the path is skipped. |
| `core` | false | Also search the three core units (see the rules): sci's `Avg` over the variant's suites, at its best static (γi, γd), along γi with γd = 0, and along γd with γi = 0. `cd1` and `dr1` turned it on in a later round. |
| `levels` | every level | The unit levels searched, a subset of `simpoint`, `input`, `workload`, `suite`, `core`. Without it, every level is searched (core only with `core` on). `["core"]` searches only the three core units: each round runs just their needs, on every simpoint, and the report still shows each suite's gain at every point measured. `dr2` does this, started at `dr1`'s core optima. |
| `jump` | `ladder` | Where an incumbent jumps when a point measured off its climb beats it by `min_gain_pp`. `ladder` takes the smallest-γ such point, and repeats, so the incumbent ends within `min_gain_pp` of the best measured point. A jump doesn't count as a climb step for the lookahead. `max` jumps straight to the best measured point. Then a frontier that creeps up by less than `min_gain_pp` per step drags the incumbent, and its needs, along every round: cd1's golden_small_incl Google suite crept from γi 180 to 240 at +0.01 pp per step before `cd1` switched to `ladder` in round 19. |
| `min_gain_pp` | simpoint 0.1, workload 0.1, suite 0.05, core 0.05 | A point counts as better only if its gain over gi0_gd0 beats the incumbent's by this many percentage points. |
| `partial_gain` | 0.5 | If a neighbor beats the incumbent by at least this fraction of `min_gain_pp`, the point one step further also runs. |
| `gi_sensitive_pct` | 0.2 | A simpoint is γi-insensitive if no γi>0 run moves its IPC by more than this from γi=0 at the same γd. |
| `max_gamma` | [192, 192] | Upper bounds for γi and γd. `cd1` started at 192 and was raised to 384 when simpoints reached 192 still improving. |
| `max_rounds` | 5 | |
| `where` | `any` | `cloudhost sweep --where`, or a map from template tag to it, e.g. `{"google": "cloud", "rest": "any"}`. With `any`, a sweep already sends google (its `--slow-suites`) to the fastest free machines first: H4D, then pakuchi, then N4D. |
| `shard_size` | 64 | Sims per sweep shard (`cloudhost sweep --shard-size`), an int or `{tag: int}`. A suite with fewer simpoints than this keeps all of them in each shard, with as many configs as fit; a bigger one is cut into chunks of simpoints, one config per shard. Fewer, bigger shards mean fewer launches; smaller ones finish a round's tail sooner. `dr2`: google 64, the rest 128. |
| `mem_per_sim_gb` | 0 | GB of RAM one sim needs, a float or `{tag: float}`, passed to `cloudhost sweep --mem-per-sim`. cloudhost sizes each shard's slice of a machine by it. Google at 50M+200M reaches ~5 GB RSS per sim: `cd1`, `dr1` and `dr2` set google to 5.5 (125 per H4D, 65 per n4d). |
| `extra_configs` | `{}` | `{name: params}` added to round 1 only, e.g. `{"lru": ""}` to check that gi0_gd0 is plain LRU. |
| `state_dir` | `/mnt/storage/mgiordan/gsearch` | State goes in `<state_dir>/<name>/`. |

## Search rules

A point is (γi, γd) on multiples of `step`. gi0_gd0 is the base policy with no boost (plain LRU for REPL_MLP), and it is the baseline. Values are computed as `./sci` on main computes them: `stat_collector`'s IPC row, then `scarab_stats/aggregation.py`. All counts are the measured region's `Periodic_Instructions` and `Periodic_Cycles`. The search works on four levels of unit:
- **simpoint:** its IPC.
- **input:** one `workloads_db` entry, e.g. spec2006 `gcc_2`. Its value is cycle-weighted over its simpoints: Σ w·insts / Σ w·cycles, with w the top-N renormalized weight.
- **workload:** the inputs of one benchmark (`gcc`, `gcc_2` … `gcc_9`), grouped by `aggregation.Hierarchy`. Its value is the geomean of its inputs.
- **suite:** the geomean of its workloads.
- **core** (with `core` on): sci's `Avg`, the geomean of the variant's suites, each weighted equally. There are three core units over all the variant's simpoints: `all` searches both gammas for the single best static setting; `gd=0` searches γi along γd = 0 (the instruction boost alone); `gi=0` searches γd along γi = 0 (the data boost alone). A line unit's incumbent and needs stay on its line.

A unit's gain is its value over its value at gi0_gd0. A unit has a value at a point only if every one of its simpoints has that point. `gsearch.py check-metric STUDY [SHARD ...]` checks the input values against sci's IPC row and the suite gains against `aggregation.Hierarchy` on finished shards. On `cd1` they agree to 1e-15 relative and 2e-14 pp.

1. **Round 1:** every simpoint runs `init`.
2. **γi sensitivity:** a simpoint is γi-insensitive when its γi>0 runs all stay within `gi_sensitive_pct` of the γi=0 run at the same γd.
   - Inside the box its runs cover, its γi>0 values are its γi=0 values. Outside the box, they are run.
   - A unit with no γi-sensitive simpoint searches γd only, at γi=0. On SPEC this cuts the γi axis after round 1.
3. **Incumbent:** for each unit, the incumbent starts at its best round-1 point. It then moves to a point 1 or 2 steps away along a searched axis while one beats it by at least `min_gain_pp`.
4. **Next round:** the unit needs:
   - each neighbor 1 step away on each searched axis, inside [0, `max_gamma`], that it doesn't have yet;
   - while it is still climbing in a direction, every step out to its horizon. "Climbing" means it last moved that way, or the point behind it is lower by `min_gain_pp`, or the neighbor ahead is higher by `partial_gain` × `min_gain_pp`. The horizon is `lookahead` steps, doubling with each consecutive move that way, up to `max_lookahead`.
   - side checks: where the incumbent jumped two or more steps along one axis, the ±1 neighbors on the other axis of the points it passed over. With these, the search measures everything a step-by-step climb along that path would have measured.

   This is the edge rule: if the best is on the largest γ searched, the search keeps going that way.
5. **Fan-out:** a core unit's needs run on every simpoint of the variant, a suite's on all of its simpoints, a workload's or input's on its simpoints, and a simpoint's on itself.
6. **Stopping:** a unit has converged when it needs nothing. It is `at_cap` if its incumbent sits on `max_gamma` while still climbing. The study stops when no unit needs anything, or after `max_rounds`.
7. **Failures:** a failed run is retried the next round. A simpoint with a point that failed twice is dropped from every unit and listed.

**Replay check:** `test_search.py --replay` replays the 30-simpoint countdown grid `mlp_cdgamma` (golden_small and golden_small_incl), interpolated onto step 12.
- **Rounds and cost:** every unit converged in 4–5 rounds, using about 700 sims, against 3,960 for a dense step-12 grid over γi 0–120 × γd 0–60.
- **Accuracy:** no simpoint's best measured point is more than 0.3 pp below the dense grid's best. The largest misses are SPEC simpoints whose γi effect is under the 0.2% cutoff and changes sign across γd.

## A new study

`new-study` copies an existing study's suites, run lengths and search settings to new architectures or base params:

```
python tools/mlp_gsearch/gsearch.py new-study cove1 --arch golden_cove --arch golden_cove_incl
python tools/mlp_gsearch/gsearch.py new-study dr1 --arch golden_small \
    --params "--mlc_cache_repl_policy 18 --mlp_rrip_boost 2" --step "[4, 1]" \
    --init '{"0": [0, 1, 2, 4, 8], "16": [0, 4], "32": [0, 8]}' --max-gamma "[128, 64]" \
    --extra "lru=--mlc_cache_repl_policy 0" --extra "drrip=--mlc_cache_repl_policy 18"
```

It writes three things:
- `json/gs_tmpl_<name>_<tag>_<arch>.json`: the source's templates, with `architecture`, `scarab_path`, `binary` and, with `--params`, the base params changed. The templates' `--inst_limit` is kept.
- `studies/<name>.json`: the study, with `--step`, `--init`, `--max-gamma`, `--max-rounds` and `--extra` applied.
- `studies/<name>_smoke.json`: one workload per suite, top simpoint, one round on pakuchi. Plain LRU and the `--extra` configs are added, so round 1 shows whether gi0_gd0 is the base policy.

It checks that `PARAMS.<arch>` exists. It refuses a binary that is not its scarab checkout's HEAD unless `--scarab-path` names a separate clone, because sci checks a binary out in place to build it, under any study shipping from that clone. Then:

```
python tools/mlp_gsearch/gsearch.py run cove1_smoke --yes
python tools/mlp_gsearch/gsearch.py plan cove1
setsid nohup python -u tools/mlp_gsearch/gsearch.py run cove1 --yes > /mnt/storage/mgiordan/gsearch/cove1/run.log 2>&1 < /dev/null &
```

Studies can run at the same time. They share the pool, and their sweeps' shards queue for machines.

## Running

```
python tools/mlp_gsearch/gsearch.py plan cd1          # what round 1 would run, its sims and cost
python tools/mlp_gsearch/gsearch.py run cd1 --yes     # all rounds, unattended
python tools/mlp_gsearch/gsearch.py status cd1        # round descriptors, their sweeps, and where each unit stands
python tools/mlp_gsearch/gsearch.py report cd1       # summary.md + CSVs in <state_dir>/cd1/report/
python tools/mlp_gsearch/gsearch.py pull cd1          # only for rounds run before sweeps (sweeps pull their own results)
python tools/mlp_gsearch/gsearch.py replan cd1        # re-lay out the current round's descriptors not yet started (stop `run` first)
python tools/mlp_gsearch/gsearch.py top-up cd1 [--yes] # add to the current round what a fresh decision wants that it doesn't run yet,
                                                      # e.g. after turning on `core` (stop `run` first; without --yes, a dry run)
python tools/mlp_gsearch/gsearch.py reingest cd1      # rewrite obs/ from every shard's collected_stats.csv (stop `run` first)
python tools/mlp_gsearch/gsearch.py check-metric cd1  # compare the search's input/suite values with sci's (round 1's shards)
```

A study runs for hours, so start it detached:

```
setsid nohup python -u tools/mlp_gsearch/gsearch.py run cd1 --yes > /mnt/storage/mgiordan/gsearch/cd1/run.log 2>&1 < /dev/null &
```

Rerunning `run` resumes the current round. Sweeps keep running while `run` is stopped; it picks them up by their ids in `state.json`. `cloudhost sweep status` shows each one's shards.

### A round

1. **Plan:** run the rules on every ingested run.
   - Group simpoints by the points they need. sci runs every config on every simulation of a descriptor, so how simpoints share a shard decides how many sims run.
   - **Named configs:** simpoints that need the same point set get `gi<I>_gd<D>` configs, when there are at least two of them and 64 sims. Round 1 is one such group.
   - **Slot shards:** every other simpoint gets slot configs `s0..s<K-1>`, using `workload_params` (scarab-infra #17). In slot j, each simpoint runs its own j-th point, through the selector `suite/subsuite/workload:cluster`, so it runs exactly the points it needs. `gsearch.slots` in the descriptor maps simpoint and slot to (γi, γd).
     - **Grouping:** slot groups are formed by K, the points per simpoint. A small group joins the next K only while padding it costs less than one more shard (10 minutes of a whole H4D).
     - **Padding:** a padded slot first takes a speculative neighbor of the simpoint's incumbent that hasn't been measured yet: a diagonal, then a 2-step point. It repeats a point only after those run out. On a single-wave shard those sims use idle cores, and they check the corners an axis-by-axis climb never visits.
     - **Before slots:** sci's cross product made cd1 run 40,280 sims where it needed 19,583. Late rounds ran 3–6x what they needed.
     - **Check:** slot runs were cycle-identical to named runs of the same points (15/15).
   - Each group is one round descriptor, `json/gs_<study>_r<k>_<variant>_<tag>_<i>.json`: a copy of the template with its own `simulations` and configurations, one experiment.
2. **Build:** `./sci --build-scarab` once per binary, before any launch, so no two sci processes build the same hash at once, and cloud machines get the cached build.
3. **Launch:** each descriptor goes out as `cloudhost sweep start <descriptor> --detach` (longest sims first), with the study's `where`, `shard_size` and `mem_per_sim_gb`, as `gsearch-<study>`. The sweep cuts it into shards that share the experiment (sci's multi-runner fix, scarab-infra #22), places them across the pool, and:
   - retries a shard whose sims did not all finish, on the host that has the rest, up to 3 times;
   - pulls each cloud shard to pakuchi as it ends;
   - checks the whole experiment with `./sci --status` and collects it once with `./sci --collect-stats`.
4. **Wait:** each sweep's `state.json` is read every minute.
   - **Complete:** ingest its `collected_stats.csv`.
   - **Failed or incomplete** (some shard failed 3 times): `cloudhost sweep resume --retry-failed`, twice; then collect what finished here, and the search retries the missing points next round (a point that fails twice drops its simpoint).
   - **No driver** (it died; its state file unchanged for 5 minutes): `cloudhost sweep resume`.
   - **Mostly empty** (under half its runs have stats): stop the study rather than record them all as failures.
5. **Ingest:** each descriptor's IPCs go to `obs/<descriptor>.json`, and `report/` is rewritten.

Rounds before `dr2` ran on gsearch's own launcher (one `cloudhost run` per VM-sized shard, collecting on the machine); the per-core leases and sweeps of cloudhost replaced it. Those studies' reports still regenerate from their `obs/`.

### State (`<state_dir>/<study>/`)

- `study.json` and `templates/` are frozen copies; later edits to the originals are ignored.
- `state.json`: rounds, their descriptors and sweep ids.
- `obs/`: per-shard IPCs.
- `rounds/r<k>/decision.json`: every unit's incumbent and needs when round k was planned.
- `rounds/r<k>/shards/`
- `logs/`
- `report/`

### Report

`summary.md` has, per variant:
- the core units (with `core` on): incumbent, best measured point, each suite's gain at the incumbent, and a grid of the `Avg` gain at every point all simpoints have;
- per searched suite: the incumbent and the best measured point, with their gains, per-workload and per-simpoint oracles, and convergence;
- per suite (searched or not): a grid of its gain at every point all its simpoints have.

`units.csv` and `best_{simpoint,input,workload,suite}.csv` list every unit's incumbent, best measured point, gain, flags and remaining needs. `obs.csv` has every run, with its Periodic instructions and cycles, its sci workload and its simpoint's γi sensitivity.
