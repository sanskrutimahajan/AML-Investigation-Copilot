# ADR 007: Entity resolution (Phase 2)

- **Status:** Open. The Phase 2 decisions (label-noise fix, homonym stratum, shared attribute
  function, the homonym leak judgment call, the determinism fix, and Step A/B results) are still
  being verified. Following the working rule in CLAUDE.md, they are logged in
  [docs/worklog.md](../worklog.md) and will be summarized here once resolved: fix implemented, tests
  passing, verification re-run.
- **Already approved and settled** (see the approved plan,
  [docs/plans/phase2-entity-resolution.md](../plans/phase2-entity-resolution.md)):
  - scope: customer ↔ source entity only;
  - dependencies, with anyascii in place of GPL unidecode;
  - LLM adjudication in two steps (the Anthropic API, cost estimate and spend cap are superseded
    by [ADR 008](008-llm-provider.md): local Ollama, nothing billed);
  - a 50/50 split grouped by `source_entity_id` and stratified by role and seed reason, with the
    test split scored once;
  - seed 42 as the official seed.

## Resolved so far (2026-11-15)
- **Homonym stratum and shared attributes:**
  - 375 homonyms (`homonym_rate` 0.01) carry a real record's exact name, with a conflicting DOB
    or country drawn from the shared attribute rule.
  - Every non-name attribute, for every role, comes from one function.
  - Chi-square and KS tests check attribute independence across roles.
- **ICIJ sampling determinism:** the `unique()` mask misalignment is fixed, and the determinism
  test fails with the bug and passes with the fix.
- **Leakage baseline:**
  - Four comparisons moved more than ±0.025 between the accepted run and the corrected stage.
    Ten evaluation draws on one stage showed that this was a real data change caused by the ICIJ
    fix, not noise; most comparisons became less leaky.
  - The user accepted the corrected stage (2026-11-15).
  - After the address-determinism fix (ADR 006), one run gave a **maximum of 0.621** (homonyms vs
    plain, GB). That is above the 0.60 target and within the 0.625 ceiling the user set for the
    homonym judgment call.
  - Every comparison involving seeds is at most 0.569.
  - Reported as it came out (`eval/results/leak_test_20261115T060250Z.json`, README).
  - ER must beat this baseline and report its homonym false-positive rate.

Still open: Step A on dev, then label-noise verification, Step B and the single test-split score.
