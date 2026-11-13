#!/usr/bin/env bash
# Run the REPL_MLP gamma-sweep descriptors one after another, collecting stats after each.
#
#   tools/mlp_gamma/run_sweep.sh                      # every sweep descriptor, google first
#   tools/mlp_gamma/run_sweep.sh mlp_gamma_google_small mlp_gamma_google_small_incl
#   tools/mlp_gamma/run_sweep.sh --check              # the smoke test
#
# In manual mode ./sci --sim returns once all of its simulations finish, so running the
# descriptors in sequence keeps the machine at one descriptor's max_parallel. The binary is built
# once up front, so no --sim builds while another descriptor's simulations run. A descriptor whose
# --sim fails is reported at the end and the rest still run.
set -uo pipefail
cd "$(dirname "$0")/../.."

SUITES=(google datacenter spec2006 spec2017 geekbench)
descs=()
if [[ $# -eq 0 ]]; then
  for s in "${SUITES[@]}"; do descs+=("mlp_gamma_${s}_small" "mlp_gamma_${s}_small_incl"); done
elif [[ $1 == --check ]]; then
  for s in google datacenter; do descs+=("mlp_gamma_check_${s}_small" "mlp_gamma_check_${s}_small_incl"); done
else
  descs=("$@")
fi

for d in "${descs[@]}"; do
  [[ -f json/$d.json ]] || { echo "no json/$d.json" >&2; exit 1; }
done

echo "$(date '+%F %T') building the binary for ${descs[0]}"
./sci --build-scarab "${descs[0]}" || { echo "build failed" >&2; exit 1; }

failed=()
for d in "${descs[@]}"; do
  echo "$(date '+%F %T') --sim $d"
  ./sci --sim "$d" || failed+=("$d (sim)")
  echo "$(date '+%F %T') --collect-stats $d"
  ./sci --collect-stats "$d" || failed+=("$d (collect)")
done

echo "$(date '+%F %T') done"
if [[ ${#failed[@]} -gt 0 ]]; then
  printf 'failed: %s\n' "${failed[@]}" >&2
  exit 1
fi
