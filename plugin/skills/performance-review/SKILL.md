---
name: reviewer_performance-review
description: Review code changes only for performance and efficiency risks (N+1 queries, repeated work, bad asymptotics, missing batching/caching, blocking I/O, memory growth). Use when the user explicitly asks for a performance review of a diff/PR.
---

# Performance Review

<!-- include: _common/dimension-scope.md -->

<!-- include: _common/tool-usage.md -->
Use the PR-session tools above.

## Goal

Look only for performance and efficiency risks in the selected changes. Ignore style,
architecture, tests, and general correctness unless they materially affect performance.

Prioritize findings such as:

- N+1 queries and repeated remote calls;
- unnecessary loops or repeated work;
- bad asymptotic behavior on hot paths;
- redundant rendering, serialization, parsing, allocations, or avoidable copies;
- missing batching, caching, pagination, or streaming where the diff makes that risk
  likely;
- blocking I/O or CPU-heavy work on latency-sensitive paths;
- memory growth or large payload handling.

## Method

1. Read the diff first.
2. Open only the nearby code needed to understand whether the changed path is
   performance-sensitive. In `/reviewer_review-pr` use the reviewer MCP tools: `read_file`,
   `search_code`, `find_callers`.
3. Prefer concrete findings over vague perf speculation.
4. If a concern depends on an assumption, state that assumption explicitly.
5. If a path is probably not performance-sensitive, do not invent issues.

## Severity

- `critical` / `high`: likely severe latency, throughput, or resource regression on
  an important path.
- `medium`: meaningful inefficiency or scaling risk that should probably be fixed.
- `low`: worthwhile optimization or preventive note, not a blocker.

## Output

Return only actionable findings.

Return ONLY the findings JSON used by the review pipeline, with
`"category": "performance"`:

<!-- include: _common/findings-schema.md -->
Set "category" to "performance"; "side" is always "RIGHT".

<!-- include: _common/dimension-output-tail.md -->
