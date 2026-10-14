#!/usr/bin/env bash
# Report which test failures are new since a recorded baseline.
#
# Why: after a merge the suite reports N failures and nothing separates a new
# regression from a known pre-existing one. Record the failures of a known-good
# state once, then ask what changed since. It also refuses to compare two runs
# that collected different numbers of tests, so a subset or a broken collection
# can't quietly hide failures.
#
# Usage:
#   scripts/test-delta.sh                 # report NEW / FIXED / KNOWN vs the baseline
#   scripts/test-delta.sh --save-baseline # re-record the baseline (from a known-good main)
#   scripts/test-delta.sh --quiet         # verdict line only
#   scripts/test-delta.sh -- tests/agent/test_memory_store.py   # extra args go to pytest
#
# The suite runs through scripts/run-tests.sh, so the same memory cap applies.
# The baseline lives in .pytest-baseline.json (git-ignored, per checkout).
# Exit codes: 0 = no new failures, 1 = new failures, 2 = runs not comparable.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec uv run --script "$ROOT/scripts/pytest_delta.py" \
    --root "$ROOT" \
    --cmd "scripts/run-tests.sh -rfE --tb=no -p no:cacheprovider" \
    --baseline "$ROOT/.pytest-baseline.json" \
    "$@"
