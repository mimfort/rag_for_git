# PRI-223 Configure Review and Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach `configure-review` to edit per-repository branch configuration without clobbering YAML and document the complete configuration ownership model and deployment scenarios in both READMEs.

**Architecture:** The skill resolves the current repository and effective branch source with existing offline/runtime commands, then applies a line-oriented patch only to the home per-repo `repository` block after one combined preview/confirmation. Documentation pairs the ownership model, VCS token contract, and single/multi-repo/CI workflows in English and Russian.

**Tech Stack:** Markdown skill prompts, Git CLI, reviewer CLI, pytest guard tests, Ruff for Python tests.

---

## File Map

- Modify `plugin/skills/configure-review/SKILL.md`: branch resolution/edit/verification workflow.
- Modify `tests/skills/test_configure_review_skill.py`: deterministic branch safety guards.
- Modify `README.md` and `README.ru.md`: storage ownership table, VCS matrix, and three scenarios.
- Modify `docs/board-providers.md`: align board setup wording with structured access metadata.
- Modify `tests/docs/test_readme_onboarding.py` and `tests/docs/test_board_provider_docs.py`: paired documentation contracts.

## Global Constraints

- `repository` may be written only to home per-repo YAML, never committed `.review.yml`.
- Preserve top-level keys, unknown repository subkeys, comments, line endings, and surrounding YAML style.
- Never parse and rewrite the entire target through `yaml.safe_dump`.
- No credential values may be read, displayed, copied, or written by the skill.
- No network git command and no automatic index/summary/task rebuild.
- Keep English/Russian README section order paired.

---

### Task 1: Guard the Branch Configuration Contract

**Files:**
- Modify: `tests/skills/test_configure_review_skill.py`

- [ ] **Step 1: Add failing branch workflow guards**

```python
# tests/skills/test_configure_review_skill.py
def _branch_section() -> str:
    text = SKILL.read_text(encoding="utf-8")
    match = re.search(r"## Repository branches.*?(?=\n## )", text, re.DOTALL)
    assert match
    return match.group()


def test_skill_manages_primary_and_index_branches():
    section = _branch_section()
    assert "repository.primary_branch" in section
    assert "repository.index_branches" in section
    assert "primary" in section
    assert "ordered unique" in section
    assert "must be present in" in section


def test_skill_resolves_repo_offline_and_shows_effective_source():
    section = _branch_section()
    assert "git rev-parse --show-toplevel" in section
    assert "git remote get-url origin" in section
    assert "reviewer config show --repo <owner/name> --json" in section
    for forbidden in ("git fetch", "git ls-remote", "git remote show"):
        assert forbidden not in section
    assert "source" in section


def test_skill_writes_branches_only_to_home_per_repo():
    section = _branch_section()
    assert "$XDG_CONFIG_HOME/rag-reviewer/repos/<owner>/<name>.yml" in section
    assert "never write `repository` to committed `.review.yml`" in section
    assert "even when committed policy is selected" in section


def test_skill_preserves_yaml_and_forbids_whole_file_dump():
    section = _branch_section()
    for marker in (
        "line-oriented patch",
        "top-level keys",
        "unknown repository subkeys",
        "comments",
        "line endings",
        "surrounding YAML style",
    ):
        assert marker in section
    assert "never serialize the complete file with `yaml.safe_dump`" in section


def test_skill_previews_confirms_and_verifies_branch_change():
    section = _branch_section()
    assert "old and new" in section
    assert "final confirmation" in section
    assert section.count("reviewer config show --repo <owner/name> --json") >= 2
    assert "exact primary/index/source" in section


def test_skill_never_runs_branch_followups():
    section = _branch_section()
    assert "rag-reviewer:sync-codebase" in section
    assert "do not run" in section.lower()
    assert "rag-reviewer:summarize-subsystems" not in section
```

- [ ] **Step 2: Run guards and verify RED**

Run: `.venv/bin/pytest tests/skills/test_configure_review_skill.py -q -k "branch or branches or yaml"`

Expected: FAIL because `## Repository branches` does not exist.

---

### Task 2: Implement the `configure-review` Branch Workflow

**Files:**
- Modify: `plugin/skills/configure-review/SKILL.md:1-58,171-176`
- Test: `tests/skills/test_configure_review_skill.py`

- [ ] **Step 1: Expand frontmatter and Scope**

Use this frontmatter description:

```yaml
description: Use when configuring or changing a repository's tracked branches, layered review policy, ignored tracked paths, retrieval limits, summary depth, or non-secret task-board metadata.
```

Add to Scope, after the policy key list:

```markdown
Tracked branches are separate from review policy. Manage `repository.primary_branch` and
`repository.index_branches` only in the home per-repo target. The committed `.review.yml` cannot
own `repository`, because branch selection must be available before a committed ref can be read.
```

- [ ] **Step 2: Add the complete branch section before Rebuild guidance**

Insert this exact section:

```markdown
## Repository branches

Handle branches before policy analysis whenever the user asks to inspect or change tracked
branches.

1. Resolve the local repository without network calls:
   - `git rev-parse --show-toplevel` gives the git root;
   - `git remote get-url origin` gives the canonical SSH/HTTPS remote candidate;
   - normalize it to lowercase `<owner/name>` with the same SSH/HTTPS forms accepted by reviewer;
   - if origin is absent or unrecognized, ask for `<owner/name>` explicitly.
   Never run `git fetch`, `git ls-remote`, or `git remote show`.
2. Run `reviewer config show --repo <owner/name> --json` and show the effective primary branch,
   ordered index branches, and source. A policy/VCS diagnostic error does not erase the returned
   branch section; a malformed home config is a blocking error and must not fall back silently.
3. Ask for `repository.primary_branch`, then ask for the complete ordered unique
   `repository.index_branches`. The primary must be present in the index list. Reject empty names,
   duplicates, and a primary outside the list.
4. The destination is always
   `$XDG_CONFIG_HOME/rag-reviewer/repos/<owner>/<name>.yml` (or the equivalent
   `~/.config/rag-reviewer/...` path when XDG is unset). Never write `repository` to committed
   `.review.yml`, even when committed policy is selected for other keys. If policy and branches
   change together, treat them as two targets in one preview.
5. Read the destination without following symlinks. Stop on malformed, non-regular, symlinked, or
   credential-like YAML. Build a line-oriented patch: if `repository` is absent, append the
   canonical block; if it exists, replace only `primary_branch` and `index_branches`. Preserve all
   top-level keys, unknown repository subkeys, comments, line endings, and surrounding YAML style.
   Never serialize the complete file with `yaml.safe_dump`.
6. Show the destination, source, old and new branch values, and the exact patch. Request one final
   confirmation before any branch or policy write. A rejection leaves every target unchanged.
7. After writing, run `reviewer config show --repo <owner/name> --json` again and require the exact
   primary/index/source expected from the home per-repo layer. Report a mismatch as an error.

If newly added index branches are not indexed, suggest `rag-reviewer:sync-codebase` once per new
branch, but do not run it. A primary change to an already indexed branch needs no rebuild. Removing
a branch stops reviewer from selecting it but does not delete its old base index automatically.
Branch changes never trigger subsystem-summary work.
```

- [ ] **Step 3: Integrate combined preview/completion wording**

Change Pipeline step 5 and Completion so they explicitly say:

```markdown
When branch and policy changes share a run, assemble both drafts first, show both paths and diffs,
and request one final confirmation before either write.
```

Completion must report old/new branches, selected branch source, changed policy keys, generic board targets/options, and suggested follow-ups.

- [ ] **Step 4: Run all skill guards**

Run: `.venv/bin/pytest tests/skills/test_configure_review_skill.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the skill contract**

```bash
git add plugin/skills/configure-review/SKILL.md tests/skills/test_configure_review_skill.py
git commit -m "feat(skill): configure-review настраивает ветки репозитория"
```

---

### Task 3: Documentation Ownership and Scenario Guards

**Files:**
- Modify: `tests/docs/test_readme_onboarding.py`

- [ ] **Step 1: Replace the obsolete GitLab limitation test**

Delete `test_gitlab_only_check_limitation_is_explicit` and add:

```python
def test_readmes_document_configuration_ownership_and_scenarios():
    cases = (
        ("README.md", "### Configuration ownership", ("Single repository", "Second repository", "CI / server")),
        ("README.ru.md", "### Владение конфигурацией", ("Один репозиторий", "Второй репозиторий", "CI / server")),
    )
    for filename, heading, scenarios in cases:
        section = _section(_read(filename), heading)
        for marker in (
            "global `.env`",
            "home global YAML",
            "home per-repo YAML",
            "committed `.review.yml`",
            "git remote / CLI",
            "Postgres / Neo4j",
            "reviewer init --scope repo",
            "reviewer config show --repo",
        ):
            assert marker in section, (filename, marker)
        for scenario in scenarios:
            assert scenario in section, (filename, scenario)


def test_readmes_document_vcs_token_matrix_and_check_semantics():
    for filename, heading in (
        ("README.md", "### VCS credentials"),
        ("README.ru.md", "### VCS credentials"),
    ):
        section = _section(_read(filename), heading)
        for marker in (
            "`GITHUB_TOKEN`",
            "Pull requests: Read and write",
            "Contents: Read",
            "`GITLAB_TOKEN`",
            "`api` scope",
            "`/user`",
            "`/api/v4/user`",
            "identity",
            "repository permissions",
        ):
            assert marker in section, (filename, marker)
        assert "GitLab-only" not in _read(filename)
        assert "dry-run `/rag-reviewer:review-pr`" not in _read(filename)
```

- [ ] **Step 2: Run docs guards and verify RED**

Run: `.venv/bin/pytest tests/docs/test_readme_onboarding.py -q -k "ownership or vcs_token"`

Expected: FAIL because the new sections do not exist and the old workaround remains.

---

### Task 4: Write Paired README Sections

**Files:**
- Modify: `README.md:294-390,599-606,662-683`
- Modify: `README.ru.md:297-393,586-593,649-670`
- Test: `tests/docs/test_readme_onboarding.py`

- [ ] **Step 1: Add `### Configuration ownership` to README.md**

Insert after `### Required services and credentials`:

```markdown
### Configuration ownership

| Location | Owner | Stores | Must not store |
|---|---|---|---|
| global `.env` | deployment/operator | secrets, credentials, DSNs, runtime infrastructure and compatibility fallbacks | repository policy |
| home global YAML | OS account running reviewer | shared non-secret defaults | credentials |
| home per-repo YAML | OS account running reviewer | `repository.primary_branch`, `repository.index_branches`, operator-owned repo policy | credentials |
| committed `.review.yml` | repository team | team-visible review policy and non-secret task-board metadata | credentials or `repository` |
| git remote / CLI | repository/operator | canonical `owner/name` identity and explicit command overrides | persisted secrets |
| Postgres / Neo4j | reviewer runtime | derived indexes, task/review state and code graph | source-of-truth configuration |

#### Single repository

Run `reviewer init` from the clone, inspect the global `.env` and home per-repo previews, then run
`reviewer check` and `reviewer config show --repo owner/name`.

#### Second repository

Run `reviewer init --scope repo` from the second clone. It creates or previews only that repository's
home per-repo YAML and does not rewrite global `.env` or the first repository's config.

#### CI / server

Inject secrets into global `.env` or the process from a secret manager. Use noninteractive init only
for deterministic preview/write, mount home YAML for the service account, and keep team-owned policy
in committed `.review.yml`. Pass `--repo owner/name` when no usable git remote is present.
```

- [ ] **Step 2: Add the Russian paired section**

Use the same table row order and commands under `### Владение конфигурацией`, with scenario headings
`#### Один репозиторий`, `#### Второй репозиторий`, and `#### CI / server`. Translate prose, but keep
the literal storage labels tested above (`global `.env``, `home global YAML`, `home per-repo YAML`,
`committed `.review.yml``, `git remote / CLI`, `Postgres / Neo4j`).

- [ ] **Step 3: Add paired `### VCS credentials` sections**

Use this exact matrix in both READMEs (translate prose around it, not env/scope/API literals):

```markdown
### VCS credentials

| Provider | Environment | Minimum access | Reviewer reads | Reviewer writes | `reviewer check` |
|---|---|---|---|---|---|
| GitHub | `GITHUB_TOKEN` | fine-grained PAT: Pull requests: Read and write; Contents: Read | PR metadata, files, comments, contents, compare | review comments/summary and PR body backlink | authenticates `/user` identity |
| GitLab | `GITLAB_URL`, `GITLAB_TOKEN` | PAT/project token with `api` scope | MR metadata, changes, notes, repository files, compare | discussions/notes and MR description backlink | authenticates `/api/v4/user` identity |

The health check proves URL/token authentication, not every granular repository permission. The
selected repository permissions are exercised by an actual review. `reviewer init` shows the same
contract before prompting only the selected provider's credentials.
```

- [ ] **Step 4: Update skill/security/limitations prose**

- Rename the skill heading to `configure-review — update layered policy and branches` in English and
  its Russian equivalent.
- Add `tracked branches` to its When/Когда line.
- State that branch values always go to home per-repo YAML.
- Remove both GitLab-only workaround paragraphs from Try/Known limitations.
- Keep the security statement that credentials are env-only.

- [ ] **Step 5: Run README tests**

Run: `.venv/bin/pytest tests/docs/test_readme_onboarding.py -q`

Expected: PASS, including paired section order.

- [ ] **Step 6: Commit README updates**

```bash
git add README.md README.ru.md tests/docs/test_readme_onboarding.py
git commit -m "docs(config): описывает владение настройками и VCS credentials"
```

---

### Task 5: Align Board Provider Reference

**Files:**
- Modify: `docs/board-providers.md:118-223`
- Modify: `tests/docs/test_board_provider_docs.py`

- [ ] **Step 1: Add failing access-contract docs guards**

```python
# tests/docs/test_board_provider_docs.py
def test_board_docs_explain_structured_setup_contract():
    text = _read("docs/board-providers.md")
    section = text.split("## Configuration", 1)[1].split("## YouGile", 1)[0]
    for marker in (
        "minimum permissions",
        "read operations",
        "write operations",
        "validation",
        "selected provider",
        "reviewer init",
    ):
        assert marker in section


def test_yougile_docs_do_not_require_admin_role():
    text = _read("docs/board-providers.md")
    section = text.split("## YouGile", 1)[1].split("## YouTrack", 1)[0]
    assert "admin role is not required" in section
    assert "API-capable account" in section
    assert "allowOnlyOpenId" in section
```

- [ ] **Step 2: Run guards and verify RED**

Run: `.venv/bin/pytest tests/docs/test_board_provider_docs.py -q -k "structured_setup or admin_role"`

Expected: FAIL because current prose does not use the exact structured-contract wording.

- [ ] **Step 3: Add authoritative setup contract prose**

After the credentials paragraph in `## Configuration`, add:

```markdown
`reviewer init` asks for credentials only after a selected provider is known. Registry setup
metadata is complete and structured: every registered provider declares minimum permissions,
read operations, write operations, validation semantics, and an official setup URL. The installer
shows that contract before the first secret prompt. `reviewer check` repeats provider validation
without returning credentials.
```

In YouGile, add:

```markdown
An admin role is not required by reviewer itself. The API-capable account must be able to access the
selected company and perform the listed task operations. With `allowOnlyOpenId`, use a ready key from
such an account because the password acquisition endpoint is disabled.
```

- [ ] **Step 4: Run board docs tests**

Run: `.venv/bin/pytest tests/docs/test_board_provider_docs.py -q`

Expected: PASS.

- [ ] **Step 5: Commit board docs**

```bash
git add docs/board-providers.md tests/docs/test_board_provider_docs.py
git commit -m "docs(boards): фиксирует setup access contract"
```

---

### Task 6: Part D Verification

**Files:**
- Modify only if verification exposes scoped defects.

- [ ] **Step 1: Run focused skill/docs suite**

Run:

```bash
.venv/bin/pytest \
  tests/skills/test_configure_review_skill.py \
  tests/docs/test_readme_onboarding.py \
  tests/docs/test_board_provider_docs.py -q
```

Expected: all pass.

- [ ] **Step 2: Run all skill and docs guards**

Run: `.venv/bin/pytest tests/skills tests/docs -q`

Expected: all pass.

- [ ] **Step 3: Check formatting and whitespace**

Run: `.venv/bin/ruff check tests/skills/test_configure_review_skill.py tests/docs`

Expected: `All checks passed!`

Run: `git diff --check`

Expected: exit code 0 with no output.

- [ ] **Step 4: Audit the forbidden branch paths**

Run: `git diff dev...HEAD -- plugin/skills/configure-review/SKILL.md README.md README.ru.md docs/board-providers.md`

Expected: the skill writes `repository` only to home per-repo YAML, never runs summaries, and both READMEs contain paired ownership/VCS sections.

- [ ] **Step 5: Commit verification fixes if needed**

Stage only Part D files and commit:

```bash
git commit -m "fix(config): закрывает регрессии configure-review"
```

Do not create an empty commit.
