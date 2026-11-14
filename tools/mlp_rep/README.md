# Representative subset for REPL_MLP sweeps

30 single simpoints whose plain geomean gain tracks the 5-suite equal-suite geomean gain (Google, SPEC2006,
SPEC2017, Geekbench, Datacenter; golden_small, top 6 simpoints) of a full REPL_MLP sweep, at 5.5% of its cost:
840 M simulated instructions per config instead of 15,300.

| Suite | Simpoints | Picked from |
|---|---|---|
| Google (10M warmup + 50M) | 6 | bravo.a, charlie, sierra.a.6, tango, whiskey, yankee |
| SPEC2006 | 7 | soplex_2, zeusmp ×2, astar, bzip2_5, gcc, gcc_9 |
| SPEC2017 | 9 | bwaves_r_3, bwaves_r_4 ×2, cam4_r, fotonik3d_r, imagick_r, lbm_r, roms_r, gcc_r_2 |
| Geekbench | 2 | html5_browser, navigation |
| Datacenter | 6 | mysql ×2, postgres ×2, verilator, xgboost |

`simpoints.csv` lists the cluster ids. They mix workloads that gain a lot (verilator, yankee, tango),
workloads that lose (navigation, xgboost, lbm_r), and workloads that barely move (zeusmp, mysql,
postgres: their L2 never fills in 10M + 10M). The insensitive ones are what keep the average at the
full sweep's scale, so don't drop them.

## Use

```
python tools/mlp_rep/make_descriptors.py NAME --google <full google desc> --rest <full 10M+10M desc> [--configs a,b]
./sci --sim NAME_google; ./sci --sim NAME_rest
./sci --collect-stats NAME_google; ./sci --collect-stats NAME_rest
python tools/mlp_rep/report.py NAME [--baseline gi0_gd0] [--by-suite]
```

`make_descriptors.py` copies settings and configurations from the two full-sweep descriptors.
Google configs get `--inst_limit 60000000`; the others lose it.
The estimate is the plain geomean over the 30 simpoints of IPC / baseline IPC. Every simpoint counts
once: no simpoint weights and no suite weights. The selection already accounts for both.

## How it was chosen

- **Search:** swap-based local search over all 639 simpoints, best of 6 seeds by fit.
- **Target:** per config, the full sweep's equal-suite geomean gain over LRU, with each workload's IPC
  the weighted mean of its simpoints.
- **Fit:** simultaneously to four experiments, each weighted the same:
  - the γi×γd grid on golden_small (`mlp_gamma_*_small`, 36 configs);
  - the same grid on golden_small_incl (36);
  - the no-issue data-marking sweep (`mlp_dsweep_*_small`, 9);
  - the #35 data-marking sweep (`mlp_dbound_broad_*_small`, 13).

| Experiment | Full range | RMSE | Max error | Best config |
|---|---|---|---|---|
| γ grid, golden_small | −1.53 … +1.69 | 0.024 | 0.08 | same (gi64_gd16) |
| γ grid, golden_small_incl | −0.15 … +2.76 | 0.013 | 0.04 | same (gi256_gd64) |
| no-issue sweep | +1.61 … +1.80 | 0.012 | 0.02 | same (s3_min16) |
| #35 sweep | +1.40 … +1.81 | 0.019 | 0.04 | s2_min32 vs s2_min16 (tied at +1.81) |

These are in-sample. **What to expect on a new experiment** comes from leave-one-experiment-out runs:
30 simpoints fit to three of the four experiments, then scored on the fourth, over 3 seeds.

| Held out | Level RMSE | Centered RMSE (ranking) | Pairs ordered | Random 30-simpoint subset |
|---|---|---|---|---|
| γ grid, golden_small_incl | 0.09–0.27 | — | — | 1.0 level |
| no-issue sweep | 0.03–0.04 | 0.03–0.04 | 86–97% | 0.10 centered, 69% pairs |
| #35 sweep | 0.10–0.14 | 0.08–0.14 | 80–93% | 0.20 centered, 73% pairs |

- **Rule of thumb:** trust differences of about 0.15 points or more between configs. Smaller ones need
  the full sweep.
- **Fit on γ alone transfers poorly:** a set fit only to the γ grid tracks γ well (golden_small_incl
  0.09–0.13 when held out). But it ranks data-marking rules no better than a random subset. That is why
  the set is fit to all four experiments.
- **Google and SPEC alone can't track the 5-suite metric:** the best Google+SPEC-only subsets get
  0.2–0.3 points RMSE on the γ grid, because Datacenter's large instruction-side gains (verilator,
  xgboost) have a different γ shape.

## Caveats

- **Only REPL_MLP L2 replacement changes were validated:** golden_small and golden_small_incl, all with
  `--mlp_boost_in_fills 1`. Prefetchers, core changes, other architectures or main's countdown aging
  may need a re-fit.
- **Re-fitting:** rerun the selection with the new full sweep added as a fifth experiment. The
  selection scripts are not part of this directory.
