#!/usr/bin/env python3
"""Cases for force_sci_hook. Vectors live here, not on a command line the hook reads."""

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).with_name("force_sci_hook.py")

# (command, expected tool or None for allow)
CASES = [
    # status
    ('squeue -u me -o "%j" | grep exp_sw621b | wc -l', "sci_status"),
    ("sacct -n --format=JobName | grep exp_sw620", "sci_status"),
    # sweep progress that never names the experiment
    ("squeue -u hlitz -h 2>/dev/null | wc -l", "sci_status"),
    ("squeue -u hlitz -h -o '%T %R' | sort | uniq -c", "sci_status"),
    ("sacct -u hlitz --format=State | grep -c COMPLETED", "sci_status"),
    ("for i in $(seq 1 19); do n=$(squeue -h | wc -l); command sleep 30; done", "sci_status"),
    ("while true; do squeue -h > /tmp/q; command sleep 60; done", "sci_status"),
    ('squeue -h -o "%u %T" | sort | uniq -c', "sci_status"),
    # allowed: questions about the cluster itself, not about a sweep
    ('sinfo -o "%.14N %.9P %.7t %.5D %.6c"', None),
    ("scontrol show job 1795748", None),
    ('squeue -o "%.10i %.10T %.20R"', None),
    # stat aggregation: an interpreter run on raw sim output, or a glob/find/loop over stat files
    ("grep IPC /home/me/simulations/exp_sw620/*/stats.out", "sci_collect_stats"),
    ("for f in /home/me/simulations/exp/*/*/stats.out; do awk '/IPC/{print $2}' $f; done", "sci_collect_stats"),
    ("python3 /tmp/myparse.py /home/me/simulations/exp_sw619/base_stock", "sci_collect_stats"),
    ("cd /tmp && ~/miniconda3/envs/x/bin/python p.py ~/simulations/exp/lru/spec2006/", "sci_collect_stats"),
    ("nice -n 10 python3 p.py /home/me/simulations/exp/lru", "sci_collect_stats"),
    ("awk '/IPC/' /home/me/simulations/exp/lru/spec2006/rate_int/mcf/1/core.stat.0.out", "sci_collect_stats"),
    ("cat ~/simulations/exp/lru/*/*/*/*/core.stat.0.out | python3 sum.py", "sci_collect_stats"),
    ("find ~/simulations/exp -name stats.out | xargs awk '/IPC/'", "sci_collect_stats"),
    ("for e in a b; do\n  python3 parse.py ~/simulations/$e/lru\ndone", "sci_collect_stats"),
    ("for c in lru drrip; do grep IPC ~/simulations/exp/$c/x/1/stats.out; done", "sci_collect_stats"),
    # allowed: collected results are already SimPoint-weighted; analyzing them is ordinary work
    ('python3 -c "import csv; csv.reader(open(\'collected_stats.csv\'))"', None),
    ("awk -F, '{print $3}' /home/me/simulations/exp_sw620/collected_stats.csv", None),
    ("~/miniconda3/envs/x/bin/python cmp.py ~/simulations/exp/collected_stats.csv lru drrip", None),
    ("python3 -m json.tool /home/me/simulations/exp/aggregates.json", None),
    ("jq '.aggregates.IPC' ~/simulations/exp/aggregates.json", None),
    # allowed: the words appear, but nothing runs an interpreter on raw output
    ("mkdir -p ~/simulations/t && cp ~/simulations/exp/collected_stats.csv ~/simulations/t/", None),
    ("for e in a b; do mkdir -p ~/simulations/$e && cp ~/simulations/x/collected_stats.csv ~/simulations/$e/; done", None),
    ("rm -rf ~/simulations/aggtest_*", None),
    ("python3 -I - <<'EOF'\nimport json\nprint(1)\nEOF\nmkdir -p ~/simulations/aggtest", None),
    ("gh pr create --title t --body 'run python on aggregates.json, not on /simulations/x/y'", None),
    ("git commit -m 'python3 reads aggregates.json now'", None),
    ("ls ~/simulations/exp/lru/spec2006/rate_int/mcf/1/", None),
    # allowed: the user approved going around sci for this one command
    ("python3 p.py /home/me/simulations/exp/lru  # sci-hook-ok: one-off per-simpoint dump", None),
    ("python3 p.py /home/me/simulations/exp/lru  # sci-hook-ok:", "sci_collect_stats"),
    # build
    ("make -j32 scarab", "sci_build_scarab"),
    # running the simulator
    ("/home/me/simulations/scarab_7827a3c_1.opt --param_file x", "sci_sim"),
    ("srun /home/me/sim/scarab_7827a3c_1.opt", "sci_sim"),
    ("./scarab --param_file PARAMS.in", "sci_sim"),
    # allowed: sci itself
    ("./sci --status sw621b", None),
    ("cd /home/me/git/scarab-infra && ./sci --collect-stats sw620 && ./sci --visualize sw620", None),
    # allowed: inspecting one file while debugging
    ("grep -n PREF_DRAMQ_STALL /home/me/simulations/exp/feat/x/stats.out", None),
    # allowed: naming a binary is not running it
    ("md5sum /home/me/simulations/scarab_7827a3c_1.opt", None),
    ("ls -la /home/me/simulations/scarab_bc9352d_0.opt", None),
    ("stat -c%s /home/me/sim/scarab_b777a1a_1.opt", None),
    # allowed: a shell assignment is not a command invocation
    ("S=/home/me/builds/scarab_bc9352d_0.opt; md5sum $S", None),
    ("ls -la scarab_bc9352d_*.opt", None),
    # driving a build with your own checkout loop: --sim builds missing hashes itself
    ("for c in aaa1111 bbb2222; do git -C /r checkout -q $c; ./sci --build-scarab b; done", "sci_sim"),
    ("git checkout 8672066 && ./sci --build-scarab build_bisect", "sci_sim"),
    ("./sci --build-scarab build_bisect; git checkout mshr-on-miss", "sci_sim"),
    # allowed: building the tree you are on, then launching
    ("./sci --build-scarab build_x && ./sci --sim sw720", None),
    ("git checkout mshr-on-miss && ./sci --sim sw720", None),
    # allowed: unrelated work
    ("git log --oneline -5", None),
    ("gh pr view 621 --comments", None),
    ("cd /home/me/git/scarab-infra && git diff --stat", None),
]


# (command, text the denial must contain): the CLI fallback for sessions without the
# MCP server, and the way to record an approved exception.
REASON_CASES = [
    ("squeue -u me | wc -l", "./sci --status <descriptor>"),
    ("python3 p.py /home/me/simulations/exp/lru", "./sci --collect-stats <descriptor>"),
    ("make -j32 scarab", "# sci-hook-ok: <reason>"),
    ("git checkout abc1234 && ./sci --build-scarab b", "./sci --sim <descriptor>"),
]


def reason_for(command):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    out = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True
    ).stdout.strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


def verdict(command):
    reason = reason_for(command)
    if reason is None:
        return None
    return reason.split("Use the MCP tool `")[1].split("`")[0]


def main():
    failures = 0
    for command, expected in CASES:
        got = verdict(command)
        ok = got == expected
        failures += not ok
        label = "ok  " if ok else "FAIL"
        want = expected or "allow"
        shown = command.replace("\n", "\\n")[:64]
        print(f"[{label}] {want:<18} {'' if ok else f'(got {got or 'allow'}) '}{shown}")
    for command, text in REASON_CASES:
        ok = text in (reason_for(command) or "")
        failures += not ok
        print(f"[{'ok  ' if ok else 'FAIL'}] reason has {text!r}: {command[:40]}")
    total = len(CASES) + len(REASON_CASES)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
