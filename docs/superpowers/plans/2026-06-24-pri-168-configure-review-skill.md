# reviewer_configure-review Skill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone, interactive Claude Code skill `reviewer_configure-review` that scans a repo's tracked tree + churn and generates a recommended `.review.yml` context layer (cluster depth, per-prefix depth overrides, summary top-k threshold, ignore for noisy *tracked* paths) as a draft the user edits, then writes it without clobbering other keys.

**Architecture:** One deliverable — `plugin/skills/configure-review/SKILL.md` (a markdown prompt; no Python runtime). The skill is autonomous: it uses only `git` (via Bash) and file editing, so it works on a fresh repo before any indexing — no reviewer MCP / Postgres / Neo4j. The repo's convention for "testing" a prompt artifact is a content guard test under `tests/skills/`; we follow it. Verification closes with an acceptance dry-run on this repo (idempotency + key preservation).

**Tech Stack:** Markdown (skill body, English; user-facing answers in Russian), Python/pytest (guard test only), `git ls-tree` / `git log`, PyYAML (only indirectly — `ReviewPolicy.from_yaml` parses the output).

## Global Constraints

- Skill body is written in **English** (token economy), but the skill instructs the model to **answer the user in Russian** (project language). Commands, code identifiers and `path:line` stay verbatim. (Copied from spec §3.)
- The skill edits **only** the context-layer keys: `summary_cluster_depth`, `summary_cluster_depth_overrides`, `summary_topk_threshold`, `paths.ignore`. It must **not** clobber any other key (`task_board`, `categories`, `severity_threshold`, …). (Spec §2, §5.)
- **Out of scope, must not appear as implemented behavior:** backend layered-ignore (B), default ignore-list (C), any Python/backend code change beyond the guard test, auto-running reindex/resummarize, filesystem walk / untracked-junk detection, index-aware (`list_subsystem_clusters`) recommendations. (Spec §2.)
- **Tracked files only** — source of truth is `git ls-tree <branch>`; no filesystem walk. (Spec §5.)
- **Fail-open on churn** — missing history / `git log` failure ⇒ structure-only recommendations, noted to the user. (Spec §5.)
- Commits: Conventional Commits **in Russian**, **no self-attribution** (no `Co-Authored-By` / Claude mentions). Work branch already exists: `feat/pri-168-configure-review-skill`.
- Any Python touched must pass `.venv/bin/ruff check .` (line-length 100, target py311) and `.venv/bin/pytest -q` (integration excluded by default).

---

### Task 1: The skill + its content guard test

**Files:**
- Create: `plugin/skills/configure-review/SKILL.md`
- Test: `tests/skills/test_configure_review_skill.py`

**Interfaces:**
- Consumes: nothing from earlier tasks. Reads only the repo's `git` state at skill runtime.
- Produces: the file `plugin/skills/configure-review/SKILL.md` containing the verbatim phrases the guard test asserts (frontmatter `name: reviewer_configure-review`; "Always answer the user in Russian"; the four context-layer keys; "ls-tree"; "filesystem walk"; "churn"; "git log"; ".venv"; "gitignored"; "task_board"; "Never clobber"; "never write it silently"; "no reviewer MCP"; "do NOT run"; "reviewer_sync-codebase"; "reviewer_summarize-subsystems"). Task 2 relies on this file existing and being invocable as `/reviewer_configure-review`.

- [ ] **Step 1: Write the failing guard test**

Create `tests/skills/test_configure_review_skill.py`:

```python
"""Guard: скилл reviewer_configure-review — интерактивная настройка контекст-слоя
.review.yml (PRI-168). Скилл автономен (только git + правка файла), редактирует
ровно контекст-слой и не клоберит чужие ключи, пересбор не запускает.
"""
from pathlib import Path

SKILL = (Path(__file__).resolve().parents[2]
         / "plugin" / "skills" / "configure-review" / "SKILL.md")


def test_skill_exists_with_frontmatter_name():
    text = SKILL.read_text(encoding="utf-8")
    assert "name: reviewer_configure-review" in text


def test_skill_instructs_russian_output():
    text = SKILL.read_text(encoding="utf-8")
    assert "Always answer the user in Russian" in text


def test_skill_scope_is_the_four_context_keys():
    text = SKILL.read_text(encoding="utf-8")
    for key in (
        "summary_cluster_depth",
        "summary_cluster_depth_overrides",
        "summary_topk_threshold",
        "paths.ignore",
    ):
        assert key in text, f"скилл не упоминает ключ {key}"


def test_skill_scans_tracked_only_no_fs_walk():
    text = SKILL.read_text(encoding="utf-8")
    assert "ls-tree" in text                       # источник — трекаемые файлы
    assert "filesystem walk" in text               # ...и явный отказ от обхода ФС


def test_skill_uses_churn():
    text = SKILL.read_text(encoding="utf-8")
    assert "churn" in text
    assert "git log" in text


def test_skill_documents_untracked_junk_is_already_invisible():
    # Находка спеки §1.1: .venv/node_modules gitignored → невидимы индексу.
    text = SKILL.read_text(encoding="utf-8")
    assert ".venv" in text
    assert "gitignored" in text


def test_skill_preserves_foreign_keys():
    text = SKILL.read_text(encoding="utf-8")
    assert "task_board" in text                    # пример чужого ключа, который беречь
    assert "Never clobber" in text


def test_skill_asks_before_writing_ignore():
    text = SKILL.read_text(encoding="utf-8")
    assert "never write it silently" in text       # ignore — суждение, спросить


def test_skill_is_standalone_no_mcp():
    text = SKILL.read_text(encoding="utf-8")
    assert "no reviewer MCP" in text


def test_skill_suggests_rebuilds_without_running():
    text = SKILL.read_text(encoding="utf-8")
    assert "do NOT run" in text                     # не запускает пересбор сам
    assert "reviewer_sync-codebase" in text         # при смене ignore
    assert "reviewer_summarize-subsystems" in text  # при смене depth/threshold
```

- [ ] **Step 2: Run the guard test to verify it fails**

Run: `.venv/bin/pytest tests/skills/test_configure_review_skill.py -q`
Expected: FAIL — every test errors with `FileNotFoundError` (the SKILL.md does not exist yet).

- [ ] **Step 3: Create the skill**

Create `plugin/skills/configure-review/SKILL.md` with exactly this content:

````markdown
---
name: reviewer_configure-review
description: Configure or update a repo's .review.yml context layer (subsystem cluster depth, per-prefix depth overrides, summary top-k threshold, and ignore for noisy *tracked* paths) from a draft the skill generates and the user edits. Use when the user asks to set up or tune review config ("настроить .review.yml", "configure review config", "настрой контекст-слой", "tune cluster depth", "что игнорировать в ревью", "set up reviewer for this repo"). Standalone — needs only git, no reviewer MCP / DB.
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

Edit **only** the context-layer keys of `.review.yml`:
- `summary_cluster_depth` — global subsystem cluster depth.
- `summary_cluster_depth_overrides` — per-prefix depth (longest-prefix-match by directory segments).
- `summary_topk_threshold` — summary-prior scale threshold.
- `paths.ignore` — only for **tracked** noisy paths (eval, fixtures, generated, vendored, migrations, data).

Do NOT touch any other key (`task_board`, `categories`, `severity_threshold`, …). Do NOT run a
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
````

- [ ] **Step 4: Run the guard test to verify it passes**

Run: `.venv/bin/pytest tests/skills/test_configure_review_skill.py -q`
Expected: PASS — all 10 tests green.

- [ ] **Step 5: Run the skills + config-structure guards to confirm no regression**

Run: `.venv/bin/pytest tests/skills/ tests/test_review_yml_example.py -q`
Expected: PASS — the new skill carries no `<!-- include: -->` markers (so the include guards are
untouched) and the repo's own `.review.yml` is unchanged (so `test_review_yml_example.py` stays green).

- [ ] **Step 6: Commit**

```bash
git add plugin/skills/configure-review/SKILL.md tests/skills/test_configure_review_skill.py
git commit -m "feat(skills): скилл reviewer_configure-review — настройка контекст-слоя .review.yml (PRI-168)"
```

---

### Task 2: Acceptance dry-run on this repo (idempotency + key preservation)

**Files:**
- (none created/modified — acceptance verification)

**Interfaces:**
- Consumes: `plugin/skills/configure-review/SKILL.md` from Task 1; this repo's `.review.yml` and git history.
- Produces: a verification verdict (no artifact). Confirms the skill's heuristics produce a sane,
  minimal-diff draft on an already-configured repo and that no other key is clobbered.

- [ ] **Step 1: Confirm the skill's git inputs produce sane data on this repo**

Run the exact commands the skill issues, to confirm they ground the recommendation:
```bash
git -C . ls-tree -r --name-only dev | grep '\.py$' | grep -v '^tests/' | sed 's#/[^/]*$##' | cut -d/ -f1-2 | sort | uniq -c | sort -rn | head
git -C . log --since="6 months ago" --name-only --pretty=format: -- '*.py' | grep -v '^$' | sed 's#/[^/]*$##' | cut -d/ -f1-2 | sort | uniq -c | sort -rn | head
```
Expected: `reviewer/index`, `reviewer/services`, `reviewer/mcp`, etc. appear as the largest/most-churned
subtrees — consistent with the existing `.review.yml` override `reviewer/index: 3`. This shows the
heuristic (large + active → deeper) would re-derive the current config.

- [ ] **Step 2: Confirm idempotency + key preservation by inspection**

Read this repo's `.review.yml` and confirm the skill's Scope/merge rules would leave it essentially
unchanged: `task_board` (4 keys), `paths.ignore: [eval]`, `summary_cluster_depth: 2`,
`summary_cluster_depth_overrides: {reviewer/index: 3}`, `summary_topk_threshold: 20` are all already
present and all within or beside the context layer.
Expected: a re-run would propose at most cosmetic changes; `task_board` and comments are preserved
(Scope + Note "Never clobber"). Record this as the idempotency check.

- [ ] **Step 3: Full suite green (no repo-wide regression)**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration excluded by default per `pyproject.toml`). Adding a skill dir + one guard
test must not affect any other test (install tests use synthetic fixtures, not the real skills dir).

- [ ] **Step 4: Confirm the working tree carries only the intended change**

Run: `git status --short`
Expected: only the two files from Task 1 are committed and nothing in `.review.yml` or other tracked
files changed by this work. The skill is a tool users invoke, not a CI step — it must not mutate the
repo during implementation.

---

## Self-Review

**1. Spec coverage:**
- Spec §2 scope (4 context keys, exclusions) → Task 1 Step 3 `## Scope` + Global Constraints; guard `test_skill_scope_is_the_four_context_keys`.
- Spec §1.1 finding (untracked junk already invisible; default-ignore dropped) → SKILL.md `## Scope` paragraph; guard `test_skill_documents_untracked_junk_is_already_invisible`.
- Spec §3 architecture (standalone, English body/Russian answers, draft→edit) → SKILL.md header + frontmatter; guards `test_skill_is_standalone_no_mcp`, `test_skill_instructs_russian_output`.
- Spec §4 pipeline (preflight, scan, churn, read existing, generate, present+diff, write, suggest) → SKILL.md `## Pipeline` 1–8; guards for `ls-tree`/`churn`/`git log`/rebuild suggestions.
- Spec §5 invariants (no clobber, tracked-only, fail-open churn, no side effects) → SKILL.md `## Notes`; guards `test_skill_preserves_foreign_keys`, `test_skill_scans_tracked_only_no_fs_walk`, `test_skill_suggests_rebuilds_without_running`.
- Spec §6 testing (parses/guard; manual run idempotency + key preservation) → Task 1 Step 5 + Task 2.
- No spec requirement left without a task.

**2. Placeholder scan:** No "TBD/TODO/handle edge cases" — heuristic thresholds are concrete (≈20 files, 6 months / 200 commits, 3–15 files/cluster, depth cap 3); full SKILL.md and full guard test are inline.

**3. Type consistency:** The guard test asserts only literal substrings that are present verbatim in the SKILL.md content in Step 3 (cross-checked: "Always answer the user in Russian", "ls-tree", "filesystem walk", "churn", "git log", ".venv", "gitignored", "task_board", "Never clobber", "never write it silently", "no reviewer MCP", "do NOT run", "reviewer_sync-codebase", "reviewer_summarize-subsystems", the four keys). Skill name `reviewer_configure-review` is identical in frontmatter, plan title, and commands.
