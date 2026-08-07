# PRI-227 Short Skill Names Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every duplicated `reviewer_*` skill name with its directory basename while keeping the `rag-reviewer` plugin namespace consistent across Claude Code, Codex, hooks, installers, tests, and active documentation.

**Architecture:** Treat `plugin/skills/<name>/` as the only skill-name inventory and enforce `frontmatter.name == <name>` with one data-driven test. Keep one shared plugin payload for Claude and Codex, update all active references atomically, and regenerate the Codex payload digest instead of adding aliases, wrappers, or host-specific mappings.

**Tech Stack:** Markdown/YAML skill files, Python 3.11+, pytest, existing Claude/Codex installer fakes, JSON plugin manifests.

**Design:** `docs/superpowers/specs/2026-07-31-pri-227-short-skill-names-design.md`

## Global Constraints

- Keep the plugin namespace exactly `rag-reviewer`.
- Every `plugin/skills/*/SKILL.md` frontmatter `name` must equal its parent-directory basename.
- Skill names must be unique and must not start with `reviewer_`.
- Do not add aliases, compatibility wrappers, or host-specific payload transforms.
- Do not create the future `decompose-task` skill in PRI-227.
- Do not rename skill directories.
- Do not rewrite historical files under `docs/superpowers/briefs/`, `docs/superpowers/specs/`, or `docs/superpowers/plans/`.
- Active Claude examples use `/rag-reviewer:<name>`; active Codex examples use `$rag-reviewer:<name>`.
- Shared cross-skill prose and client-neutral runtime messages use `rag-reviewer:<name>` without inventing a client-specific alias.
- Preserve the already-canonical hook attribution `rag-reviewer:solve-task`.
- Use Russian comments, docstrings, and Conventional Commit messages; do not add self-attribution.
- Unit tests must not access external services or localhost.

The complete rename map is:

| Directory | Old frontmatter name | New frontmatter name |
|---|---|---|
| `ask` | `reviewer_ask` | `ask` |
| `configure-review` | `reviewer_configure-review` | `configure-review` |
| `create-task` | `reviewer_create-task` | `create-task` |
| `finish-task` | `reviewer_finish-task` | `finish-task` |
| `maintainability-review` | `reviewer_maintainability-review` | `maintainability-review` |
| `performance-review` | `reviewer_performance-review` | `performance-review` |
| `pr-walkthrough` | `reviewer_pr-walkthrough` | `pr-walkthrough` |
| `review-pr` | `reviewer_review-pr` | `review-pr` |
| `solve-task` | `reviewer_solve-task` | `solve-task` |
| `summarize-subsystems` | `reviewer_summarize-subsystems` | `summarize-subsystems` |
| `sync-codebase` | `reviewer_sync-codebase` | `sync-codebase` |
| `sync-tasks` | `reviewer_sync-tasks` | `sync-tasks` |

---

### Task 1: Make directory basenames the enforced skill-name contract

**Files:**

- Create: `tests/skills/test_skill_names.py`
- Modify: `plugin/skills/ask/SKILL.md:1-3`
- Modify: `plugin/skills/configure-review/SKILL.md:1-3`
- Modify: `plugin/skills/create-task/SKILL.md:1-3`
- Modify: `plugin/skills/finish-task/SKILL.md:1-3`
- Modify: `plugin/skills/maintainability-review/SKILL.md:1-3`
- Modify: `plugin/skills/performance-review/SKILL.md:1-3`
- Modify: `plugin/skills/pr-walkthrough/SKILL.md:1-3`
- Modify: `plugin/skills/review-pr/SKILL.md:1-3`
- Modify: `plugin/skills/solve-task/SKILL.md:1-3`
- Modify: `plugin/skills/summarize-subsystems/SKILL.md:1-3`
- Modify: `plugin/skills/sync-codebase/SKILL.md:1-3`
- Modify: `plugin/skills/sync-tasks/SKILL.md:1-3`
- Modify: `tests/skills/test_configure_review_skill.py:1-15`
- Modify: `tests/skills/test_create_task_skill.py:1-12`
- Modify: `tests/skills/test_finish_task_skill.py:1-13`

**Interfaces:**

- Consumes: skill discovery by `plugin/skills/*/SKILL.md`; no hard-coded registry.
- Produces: `registered_skill_files() -> tuple[Path, ...]`, `frontmatter_name(path: Path) -> str`, and `registered_skill_names() -> tuple[str, ...]` in the test module for later active-surface guards.

- [ ] **Step 1: Write the failing structure guard**

Create `tests/skills/test_skill_names.py` with the exact dynamic inventory and diagnostics:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = ROOT / "plugin" / "skills"


def registered_skill_files() -> tuple[Path, ...]:
    return tuple(sorted(SKILLS_ROOT.glob("*/SKILL.md")))


def frontmatter_name(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: нет начального YAML frontmatter"
    header = text.split("---", 2)[1]
    values = [
        line.removeprefix("name:").strip()
        for line in header.splitlines()
        if line.startswith("name:")
    ]
    assert len(values) == 1, f"{path}: ожидался ровно один frontmatter name"
    return values[0]


def registered_skill_names() -> tuple[str, ...]:
    return tuple(frontmatter_name(path) for path in registered_skill_files())


def test_frontmatter_names_match_skill_directories():
    mismatches = [
        (path.relative_to(ROOT).as_posix(), path.parent.name, frontmatter_name(path))
        for path in registered_skill_files()
        if frontmatter_name(path) != path.parent.name
    ]
    assert mismatches == [], f"path, expected, actual: {mismatches}"


def test_frontmatter_names_are_unique_and_have_no_reviewer_prefix():
    names = registered_skill_names()
    assert len(names) == len(set(names)), names
    assert [name for name in names if name.startswith("reviewer_")] == []
```

- [ ] **Step 2: Run the guard and verify the current schema fails**

Run:

```bash
.venv/bin/pytest tests/skills/test_skill_names.py -q
```

Expected: both tests fail and enumerate all 12 current `reviewer_*` frontmatter names.

- [ ] **Step 3: Rename every frontmatter value and remove duplicated per-skill naming tests**

Apply the complete rename table from Global Constraints to all 12 `SKILL.md` files. Do not change descriptions or bodies in this step.

Delete only these now-redundant tests:

```python
test_skill_exists_with_frontmatter_name
test_create_task_name_follows_reviewer_prefix
test_finish_task_name_follows_reviewer_prefix
```

Update the module docstring in `test_configure_review_skill.py` so it calls the skill
`configure-review`, not by its removed frontmatter name. Leave the behavior-specific tests intact.

- [ ] **Step 4: Run the naming guard and the affected behavior tests**

Run:

```bash
.venv/bin/pytest \
  tests/skills/test_skill_names.py \
  tests/skills/test_configure_review_skill.py \
  tests/skills/test_create_task_skill.py \
  tests/skills/test_finish_task_skill.py -q
```

Expected: PASS; `_common` is absent from the inventory because it has no `SKILL.md`.

- [ ] **Step 5: Commit the canonical naming contract**

```bash
git add plugin/skills tests/skills/test_skill_names.py \
  tests/skills/test_configure_review_skill.py \
  tests/skills/test_create_task_skill.py \
  tests/skills/test_finish_task_skill.py
git commit -m "refactor(skills): сократить frontmatter-имена (PRI-227)"
```

---

### Task 2: Remove legacy names from shared skill prose and runtime messages

**Files:**

- Modify: `tests/skills/test_skill_names.py`
- Modify: `plugin/skills/_common/dimension-scope.md:16`
- Modify: `plugin/skills/_common/tool-usage.md:11`
- Modify: `plugin/skills/ask/SKILL.md:41`
- Modify: `plugin/skills/configure-review/SKILL.md:38-40`
- Modify: `plugin/skills/maintainability-review/SKILL.md:12,47`
- Modify: `plugin/skills/performance-review/SKILL.md:12,36`
- Modify: `plugin/skills/solve-task/SKILL.md:3,36,64,72,295,299,309`
- Modify: `plugin/skills/summarize-subsystems/SKILL.md:29`
- Modify: `plugin/skills/sync-codebase/SKILL.md:70-73`
- Modify: `reviewer/entrypoints/mcp_server.py:287-398`
- Modify: `reviewer/mcp/service.py:959`
- Modify: `tests/skills/test_configure_review_skill.py:94-130`
- Modify: `tests/skills/test_preflight_guardrail.py:20-27`
- Modify: `tests/skills/test_create_task_skill.py:8-45`
- Modify: `tests/skills/test_finish_task_skill.py:8-48`

**Interfaces:**

- Consumes: `registered_skill_files()` and `registered_skill_names()` from Task 1.
- Produces: `_legacy_skill_names() -> tuple[str, ...]`, `_text_files(root: Path) -> tuple[Path, ...]`, and `_legacy_offenders(paths: tuple[Path, ...]) -> list[str]` for the documentation guard in Task 3.

- [ ] **Step 1: Add a failing guard for active code, plugin, and test surfaces**

Append this scanner to `tests/skills/test_skill_names.py`:

```python
TEXT_SUFFIXES = {".md", ".py", ".json", ".toml", ".yml", ".yaml"}


def _legacy_skill_names() -> tuple[str, ...]:
    return tuple(f"reviewer_{path.parent.name}" for path in registered_skill_files())


def _text_files(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in TEXT_SUFFIXES
            and "__pycache__" not in path.parts
        )
    )


def _legacy_offenders(paths: tuple[Path, ...]) -> list[str]:
    offenders: list[str] = []
    legacy = _legacy_skill_names()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for name in legacy:
            if name in text:
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
    return offenders


def test_active_code_and_plugin_surfaces_have_no_legacy_skill_names():
    paths = (
        *_text_files(ROOT / "plugin"),
        *_text_files(ROOT / "reviewer"),
        *_text_files(ROOT / "tests"),
    )
    assert _legacy_offenders(paths) == []
```

The legacy tokens are constructed dynamically, so the guard does not contain the strings it rejects.

- [ ] **Step 2: Run the guard and verify it reports all remaining active references**

Run:

```bash
.venv/bin/pytest \
  tests/skills/test_skill_names.py::test_active_code_and_plugin_surfaces_have_no_legacy_skill_names \
  -q
```

Expected: FAIL with references from skill prose, `_common`, runtime docstrings/messages, and old test expectations.

- [ ] **Step 3: Replace shared references with client-neutral canonical names**

Use these exact replacement rules on active plugin/runtime text:

```text
/reviewer_review-pr              -> rag-reviewer:review-pr
/reviewer_sync-codebase          -> rag-reviewer:sync-codebase
/reviewer_sync-tasks             -> rag-reviewer:sync-tasks
/reviewer_summarize-subsystems   -> rag-reviewer:summarize-subsystems
/reviewer_create-task            -> rag-reviewer:create-task
/reviewer_finish-task            -> rag-reviewer:finish-task
reviewer_review-pr               -> rag-reviewer:review-pr
reviewer_sync-codebase           -> rag-reviewer:sync-codebase
reviewer_sync-tasks              -> rag-reviewer:sync-tasks
```

Do not replace MCP identifiers such as `mcp__plugin_rag-reviewer_reviewer__`; those name an MCP namespace, not a skill.

For user-facing runtime messages in `reviewer/mcp/service.py`, use:

```python
"note": "(base-индекс пуст — выполните rag-reviewer:sync-codebase)"
```

Update `reviewer/entrypoints/mcp_server.py` docstrings to name
`rag-reviewer:summarize-subsystems` and `rag-reviewer:pr-walkthrough`.

Update test expectations to the same canonical tokens, for example:

```python
assert re.search(r"paths\.ignore.*rag-reviewer:sync-codebase", rules.group())
assert "rag-reviewer:summarize-subsystems" in text
```

Keep `plugin/hooks/brief_guard.py::SOLVE_ATTRIBUTION` and
`tests/hooks/test_brief_guard.py::_SOLVE_ATTRIBUTION` exactly
`"rag-reviewer:solve-task"`; they are already correct.

- [ ] **Step 4: Run the active-surface guard and targeted skill/hook tests**

Run:

```bash
.venv/bin/pytest \
  tests/skills/test_skill_names.py \
  tests/skills/test_preflight_guardrail.py \
  tests/skills/test_configure_review_skill.py \
  tests/skills/test_create_task_skill.py \
  tests/skills/test_finish_task_skill.py \
  tests/hooks/test_brief_guard.py -q
```

Expected: PASS with no legacy skill token under `plugin/`, `reviewer/`, or `tests/`.

- [ ] **Step 5: Commit cross-skill and runtime migration**

```bash
git add plugin/skills reviewer/entrypoints/mcp_server.py reviewer/mcp/service.py \
  tests/skills tests/hooks/test_brief_guard.py
git commit -m "refactor(skills): обновить внутренние ссылки (PRI-227)"
```

---

### Task 3: Update active documentation and document the breaking migration

**Files:**

- Modify: `tests/skills/test_skill_names.py`
- Modify: `tests/install/test_codex_plugin_payload.py:268-285`
- Modify: `README.md:55-75,238-270,375-480,510-555`
- Modify: `README.ru.md:55-75,241-273,378-482,512-557`
- Modify: `CLAUDE.md:52-82,145-154`
- Modify: `AGENTS.md:1-65`
- Modify: `plugin/README.md:1-20,38-78`

**Interfaces:**

- Consumes: `_legacy_offenders()` and `registered_skill_names()` from Tasks 1-2.
- Produces: an active-doc guard and bilingual migration instructions used by installer/payload tests.

- [ ] **Step 1: Add failing documentation contract tests**

Append to `tests/skills/test_skill_names.py`:

```python
ACTIVE_DOCS = (
    ROOT / "README.md",
    ROOT / "README.ru.md",
    ROOT / "CLAUDE.md",
    ROOT / "AGENTS.md",
    ROOT / "plugin" / "README.md",
)


def test_active_docs_have_no_legacy_skill_names():
    assert _legacy_offenders(ACTIVE_DOCS) == []


def test_bilingual_readmes_list_every_canonical_skill():
    for readme in (ROOT / "README.md", ROOT / "README.ru.md"):
        text = readme.read_text(encoding="utf-8")
        missing = [
            name
            for name in registered_skill_names()
            if f"/rag-reviewer:{name}" not in text
        ]
        assert missing == [], f"{readme.name}: {missing}"


def test_migration_docs_require_fresh_sessions():
    for readme in ACTIVE_DOCS:
        text = readme.read_text(encoding="utf-8")
        assert "rag-reviewer:" in text
    combined = "\n".join(path.read_text(encoding="utf-8") for path in ACTIVE_DOCS)
    assert "New Chat" in combined
    assert "new CLI session" in combined
    assert "Reload Window" in combined
```

Update `test_real_payload_registers_every_skill_directory_dynamically()` in
`tests/install/test_codex_plugin_payload.py`:

```python
for name in expected:
    marker = f"/rag-reviewer:{name}"
    assert marker in english
    assert marker in russian
```

- [ ] **Step 2: Run the documentation tests and verify the old names fail**

Run:

```bash
.venv/bin/pytest \
  tests/skills/test_skill_names.py::test_active_docs_have_no_legacy_skill_names \
  tests/skills/test_skill_names.py::test_bilingual_readmes_list_every_canonical_skill \
  tests/skills/test_skill_names.py::test_migration_docs_require_fresh_sessions \
  tests/install/test_codex_plugin_payload.py::test_real_payload_registers_every_skill_directory_dynamically \
  -q
```

Expected: FAIL on the old README/CLAUDE/AGENTS/plugin README invocations and headings.

- [ ] **Step 3: Rewrite active docs to the canonical public syntax**

Apply these rules:

```text
Claude examples: /rag-reviewer:<directory-name>
Codex examples:  $rag-reviewer:<directory-name>
Reference headings: <directory-name>
```

Ensure both root README skill references enumerate all 12 canonical names. Add the missing
`create-task` entry to `AGENTS.md` and `plugin/README.md`.

Add this migration message in equivalent English and Russian wording near the install/update instructions:

```text
This release removes the redundant reviewer_ segment from every skill name.
Legacy skill invocations are unsupported: update the plugin/cache, use the short
names listed below, then open a New Chat or new CLI session. In an IDE, also use
Reload Window.
```

Do not spell out a legacy full skill token in active docs; describe the removed
`reviewer_` segment generically so the no-legacy guard remains enforceable.

Keep the existing commands:

```bash
uvx --from rag-reviewer@latest reviewer install codex
uvx --from rag-reviewer@latest reviewer install codex --dry-run
uvx --from rag-reviewer@latest reviewer install claude-code
codex plugin list --json
claude plugin list --json
```

Change every active invocation, including dry-run examples, troubleshooting notes, and
CLAUDE.md flow descriptions. Do not touch `docs/superpowers/**`.

- [ ] **Step 4: Run bilingual docs, skill-name, and payload inventory tests**

Run:

```bash
.venv/bin/pytest \
  tests/skills/test_skill_names.py \
  tests/skills/test_readme_grounding_block.py \
  tests/install/test_codex_plugin_payload.py::test_real_payload_registers_every_skill_directory_dynamically \
  -q
```

Expected: PASS; every registered directory has a Claude-style marker in both root READMEs and no active doc contains a removed full name.

- [ ] **Step 5: Commit the documentation migration**

```bash
git add README.md README.ru.md CLAUDE.md AGENTS.md plugin/README.md \
  tests/skills/test_skill_names.py tests/install/test_codex_plugin_payload.py
git commit -m "docs(skills): описать короткие invocation names (PRI-227)"
```

---

### Task 4: Regenerate payload metadata and verify both installation surfaces

**Files:**

- Modify (generated): `plugin/.codex-plugin/plugin.json`
- Modify (generated projection): `.codex-plugin/plugin.json`
- Verify unchanged unless base version drift exists: `plugin/.claude-plugin/plugin.json`
- Verify unchanged unless source asset drift exists: `plugin/assets/icon.svg`

**Interfaces:**

- Consumes: the final shared `plugin/` payload from Tasks 1-3.
- Produces: a Codex cachebuster version whose digest matches that exact payload, plus verified Claude/Codex install behavior.

- [ ] **Step 1: Prove the committed manifest digest is stale after payload changes**

Run:

```bash
.venv/bin/python scripts/update_codex_plugin_manifest.py --check
```

Expected: exit 1 with a `manifest version ... != ...` or canonical projection mismatch. If it passes, inspect `git diff plugin/` and confirm the skill files actually changed before continuing.

- [ ] **Step 2: Regenerate canonical and projected plugin metadata**

Run:

```bash
.venv/bin/python scripts/update_codex_plugin_manifest.py
```

Expected: `Codex plugin manifests synchronized`. The `+codex.<12-hex-digest>` suffix changes in both Codex manifests, while the base version stays aligned with `pyproject.toml`.

- [ ] **Step 3: Verify generated metadata and installer fakes**

Run:

```bash
.venv/bin/python scripts/update_codex_plugin_manifest.py --check
.venv/bin/pytest \
  tests/install/test_codex_plugin_payload.py \
  tests/install/test_codex_install.py \
  tests/install/test_codex_cli.py \
  tests/install/test_claude_install.py \
  tests/install/test_claude_cli.py -q
```

Expected: PASS. The payload registers all 12 skill directories, excludes `_common`, has a valid digest, and preserves one enabled `rag-reviewer` plugin for each client.

- [ ] **Step 4: Run the complete relevant unit and static verification**

Run:

```bash
.venv/bin/pytest tests/skills tests/hooks tests/install -q
.venv/bin/ruff check plugin reviewer tests
git diff --check
.venv/bin/python scripts/update_codex_plugin_manifest.py --check
```

Expected: all commands pass without network or localhost access.

- [ ] **Step 5: Run safe installer and client smoke checks**

Always run the read-only/local checks:

```bash
uvx --from . reviewer install codex --dry-run
codex plugin list --json
claude plugin list --json
```

Expected:

- Codex dry-run proposes one enabled `rag-reviewer` plugin and exactly one `reviewer` MCP server.
- Codex and Claude plugin listings contain one enabled `rag-reviewer` entry.
- No listing exposes a second compatibility plugin or wrapper skill.

If an authenticated Claude Code CLI is available, additionally run:

```bash
claude --plugin-dir ./plugin \
  -p "/rag-reviewer:ask где находится проверка payload digest?" \
  --permission-mode bypassPermissions
```

Expected: Claude accepts `/rag-reviewer:ask`; `/skills`/debug output does not expose the removed long form. If the CLI or authentication is unavailable, record that exact skipped smoke check in the handoff; do not claim it passed.

- [ ] **Step 6: Commit generated payload metadata**

```bash
git add plugin/.codex-plugin/plugin.json .codex-plugin/plugin.json \
  plugin/.claude-plugin/plugin.json plugin/assets/icon.svg
git commit -m "chore(plugin): обновить cachebuster skills (PRI-227)"
```

- [ ] **Step 7: Confirm only intended files changed**

Run:

```bash
git status --short
git log -5 --oneline
```

Expected: no new task-owned changes remain. Pre-existing user untracked files remain untouched. The branch contains four PRI-227 implementation commits after the design/plan documentation commits.
