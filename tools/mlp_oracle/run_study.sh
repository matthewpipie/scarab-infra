#!/usr/bin/env bash
# Run one mlp_oracle study that tools/mlp_oracle/make_descriptors.py wrote: the reference, then
# the replay runs that read its records, then ./sci --collect-stats.
#
#   tools/mlp_oracle/run_study.sh mlp_oracle_google_<tag>
#   tools/mlp_oracle/run_study.sh mlp_oracle_check_<tag>       # the smoke test
#
# Each step waits until the machine has room and sets the descriptor's max_parallel to it:
#   MAX_SIMS        (256) the most of your sims to run at once, your other runs included
#   MIN_SLOTS       (32)  wait until at least this many slots are free (or the whole step fits)
#   MEM_RESERVE_GB  (300) memory to leave free
#   MEM_PER_SIM_GB  (4)   memory per sim (a 550M google replay run holds about 3 GB)
#   IDLE_RESERVE    (32)  cores to leave idle
# Your other runs count by their scarab processes and, since a run with container_reuse holds a
# container per slot while it starts up and between sims, by their pool containers too.
#
# Rerunning is safe: sci skips simpoints that already have stats, so a killed study resumes where
# it stopped. ./sci --status then counts only the latest run's job logs, so check the simpoint
# directories (or report.py) rather than its Completed column.
set -uo pipefail
cd "$(dirname "$0")/../.."

MAX_SIMS=${MAX_SIMS:-256}
MIN_SLOTS=${MIN_SLOTS:-32}
MEM_RESERVE_GB=${MEM_RESERVE_GB:-300}
MEM_PER_SIM_GB=${MEM_PER_SIM_GB:-4}
IDLE_RESERVE=${IDLE_RESERVE:-32}
PY=${PY:-python3}

[[ $# -eq 1 ]] || { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
exp=${1%.json}; exp=${exp#json/}
ref=${exp}_ref
for d in "$ref" "$exp"; do
  [[ -f json/$d.json ]] || { echo "no json/$d.json; write it with tools/mlp_oracle/make_descriptors.py" >&2; exit 1; }
done

field() {  # field <desc> <python expression over the descriptor d>
  "$PY" -c "import json,sys; d=json.load(open('json/$1.json')); print($2)"
}
set_parallel() {  # set_parallel <desc> <n>, keeping the canonical JSON format
  "$PY" - "$1" "$2" <<'PY'
import json, sys
p = f"json/{sys.argv[1]}.json"
d = json.load(open(p))
d["max_parallel"] = int(sys.argv[2])
open(p, "w").write(json.dumps(d, indent=2, separators=(",", ":")) + "\n")
PY
}
log() { echo "$(date '+%F %T') $*"; }

room() {  # sets slots, from your running sims, idle cores and free memory
  local procs pools load avail
  procs=$(ps -eo user=,args= | grep "[s]rc/scarab" | grep -c "^$USER ")
  pools=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -cE "_pool[0-9]+_.*_${USER}$")
  mine=$(( procs > pools ? procs : pools ))
  load=$(cut -d' ' -f1 /proc/loadavg | cut -d. -f1)
  avail=$(( $(grep MemAvailable /proc/meminfo | tr -s ' ' | cut -d' ' -f2) / 1048576 ))
  slots=$(( MAX_SIMS - mine ))
  local idle=$(( $(nproc) - load - IDLE_RESERVE ))
  local mem=$(( (avail - MEM_RESERVE_GB) / MEM_PER_SIM_GB ))
  (( idle < slots )) && slots=$idle
  (( mem < slots )) && slots=$mem
  (( slots < 0 )) && slots=0
  status="yours=$mine load=$load free=${avail}G"
}

run() {  # run <desc>: wait for room, size max_parallel, ./sci --sim
  local d=$1 n want
  n=$(field "$d" "len(d['simulations']) * len(d['configurations'])")
  want=$(( n < MIN_SLOTS ? n : MIN_SLOTS ))
  while true; do
    room
    (( slots >= want )) && break
    log "$d: $slots slots free ($status); waiting for $want"
    sleep 120
  done
  (( slots > n )) && slots=$n
  set_parallel "$d" "$slots"
  log "$d: $n sims at max_parallel $slots ($status)"
  ./sci --sim "$d"
}

root=$(field "$ref" "d['root_dir']")
n_sp=$(field "$ref" "len(d['simulations'])")
[[ $(field "$exp" "d['root_dir']") == "$root" ]] || { echo "$ref and $exp must share root_dir" >&2; exit 1; }

log "building the binary"
./sci --build-scarab "$ref" || { echo "build failed" >&2; exit 1; }

run "$ref" || log "$ref: ./sci --sim exited $?"
track="${root%/}/simulations/$(field "$ref" "d['experiment']")/track"
records=$(find "$track" -name 'mlp_oracle_misses.*.bin' 2>/dev/null | wc -l)
done_sp=$(find "$track" -name 'memory.stat.0.csv' 2>/dev/null | wc -l)
log "$ref: $done_sp of $n_sp simpoints finished, $records records"
if (( records != n_sp || done_sp != n_sp )); then
  echo "The reference is incomplete, so the replay runs would fail or skip simpoints. Rerun this script to finish it." >&2
  exit 1
fi

run "$exp" || log "$exp: ./sci --sim exited $?"
./sci --collect-stats "$exp"
log "done. Results: python tools/mlp_oracle/report.py $exp"
