#!/usr/bin/env python3
"""PreToolUse hook: deny Bash that reimplements ./sci, naming the tool to use instead."""

import json
import re
import sys

# Slurm job listings. How far along a sweep is, is sci's question, not squeue's.
QUEUE = r"\b(?:squeue|sacct)\b"
# An interpreter that is the command being run: at the start of a line or after a
# separator, loop keyword, xargs or -exec, optionally behind env assignments or a
# wrapper (nice, timeout, ...). The word "python" inside a heredoc body, a commit
# message or a PR description is not a command and does not count.
SCRIPTED_CMD = (
    r"(?:^|[\n|;&(]|\bdo\b|\bthen\b|\bxargs\b|-exec\b)\s*"
    r"(?:\w+=\S*\s+)*"
    r"(?:(?:sudo|nice|nohup|time|env|srun|timeout|stdbuf)\b[^|;&\n]*?\s+)?"
    r"(?:[^\s|;&'\"]*/)?(?:python[\d.]*|awk|gawk|mawk|perl|ruby)\b"
)
# Raw per-simulation stat files.
STAT_FILE = r"(?:\bstats\.out\b|\.stat\.\d+\.(?:out|csv)\b)"
# Raw per-simulation output: a stat file, or a path under /simulations/ other than the
# collected results and reports sci writes at the top of an experiment (CSV, JSON,
# Markdown, PNG). Those are already SimPoint-weighted, so analyzing them is fine.
RAW_ARG = (
    r"(?:" + STAT_FILE + r"|/simulations/[^\s'\"|;&<>()]*"
    r"(?<!\.csv)(?<!\.json)(?<!\.md)(?<!\.png)(?=[\s'\"|;&<>()]|$))"
)
# A glob over stat files, or a find for them: aggregating across simpoints.
STAT_GLOB = r"\*[^\s]*" + STAT_FILE
STAT_FIND = r"\bfind\b[^|;&\n]*-i?name\s+['\"]?[^\s'\"]*" + STAT_FILE

RULES = [
    # (regex, replacement tool). Matched with re.IGNORECASE | re.DOTALL.
    # Sweep progress, however it is phrased. Naming the experiment is only one way to
    # ask: counting your own jobs, or polling in a loop, asks the same thing and used to
    # slip through because neither mentions exp_. A per-user count is the worst of them,
    # since it also counts held stragglers from other experiments and so reports a
    # finished sweep as stuck. Asking about the cluster -- which nodes are up, who else
    # is queued -- is a different question and stays allowed, as does sinfo.
    (QUEUE + r"(?=.*\bexp_)",                          "sci_status"),
    (QUEUE + r"[^\n]*\s-{1,2}u(?:ser)?[= ]",           "sci_status"),
    (QUEUE + r"[\s\S]*\|[\s\S]*\b(?:wc|uniq)\b",       "sci_status"),
    (QUEUE + r"[\s\S]*\|[\s\S]*\bgrep\b[^|\n]*-[a-z]*c", "sci_status"),
    (r"\b(?:for|while)\b[\s\S]*\bdo\b[\s\S]*" + QUEUE, "sci_status"),
    # Parsing raw sim output by hand: an interpreter whose own arguments (same simple
    # command) name raw output, or any glob, find or loop over stat files. Reading one
    # stats.out with cat/grep while debugging is not blocked.
    (SCRIPTED_CMD + r"[^|;&\n]*" + RAW_ARG,            "sci_collect_stats"),
    (STAT_GLOB,                                        "sci_collect_stats"),
    (STAT_FIND + r"[\s\S]*" + SCRIPTED_CMD,            "sci_collect_stats"),
    (r"(?:^|[\n|;&(])\s*(?:for|while)\b[\s\S]*\bdo\b[\s\S]*" + STAT_FILE, "sci_collect_stats"),
    (r"\bmake\b[^|;]*\bscarab\b",                      "sci_build_scarab"),
    # Command position, and only a real binary: naming one (md5sum, ls, stat) is not
    # running it, and a bare "scarab" in prose or a markdown table is not either.
    (r"(?:^|[|;&]\s*|\bsudo\s+|\bsrun\s+)\s*(?:[^\s|;&='\"]*/)?(?:scarab_[0-9a-f]{7}[^\s]*\.opt|\./scarab)\b", "sci_sim"),
]
# `./sci ...` itself is always fine, and so is anything that merely mentions sci.
ALLOW = re.compile(r"(?:^|[|;&]\s*|\s)\./sci\b")
# The user approved going around sci for this command; the reason stays in the transcript.
OVERRIDE = re.compile(r"#\s*sci-hook-ok:\s*\S")

# Checked before ALLOW: these call sci but drive it by hand, so the allow-list would
# wave them through. A descriptor that names scarab_<githash> binaries already gets
# them checked out and built by --sim.
CHECKOUT = r"\bgit\b[^\n;|&]*\bcheckout\b"
BUILD = r"--build-scarab\b"
DENY_FIRST = [
    (CHECKOUT + r"[\s\S]*" + BUILD, "sci_sim"),
    (BUILD + r"[\s\S]*" + CHECKOUT, "sci_sim"),
]

# The MCP server is only loaded where it is registered, so name the CLI too.
CLI = {
    "sci_status": "--status",
    "sci_collect_stats": "--collect-stats",
    "sci_build_scarab": "--build-scarab",
    "sci_sim": "--sim",
}
ESCAPE = (
    "If sci genuinely cannot express what you need, say so to the user and ask; with "
    "their OK, rerun the command with a trailing `# sci-hook-ok: <reason>` comment.")
DENY_FIRST_REASON = (
    "Blocked: do not drive a scarab build with your own checkout loop. Use the MCP "
    "tool `{tool}` (scarab-infra server) or `./sci {flag} <descriptor>`: name each "
    "binary scarab_<githash> in the descriptor's configurations and launch once. sci "
    "checks out and builds every hash that is not cached, restoring your branch and "
    "working tree afterwards. " + ESCAPE)
REASON = (
    "Blocked: this reimplements scarab-infra. Use the MCP tool `{tool}` (scarab-infra "
    "server), or `./sci {flag} <descriptor>` where that server is not loaded, instead "
    "of hand-rolling it in Bash. " + ESCAPE)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if payload.get("tool_name") != "Bash":
        sys.exit(0)
    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not cmd or OVERRIDE.search(cmd):
        sys.exit(0)

    for pat, tool in DENY_FIRST:
        if re.search(pat, cmd, re.IGNORECASE | re.DOTALL):
            deny(DENY_FIRST_REASON.format(tool=tool, flag=CLI[tool]))

    if ALLOW.search(cmd):
        sys.exit(0)

    for pat, tool in RULES:
        if re.search(pat, cmd, re.IGNORECASE | re.DOTALL):
            deny(REASON.format(tool=tool, flag=CLI[tool]))
    sys.exit(0)


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


if __name__ == "__main__":
    main()
