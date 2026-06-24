---
name: reviewer_configure-review
description: Configure or update a repo's .review.yml context layer (subsystem cluster depth, per-prefix depth overrides, summary top-k threshold, ignore for noisy *tracked* paths) and its task board selection (which board this repo uses — yougile/youtrack — key_pattern, url_template; never credentials) from a draft the skill generates and the user edits. Use when the user asks to set up or tune review config ("настроить .review.yml", "configure review config", "настрой контекст-слой", "tune cluster depth", "что игнорировать в ревью", "выбрать доску для репо", "set up reviewer for this repo"). Standalone — needs only git, no reviewer MCP / DB.
---

# Configure review (.review.yml context layer)

Scan the repo's **tracked** tree (plus churn), generate a recommended `.review.yml` context layer
(cluster depth, per-prefix depth overrides, summary top-k threshold, ignore for noisy tracked
paths), show it as a draft + diff, let the user adjust, then write it — preserving every other key.
Standalone: uses only `git` and file editing — **no reviewer MCP / Postgres / Neo4j** — so it works
on a fresh repo before the first index.

**Always answer the user in Russian** (the project language), regardless of this file's language.
Commands, code identifiers and `path:line` stay verbatim.

## Scope

Edit **only** these keys of `.review.yml`:
- `summary_cluster_depth` — global subsystem cluster depth.
- `summary_cluster_depth_overrides` — per-prefix depth (longest-prefix-match by directory segments).
- `summary_topk_threshold` — summary-prior scale threshold.
- `paths.ignore` — only for **tracked** noisy paths (eval, fixtures, generated, vendored, migrations, data).
- `task_board` — which board THIS repo uses (`type: yougile|youtrack`), plus `key_pattern` and (yougile only)
  `url_template`. **NEVER** write credentials here — board API keys live only in the reviewer deploy env
  (`YOUGILE_API_KEY` / `YOUTRACK_TOKEN` + `YOUTRACK_BASE_URL`). An empty `task_board:` disables the board for the repo.

Do NOT touch any other key (`categories`, `severity_threshold`, `max_comments`, `min_confidence`, …). Do NOT run a
reindex/resummarize. Do NOT walk the filesystem or try to detect untracked junk: `.venv`,
`node_modules`, `__pycache__`, `dist`, `build` are gitignored, so they never reach the git-tracked
index / graph / summaries — there is nothing to add to ignore for them.

## Inputs

Parse from $ARGUMENTS (all optional):
- `--path <path>`: repo clone path. Default: current working directory.
- `--branch <branch>`: branch whose tree to scan and whose `.review.yml` to edit. Default: the
  current git branch.

## Pipeline

1. **Preflight.** Resolve `--path` (default cwd) and `--branch`
   (`git -C <path> branch --show-current`; if empty/detached, use the current HEAD ref). Verify a git
   repo: `git -C <path> rev-parse --git-dir`. Not a repo → tell the user (in Russian) and stop. No
   database or reviewer MCP is required.

1.5. **Check .env completeness (offer `reviewer init` if needed).**
   Resolve the canonical .env path:
   ```bash
   echo "${REVIEWER_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/rag-reviewer/.env}"
   ```
   (fallback: `~/.config/rag-reviewer/.env`, then `./.env` for dev). Read and parse `KEY=VALUE` lines
   (skip comments and blank lines). If the file doesn't exist — tell the user (Russian):
   > .env не найден по пути `<path>`. Запустить `reviewer init` для первоначальной настройки?

   If the file exists, check critical groups:
   - **GitLab VCS:** `GITLAB_TOKEN` — if empty, warn.
   - **Доска задач:** `YOUGILE_API_KEY` and `YOUTRACK_TOKEN` — if both empty, warn.
   If any are missing → tell the user (Russian):
   > В .env не хватает полей: `<list>`. Запустить `reviewer init` чтобы дополнить?

   User can decline — skill continues normal pipeline. This check is **read-only** (parse
   `KEY=VALUE` lines); no reviewer MCP / Postgres / Neo4j needed. **Do NOT run** `reviewer init`
   automatically — only offer.

2. **Scan the tracked tree.**
   ```bash
   git -C <path> ls-tree -r --name-only <branch> | grep '\.py$'
   ```
   From the file list, count `.py` files under each directory prefix at depths 1, 2 and 3. This is
   the only file source — exactly the tracked set that gets indexed; no filesystem walk.

3. **Measure churn (fail-open).**
   ```bash
   git -C <path> log --since="6 months ago" --name-only --pretty=format: -- '*.py'
   ```
   Aggregate how many commits touched each subtree; activity = commits-touching ÷ file-count
   (size-normalized). Classify each subtree "active" vs "stable" against the median. Young repo / few
   commits → fall back to the last ~200 commits (`git -C <path> log -n 200 --name-only --pretty=format: -- '*.py'`).
   Empty or failing `git log` → skip churn, recommend from structure only, and say so to the user.

4. **Read the existing `.review.yml`** (working-tree file, or `git -C <path> show <branch>:.review.yml`).
   Parse it; KEEP every key outside the context layer (`task_board`, `categories`, …) and all
   existing comments verbatim. Keep existing `paths.ignore` entries. No file → you will create one,
   with explanatory comments in the style of this repo's `.review.yml`.

5. **Generate the recommended draft (heuristics).**
   - **`summary_cluster_depth`** (global): pick `d ∈ {1,2,3}` so clusters are a sensible size — aim
     ~3–15 files per cluster; avoid one giant cluster (too coarse) and one-file clusters (too fine).
     Default 2; tiny repos 1. From step 2's per-depth aggregates, choose `d` minimizing the share of
     too-coarse (> ~20 files) and too-fine (1 file) clusters, preferring 2 on ties.
   - **`summary_cluster_depth_overrides`**: for a subtree that is **large AND active** (size > ~20
     files and activity above median) → override `depth = d+1` (finer clusters → pointed invalidation,
     richer prior). Large-but-stable → leave at global `d`. Keys = the shortest distinguishing
     directory prefix (longest-prefix-match). Cap depth at 3.
   - **`summary_topk_threshold`**: estimate the cluster count at the chosen `d` + overrides (≈ number
     of distinct cluster keys). Above the default 20 → keep 20 (ANN top-k engages); otherwise keep
     the default. Mostly informational — show the estimated cluster count to the user.
   - **`paths.ignore`**: propose **candidates** among tracked paths that look like non-product noise
     (`eval`/`evals`, `fixtures`/`testdata`, `examples`/`samples`, `vendor`/`third_party`,
     `generated`/`gen`/`*_pb2.py`, `migrations`, large `data` modules). This is a judgment call, so
     **ask the user per candidate — never write it silently.**

5b. **Task board selection (ask before writing).** Read the existing `task_board` block (keep it
   verbatim if present). Ask the user which board this repo uses:
   - `yougile` → write `{type: yougile, mcp: yougile, key_pattern: '[A-Z]+-\d+', url_template: <ask>}`.
   - `youtrack` → write `{type: youtrack, key_pattern: '[A-Z]+-\d+'}` (NO `url_template` — youtrack derives
     the link from its base URL; NO `mcp` — youtrack is read server-side via sync, not board-MCP).
   - off / none → write an empty `task_board:` (disables the board for this repo).
   - leave unchanged → skip.

   **Then ask which PROJECT this repo uses** (e.g. PRI-170) and write it to `task_board.project` — the task
   **code prefix** (e.g. `PRI`, `TES`), the part before the dash in task codes. Warn the user (in Russian):
   если `project` пуст — и синк, и выдача/граф затянут **все проекты** аккаунта/инстанса вперемешку
   (напр. чужой `TES-1` всплывёт в связях задачи `PRI`); один аккаунт с несколькими проектами без
   `project` смешивает их. Пустой `task_board.project` = текущее глобальное поведение.

   **Never write credentials.** Remind the user (in Russian): ключи доски (`YOUTRACK_TOKEN`/
   `YOUTRACK_BASE_URL` для youtrack, `YOUGILE_API_KEY` для yougile) задаются в env деплоя reviewer-mcp,
   не в `.review.yml`. Грабли youtrack: `YOUTRACK_BASE_URL` обязан оканчиваться на `/api`. Changing the
   board has no effect until those env keys are set and the board is synced (`/reviewer_sync-tasks`).

6. **Present draft + diff.** Show the proposed context layer and a unified diff against the current
   `.review.yml` (or "new file"). Briefly justify each recommendation in Russian (why this depth; why
   an override on this subtree — cite its size/churn; why each ignore candidate). Take the user's
   edits in free dialogue and revise the draft.

7. **Write `.review.yml`.** Write the result by **merging** — preserve every other key and the
   explanatory comments. **Never clobber** keys outside the context layer. Idempotent: re-running on
   an already-configured repo yields a minimal diff.

8. **Suggest rebuild commands (do NOT run them).**
   - `paths.ignore` changed → suggest `/reviewer_sync-codebase --path <path> --ref <branch>`
     (re-index vectors + graph).
   - `summary_cluster_depth` / `*_overrides` / `summary_topk_threshold` changed → suggest
     `/reviewer_summarize-subsystems` (changing depth changes every `cluster_key` → a full summary
     rebuild; old-depth summaries orphan and are pruned on a full pass).
   - Remind the user (in Russian): changes take effect only after a rebuild, and only from the branch
     the `.review.yml` is committed to (policy is read from the target/index branch).

## Notes

- **Never clobber** keys outside the context layer — edit by merge.
- **Tracked files only** — `git ls-tree`, the exact set that gets indexed. No filesystem walk.
- **Fail-open on churn** — no history / `git log` failure → structure-only recommendations, noted.
- **No index side effects** — the skill only edits the file and suggests commands.
