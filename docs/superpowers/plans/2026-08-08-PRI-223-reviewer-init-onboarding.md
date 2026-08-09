# PRI-223 Reviewer Init Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Разделить `reviewer init` на independently runnable global и repo stages с offline git autodetect, preview-before-write и сохранением legacy/advanced config.

**Architecture:** Pure repo detection/planning/apply живёт в новом `reviewer/config/onboarding.py`, а `reviewer/entrypoints/cli.py` только собирает user input и оркестрирует планы. Global `.env` продолжает использовать существующие `install.py` render/redaction helpers; repo YAML использует public branch resolver и общую create/append/noop механику с migration.

**Tech Stack:** Python 3.11–3.13, Click, dataclasses, pathlib, PyYAML, pytest, Ruff.

---

## File Map

- Modify `reviewer/gitutil.py`: offline git-root и local `origin/HEAD` helpers.
- Create `reviewer/config/onboarding.py`: immutable repo detection/config plans, validation, preview and apply.
- Modify `reviewer/config/branches.py`: общий renderer/publisher `repository` block для migration и onboarding.
- Modify `reviewer/install.py`: standard wizard fields без repo/web/legacy board MCP.
- Modify `reviewer/entrypoints/cli.py`: `--scope`, `--repo`, unified preview, final confirmation, apply and effective branch output.
- Modify `reviewer/launcher/metadata.py`: `init` details отражают два target layers.
- Modify `tests/test_gitutil.py`: git helper tests.
- Create `tests/config/test_onboarding.py`: pure plan/apply tests.
- Modify `tests/config/test_branches_migrate.py`: regression общей persistence.
- Modify `tests/install/test_install_wizard.py` and `tests/test_install_wizard.py`: wizard compatibility contract.
- Create `tests/entrypoints/test_cli_init_onboarding.py`: scope/noninteractive/interactive orchestration tests.
- Modify `tests/launcher/test_catalog.py`: Click choice and repo option remain discoverable.

## Global Constraints

- Не читать committed `.review.yml` для branch bootstrap.
- Не использовать network git commands (`fetch`, `ls-remote`, `remote show`).
- `--yes` и `--dry-run` не вызывают prompt, browser, provider setup, check или VCS policy lookup.
- До unified preview/final confirmation не создавать directories и не писать files.
- Existing `repository` block не перезаписывать; existing removed env keys сохранять через `extra`.
- Часть C provider-scoped credentials и часть D configure-review/docs не реализуются здесь.
- Каждый production change проходит RED → GREEN и отдельный focused commit.

---

### Task 1: Offline Git Repository Detection

**Files:**
- Modify: `reviewer/gitutil.py:7-47`
- Test: `tests/test_gitutil.py`

- [ ] **Step 1: Write failing git helper tests**

Update imports and add:

```python
from reviewer.gitutil import (
    changed_files,
    commits_behind,
    file_at_ref,
    remote_default_branch,
    remote_url,
    repo_root,
)


def test_repo_root_returns_top_level_from_nested_path(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    _run("git", "init", "-q", cwd=repo)

    assert repo_root(str(nested)) == str(repo.resolve())
    assert repo_root(str(tmp_path / "missing")) is None


def test_remote_default_branch_reads_local_origin_head(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "-q", cwd=repo)
    _run("git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/dev", cwd=repo)

    assert remote_default_branch(str(repo)) == "dev"


def test_remote_default_branch_missing_ref_is_none(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "-q", cwd=repo)

    assert remote_default_branch(str(repo)) is None


def test_remote_default_branch_uses_only_local_symbolic_ref(monkeypatch):
    calls = []

    def fake_git(repo, *args):
        calls.append((repo, args))
        return "refs/remotes/origin/dev\n"

    monkeypatch.setattr("reviewer.gitutil._git", fake_git)

    assert remote_default_branch("/repo") == "dev"
    assert calls == [
        ("/repo", ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"))
    ]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest tests/test_gitutil.py -q`

Expected: collection error because `repo_root` and `remote_default_branch` do not exist.

- [ ] **Step 3: Implement fail-soft local helpers**

Add to `reviewer/gitutil.py`:

```python
def repo_root(path: str = ".") -> str | None:
    """Вернуть absolute git top-level либо None вне рабочего дерева."""
    try:
        return _git(path, "rev-parse", "--show-toplevel").strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def remote_default_branch(repo: str) -> str | None:
    """Вернуть default branch из локального origin/HEAD без обращения к сети."""
    try:
        ref = _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    prefix = "refs/remotes/origin/"
    return ref.removeprefix(prefix) if ref.startswith(prefix) else None
```

- [ ] **Step 4: Run focused tests and lint**

Run: `.venv/bin/pytest tests/test_gitutil.py -q && .venv/bin/ruff check reviewer/gitutil.py tests/test_gitutil.py`

Expected: all tests pass; Ruff reports no errors.

- [ ] **Step 5: Commit**

```bash
git add reviewer/gitutil.py tests/test_gitutil.py
git commit -m "feat(init): определяет repo и primary branch локально"
```

---

### Task 2: Pure Repo Onboarding Plan and Persistence

**Files:**
- Create: `reviewer/config/onboarding.py`
- Modify: `reviewer/config/branches.py:127-194`
- Create: `tests/config/test_onboarding.py`
- Modify: `tests/config/test_branches_migrate.py`

- [ ] **Step 1: Write failing detection/planning tests**

Create `tests/config/test_onboarding.py` with helpers and core cases:

```python
from pathlib import Path

import yaml

from reviewer.config.onboarding import (
    RepositoryConfigPlan,
    RepositoryDetection,
    apply_repository_config,
    detect_repository,
    parse_branch_csv,
    plan_repository_config,
)
from reviewer.config.settings import Settings


def _settings(monkeypatch, branches="main"):
    monkeypatch.setenv("REVIEW_BRANCHES", branches)
    return Settings(_env_file=None)


def test_detect_repository_prefers_cli_repo(monkeypatch, tmp_path):
    monkeypatch.setattr("reviewer.config.onboarding.repo_root", lambda _path: str(tmp_path))
    monkeypatch.setattr(
        "reviewer.config.onboarding.remote_url",
        lambda _path: "https://github.com/remote/name.git",
    )
    monkeypatch.setattr("reviewer.config.onboarding.remote_default_branch", lambda _path: "dev")

    result = detect_repository(".", "CLI/Repo", settings=_settings(monkeypatch))

    assert result == RepositoryDetection(
        root=tmp_path,
        repo="cli/repo",
        repo_source="cli",
        primary="dev",
        primary_source="git:origin/HEAD",
    )


def test_detect_repository_falls_back_to_effective_primary(monkeypatch, tmp_path):
    monkeypatch.setattr("reviewer.config.onboarding.repo_root", lambda _path: str(tmp_path))
    monkeypatch.setattr(
        "reviewer.config.onboarding.remote_url",
        lambda _path: "https://github.com/o/r.git",
    )
    monkeypatch.setattr("reviewer.config.onboarding.remote_default_branch", lambda _path: None)

    result = detect_repository(".", None, settings=_settings(monkeypatch, "release,main"))

    assert result.repo == "o/r"
    assert result.primary == "release"
    assert result.primary_source == "env"


def test_plan_uses_detected_primary_when_effective_index_is_incompatible(monkeypatch, tmp_path):
    detection = RepositoryDetection(tmp_path, "o/r", "git:origin", "dev", "git:origin/HEAD")

    plan = plan_repository_config(
        detection,
        settings=_settings(monkeypatch, "main,master"),
        config_root=tmp_path,
    )

    assert plan.primary == "dev"
    assert plan.index == ("dev",)
    assert plan.action == "create"


def test_parse_branch_csv_requires_primary_in_unique_nonempty_index():
    assert parse_branch_csv("dev, main", "dev") == ("dev", "main")
    for raw in ("", "dev,dev", "dev,,main"):
        try:
            parse_branch_csv(raw, "dev")
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {raw!r}")
```

- [ ] **Step 2: Write failing apply/idempotency tests**

Append:

```python
def test_apply_creates_and_repeat_is_byte_for_byte_noop(monkeypatch, tmp_path):
    detection = RepositoryDetection(tmp_path, "o/r", "git:origin", "dev", "git:origin/HEAD")
    plan = plan_repository_config(
        detection,
        settings=_settings(monkeypatch),
        index=("dev", "main"),
        config_root=tmp_path,
    )

    apply_repository_config(plan)
    first = plan.path.read_text(encoding="utf-8")
    apply_repository_config(
        plan_repository_config(
            detection,
            settings=_settings(monkeypatch),
            config_root=tmp_path,
        )
    )

    assert plan.path.read_text(encoding="utf-8") == first
    assert yaml.safe_load(first)["repository"] == {
        "primary_branch": "dev",
        "index_branches": ["dev", "main"],
    }


def test_apply_appends_without_losing_comments(monkeypatch, tmp_path):
    path = tmp_path / "repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("# keep me\nmax_comments: 7\n", encoding="utf-8")
    detection = RepositoryDetection(tmp_path, "o/r", "cli", "2.0", "default")
    plan = plan_repository_config(
        detection,
        settings=_settings(monkeypatch),
        index=("2.0", "on", "feature{x}"),
        config_root=tmp_path,
    )

    apply_repository_config(plan)
    text = path.read_text(encoding="utf-8")

    assert "# keep me" in text
    assert yaml.safe_load(text)["repository"]["index_branches"] == [
        "2.0",
        "on",
        "feature{x}",
    ]


def test_existing_repository_is_noop(monkeypatch, tmp_path):
    path = tmp_path / "repos/o/r.yml"
    path.parent.mkdir(parents=True)
    original = "repository:\n  index_branches: [trunk]\n  future: keep\n"
    path.write_text(original, encoding="utf-8")
    detection = RepositoryDetection(tmp_path, "o/r", "git:origin", "dev", "git:origin/HEAD")

    plan = plan_repository_config(
        detection,
        settings=_settings(monkeypatch),
        config_root=tmp_path,
    )
    apply_repository_config(plan)

    assert plan.action == "noop"
    assert path.read_text(encoding="utf-8") == original
```

- [ ] **Step 3: Run tests and verify RED**

Run: `.venv/bin/pytest tests/config/test_onboarding.py -q`

Expected: collection error because `reviewer.config.onboarding` does not exist.

- [ ] **Step 4: Extract reusable repository block publisher**

In `reviewer/config/branches.py`, replace `_render_block` with a primary-aware renderer and add a
single publisher used by migration and onboarding:

```python
def render_repository_block(primary: str, index: tuple[str, ...], *, migrated: bool = False) -> str:
    data = {BRANCHES_KEY: {"primary_branch": primary, "index_branches": list(index)}}
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    if not migrated:
        return body
    return (
        "# Отслеживаемые ветки репозитория (перенесено из REVIEW_BRANCHES).\n"
        + body
    )


def publish_repository_block(path: Path, source: str, block: str) -> str:
    """Create/append/noop repository block and return the applied action."""
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "x", encoding="utf-8") as handle:
                handle.write(block)
        except FileExistsError:
            return "noop"
        return "create"
    if BRANCHES_KEY in _read_mapping(existing, source):
        return "noop"
    separator = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(existing + separator + "\n" + block, encoding="utf-8")
    return "append"
```

Update `migrate_repo_branches` to call
`render_repository_block(index[0], index, migrated=True)` and `publish_repository_block(...)`,
replacing its existing direct create/append block with:

```python
action = publish_repository_block(destination, source, block)
return BranchMigrationResult(
    path=destination,
    created=action == "create",
    noop=action == "noop",
)
```

- [ ] **Step 5: Implement `reviewer/config/onboarding.py`**

Create the module with immutable data and no Click dependency:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from typing import Literal

from reviewer.config.branches import (
    publish_repository_block,
    render_repository_block,
    resolve_repo_branches,
)
from reviewer.config.layers import _read_mapping, home_repo_path, reviewer_config_root
from reviewer.config.settings import Settings
from reviewer.gitutil import remote_default_branch, remote_url, repo_root
from reviewer.services.repo_id import derive_repo_from_remote, normalize_repo

PlanAction = Literal["create", "append", "noop"]


@dataclass(frozen=True)
class RepositoryDetection:
    root: Path
    repo: str
    repo_source: str
    primary: str
    primary_source: str


@dataclass(frozen=True)
class RepositoryConfigPlan:
    path: Path
    repo: str
    primary: str
    index: tuple[str, ...]
    repo_source: str
    primary_source: str
    action: PlanAction
    preview: str


def parse_branch_csv(raw: str, primary: str) -> tuple[str, ...]:
    parts = [part.strip() for part in raw.split(",")]
    if not parts or any(not part for part in parts):
        raise ValueError("index branches должны быть непустым CSV")
    if len(parts) != len(set(parts)):
        raise ValueError("index branches содержат дубли")
    if primary not in parts:
        raise ValueError(f"primary branch {primary!r} отсутствует в index branches")
    return tuple(parts)


def detect_repository(
    path: str,
    repo_override: str | None,
    *,
    settings: Settings,
) -> RepositoryDetection | None:
    root_value = repo_root(path)
    if root_value is None:
        return None
    root = Path(root_value)
    if repo_override:
        repo = normalize_repo(repo_override)
        repo_source = "cli"
    else:
        repo = derive_repo_from_remote(remote_url(root_value) or "")
        if repo is None:
            return None
        repo_source = "git:origin"
    branches = resolve_repo_branches(repo, settings=settings)
    detected = remote_default_branch(root_value)
    return RepositoryDetection(
        root=root,
        repo=repo,
        repo_source=repo_source,
        primary=detected or branches.primary,
        primary_source="git:origin/HEAD" if detected else branches.source,
    )


def plan_repository_config(
    detection: RepositoryDetection,
    *,
    settings: Settings,
    primary: str | None = None,
    index: tuple[str, ...] | None = None,
    config_root: Path | None = None,
) -> RepositoryConfigPlan:
    root = config_root or reviewer_config_root()
    path = home_repo_path(detection.repo, root)
    effective = resolve_repo_branches(detection.repo, settings=settings, config_root=root)
    if effective.source == f"home:repos/{detection.repo}.yml":
        selected_primary = effective.primary
        selected_index = effective.index
        action: PlanAction = "noop"
    else:
        selected_primary = primary or detection.primary
        selected_index = index or (
            effective.index if selected_primary in effective.index else (selected_primary,)
        )
        parse_branch_csv(",".join(selected_index), selected_primary)
        try:
            existing = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            action = "create"
        else:
            action = "noop" if "repository" in _read_mapping(
                existing, f"home:repos/{detection.repo}.yml"
            ) else "append"
    preview = render_repository_block(selected_primary, selected_index)
    return RepositoryConfigPlan(
        path=path,
        repo=detection.repo,
        primary=selected_primary,
        index=selected_index,
        repo_source=detection.repo_source,
        primary_source=detection.primary_source,
        action=action,
        preview=preview,
    )


def apply_repository_config(plan: RepositoryConfigPlan) -> None:
    if plan.action == "noop":
        return
    action = publish_repository_block(
        plan.path,
        f"home:repos/{plan.repo}.yml",
        plan.preview,
    )
    if action not in {plan.action, "noop"}:
        raise RuntimeError(f"ожидалось действие {plan.action}, выполнено {action}")
```

- [ ] **Step 6: Run focused tests and existing migration regressions**

Run: `.venv/bin/pytest tests/config/test_onboarding.py tests/config/test_branches_migrate.py tests/config/test_branches.py -q`

Expected: all tests pass.

- [ ] **Step 7: Lint and commit**

Run: `.venv/bin/ruff check reviewer/config/onboarding.py reviewer/config/branches.py tests/config/test_onboarding.py tests/config/test_branches_migrate.py`

Then:

```bash
git add reviewer/config/onboarding.py reviewer/config/branches.py tests/config/test_onboarding.py tests/config/test_branches_migrate.py
git commit -m "feat(config): добавляет план repo onboarding"
```

---

### Task 3: Standard Wizard Contract Cleanup

**Files:**
- Modify: `reviewer/install.py:29-35,132-179,182-316`
- Modify: `tests/install/test_install_wizard.py`
- Modify: `tests/test_install_wizard.py`

- [ ] **Step 1: Change tests to the new standard-field contract**

Replace assertions that require removed fields:

```python
REMOVED_STANDARD_KEYS = {
    "DEFAULT_REPO",
    "REVIEW_BRANCHES",
    "WEB_ADMIN_USER",
    "WEB_ADMIN_PASSWORD",
    "TASK_BOARD_MCP",
}


def test_fresh_wizard_omits_repo_web_and_legacy_board_mcp():
    keys = {field.key for group in inst.WIZARD_GROUPS for field in group.fields}
    assert keys.isdisjoint(REMOVED_STANDARD_KEYS)


def test_runtime_template_keeps_compatibility_keys():
    template_keys = _keys_from_text(inst.ENV_TEMPLATE)
    assert REMOVED_STANDARD_KEYS <= template_keys


def test_render_env_preserves_removed_existing_keys_as_extra():
    values = {field.key: field.default for group in inst.WIZARD_GROUPS for field in group.fields}
    extra = {key: f"existing-{key.lower()}" for key in REMOVED_STANDARD_KEYS}

    rendered = inst.render_env(values, extra)

    for key, value in extra.items():
        assert f"{key}={value}" in rendered
```

Update board registry assertions to require exactly
`{"TASK_BOARD_KEY_PATTERN", "TASK_BOARD_URL_TEMPLATE"}` as common fields and adjust the field
count from `registry_fields + 3` to `registry_fields + 2`.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py tests/test_install_wizard.py -q`

Expected: failures because removed keys are still present in `WIZARD_GROUPS`.

- [ ] **Step 3: Remove only standard wizard declarations**

In `reviewer/install.py`:

```python
COMMON_BOARD_ENV_KEYS = frozenset(
    {
        "TASK_BOARD_KEY_PATTERN",
        "TASK_BOARD_URL_TEMPLATE",
    }
)
```

Remove the `TASK_BOARD_MCP` `EnvField`, the complete `Мульти-репо / ветки` `EnvGroup`, the
complete `Веб-админка` `EnvGroup`, and their `_GROUP_HEADERS` entries. Keep
`_ENV_TEMPLATE_BASE`, `.env.example`, `Settings`, and compatibility comments unchanged.

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py tests/test_install_wizard.py -q`

Expected: all tests pass.

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/ruff check reviewer/install.py tests/install/test_install_wizard.py tests/test_install_wizard.py
```

---

### Task 4: CLI Scope and Noninteractive Unified Preview

**Files:**
- Modify: `reviewer/entrypoints/cli.py:97-103,1083-1195`
- Create: `tests/entrypoints/test_cli_init_onboarding.py`

- [ ] **Step 1: Write scope/dry-run/yes tests**

Create tests with a helper that isolates env and home paths:

```python
from pathlib import Path

from click.testing import CliRunner

from reviewer.entrypoints.cli import cli


def _isolate(monkeypatch, tmp_path):
    env = tmp_path / "runtime.env"
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: env)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return env


def _detection(tmp_path):
    from reviewer.config.onboarding import RepositoryDetection

    return RepositoryDetection(
        root=tmp_path,
        repo="o/r",
        repo_source="git:origin",
        primary="dev",
        primary_source="git:origin/HEAD",
    )


def test_init_scope_global_never_detects_repo(monkeypatch, tmp_path):
    env = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("repo detection called")),
    )

    result = CliRunner().invoke(cli, ["init", "--scope", "global", "--yes"])

    assert result.exit_code == 0, result.output
    assert env.exists()


def test_init_scope_repo_does_not_rewrite_env(monkeypatch, tmp_path):
    env = _isolate(monkeypatch, tmp_path)
    env.write_text("SENTINEL=unchanged\n", encoding="utf-8")
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )

    result = CliRunner().invoke(cli, ["init", "--scope", "repo", "--yes"])

    assert result.exit_code == 0, result.output
    assert env.read_text(encoding="utf-8") == "SENTINEL=unchanged\n"
    assert (tmp_path / "xdg/rag-reviewer/repos/o/r.yml").exists()


def test_init_dry_run_previews_both_targets_without_writes(monkeypatch, tmp_path):
    env = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.apply_repository_config",
        lambda _plan: (_ for _ in ()).throw(AssertionError("repo write called")),
    )
    monkeypatch.setattr(
        "click.prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("prompt called")),
    )

    result = CliRunner().invoke(cli, ["init", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert str(env) in result.output
    assert "repos/o/r.yml" in result.output
    assert "git:origin/HEAD" in result.output
    assert not env.exists()
    assert not (tmp_path / "xdg").exists()


def test_init_yes_never_enters_provider_or_post_network_stages(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )
    monkeypatch.setattr(
        "reviewer.tasks.boards.setup.configure_board_provider",
        lambda *_args: (_ for _ in ()).throw(AssertionError("provider setup called")),
    )
    monkeypatch.setattr(
        "reviewer.entrypoints.cli._run_codex_target",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("client install called")),
    )

    result = CliRunner().invoke(cli, ["init", "--yes"])

    assert result.exit_code == 0, result.output
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_init_onboarding.py -q`

Expected: Click rejects `--scope`/`--repo`, and onboarding imports do not exist in CLI.

- [ ] **Step 3: Add CLI options and small plan helpers**

Import onboarding symbols and add:

```python
from reviewer.config.onboarding import (
    RepositoryConfigPlan,
    apply_repository_config,
    detect_repository,
    parse_branch_csv,
    plan_repository_config,
)


@dataclass(frozen=True)
class _GlobalInitPlan:
    path: Path
    content: str
    preview: str
    source: str


def _render_init_preview(
    global_plan: _GlobalInitPlan | None,
    repo_plan: RepositoryConfigPlan | None,
) -> None:
    click.echo("# reviewer init preview")
    if global_plan is not None:
        click.echo(f"\nfile: {global_plan.path}")
        click.echo(f"source: {global_plan.source}")
        click.echo(global_plan.preview)
    if repo_plan is not None:
        click.echo(f"\nfile: {repo_plan.path}")
        click.echo(f"action: {repo_plan.action}")
        click.echo(f"repo: {repo_plan.repo} ({repo_plan.repo_source})")
        click.echo(f"primary: {repo_plan.primary} ({repo_plan.primary_source})")
        click.echo(repo_plan.preview)
```

Extend Click declaration and signature:

```python
@click.option(
    "--scope",
    type=click.Choice(("all", "global", "repo")),
    default="all",
    show_default=True,
)
@click.option("--repo", "repo_opt", default=None, help="owner/name для repo stage")
def init(
    path_opt: str | None,
    yes: bool,
    dry_run: bool,
    scope: str,
    repo_opt: str | None,
) -> None:
```

- [ ] **Step 4: Refactor command into plan/preview/apply phases**

Keep current board setup logic, but execute it only for global scope and interactive mode. The
command body must follow this concrete shape:

```python
run_global = scope in {"all", "global"}
run_repo = scope in {"all", "repo"}
settings = Settings()
global_plan = None
repo_plan = None

if run_global:
    dest = Path(path_opt).expanduser() if path_opt else inst.default_env_path()
    current = inst.read_env(dest)
    values = inst.prompt_groups(inst.WIZARD_GROUPS, current=current, yes=yes or dry_run)
    extra = {
        key: value
        for key, value in current.items()
        if key not in {field.key for group in inst.WIZARD_GROUPS for field in group.fields}
    }
    content = inst.render_env(values, extra)
    global_plan = _GlobalInitPlan(
        path=dest,
        content=content,
        preview=inst.render_env_preview(values, extra),
        source=f"existing:{dest}" if dest.is_file() else "wizard defaults/input",
    )

if run_repo:
    detection = detect_repository(".", repo_opt, settings=settings)
    if detection is None:
        message = "repo не определён: запустите из git repository или передайте --repo owner/name"
        if scope == "repo":
            raise click.ClickException(message)
        click.echo(f"repo stage пропущен: {message}")
    else:
        repo_plan = plan_repository_config(detection, settings=settings)

_render_init_preview(global_plan, repo_plan)
if dry_run:
    return
if not yes and not click.confirm("\nЗаписать показанные изменения?", default=True):
    click.echo("Отменено — файлы не изменены.")
    return
if global_plan is not None:
    global_plan.path.parent.mkdir(parents=True, exist_ok=True)
    global_plan.path.write_text(global_plan.content, encoding="utf-8")
    click.echo(f"✓ Записан {global_plan.path}")
if repo_plan is not None:
    apply_repository_config(repo_plan)
    click.echo(f"✓ Repo config: {repo_plan.action} → {repo_plan.path}")
```

Preserve existing interactive provider setup between global values collection and content render;
preserve reviewer check/Codex prompts only after successful apply and only without `--yes`.

- [ ] **Step 5: Run focused CLI and legacy tests**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_init_onboarding.py tests/install/test_install_wizard.py -q`

Expected: all tests pass.

- [ ] **Step 6: Lint and commit**

```bash
.venv/bin/ruff check reviewer/entrypoints/cli.py tests/entrypoints/test_cli_init_onboarding.py
```

---

### Task 5: Interactive Correction and Preview-Before-Write Guard

**Files:**
- Modify: `reviewer/entrypoints/cli.py`
- Modify: `tests/entrypoints/test_cli_init_onboarding.py`

- [ ] **Step 1: Add failing interactive tests**

Append:

```python
def test_init_interactive_can_correct_detected_branches(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )
    monkeypatch.setattr("reviewer.install.prompt_groups", lambda *_args, **_kwargs: {})
    answers = iter(["release", "release,main"])
    monkeypatch.setattr("click.prompt", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr("click.confirm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("reviewer.entrypoints.cli._shutil.which", lambda _name: None)

    result = CliRunner().invoke(cli, ["init", "--scope", "repo"])

    assert result.exit_code == 0, result.output
    data = yaml.safe_load((tmp_path / "xdg/rag-reviewer/repos/o/r.yml").read_text())
    assert data["repository"] == {
        "primary_branch": "release",
        "index_branches": ["release", "main"],
    }


def test_init_rejects_preview_before_first_write(monkeypatch, tmp_path):
    env = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )
    monkeypatch.setattr("reviewer.install.prompt_groups", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("click.prompt", lambda _text, default, **_kwargs: default)
    confirmations = iter([False, False])  # provider disabled, final write rejected
    monkeypatch.setattr("click.confirm", lambda *_args, **_kwargs: next(confirmations))

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0, result.output
    assert "Отменено" in result.output
    assert not env.exists()
    assert not (tmp_path / "xdg").exists()


def test_init_scope_repo_missing_detection_is_hard_error(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("reviewer.entrypoints.cli.detect_repository", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(cli, ["init", "--scope", "repo", "--yes"])

    assert result.exit_code != 0
    assert "--repo owner/name" in result.output


def test_init_interactive_asks_repo_when_remote_is_unrecognized(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    detection = _detection(tmp_path)
    calls = iter([None, detection])
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: next(calls),
    )
    answers = iter(["o/r", "dev", "dev"])
    monkeypatch.setattr("click.prompt", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr("click.confirm", lambda *_args, **_kwargs: True)

    result = CliRunner().invoke(cli, ["init", "--scope", "repo"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "xdg/rag-reviewer/repos/o/r.yml").exists()
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_init_onboarding.py -q`

Expected: branch correction test writes detected `dev`, not requested `release`.

- [ ] **Step 3: Add interactive branch correction loop**

Before `plan_repository_config` in interactive repo mode, retry detection once with an explicitly
entered repo id, then collect branches:

```python
detection = detect_repository(".", repo_opt, settings=settings)
if detection is None and not yes and not dry_run and repo_opt is None:
    entered_repo = click.prompt("Repository (owner/name)", default="", show_default=False).strip()
    if entered_repo:
        detection = detect_repository(".", entered_repo, settings=settings)

if detection is None:
    message = "repo не определён: запустите из git repository или передайте --repo owner/name"
    if scope == "repo":
        raise click.ClickException(message)
    click.echo(f"repo stage пропущен: {message}")

primary = detection.primary
index = None
initial_plan = plan_repository_config(detection, settings=settings)
if initial_plan.action != "noop" and not yes and not dry_run:
    primary = click.prompt("Primary branch", default=detection.primary).strip()
    while True:
        raw_index = click.prompt("Index branches (CSV)", default=primary)
        try:
            index = parse_branch_csv(raw_index, primary)
        except ValueError as exc:
            click.echo(f"Некорректные ветки: {exc}")
            continue
        break
repo_plan = plan_repository_config(
    detection,
    settings=settings,
    primary=primary,
    index=index,
)
```

If the initial plan is `noop`, skip branch prompts and keep its effective values.

- [ ] **Step 4: Run CLI tests**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_init_onboarding.py -q`

Expected: all tests pass.

- [ ] **Step 5: Run existing provider flow regression**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py::test_init_interactive_configures_selected_registry_provider tests/install/test_install_wizard.py::test_init_noninteractive_modes_never_touch_provider_setup_stages -q`

Expected: both pass after updating test confirmation sequences for the final preview prompt.

- [ ] **Step 6: Commit**

```bash
git add reviewer/entrypoints/cli.py tests/entrypoints/test_cli_init_onboarding.py tests/install/test_install_wizard.py
```

---

### Task 6: Effective Output and Launcher Contract

**Files:**
- Modify: `reviewer/entrypoints/cli.py:97-103,159-207`
- Modify: `reviewer/launcher/metadata.py:55-60`
- Modify: `tests/entrypoints/test_cli_init_onboarding.py`
- Modify: `tests/launcher/test_catalog.py`

- [ ] **Step 1: Add failing output/catalog tests**

Append CLI assertion:

```python
def test_init_repo_prints_effective_branch_source_and_follow_up(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )

    result = CliRunner().invoke(cli, ["init", "--scope", "repo", "--yes"])

    assert result.exit_code == 0, result.output
    assert "branches:" in result.output
    assert "home:repos/o/r.yml" in result.output
    assert "reviewer config show --repo o/r" in result.output
```

Add launcher schema assertion:

```python
def test_init_schema_exposes_scope_and_repo_options():
    init = next(item for item in build_catalog(cli) if item.path == ("init",))
    by_name = {parameter.name: parameter for parameter in init.params}

    assert by_name["scope"].choices == ("all", "global", "repo")
    assert by_name["repo_opt"].option_strings == ("--repo",)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_init_onboarding.py tests/launcher/test_catalog.py -q`

Expected: CLI output assertions fail; launcher metadata may need updated details.

- [ ] **Step 3: Extract and reuse branches report helper**

Add near `_render_config_report`:

```python
def _branches_report(branches) -> dict[str, object]:
    return {
        "branches": {
            "primary": branches.primary,
            "index": list(branches.index),
            "source": branches.source,
        }
    }
```

Use `_branches_report(branches)` in `config_show` instead of the inline payload. After repo apply,
resolve and render locally:

```python
effective = resolve_repo_branches(repo_plan.repo, settings=settings)
_render_config_report(_branches_report(effective))
click.echo(f"Полный отчёт: reviewer config show --repo {repo_plan.repo}")
```

This path must not call `_config_context`, VCS, or policy resolution.

- [ ] **Step 4: Update launcher text**

Change `reviewer/launcher/metadata.py`:

```python
("init",): CommandPresentation(
    summary="Настроить reviewer",
    details="Планирует и настраивает global .env и per-repo branch config с preview до записи.",
    effects=(Effect.WRITE,),
    scenarios=("Первичная настройка", "Добавление репозитория", "Смена credentials"),
    keywords=("config", "env", "repository", "wizard"),
),
```

- [ ] **Step 5: Run focused tests and lint**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_init_onboarding.py tests/entrypoints/test_cli_config_show_branches.py tests/launcher/test_catalog.py -q`

Run: `.venv/bin/ruff check reviewer/entrypoints/cli.py reviewer/launcher/metadata.py tests/entrypoints/test_cli_init_onboarding.py tests/launcher/test_catalog.py`

Expected: tests and Ruff pass.

- [ ] **Step 6: Commit**

```bash
git add reviewer/entrypoints/cli.py reviewer/launcher/metadata.py tests/entrypoints/test_cli_init_onboarding.py tests/launcher/test_catalog.py
```

---

### Task 7: Acceptance Verification and Scope Guard

**Files:**
- Modify only if failures expose scoped defects.

- [ ] **Step 1: Run complete focused onboarding/config suite**

Run:

```bash
.venv/bin/pytest \
  tests/test_gitutil.py \
  tests/config/test_onboarding.py \
  tests/config/test_branches.py \
  tests/config/test_branches_migrate.py \
  tests/install/test_install_wizard.py \
  tests/test_install_wizard.py \
  tests/entrypoints/test_cli_init_onboarding.py \
  tests/entrypoints/test_cli_config_show_branches.py \
  tests/launcher/test_catalog.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run neighboring CLI/config tests**

Run:

```bash
.venv/bin/pytest \
  tests/entrypoints/test_cli.py \
  tests/entrypoints/test_config_commands.py \
  tests/config/test_settings.py \
  tests/config/test_layers.py -q
```

Expected: all tests pass; runtime legacy env behavior remains intact.

- [ ] **Step 3: Run full unit suite**

Run: `.venv/bin/pytest -q`

Expected: all non-integration tests pass with no infrastructure/network access.

- [ ] **Step 4: Run static and diff checks**

Run: `.venv/bin/ruff check . && git diff --check`

Expected: both commands exit 0.

- [ ] **Step 5: Audit scope mechanically**

Run:

```bash
git diff --name-only dev...HEAD
git grep -n "DEFAULT_REPO\|REVIEW_BRANCHES\|WEB_ADMIN_\|TASK_BOARD_MCP" -- reviewer/install.py tests/install/test_install_wizard.py tests/test_install_wizard.py
```

Expected:

- changed production files are limited to gitutil, config onboarding/branches, install, CLI and launcher metadata;
- removed keys remain only in compatibility template/comments/tests, not `WIZARD_GROUPS` fields;
- no provider registry/setup, configure-review skill or README changes.

- [ ] **Step 6: Commit any verification-only fixes**

If Step 1-5 required code changes, stage only this plan's production/test files and commit:

```bash
git add reviewer/gitutil.py reviewer/config/onboarding.py reviewer/config/branches.py reviewer/install.py reviewer/entrypoints/cli.py reviewer/launcher/metadata.py tests/test_gitutil.py tests/config/test_onboarding.py tests/config/test_branches_migrate.py tests/install/test_install_wizard.py tests/test_install_wizard.py tests/entrypoints/test_cli_init_onboarding.py tests/launcher/test_catalog.py
git commit -m "fix(init): закрывает регрессии repo onboarding"
```

If no files changed, do not create an empty commit.

---

## Completion Criteria

- Fresh `reviewer init` does not ask or generate removed standard fields.
- `reviewer init --scope repo` configures a second repo without reading/writing global `.env`.
- Repo ID and primary provenance appear in preview before any write.
- `--yes` and `--dry-run` are prompt-free and network-free; dry-run is write-free.
- Existing advanced env and existing per-repo YAML keys/comments survive repeated runs.
- Effective branches and source are shown through the shared `config show` renderer contract.
- Parts C and D remain untouched.
