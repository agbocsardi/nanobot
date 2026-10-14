#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""pytest_delta — run a pytest suite and diff failures against a stored baseline.

Why: after a merge the suite reports N failures, and there is no quick way to tell
a new regression from a known pre-existing failure. Yesterday that needed a second
run in a throwaway worktree at the parent commit, and the first count reported was
wrong (2 vs 4) because one run collected fewer tests than the other.

So this tool does two things:
  1. Diffs failing test node IDs against a baseline -> NEW / FIXED / KNOWN.
  2. Checks the collected-test count against the baseline -> a run that collects
     fewer tests (worktree, subset, import error) is flagged loudly instead of
     silently hiding failures.

Pure stdlib: it shells out to pytest and parses the output, so it uses the target
project's own environment rather than re-resolving dependencies.

Usage:
  uv run pytest_delta.py --save-baseline            # record current state as baseline
  uv run pytest_delta.py                            # report delta vs baseline
  uv run pytest_delta.py --cmd "~/nanobot/.venv/bin/python -m pytest -q -rf --tb=no"
  uv run pytest_delta.py --root ~/nanobot -- -- tests/test_cron.py   # extra args passed through

In this repo use the wrapper, which pins the root, the memory-capped runner and the
baseline location: scripts/test-delta.sh [--save-baseline] [--quiet] [-- <pytest args>].
Run without -q: the collected-test count comes from pytest's "collected N items" header.

Exit codes: 0 = no new failures, 1 = new failures, 2 = collection/comparability problem.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

DEFAULT_CMD = "{python} -m pytest -q -rf --tb=no -p no:cacheprovider"

FAILED_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)")
SUMMARY_RE = re.compile(r"^(?P<body>(?:\d+\s+\w+(?:,\s*)?)+?)\s+in\s+[\d.]+s", re.MULTILINE)
COUNT_RE = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed|deselected|warnings?|warning)"
)


def parse_pytest_output(out: str) -> dict:
    """Pull failing node IDs and the count summary out of pytest stdout."""
    failed: list[str] = []
    for line in out.splitlines():
        m = FAILED_RE.match(line.strip())
        if m:
            nodeid = m.group(2)
            if nodeid not in failed:
                failed.append(nodeid)

    counts: dict[str, int] = {}
    collected = None
    # Final summary line, e.g. "4 failed, 3486 passed, 2 warnings in 123.45s".
    # A short summary is padded into a banner ("===== 1 failed in 0.01s ====="), so
    # strip the padding before matching - otherwise the line is skipped entirely and
    # the counts come out empty even though pytest reported them.
    for raw in reversed(out.splitlines()):
        line = raw.strip().strip("=").strip()
        if SUMMARY_RE.match(line):
            for n, what in COUNT_RE.findall(line):
                key = what.rstrip("s") if what not in ("passed", "failed") else what
                counts[key] = counts.get(key, 0) + int(n)
            break
    # "collected 3490 items"
    cm = re.search(r"^collected\s+(\d+)\s+items?", out, re.MULTILINE)
    if cm:
        collected = int(cm.group(1))
    # Fall back: passed + failed + errors + skipped
    if collected is None and counts:
        collected = sum(counts.get(k, 0) for k in ("passed", "failed", "error", "skipped"))
    return {"failed": sorted(failed), "counts": counts, "collected": collected}


def load_baseline(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"! baseline unreadable ({exc}); treating as absent", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", default=".", help="directory to run the suite in (default: .)")
    ap.add_argument("--baseline", default="pytest_baseline.json", help="baseline file path")
    ap.add_argument("--cmd", default=None, help=f"command template (default: {DEFAULT_CMD!r})")
    ap.add_argument("--save-baseline", action="store_true", help="record this run as the baseline")
    ap.add_argument("--quiet", action="store_true", help="only print the verdict line")
    ap.add_argument("extra", nargs="*", help="extra args appended to the pytest command")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    baseline_path = Path(args.baseline).expanduser()
    if not baseline_path.is_absolute():
        baseline_path = root / baseline_path

    tmpl = args.cmd or DEFAULT_CMD
    cmd = tmpl.format(python=sys.executable)
    argv = shlex.split(cmd) + list(args.extra)

    print(f"$ (cd {root} && {' '.join(argv)})")
    proc = subprocess.run(argv, cwd=root, capture_output=True, text=True)
    out = proc.stdout + "\n" + proc.stderr

    result = parse_pytest_output(out)
    result["cmd"] = " ".join(argv)
    result["timestamp"] = subprocess.run(
        ["date", "-Is"], capture_output=True, text=True
    ).stdout.strip()

    print(f"  collected={result['collected']} counts={result['counts'] or '{}'}")
    for n in result["failed"]:
        print(f"  FAILED {n}")

    if args.save_baseline:
        baseline_path.write_text(json.dumps(result, indent=2) + "\n")
        print(f"baseline saved -> {baseline_path} ({len(result['failed'])} known failures)")
        return 0

    base = load_baseline(baseline_path)
    if base is None:
        print(f"no baseline at {baseline_path}; run with --save-baseline first")
        return 2

    known = set(base.get("failed") or [])
    now = set(result["failed"])
    new = sorted(now - known)
    fixed = sorted(known - now)

    problems = []
    if result["collected"] is None:
        problems.append("could not read a collected-test count from the output")
    elif base.get("collected") is not None and result["collected"] != base["collected"]:
        problems.append(
            f"collected {result['collected']} tests but baseline collected {base['collected']} "
            "-> runs are not comparable (worktree, subset, or an import error can mask failures)"
        )

    if not args.quiet:
        print()
        print(f"NEW ({len(new)}):")
        for n in new:
            print(f"  + {n}")
        print(f"FIXED ({len(fixed)}):")
        for n in fixed:
            print(f"  - {n}")
        print(f"KNOWN ({len(now & known)}):")
        for n in sorted(now & known):
            print(f"  = {n}")

    print()
    if problems:
        for p in problems:
            print(f"! {p}")
    if new:
        print(f"VERDICT: {len(new)} new failure(s)")
        return 1
    if problems:
        print("VERDICT: no new failures, but the run is not comparable to the baseline")
        return 2
    print(f"VERDICT: clean delta ({len(now & known)} known failure(s), none new)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
