# Cron Correctness and Maintenance TODO

## Checklist

- [x] Audit and close or update GitHub issues #18–#24 with commit/test evidence
- [x] Fix #13 cron list output for `silent`, `isolated`, and model preset values
- [x] Correct stale README claims without adding fork productization
- [x] Add minimal GitHub Actions CI for Ruff and pytest
- [x] Design #25 misfire, claim/lease, bounded concurrency, and diagnostics behavior
- [ ] Implement and test the agreed #25 correctness tranche
- [ ] Run targeted and full validation
- [ ] Commit, merge to `main`, push, and deploy to `uhl`

## Log

### 2026-09-02

- Recovered the accepted plan from the previous Prime Agent refinement.
- Confirmed a clean `main` at `3f1e2402` and created `feat/cron-correctness-and-maintenance`.
- Started parallel Luna audits for issue closure evidence, #13, README/CI, and #25 design.
- Closed #19, #21, and #24 with implementation evidence; posted exact remaining gaps on #18, #20, #22, and #23.
- Implemented #13 list visibility with 107 cron tests passing.
- Confirmed #25 needs staged misfire/diagnostics, bounded concurrency, and transactional claim work; stage 1 implementation is in progress.
- Replaced the stale upstream-facing README with an accurate private-fork overview and source workflow.
- Added minimal GitHub Actions CI for Ruff and pytest.
