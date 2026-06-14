---
name: reviewer_review-pr
description: Review a GitHub pull request with the RAG + code-graph pipeline (reviewer MCP server). Use when the user asks to review a PR ("review PR 123", "заревьюй PR", a PR URL). Requires ParadeDB/Neo4j running and a built base index.
---

# PR Review Pipeline

Orchestrate a full PR review using the `reviewer` MCP server tools. The deterministic
tail (policy gate, line grounding, dedup, idempotency, comment cap, publishing) is
handled by `publish_review` — your job is analysis quality, not formatting rules.

## Inputs

Parse from $ARGUMENTS: target PR as `owner/repo#N`, `owner/repo N`, or a GitHub PR URL.
`--dry-run` flag → pass `dry_run=true` to publish_review and show the report instead
of posting.

## Pipeline

1. **Prepare.** Call `prepare_review(repo, pr)`. The payload contains:
   - `pr`: `{number, title, body, base_sha, head_sha, base_ref, draft}`
   - `policy`: `{severity_threshold, min_confidence, max_comments, categories, ignore, output_language}`
   - `units`: list of `{path, patch, commentable_right, commentable_left}`
   - `task_board`: `{type, mcp, key_pattern}` or null — task board config from `.review.yml`
   - `task_keys`: `{primary, others}` or null — task keys extracted from the PR by the server
   - `skipped_paths`, `skip_drafts`, `suggestions_mode`

   If the payload has `status: "skipped"`, this is NOT an error but an expected skip
   (the PR's target branch is not in `REVIEW_BRANCHES`). Tell the user the `reason`
   value and stop: do not run analyze/publish, and do not treat it as a failure.

   If `pr.draft` is true and `skip_drafts` is true, stop and tell the user.
   Note `policy.output_language` — ALL finding messages, suggestions and the summary
   MUST be written in that language.

2. **Task context (optional).** Only if `task_board` is non-null. Resolve the task key: an
   explicit key in `$ARGUMENTS` wins; otherwise use `task_keys.primary`. If no key is available,
   skip this step and note in the summary that no task key was found.
   With a key, read the task using the playbook for `task_board.type`
   (`references/task-context-yougile.md` or `references/task-context-jira.md`): call the board MCP
   server named by `task_board.mcp` and build a `TaskBrief`
   `{key, aliases[], title, description, criteria[], status, url, links[]}`.
   If the board MCP is not connected, the tool errors, or the task is not found: skip the
   requirements dimension and note the reason in the summary — NEVER abort the review.

   The `TaskBrief` schema is `{key, aliases[], title, description, criteria[], status, url, links[]}`
   (phase 3 adds `aliases[]` and uses `links[]`; see the board playbook for how to fill them).
   Once the `TaskBrief` is built, call `index_task(TaskBrief)` to persist it (idempotent — safe to
   repeat). Then gather task context to sharpen the requirements check:
   - `get_task_context(TaskBrief.key)` → linked tasks, their PRs, and the code those PRs touched;
   - `search_tasks("<TaskBrief.title>. <first lines of description>")` → semantically similar tasks.
   Keep ONLY the related/similar items that look relevant; you will pass them to the requirements
   dimension in step 4. All of this is best-effort: if `index_task`/`get_task_context`/`search_tasks`
   return a "(… unavailable)" note or error, continue — never abort the review.

3. **Analyze (fan-out).** For each unit in `units`, dispatch a subagent (Task tool,
   run independent subagents in parallel; batch units if there are more than ~10) with:
   - the contents of `references/analyze-prompt.md` (read it once, include verbatim);
   - the unit's `path` and `patch`, the PR `title`/`body`;
   - the repo/pr identifiers so the subagent can call the reviewer MCP tools
     (`search_code`, `get_related_symbols`, `read_file`, `get_definition`,
     `find_callers`, `get_changed_file_diff`);
   - the target output language.
   Each subagent returns a JSON object `{"findings": [...]}` (schema in the prompt).

4. **Dimensions (parallel with step 3).** Dispatch whole-diff subagents:
   - performance: follow the methodology of `../performance-review/SKILL.md`
     (Goal, Method, Severity sections);
   - maintainability: follow `../maintainability-review/SKILL.md`;
   - requirements (ONLY if a `TaskBrief` was built in step 2): dispatch one subagent with
     `references/requirements-prompt.md`, the diffs of all units (path + patch), the `TaskBrief`,
     plus the related/similar task context gathered in step 2 (linked tasks, their PRs, touched code,
     similar tasks) as an optional "Related context" block, the repo/pr identifiers (so it can call
     the reviewer MCP tools), and the target output language. It returns the same findings JSON
     schema with category `requirements`.
   Give the performance/maintainability subagents: the diffs of all units (path + patch), the
   repo/pr identifiers so they can call the reviewer MCP tools, and the target output language.
   They must return the same findings JSON schema (category `performance` / `maintainability`).

5. **Verify.** Collect all findings into one numbered list. Dispatch one subagent
   with `references/verify-prompt.md`, the findings list, the diffs, and the
   repo/pr identifiers so the subagent can call the reviewer MCP tools
   (`read_file`, `search_code`, `find_callers`, `get_definition`). It returns
   `{"verdicts": [{"index": N, "is_real": true|false}]}`. Drop findings with
   `is_real=false`. If the verifier fails or returns malformed output, KEEP all
   findings (recall-safe).

6. **Publish.** Compose a short review summary (2-5 sentences, in
   `policy.output_language`): what the PR does, overall assessment, key risks.
   If a task was read, state whether the PR meets the task's requirements; if the task context was
   requested but unavailable (no key, board MCP not connected, task not found), say so briefly.
   Mention files that were not analyzed: failed subagents and `skipped_paths`
   from the prepare payload. Call `publish_review(repo, pr, summary, findings, dry_run, task_key)`
   where `task_key` is the canonical `TaskBrief.key` if a task was read (else omit / null). When
   published, this links the PR to the task in the graph for future reviews. Report to the user:
   posted/dry-run, inline count, and the report counters
   (dropped_by_gate/deduped/invalid/already_posted/moved_to_summary/capped), run_id.

## Failure handling

- A failed analyze subagent must not abort the run: continue with the other units
  and mention the skipped file in the summary.
- A `prepare_review` payload with `status: "skipped"` is not a failure: report its
  `reason` (target branch not tracked in `REVIEW_BRANCHES`) and stop without analyze/publish.
- If `prepare_review` fails, surface its error text to the user as-is (it contains
  the remediation hint, e.g. "docker compose up -d").
- Never post comments yourself via gh/git — only through `publish_review`.
