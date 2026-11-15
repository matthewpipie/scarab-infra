- google / golden_small: golden_small · 12 workloads, 63 simpoints · 50M warmup + 200M measured
- google / golden_small_berti: golden_small_berti · 12 workloads, 63 simpoints · 50M warmup + 200M measured
- spec2006 / golden_small: golden_small · 29 workloads (55 inputs), 163 simpoints · 50M warmup + 10M measured
- spec2006 / golden_small_berti: golden_small_berti · 29 workloads (55 inputs), 163 simpoints · 50M warmup + 10M measured
- spec2017 / golden_small: golden_small · 23 workloads (36 inputs), 106 simpoints · 50M warmup + 10M measured
- spec2017 / golden_small_berti: golden_small_berti · 23 workloads (36 inputs), 106 simpoints · 50M warmup + 10M measured
- spec2026 / golden_small: golden_small · 26 workloads (90 inputs), 269 simpoints · 50M warmup + 10M measured
- spec2026 / golden_small_berti: golden_small_berti · 26 workloads (90 inputs), 269 simpoints · 50M warmup + 10M measured
- datacenter / golden_small: golden_small · 7 workloads, 20 simpoints · 10M warmup + 10M measured
- datacenter / golden_small_berti: golden_small_berti · 7 workloads, 20 simpoints · 10M warmup + 10M measured

| Suite | Core | Side | Marked | Ceiling | Oracle | Control | Oracle − control |
|---|---|---|---|---|---|---|---|
| google | golden_small | instruction | 10.7% | +6.43 | +3.35 | +0.62 | +2.73 |
| google | golden_small | data | 35.9% | +22.17 | +11.51 | +7.41 | +4.09 |
| google | golden_small | both | 19.0% | +34.88 | +15.60 | +8.15 | +7.45 |
| google | golden_small_berti | instruction | 10.8% | +6.77 | +3.52 | +0.65 | +2.87 |
| google | golden_small_berti | data | 36.0% | +21.59 | +10.93 | +7.22 | +3.71 |
| google | golden_small_berti | both | 18.6% | +34.88 | +15.22 | +8.01 | +7.21 |
| spec2006 | golden_small | instruction | 12.3% | +0.01 | +0.00 | +0.00 | +0.00 |
| spec2006 | golden_small | data | 29.6% | +29.18 | +8.44 | +6.13 | +2.31 |
| spec2006 | golden_small | both | 29.5% | +29.25 | +8.45 | +6.13 | +2.32 |
| spec2006 | golden_small_berti | instruction | 13.2% | +0.00 | -0.01 | -0.00 | -0.00 |
| spec2006 | golden_small_berti | data | 25.8% | +21.01 | +6.37 | +4.15 | +2.22 |
| spec2006 | golden_small_berti | both | 25.8% | +21.08 | +6.40 | +4.15 | +2.26 |
| spec2017 | golden_small | instruction | 9.4% | +0.06 | +0.04 | +0.00 | +0.04 |
| spec2017 | golden_small | data | 21.1% | +12.11 | +3.79 | +3.01 | +0.78 |
| spec2017 | golden_small | both | 20.8% | +12.23 | +3.83 | +3.01 | +0.82 |
| spec2017 | golden_small_berti | instruction | 9.6% | +0.08 | +0.05 | +0.03 | +0.02 |
| spec2017 | golden_small_berti | data | 17.2% | +8.81 | +2.76 | +2.16 | +0.60 |
| spec2017 | golden_small_berti | both | 16.8% | +8.91 | +2.81 | +2.17 | +0.64 |
| spec2026 | golden_small | instruction | 10.4% | +0.29 | +0.19 | +0.02 | +0.16 |
| spec2026 | golden_small | data | 21.7% | +11.02 | +2.98 | +2.29 | +0.69 |
| spec2026 | golden_small | both | 20.8% | +11.40 | +3.19 | +2.32 | +0.87 |
| spec2026 | golden_small_berti | instruction | 10.4% | +0.34 | +0.23 | +0.05 | +0.17 |
| spec2026 | golden_small_berti | data | 22.8% | +6.84 | +1.98 | +1.39 | +0.60 |
| spec2026 | golden_small_berti | both | 21.5% | +7.20 | +2.17 | +1.44 | +0.74 |
| datacenter | golden_small | instruction | 16.4% | +36.97 | +22.11 | +7.12 | +14.99 |
| datacenter | golden_small | data | 24.9% | +2.63 | +1.06 | +0.82 | +0.23 |
| datacenter | golden_small | both | 17.3% | +46.83 | +23.56 | +8.10 | +15.46 |
| datacenter | golden_small_berti | instruction | 16.4% | +37.12 | +22.19 | +7.15 | +15.04 |
| datacenter | golden_small_berti | data | 21.4% | +1.32 | +0.96 | +0.76 | +0.20 |
| datacenter | golden_small_berti | both | 16.9% | +45.03 | +23.50 | +8.01 | +15.48 |

| Suite | Core | Side | marked misses at 8–63 fills | control misses at 8–63 fills | LRU hit rate, marked lines | unmarked lines |
|---|---|---|---|---|---|---|
| google | golden_small | instruction | 62.1% | 35.4% | 80.4% | 69.5% |
| google | golden_small | data | 47.8% | 34.5% | 69.5% | 70.6% |
| google | golden_small_berti | instruction | 61.7% | 35.1% | 80.3% | 69.1% |
| google | golden_small_berti | data | 47.1% | 33.8% | 69.7% | 70.4% |
| spec2006 | golden_small | instruction | 69.2% | 67.5% | 81.8% | 82.2% |
| spec2006 | golden_small | data | 63.2% | 59.1% | 72.6% | 76.5% |
| spec2006 | golden_small_berti | instruction | 69.4% | 66.9% | 81.3% | 81.6% |
| spec2006 | golden_small_berti | data | 65.3% | 63.3% | 77.0% | 82.7% |
| spec2017 | golden_small | instruction | 73.7% | 71.6% | 85.8% | 80.6% |
| spec2017 | golden_small | data | 85.0% | 83.3% | 76.8% | 81.9% |
| spec2017 | golden_small_berti | instruction | 71.0% | 69.2% | 85.7% | 81.7% |
| spec2017 | golden_small_berti | data | 79.5% | 71.9% | 86.2% | 91.2% |
| spec2026 | golden_small | instruction | 76.4% | 75.6% | 93.0% | 87.1% |
| spec2026 | golden_small | data | 75.0% | 74.8% | 89.6% | 91.4% |
| spec2026 | golden_small_berti | instruction | 76.8% | 76.5% | 92.9% | 86.9% |
| spec2026 | golden_small_berti | data | 73.7% | 73.9% | 90.1% | 92.7% |
| datacenter | golden_small | instruction | 92.0% | 84.1% | 75.2% | 73.7% |
| datacenter | golden_small | data | 93.7% | 93.7% | 81.6% | 77.1% |
| datacenter | golden_small_berti | instruction | 90.2% | 82.2% | 75.2% | 73.7% |
| datacenter | golden_small_berti | data | 93.0% | 91.6% | 81.6% | 77.9% |
