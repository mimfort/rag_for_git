# PRI-296 Test Exemplars Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. The steps use checkboxes.

**Goal** — Reserve all slots in `Test exemplars` for test paths and measure the effect on the three reported requests.

**Architecture** — Pass a private test-only option from task context to multi-query selection. Apply the filter after graph expansion and before file diversification.

**Tech Stack** — Python, pytest, ParadeDB, Neo4j, Voyage embeddings.

**Spec** — `docs/superpowers/specs/2026-11-21-pri-296-test-only-exemplars-design.md`.

## Constraints

Keep the existing file budget, public search behavior, classification of test paths and failure handling. Use one pinned index for both sides of the evaluation.

## Review focus

Exercise a test ranked below ordinary code, a graph-only test, a result with no tests, mixed public search, and a test path under `tests/` with a non-test basename.

## Task 1 — Test-only selection

- [ ] Extend `tests/retrieval/test_multiquery.py` with a regression for filtering before the file budget, graph candidates and no matches. Update the service wiring check in `tests/mcp/test_service.py`. Run and confirm the expected failures.
- [ ] Add the private keyword `tests_only` to `reviewer/retrieval/multiquery.py` and `reviewer/mcp/service.py`. Enable it only for `_TaskContextDeps.test_exemplars`, and prevent ordinary paths from entering through augmentation.
- [ ] Run the retrieval and MCP tests, including the existing public mixed-search contract.

## Task 2 — Acceptance measurements

- [ ] Record a test-path reference set for each task based on the indexed code and the card. Run paired searches with the same index, queries, and limit for PRI-278, PRI-291, and PRI-256.
- [ ] Record test share, coverage of labeled test paths, overlap with the code section, and measured latency in `eval/pri296/report.md`. Document the reproduction procedure and the scope of the measurements.
- [ ] Run the unit suite and check the changes against the design and card.
