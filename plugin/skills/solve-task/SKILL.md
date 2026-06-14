---
name: reviewer_solve-task
description: Gather disciplined context for solving a task, then hand off to development. Use when the user asks to solve/implement a task ("solve PRI-4", "/reviewer_solve-task <key or description>", "реши задачу X"). Reads the task from a connected board (if a key + board), pulls related/similar tasks and relevant code, distills a brief, and enters brainstorming. Requires the reviewer MCP server (and optionally a board MCP).
---

# Solve Task

Gather the right context for a task, distill it into a brief, then enter the normal development
workflow. This skill does NOT plan or implement — it disciplines context-gathering and hands the
brief to `superpowers:brainstorming` (which leads to writing-plans → subagent-driven-development).

## Inputs

`$ARGUMENTS` is either:
- a task key (e.g. `PRI-4`, matching the board's `key_pattern`), or
- a free-text description (e.g. "add a logout endpoint").

## Pipeline

1. **Config.** Resolve the `task_board` block (`type`, `mcp`, `key_pattern`): first from the repo's
   `.review.yml`, and if there is no block there, from the deploy-wide default via
   `get_board_config()` (reviewer MCP) — so a per-repo `.review.yml` is not required when the board
   is configured once in the reviewer deploy (`TASK_BOARD_*` env). If a board is resolved, its tools
   are `mcp__<task_board.mcp>__*`. No block anywhere (`get_board_config()` → `null`), or the board MCP
   is not connected → board-less mode (continue without it).

2. **Identify the task.**
   - If `$ARGUMENTS` matches the board's `key_pattern` AND a board is configured/connected: read the
     task via the playbook `../review-pr/references/task-context-<task_board.type>.md` and build a
     `TaskBrief` `{key, aliases[], title, description, criteria[], status, url, links[]}`. Then call
     `index_task(TaskBrief)` to persist it (idempotent — safe to repeat).
   - Otherwise: treat `$ARGUMENTS` as the task description; do not read the board.

3. **Gather context (best-effort, fail-open).** Any tool returning a "(… unavailable)" / "(ничего не
   найдено)" note or an error is non-fatal — continue.
   - If you have a task key: `get_task_context(key)` → linked tasks, their PRs, and the code those PRs
     touched.
   - `search_tasks("<title>. <first lines of description>")` → semantically similar tasks. If a board
     is connected, you may read the most relevant similar tasks from the board for fuller detail.
   - `search_codebase("<task description>")` → relevant existing code (files/symbols to touch or
     mimic).

   **Branch selection for `search_codebase`.** Before calling `search_codebase`, determine the
   current git branch of the project: `git branch --show-current`. If it is in `REVIEW_BRANCHES`
   (the tracked branches list), pass it as the `branch` parameter — the search will use that
   branch's index. If the user explicitly stated which branch to work from, use that branch instead.
   Otherwise, omit `branch` entirely and the server will use the primary branch (the first entry in
   `REVIEW_BRANCHES`).

4. **Distill the solution brief.** Write a structured markdown brief. Apply a strict relevance
   filter: include an item ONLY if it directly informs the implementation; drop the rest and note how
   many were dropped. Sections:
   - **Task** — key/title/requirements/criteria (or the user's formulation in board-less mode).
   - **Related work** — only the relevant linked/similar tasks and their PRs (what to reuse / follow).
   - **Relevant code** — files/symbols to touch or mimic, each with a one-line "why".
   - **Constraints / open questions** — limits, unknowns, and context gaps (e.g. "board unavailable",
     "task corpus empty").

5. **Hand off to development.** Show the brief, then invoke `superpowers:brainstorming` with the brief
   as the seed/context. From there the normal cycle takes over (brainstorming → writing-plans →
   subagent-driven-development/TDD). Your job ends at the handoff — do NOT plan or implement here.

## Failure handling (fail-open)

- No `task_board` / board MCP not connected / task not found → board-less: build the brief from
  `search_tasks` (if the corpus is warm) + `search_codebase` + the user's formulation; note the gap.
- Neo4j down → `get_task_context` / `index_task` graph parts degrade (empty + warning); build the
  brief from `search_tasks` + `search_codebase`.
- Empty task corpus (no prior `/reviewer_sync-tasks` or reviews) → `search_tasks` is empty; rely on the board
  (if a key) + `search_codebase`.
- Postgres down → `search_codebase` / `search_tasks` return empty; build the brief from the board (if
  a key) or the user's formulation alone; still hand off to brainstorming.
- Never abort: with any gap, distill what you have, note the deficit in the brief, and still hand off
  to brainstorming.
- Read-only on the board; this skill never writes to it.
