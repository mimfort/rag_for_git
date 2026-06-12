---
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
   - `skipped_paths`, `skip_drafts`, `suggestions_mode`

   If `pr.draft` is true and `skip_drafts` is true, stop and tell the user.
   Note `policy.output_language` — ALL finding messages, suggestions and the summary
   MUST be written in that language.

2. **Analyze (fan-out).** For each unit in `units`, dispatch a subagent (Task tool,
   run independent subagents in parallel; batch units if there are more than ~10) with:
   - the contents of `references/analyze-prompt.md` (read it once, include verbatim);
   - the unit's `path` and `patch`, the PR `title`/`body`;
   - the repo/pr identifiers so the subagent can call the reviewer MCP tools
     (`search_code`, `get_related_symbols`, `read_file`, `get_definition`,
     `find_callers`, `get_changed_file_diff`);
   - the target output language.
   Each subagent returns a JSON object `{"findings": [...]}` (schema in the prompt).

3. **Dimensions (parallel with step 2).** Dispatch two whole-diff subagents:
   - performance: follow the methodology of `../performance-review/SKILL.md`
     (Goal, Method, Severity sections);
   - maintainability: follow `../maintainability-review/SKILL.md`.
   Both must return the same findings JSON schema (category `performance` /
   `maintainability`).

4. **Verify.** Collect all findings into one numbered list. Dispatch one subagent
   with `references/verify-prompt.md`, the findings list and the diffs. It returns
   `{"verdicts": [{"index": N, "is_real": true|false}]}`. Drop findings with
   `is_real=false`. If the verifier fails or returns malformed output, KEEP all
   findings (recall-safe).

5. **Publish.** Compose a short review summary (2-5 sentences, in
   `policy.output_language`): what the PR does, overall assessment, key risks.
   Mention files that were not analyzed: failed subagents and `skipped_paths`
   from the prepare payload. Call `publish_review(repo, pr, summary, findings,
   dry_run)`. Report to the user: posted/dry-run, inline count, and the report
   counters (dropped_by_gate/deduped/invalid/already_posted/moved_to_summary/capped),
   run_id.

## Failure handling

- A failed analyze subagent must not abort the run: continue with the other units
  and mention the skipped file in the summary.
- If `prepare_review` fails, surface its error text to the user as-is (it contains
  the remediation hint, e.g. "docker compose up -d").
- Never post comments yourself via gh/git — only through `publish_review`.
