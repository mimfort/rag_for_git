---
name: solve-task
description: Gather disciplined context for solving a task, then hand off to development. Use when the user asks to solve/implement a task ("solve PRI-4", "rag-reviewer:solve-task <key or description>", "реши задачу X"). Reads a keyed task from the reviewer store, pulls related/similar tasks and relevant code, distills a brief, and enters brainstorming. Requires the reviewer MCP server.
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

0. **Startup: survey + Preflight (index freshness + task-corpus warm-up).** Run this BEFORE anything else.
   First resolve, once, the repo path (`git rev-parse --show-toplevel`) and the working branch
   (`git branch --show-current`; if it is in `REVIEW_BRANCHES` use it, else the primary branch) —
   step 3 reuses the same branch for `search_codebase`.

   **Resolve `task_board` exactly once before any board call.** Read the repo `.review.yml` block:
   its `task_board` mapping has priority; an absent block falls back to `get_board_config()`;
   an explicit empty `task_board:` disables board work for this run. Keep the resolved
   `{type, project, key_pattern, create_target, done_target, options}` value and **reuse this resolved value** in Step 1 and every board operation. Never call the deploy fallback when the
   repo explicitly disables the board.

   0. **Startup survey.** Ask the user, in **one panel** (`AskUserQuestion`), three questions at
      once. This is the only survey of the run: none of the three is asked again later. Talk to the
      user in Russian.
      1. **Brief model tier** — `cheap` / `mid` (recommended) / `premium`. Phrase the choice by
         tier, not by concrete model names, so it works across CLIs (Claude Code, Codex, Gemini,
         Cursor, …). Do not recommend a coarse tier such as Fable — the brief still needs sound
         judgment. This question replaces the former Step 1.5.
      2. **Interaction mode** — three values; the option text must **explain what it means**:
         - `normal` — «вопросы на брейншторме, апрув спеки и апрув плана» (current behaviour);
         - `auto` — «вопросы задаются, апрувы спеки и плана не запрашиваются»;
         - `full-auto` — «вопросы не задаются, на каждой развилке берётся рекомендованный вариант,
           апрувы не запрашиваются». Add the cost to the same option text: «уместен для задач с
           полным описанием и критериями; для расплывчатых формулировок подавляет канал, по
           которому в дизайн попадает недостающая информация».
      3. **Execution strategy** — `inline` (superpowers:executing-plans), `subagent`
         (superpowers:subagent-driven-development as-is), `lite` (the profile at
         `_profiles/execution-lite.md`), `auto` (resolved by the rubric in Step 5 after the plan is
         written). Asked now, applied later.

      **Defaults (fail-open).** No answer, a decline, or a headless / `non-interactive` run → tier
      `mid`, mode `normal`, strategy `subagent`. In a headless / `non-interactive` run do not show
      the panel at all and apply those defaults silently. Otherwise the panel is always shown.
      **never block** — the survey must not stop the pipeline under any circumstance.

      **The mode governs the preflight questions below.** In `full-auto`, do not ask the
      confirmations of steps 1 and 4 (stale index, missing summaries): take the recommended option
      in each (reindex; warm the summaries) and record each one as a decision made on the user's
      behalf, per Step 4's run-state file. In `normal` and `auto`, ask them as written.

   1. **Base-index freshness.** Run
      `uvx --from rag-reviewer reviewer status <path> --branch <branch> --json` and read `drift`
      for that branch:
      - `drift == 0` → continue;
      - `drift > 0` → tell the user (in Russian) «индекс отстаёт на N коммитов» and **ask for
        confirmation**: reindex now? **Yes** → delegate to `rag-reviewer:sync-codebase`
        (`--path <path> --ref <branch>`), which reindexes and reports problems, then continue;
        **No** → continue on the stale index and record the gap under **Constraints / open
        questions** in the brief;
      - `drift == null` (no clone / no index record) → do not block; note it in the brief.
   2. **Problem report — in the style of `sync-codebase`.** If `reviewer status` fails (Postgres /
      reviewer MCP / Neo4j unreachable, no index, or `uvx` missing): tell the user (in Russian)
      what is missing and the command to fix it. **Fail-open** — never abort; continue on the
      stale/unknown index.
   3. **Warm the task corpus.** Call
      `sync_board(board=<task_board.project or null>, board_type=<task_board.type or null>,
      provider_options=<task_board.options or {}>, limit=null, purge_orphaned=false)` —
      the resolved `task_board.type`, `task_board.project`, and `task_board.options` from this
      preflight. If board work is disabled or no board resolves, skip this call and continue
      board-less.
      Скоупированный прогрев корпуса своего проекта (PRI-170); пустой project → весь корпус.
      Incremental (timestamp watermark), cheap when the corpus is warm. Board not configured or
      `status=error` → tell the user to run `reviewer init`, configure the selected registered
      provider's registry-declared credentials as documented in `docs/board-providers.md`, run
      `reviewer check`, and reconnect MCP; continue board-less.
   4. **Summary warmth.** Read `summaries` from the branch object of the Step 0.1 status payload —
      do NOT probe the summaries tool here. Skip this check if `drift == null` (no index at all —
      summaries can't exist). If Step 0.1 produced no payload at all (the fail-open path in Step
      0.2), treat that the same as the key being absent below.
      - `summaries > 0` → silently continue (no message needed — summaries are warm).
      - `summaries == 0` (summaries not built yet) → tell the user (in Russian): «Сводки подсистем
        не построены — архитектурный приор будет пустым. Как поступим?» and present **three
        options**:
        1. «Прогреть сейчас» → delegate to `rag-reviewer:summarize-subsystems`, wait for it to
           complete, then continue. (Good if using the default model.)
        2. «Прогрею сам» → **PAUSE HERE** and wait for the user to write something like «готово»,
           «прогрел», «done» or any confirmation that they have run their own tool (e.g. an
           external CLI with a cheaper model). Once confirmed, re-run
           `uvx --from rag-reviewer reviewer status <path> --branch <branch> --json` and verify
           `summaries > 0`, then continue.
        3. «Пропустить» → note in brief under **Constraints**: «сводки подсистем не построены;
           `rag-reviewer:summarize-subsystems` не запускался». Continue without them.
      - `summaries` is `null`, or the key is absent (deploy older than this field) → fall back to
        the legacy probe: call `get_subsystem_summaries(repo, branch)` and use the returned count
        with the same three options; unlike the main path, this fallback's option 2 re-verifies by
        repeating the same legacy probe rather than re-reading the status payload. An error from
        the probe counts as 0 and adds the error detail to option 3's Constraints note.

   Decisions: stale → confirmation, never auto (Voyage free tier is 3 RPM / 10K TPM); failures →
   reported like `sync-codebase`; `sync_board` runs incrementally at start; summaries missing →
   three-way choice (build now / build yourself / skip), read from the status
   payload instead of dumping every summary into context.

1. **Config.** Reuse the resolved value from preflight; do not read `.review.yml` or call
   `get_board_config()` again. If no board resolved, continue board-less. For incomplete metadata
   call `get_board_targets(board_type=<task_board.type>,
   project=<task_board.project>, provider_options=<task_board.options or {}>)`: select from
   `targets` by `label`, and use option `required_for` / `choices` to ask for missing `options`.
   Never guess a target or an option and never branch on a board type.

**Brief-building unit (Steps 2–4) runs on the chosen model.** Steps 2–4 (identify → gather → distill
→ persist) are non-interactive; run them on the model chosen in the Step 0 startup survey:
- **Path A — per-subagent model override available:** **dispatch a subagent on the chosen model** to
  execute Steps 2–4, giving it the reviewer session-less tools (`get_task`, `search_codebase`,
  `get_subsystem_summaries`, `get_task_context`, `search_tasks`, the graph tools, `get_pr_diff`) plus
  the harness `Read`/`Bash`/`Glob`/`Write` (to persist the brief). The subagent returns the brief file
  path and a short summary (kept / dropped).
- **Path B — per-subagent model override unavailable** (some CLIs): build the brief **inline** on the
  session model, or offer the escape-hatch «switch model / run it yourself» in the spirit of the
  preflight «Прогрею сам» option (Step 0.4). Note in the report that the brief was built inline.
- **Existing-artifacts warn** (Step 4, user-facing «warn, don't block»): the **orchestrator** runs
  that scan-and-warn **before dispatch** (a subagent must not prompt the user). It derives the task
  KEY itself — the same `$ARGUMENTS`-vs-`key_pattern` regex match Step 2 opens with (no `get_task`
  needed) — so the KEY-based artifact globs run pre-dispatch. When Steps 2–4 run in a subagent, the
  Step 4 warn is thus **orchestrator-only**; only the idempotency overwrite-glob stays inside the
  subagent's persist.
- After the unit returns, the orchestrator **appends a marker line to the brief**:
  `Собран на: <tier/модель>, режим: subagent | inline` — records which model built the brief. The
  `brief_cost` token block is best-effort and may miss subagent sidechain tokens (documented limitation).
- Fail-open: an error or empty return from the subagent → the orchestrator finishes the brief inline
  on the session model. Model choice must never break the pipeline.

2. **Identify the task.**
   - If `$ARGUMENTS` matches the board's `key_pattern`:
     1. **Store-first.** Call reviewer `get_task(key, project=<task_board.project>)` — it returns the task's own normalized
        content (`{key, aliases[], title, description, criteria[], status, url}`) from the reviewer
        store, which the preflight `sync_board` (step 0.3) just refreshed.
        - **Hit** (a task object with a `key`): use it directly as the `TaskBrief`. The task is
          already indexed (the preflight sync persisted it) — do NOT call `index_task`. Note in the
          brief that the task data came from the reviewer store (after sync).
          - **Thin criteria (optional, fail-open).** The store can return `criteria=[]`; requirements
            normally live in `description`. If it has NO heading matching
            `(?i)(критери|приёмк|acceptance)`, leave `criteria` empty and record the gap. Do NOT call `index_task`.
        - **Miss** (`null` / no `key`) AND a board is resolved: call generic incremental
          `sync_board(board=<task_board.project or null>, board_type=<task_board.type>,
          provider_options=<task_board.options or {}>, limit=null, purge_orphaned=false)`, then
          retry `get_task(key, project=<task_board.project>)` once. Error or second miss →
          board-less: treat `$ARGUMENTS` as the task description and record the gap.
        - **Miss** AND no board: board-less — treat `$ARGUMENTS` as the task description.
   - Otherwise: treat `$ARGUMENTS` as the task description; do not perform external task reads.

   Store-first cuts the double-fetch: the preflight `sync_board` already pulled the whole board into
   the reviewer store, and a miss gets one generic incremental sync/retry (fewer LLM tokens and no
   provider-specific client dependency).

3. **Gather context (best-effort, fail-open).** Any tool returning a "(… unavailable)" / "(ничего не
   найдено)" note or an error is non-fatal — continue.
   - **Subsystem prior (architectural map).** Call
     `get_subsystem_summaries(repo, branch, query="<task title>. <first lines of description>")`
     → top-k relevant subsystems by proximity (top-k vs all is server-side; PRI-167).
     Use the same `branch` as `search_codebase`. Fail-open: an empty list / a `(… недоступно)`
     note / an error is non-fatal — omit the `## Subsystems` brief section and note the gap.
     The summary is only a prior — every `path:line` in the brief still comes from
     `search_codebase` snippets, never from the summary text.
     If a returned summary has `stale: true`, keep it only as a weak prior, do not use it for structural
     claims, and prefix its `## Subsystems` line with `[stale]`. `stale: null` is unknown freshness and
     gets no marker. For `stale: true`, either omit the item or use exactly this line shape:
     `- [stale] <cluster_key> — summary content omitted; verify against code.` Do not interpolate its
     title or summary claims. Omitting a stale summary does not change the directly-informing
     `search_codebase` entries selected for `## Relevant code`: evaluate every code item solely against
     the task, independently of whether it corroborates or refutes any stale-summary claim.
   - **Project scope.** Pass `project=<task_board.project>` (from Step 1; empty = unscoped) to
     `get_task`, `get_task_context`, and `search_tasks` so only this repo's project surfaces (PRI-170).
   - If you have a task key: `get_task_context(key, project=<task_board.project>)` → linked tasks, their PRs, and the code those PRs
     touched.
   - `search_tasks("<title>. <first lines of description>", project=<task_board.project>)` → semantically similar tasks from the
     reviewer store. Use their indexed fields only; if detail is missing, record that task-context gap.
   - **Related work = linked ∪ similar.** The «Related work» brief section draws from two sources —
     `get_task_context` (linked) and `search_tasks` (similar). They overlap; the Step 4 filter
     deduplicates them by key before the cap.
   - `search_codebase("<task description>")` → relevant existing code (files/symbols to touch or
     mimic).
   - **Lazy expansion (no user prompt).** If a tool's output ends with a cliff/rails note reporting a
     high-scoring tail beyond the cut AND the task looks broad, you MAY re-call the tool once with a
     higher ceiling (pass `top_k=<bigger>`), then merge. Do this silently — never pause to ask the user.
   - **Test exemplars (optional — when `search_codebase` surfaced concrete symbols).** One extra
     `search_codebase("<how the task's area is tested — fixtures/mocks for the feature>", include_tests=True)`
     on the same `branch` — a targeted *test* query (how the area is tested), not the code query with
     the flag flipped, so it surfaces the testing pattern the TDD hand-off should mimic. Snippets are
     line-numbered like the code retrieval → cite `path:line` directly. Apply the same Step 4 adaptive
     relevance filter (every directly-informing test file/symbol, no fixed cap). Fail-open: no tests
     surfaced / a `(ничего не найдено)` note / an error → omit the `## Test exemplars` brief section;
     the default code retrieval (`include_tests=False`) is unchanged.
   - **Deepen via the code graph (optional — when `search_codebase` surfaced concrete symbols).**
     `search_codebase` chunks are headed by `path#fqn (path:start-end)`; feed those `node_id`s to the
     session-less graph tools to sharpen the brief. The default `search_codebase` (code retrieval,
     `include_tests=False`) returns deduplicated, line-numbered, test-free snippets — expand only the
     few symbols central to the task (feed graph tools the code node_ids, not test-exemplar ones), and cite
     `path:line` from the line-numbered snippets directly (no re-Read needed for grounding).
     For OO/registry/dispatch tasks («add a new provider / handler»), prefer directed
     `implementations(node_id)` (incoming IMPLEMENTS — who subclasses/overrides X) over the
     undirected `related_symbols`, which mixes callers/tests/implements. A class node → its
     subclasses; a method node → its overrides. Accurate after a full `reviewer index` with SCIP;
     fail-soft `(implementations не найдены)` is non-fatal — continue.
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

   **Relevance filter (adaptive — retrieval is already bounded server-side).** Server-side cliff
   (`search_codebase`) and rails (`search_tasks`) already cap retrieval adaptively per task — and
   `search_tasks`'s `score` is an RRF rank score (`SUM(1/(60+rank))`, ≈0.016–0.033), NOT comparable
   across queries, so never gate on an absolute value (`search_codebase` exposes no score at all,
   only result order). So DO NOT re-truncate to a fixed number and DO NOT pad artificially: include
   EVERY returned item that *directly informs* the implementation. The keep/drop judgment stays
   binary (directly-informs), and end each section with `(dropped N: reason)`.
   - **Order** candidates by result rank (tasks: rank/score; code: rank).
   - **No fixed ceilings.** Take exactly the directly-informing items the tools returned. Related
     tasks are bounded by the search rails; the brief lists those that directly inform. Expand the
     graph (`related_symbols`/`callers`/`definition`) only for the few symbols central to the task.
   - **Keep/drop is a binary judgment** — include an item ONLY if it *directly informs the
     implementation*. Rank/score only sets review order; it is not a numeric gate.
     - ✅ INCLUDE: a symbol/file you will edit or mimic; a task whose PR shows a concrete pattern to
       follow; a constraint that narrows the approach.
     - ❌ EXCLUDE: a task in the same area but a different mechanism; a file the search surfaced that
       you won't touch or copy; background you won't act on.
   - **Report what you dropped:** end the Related work, Relevant code and Test exemplars sections with
     `(dropped N: reason)`.
   - **Dedup related sources by key (linked ∪ similar).** «Related work» draws from
     `get_task_context` (linked) and `search_tasks` (similar). Deduplicate by canonical task key
     before inclusion, matching `PRI-N`↔`ID-N` via `aliases` (one task, two codes). On collision
     keep the linked entry (richer — carries PR/graph context) and drop the similar duplicate, so a
     task never appears twice in the brief.

   **Brief skeleton — fill it, keep each item to one line:**

   ```
   # Brief — <KEY> <title>
   ## Task — key/title/requirements/criteria (or the user's formulation in board-less mode). ≤~6 lines.
   ## Related work — every directly-informing task, one line each: «KEY — what to reuse / follow». (dropped N: …)
   ## Subsystems — ≤8 relevant subsystems, one line: «cluster_key — gist of summary». For `stale: true`,
   either omit the item or use exactly: `- [stale] <cluster_key> — summary content omitted; verify against code.`
   (omit if prior empty)
   ## Relevant code — every directly-informing file/symbol, one line: «path:line — why» (+ blast radius from the graph). (dropped N: …)
   ## Test exemplars — every directly-informing test file/symbol, one line: «path:line — what's mocked / which pattern». (omit if none; dropped N: …)
   ## Constraints / open questions — terse bullets: limits, unknowns, context gaps (e.g. "board unavailable", "task corpus empty").
   ```

   Cite `path:line` straight from the line-numbered Step 3 snippets — no re-Read (Step 3 contract).

   **Persist the brief (survivability).** After distilling, save the brief to a file so it
   survives context compaction / a new session and seeds the trace задача→бриф→спека→план→PR.
   - **Directory:** `docs/superpowers/briefs/` — create it if missing (`mkdir -p`). Committed like
     `specs/`/`plans/` (leave a trace, do not gitignore).
   - **Filename:** with a task key — `YYYY-MM-DD-<KEY>-<slug>.md`, where `KEY` is the board key
     matching `key_pattern` (e.g. `PRI-163`, NOT the normalized store key `ID-163`) and `slug` is a
     short ASCII kebab of the title. **Board-less** (no key): `YYYY-MM-DD-<slug>.md` (slug from the
     user's formulation). `YYYY-MM-DD` = today's date.
   - **Check for existing artifacts (warn, don't block).** Before writing the brief, scan the
     three artifact directories for files matching this task key (case-insensitive):
     - `docs/superpowers/briefs/*<KEY>*`
     - `docs/superpowers/specs/*<key>*-design.md`
     - `docs/superpowers/plans/*<key>*.md`
     Use case-insensitive matching (e.g., try both `PRI-176` and `pri-176` globs, or lowercase
     file names before matching). If any artifacts are found, warn the user (in Russian):
     > "⚠️ Похожие артефакты уже существуют: briefs/PRI-176-..., specs/pri-176-...-design.md,
     > plans/pri-176-....md. Продолжить? [Y/n]"
     Do **not** block — continue unless the user explicitly says no. If the user continues (or
     auto-permission mode leaves no choice), list the found artifacts under `## Constraints` with
     the tag `[existing_artifacts]`.
   - **Idempotency:** before writing, glob `docs/superpowers/briefs/*-<KEY>-*.md` and overwrite
     the match if any (slug drift between runs must not spawn duplicates); board-less → exact name.
   - **Content:** the distilled brief verbatim (the `# Brief — <KEY> <title>` skeleton); add the
     task `url` on the line below the heading when available, for grep-by-key.
   - **Fail-open:** a failed write (read-only FS, no permission) is non-fatal — note it and still
     hand off with the in-context brief.

   **Persist the run state (mode + strategy).** The survey's answers must survive context
   compaction and two skill handoffs, but they must NOT land in a committed artifact: the spec and
   the plan end up in the PR, where a list of decisions made on the user's behalf reads as a
   receipt that nobody approved the design. So they go to a **git-ignored** run-state file instead.
   - **Path:** `.superpowers/solve-task/<KEY>.md` — board-less: `.superpowers/solve-task/<slug>.md`.
     `.superpowers/` is already git-ignored (it is where subagent-driven-development keeps its
     ledger). Create the directory if missing (`mkdir -p`). The path is derived from the task KEY,
     so any later step can rebuild it without remembering the conversation.
   - **Content:**

     ```
     Режим: full-auto
     Стратегия: lite
     Профиль: /absolute/path/to/plugin/skills/_profiles/execution-lite.md
     Бриф: docs/superpowers/briefs/2026-08-12-PRI-243-….md

     ## Решения, принятые за пользователя
     - Предполёт: индекс отставал на 12 коммитов → переиндексирован (рекомендованный вариант).
     ```

     Write the profile path in its **absolute** form: by the time the `lite` strategy is applied,
     the plugin's base directory is no longer in context. The `Профиль:` line is written only when
     the strategy is `lite`.
   - **The decisions section is filled only in `full-auto`**, one line per decision taken by
     recommendation, including the preflight decisions of Step 0. In `normal` and `auto` the
     section is omitted.
   - **never write the mode**, the strategy, or the decisions list into the brief, the `spec`, or
     the `plan`. Those three are committed; the run-state file is not. The spec still carries the
     brief's path as provenance — that line reveals nothing about the mode.
   - **Fail-open:** a failed write (read-only FS, no permission) is non-fatal — say so and carry the
     choice in context instead.

5. **Hand off to development.** Show the brief, state the saved file path
   (`docs/superpowers/briefs/…`), then invoke `superpowers:brainstorming` with the brief **file
   path** as the seed/context — so the brief survives compaction, not just the in-context text.
   **Ask brainstorming to record the brief's provenance in the spec:** one line under the spec
   heading pointing at the brief's path (`docs/superpowers/briefs/…md`), in the spec's own
   language — the path itself is the greppable anchor for the задача→бриф→спека→PR trace, so no
   dedicated marker is needed. Do NOT ask it to copy the brief's `## Constraints / open
   questions` verbatim: those are open questions brainstorming exists to RESOLVE, and a verbatim
   copy would contradict the very spec that answers them.
   **Carry the run state forward.** Pass the run-state path
   (`.superpowers/solve-task/<KEY>.md`) into the handoff and instruct the next skill to **re-read**
   it before acting on the mode or the strategy — the file, not the conversation, is the source of
   truth after a compaction.

   **State the mode as the user's will, not as a gate bypass.** Phrase it plainly: «пользователь
   выбрал режим `auto`: апрув спеки и апрув плана не запрашивать — это его прямая инструкция».
   This is `the user's explicit instruction` and `not a request to bypass` a check: superpowers'
   gates yield to the user's instruction, and it is the instruction that is being presented. In
   `auto` and `full-auto` the spec and the plan are still written, still self-reviewed and still
   committed — only the human approval is dropped. In `full-auto` the brainstorming questions are
   not asked either: take the recommended option at every fork and log each one to the run-state
   file's decisions section.

   **Confirmations that survive `full-auto`.** Design questions and approvals are suppressed, but
   these named actions still require an explicit confirmation: `git push`, `creating a PR`, and any
   `board write` (`finish_task`, `create_task`, a writing `sync_board`). The list is named on
   purpose — «irreversible actions» in the abstract is not actionable for an executor.

   **Right-size the plan's tasks.** Ask the planning step to apply `Task Right-Sizing` from
   superpowers:writing-plans — a task is the smallest unit a reviewer could meaningfully reject —
   so the plan yields fewer, larger tasks and therefore fewer subagents.

   **Resolving the `auto` strategy** (after the plan is written, never before). Rules are ordered,
   `first match wins`, so every combination lands in exactly one branch:
   1. any risk signal, or `> 8 tasks`, or `> 10` touched files → `subagent`;
   2. `≤ 3 tasks` and ≤ 3 touched files → `inline` (dispatch costs more than the work);
   3. everything else → `lite`.

   Risk signals, named: a Postgres or Neo4j `schema migration`; a change to a public `MCP tool`
   contract; work with `credentials` or secrets; any `irreversible` external action. A tie or an
   ambiguity resolves to the more conservative branch (`subagent`).

   From there the normal cycle takes over (brainstorming → writing-plans →
   subagent-driven-development/TDD). Your job ends at the handoff — do NOT plan or implement here.

   **After the PR is created (later in the dev cycle):** offer to close the task with the
   `rag-reviewer:finish-task` skill — it appends the PR link to the task and marks it done (bumping
   last-modified so the sync re-indexes the closed task). Skip in board-less mode (no task key).

   **Board-less mode:** when the user's formulation has no task key and a board IS configured,
   you may offer `rag-reviewer:create-task` first — it files the task with the canonical structure,
   so the work gets a key, a URL and a place in the task corpus before implementation starts.

## Failure handling (fail-open)

- No configured `task_board` / failed generic sync / task not found → board-less: build the brief
  from `search_tasks` (if the corpus is warm) + `search_codebase` + the user's formulation; note
  the missing task context.
- Neo4j down → `get_task_context` / `index_task` graph parts degrade (empty + warning); build the
  brief from `search_tasks` + `search_codebase`.
- Empty task corpus (no prior `rag-reviewer:sync-tasks` or reviews) → `search_tasks` is empty; use
  `search_codebase` + the user's formulation and note the missing task context.
- Postgres down → `search_codebase` / `search_tasks` return empty; build the brief from the user's
  formulation alone and note the missing task context; still hand off to brainstorming.
- Never abort: with any gap, distill what you have, note the deficit in the brief, and still hand off
  to brainstorming.
- This skill reads task data only through the reviewer store and generic `sync_board`/retry. The
  brief file under `docs/superpowers/briefs/` is its only repository write.

## Reporting a reviewer defect

<!-- include: _common/bug-reporting.md -->
