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

0. **Preflight (index freshness + task-corpus warm-up).** Run this BEFORE anything else.
   First resolve, once, the repo path (`git rev-parse --show-toplevel`) and the working branch
   (`git branch --show-current`; if it is in `REVIEW_BRANCHES` use it, else the primary branch) —
   step 3 reuses the same branch for `search_codebase`.

   1. **Base-index freshness.** Run
      `uvx --from rag-reviewer reviewer status <path> --branch <branch> --json` and read `drift`
      for that branch:
      - `drift == 0` → continue;
      - `drift > 0` → tell the user (in Russian) «индекс отстаёт на N коммитов» and **ask for
        confirmation**: reindex now? **Yes** → delegate to `/reviewer_sync-codebase`
        (`--path <path> --ref <branch>`), which reindexes and reports problems, then continue;
        **No** → continue on the stale index and record the gap under **Constraints / open
        questions** in the brief;
      - `drift == null` (no clone / no index record) → do not block; note it in the brief.
   2. **Problem report — in the style of `sync-codebase`.** If `reviewer status` fails (Postgres /
      reviewer MCP / Neo4j unreachable, no index, or `uvx` missing): tell the user (in Russian)
      what is missing and the command to fix it. **Fail-open** — never abort; continue on the
      stale/unknown index.
   3. **Warm the task corpus.** Call
      `sync_board(board=<task_board.project or null>, board_type=<task_board.type or null>, limit=null, purge_orphaned=false)` —
      `task_board.type` и `task_board.project` берутся из `<root>/.review.yml` (прочитай здесь,
      до вызова `sync_board`; при отсутствии файла или блока `task_board` — используй `null`).
      Скоупированный прогрев корпуса своего проекта (PRI-170); пустой project → весь корпус.
      Incremental (timestamp watermark), cheap when the corpus is warm. Board not configured or
      `status=error` → print the `TASK_BOARD_*` hint and continue board-less.
   4. **Summary warmth.** Call `get_subsystem_summaries(repo, branch)` (without `query`) and check
      the returned count. Skip this check if `drift == null` (no index at all — summaries can't
      exist). If count == 0 (summaries not built yet):
      - Tell the user (in Russian): «Сводки подсистем не построены — архитектурный приор будет
        пустым. Как поступим?» and present **three options**:
        1. «Прогреть сейчас» → delegate to `/reviewer_summarize-subsystems`, wait for it to
           complete, then continue. (Good if using the default model.)
        2. «Прогрею сам» → **PAUSE HERE** and wait for the user to write something like
           «готово», «прогрел», «done» or any confirmation that they have run their own tool
           (e.g. an external CLI with a cheaper model). Once confirmed, call
           `get_subsystem_summaries(repo, branch)` again to verify count > 0, then continue.
        3. «Пропустить» → note in brief under **Constraints**: «сводки подсистем не построены;
           `/reviewer_summarize-subsystems` не запускался». Continue without them.
      - If count > 0: silently continue (no message needed — summaries are warm).
      - Fail-open: an error from `get_subsystem_summaries` → treat as count == 0 and offer the
        same options, but include the error detail in option 3's Constraints note.

   Decisions: stale → confirmation, never auto (Voyage free tier is 3 RPM / 10K TPM); failures →
   reported like `sync-codebase`; `sync_board` runs incrementally at start; summaries missing →
   three-way choice (build now / build yourself / skip).

1. **Config.** Resolve the `task_board` block (`type`, `mcp`, `key_pattern`, `project`): first from the repo's
   `.review.yml`, and if there is no block there, from the deploy-wide default via
   `get_board_config()` (reviewer MCP) — so a per-repo `.review.yml` is not required when the board
   is configured once in the reviewer deploy (`TASK_BOARD_*` env). If a board is resolved, its tools
   are `mcp__<task_board.mcp>__*`. No block anywhere (`get_board_config()` → `null`), or the board MCP
   is not connected → board-less mode (continue without it).

2. **Identify the task.**
   - If `$ARGUMENTS` matches the board's `key_pattern`:
     1. **Store-first.** Call reviewer `get_task(key, project=<task_board.project>)` — it returns the task's own normalized
        content (`{key, aliases[], title, description, criteria[], status, url}`) from the reviewer
        store, which the preflight `sync_board` (step 0.3) just refreshed.
        - **Hit** (a task object with a `key`): use it directly as the `TaskBrief`. The task is
          already indexed (the preflight sync persisted it) — do NOT call `index_task`. Note in the
          brief that the task data came from the reviewer store (after sync).
        - **Miss** (`null` / no `key`) AND a board is configured/connected: read the task via the
          playbook `../review-pr/references/task-context-<task_board.type>.md`, build a `TaskBrief`
          `{key, aliases[], title, description, criteria[], status, url, links[]}`, then call
          `index_task(TaskBrief)` to persist it (idempotent — safe to repeat).
        - **Miss** AND no board (or board MCP not connected): board-less — treat `$ARGUMENTS` as the
          task description.
   - Otherwise: treat `$ARGUMENTS` as the task description; do not read the board.

   Store-first cuts the double-fetch: the preflight `sync_board` already pulled the whole board into
   the reviewer store, so a single read of our own store avoids re-enumerating the board via board-MCP
   (fewer LLM tokens, fewer external deps). The board-MCP fallback stays for misses and for boards
   without a REST provider.

3. **Gather context (best-effort, fail-open).** Any tool returning a "(… unavailable)" / "(ничего не
   найдено)" note or an error is non-fatal — continue.
   - **Subsystem prior (architectural map).** Call
     `get_subsystem_summaries(repo, branch, query="<task title>. <first lines of description>")`
     → top-k relevant subsystems by proximity (top-k vs all is server-side; PRI-167).
     Use the same `branch` as `search_codebase`. Fail-open: an empty list / a `(… недоступно)`
     note / an error is non-fatal — omit the `## Subsystems` brief section and note the gap.
   - **Project scope.** Pass `project=<task_board.project>` (from Step 1; empty = unscoped) to
     `get_task`, `get_task_context`, and `search_tasks` so only this repo's project surfaces (PRI-170).
   - If you have a task key: `get_task_context(key, project=<task_board.project>)` → linked tasks, their PRs, and the code those PRs
     touched.
   - `search_tasks("<title>. <first lines of description>", project=<task_board.project>)` → semantically similar tasks. If a board
     is connected, you may read the most relevant similar tasks from the board for fuller detail.
   - `search_codebase("<task description>")` → relevant existing code (files/symbols to touch or
     mimic).
   - **Deepen via the code graph (optional — when `search_codebase` surfaced concrete symbols).**
     `search_codebase` chunks are headed by `path#fqn (path:start-end)`; feed those `node_id`s to the
     session-less graph tools to sharpen the brief. `search_codebase` now returns deduplicated,
     line-numbered, test-free snippets — expand only the few symbols central to the task, and cite
     `path:line` from the line-numbered snippets directly (no re-Read needed for grounding).
     Pass the same `branch` you pass to `search_codebase`.
     Fail-open: a `(граф недоступен)` / `(нет связей)` / `(вызовов не найдено)` note is non-fatal — continue.
   - **Lazy PR diff (optional).** `get_task_context` surfaces a task and its PRs (id form
     `owner/name#N`); `search_tasks` surfaces similar task keys — fetch a key's context to see
     its PRs. If a related task passed the relevance filter AND its PR is worth inspecting for
     the implementation, parse `repo`/`number` from the PR id and call `get_pr_diff(repo, number)`
     to see what that PR changed — pull it lazily, only when the LLM judges it useful (don't
     fetch diffs for low-relevance tasks).
     Fail-open: a `(diff PR недоступен)` / `(repo не задан…)` note is non-fatal — continue.
   - **Relevance signals → Step 4 filter.** `search_tasks` `score` is an RRF rank score
     (≈0.016–0.033), not comparable across queries; `search_codebase` has no score, only order.
     Carry *rank/order* — not absolute score — into the Step 4 filter, and fetch `get_pr_diff`
     only for a related task that survives that filter (within top-3, directly informing).

<!-- include: _common/tool-usage.md -->
Use the session-less tools above.

   **Branch selection for `search_codebase`.**

<!-- include: _common/branch-selection.md -->

4. **Distill the solution brief.** Write a structured markdown brief whose only job is to seed
   `brainstorming` — compact, scannable, nothing the implementer won't act on.

   **Relevance filter (rank-based, no absolute score cutoff).** `search_tasks` returns a per-result
   `score`, but it is an RRF rank score (`SUM(1/(60+rank))`, ≈0.016–0.033) — NOT comparable across
   queries, so never gate on an absolute value. `search_codebase` exposes no score at all, only
   result order. Therefore:
   - **Order** candidates by result rank (tasks: rank/score; code: rank).
   - **Caps (ceilings — take fewer if that's enough):** ≤3 related tasks · ≤5 files/symbols in
     Relevant code. Expand the graph (`related_symbols`/`callers`/`definition`) only for the few
     symbols central to the task.
   - **Keep/drop is a binary judgment** — include an item ONLY if it *directly informs the
     implementation*. Rank/score only sets review order and breaks ties at the cap; it is not a
     numeric gate.
     - ✅ INCLUDE: a symbol/file you will edit or mimic; a task whose PR shows a concrete pattern to
       follow; a constraint that narrows the approach.
     - ❌ EXCLUDE: a task in the same area but a different mechanism; a file the search surfaced that
       you won't touch or copy; background you won't act on.
   - **Report what you dropped:** end the Related work and Relevant code sections with
     `(dropped N: reason)`.

   **Brief skeleton — fill it, keep each item to one line:**

   ```
   # Brief — <KEY> <title>
   ## Task — key/title/requirements/criteria (or the user's formulation in board-less mode). ≤~6 lines.
   ## Related work — ≤3 tasks, one line each: «KEY — what to reuse / follow». (dropped N: …)
   ## Subsystems — ≤8 relevant subsystems, one line: «cluster_key — gist of summary». (omit if prior empty)
   ## Relevant code — ≤5 files/symbols, one line: «path:line — why» (+ blast radius from the graph). (dropped N: …)
   ## Constraints / open questions — terse bullets: limits, unknowns, context gaps (e.g. "board unavailable", "task corpus empty").
   ```

   Cite `path:line` straight from the line-numbered Step 3 snippets — no re-Read (Step 3 contract).

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
