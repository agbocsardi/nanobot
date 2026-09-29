#!/usr/bin/env bash
# Run the test suite inside a memory-capped sandbox.
#
# Why: this machine runs earlyoom, and a runaway test (an unbounded loop that
# allocates per iteration) can grow to multiple GB and get the *server* killed.
# Capping the test process means a runaway is terminated on its own instead.
#
# Usage:
#   scripts/run-tests.sh                       # whole suite
#   scripts/run-tests.sh tests/agent -q        # any pytest args are forwarded
#
# Override the cap with NANOBOT_TEST_MEMORY_MB (default 3072).
set -euo pipefail

LIMIT_MB="${NANOBOT_TEST_MEMORY_MB:-3072}"
PYTEST=(uv run --extra dev python -m pytest)

if systemd-run --user --scope -q true >/dev/null 2>&1; then
    exec systemd-run --user --scope -q \
        -p "MemoryMax=${LIMIT_MB}M" -p MemorySwapMax=0 \
        -- "${PYTEST[@]}" "$@"
fi

# Fallback: cap the address space of this process tree.
exec prlimit --as=$((LIMIT_MB * 1024 * 1024)) -- "${PYTEST[@]}" "$@"
