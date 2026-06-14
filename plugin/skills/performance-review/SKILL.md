---
name: reviewer_performance-review
description: Review code changes only for performance and efficiency risks (N+1 queries, repeated work, bad asymptotics, missing batching/caching, blocking I/O, memory growth). Use when the user explicitly asks for a performance review of a diff/PR.
---

# Performance Review

## Scope

Standalone: ask the user which diff to review if the scope is not clear:

- `staged` — review only the staged diff;
- `unstaged` — review only the unstaged diff;
- uncommitted changes — staged plus unstaged;
- branch-vs-base — compare the current branch against its base branch (state the
  base branch used; infer from upstream, remote default, or common names: `main`,
  `master`, `develop`, `trunk`);
- commit, branch comparison, file list, or PR-like scope — review exactly that.

Do not pick a scope yourself unless the user already made it clear. If the
resulting diff is empty, stop and say there is nothing to review.

Inside `/reviewer_review-pr`: the orchestrator provides the diffs of all units (path + patch)
— review those. Call the reviewer MCP tools as needed (`search_code`,
`get_related_symbols`, `read_file`, `get_definition`, `find_callers`,
`get_changed_file_diff`) to verify whether a path is truly performance-sensitive.

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

```json
{"findings": [{
  "category": "performance",
  "severity": "low|medium|high|critical",
  "file": "<path of the reviewed file>",
  "line": <line number in the NEW file or null>,
  "side": "RIGHT",
  "code_quote": "<exact line from the new file>",
  "message": "<what is wrong and why it matters>",
  "suggestion": "<short advice or null>",
  "fix": {"start_line": N, "end_line": M, "replacement": "<new code>"} | null,
  "confidence": 0.0
}]}
```

Standalone runs may additionally render the findings as a readable list after the JSON.

If a finding cannot be tied to a specific line, use the closest changed line and
explain the scope in `message`.

If there are no meaningful performance findings, return `{"findings": []}` and say so.

Write `message` and `suggestion` in the output language given by the orchestrator
(standalone: the user's language).
