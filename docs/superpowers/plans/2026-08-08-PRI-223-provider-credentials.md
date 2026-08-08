# PRI-223 Provider Credentials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `reviewer init` request credentials only for the selected VCS/board provider, show structured least-privilege guidance, and make `reviewer check` support GitHub-only, GitLab-only, and dual-provider deployments.

**Architecture:** A small provider-access value object carries permissions/read/write/validation metadata. Existing board specs and a compact VCS setup catalog consume it; `reviewer init` filters interactive prompts while retaining the canonical full env render schema. `reviewer check` validates every configured VCS token independently.

**Tech Stack:** Python 3.11+, dataclasses, Click, httpx, pytest, Ruff.

---

## File Map

- Create `reviewer/config/provider_access.py`: immutable access metadata and deterministic renderer.
- Modify `reviewer/tasks/boards/registry.py`: require access metadata in every board setup spec.
- Modify all 11 `reviewer/tasks/boards/<provider>.py` specs: declare exact access contract.
- Modify `reviewer/tasks/boards/setup.py`: render structured access before credential prompts.
- Modify `reviewer/install.py`: VCS setup catalog, canonical VCS group, scoped prompt helpers.
- Modify `reviewer/entrypoints/cli.py`: selected-VCS setup and multi-provider VCS health check.
- Modify focused tests under `tests/config`, `tests/tasks/boards`, `tests/install`, and `tests/entrypoints`.

## Global Constraints

- Preserve every existing env key and runtime fallback; only interactive prompting changes.
- `--yes` and `--dry-run` perform no prompt, browser, provider validation, or network operation.
- Existing credentials for an unselected provider survive unchanged and are never printed.
- YouGile acquisition logic remains one implementation in `setup.py`; do not duplicate it.
- All setup/validation errors must remain secret-safe.
- Follow RED → GREEN for every production change.

---

### Task 1: Provider Access Metadata

**Files:**
- Create: `reviewer/config/provider_access.py`
- Create: `tests/config/test_provider_access.py`
- Modify: `reviewer/tasks/boards/registry.py:19-66,189-216`
- Modify: `tests/tasks/boards/provider_fakes.py:1-70`
- Modify: `tests/tasks/boards/test_registry.py:90-140,367-400`
- Modify: `tests/test_install_wizard.py:52-84`

- [ ] **Step 1: Write failing renderer tests**

```python
# tests/config/test_provider_access.py
import pytest

from reviewer.config.provider_access import ProviderAccessSpec, render_provider_access


def test_render_provider_access_has_stable_complete_order():
    access = ProviderAccessSpec(
        minimum_permissions="Issues: Read and write",
        read_operations=("читать задачи", "читать комментарии"),
        write_operations=("создавать задачи", "закрывать задачи"),
        validation="identity и доступ к проекту",
    )

    text = render_provider_access(
        label="Example",
        help_text="Создайте token.",
        help_url="https://example.test/token",
        access=access,
    )

    markers = (
        "Example",
        "Создайте token.",
        "Минимальные права: Issues: Read and write",
        "Чтение: читать задачи; читать комментарии",
        "Запись: создавать задачи; закрывать задачи",
        "Проверка: identity и доступ к проекту",
        "https://example.test/token",
    )
    positions = [text.index(marker) for marker in markers]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    "field",
    ("minimum_permissions", "read_operations", "write_operations", "validation"),
)
def test_provider_access_rejects_empty_contract(field):
    values = {
        "minimum_permissions": "read/write",
        "read_operations": ("read",),
        "write_operations": ("write",),
        "validation": "identity",
    }
    values[field] = () if field.endswith("operations") else ""

    with pytest.raises(ValueError, match=field):
        ProviderAccessSpec(**values)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest tests/config/test_provider_access.py -q`

Expected: collection error because `reviewer.config.provider_access` does not exist.

- [ ] **Step 3: Implement the immutable model and renderer**

```python
# reviewer/config/provider_access.py
"""Общие несекретные подсказки по доступу внешнего provider."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderAccessSpec:
    """Минимальные права и фактические операции reviewer."""

    minimum_permissions: str
    read_operations: tuple[str, ...]
    write_operations: tuple[str, ...]
    validation: str

    def __post_init__(self) -> None:
        for name in ("minimum_permissions", "validation"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        for name in ("read_operations", "write_operations"):
            values = getattr(self, name)
            if not values or any(not value.strip() for value in values):
                raise ValueError(f"{name} must not be empty")


def render_provider_access(
    *,
    label: str,
    help_text: str,
    help_url: str,
    access: ProviderAccessSpec,
) -> str:
    """Сформировать одинаковую provider setup-подсказку без credentials."""
    return "\n".join(
        (
            f"[{label}]",
            help_text,
            f"Минимальные права: {access.minimum_permissions}",
            f"Чтение: {'; '.join(access.read_operations)}",
            f"Запись: {'; '.join(access.write_operations)}",
            f"Проверка: {access.validation}",
            f"Официальная инструкция: {help_url}",
        )
    )
```

- [ ] **Step 4: Require access metadata in board specs**

Add to `ProviderSetupSpec`:

```python
from reviewer.config.provider_access import ProviderAccessSpec


@dataclass(frozen=True)
class ProviderSetupSpec:
    label: str
    help_url: str
    help_text: str
    access: ProviderAccessSpec
    acquisition: Callable[[SetupIO], dict[str, str]] | None = None
    help_url_builder: Callable[[Mapping[str, str]], str] | None = None
```

Extend `_validate_spec` after the existing setup metadata check:

```python
        if not isinstance(spec.setup.access, ProviderAccessSpec):
            raise ValueError("provider access metadata must be complete")
```

Update fake `ProviderSetupSpec` constructors with this exact fixture:

```python
ProviderAccessSpec(
    minimum_permissions="test read/write",
    read_operations=("read test data",),
    write_operations=("write test data",),
    validation="test identity",
)
```

- [ ] **Step 5: Add registry completeness assertion**

In `test_default_registry_builds_and_validates_every_registered_provider` add:

```python
            assert spec.setup.access.minimum_permissions
            assert spec.setup.access.read_operations
            assert spec.setup.access.write_operations
            assert spec.setup.access.validation
```

- [ ] **Step 6: Run focused tests**

Run: `.venv/bin/pytest tests/config/test_provider_access.py tests/tasks/boards/test_registry.py tests/test_install_wizard.py -q`

Expected: access tests pass; production registry tests fail until all 11 specs are updated in Task 2.

- [ ] **Step 7: Keep the RED contract uncommitted until Task 2 is green**

Do not commit a registry contract that production providers cannot yet satisfy. Continue directly
to Task 2 with all Task 1 changes in the worktree.

---

### Task 2: Access Contracts for Every Board Provider

**Files:**
- Modify: `reviewer/tasks/boards/yougile.py:90-123`
- Modify: `reviewer/tasks/boards/youtrack.py:91-128`
- Modify: `reviewer/tasks/boards/jira.py:61-89`
- Modify: `reviewer/tasks/boards/github.py:87-135`
- Modify: `reviewer/tasks/boards/trello.py:75-124`
- Modify: `reviewer/tasks/boards/linear.py:180-215`
- Modify: `reviewer/tasks/boards/clickup.py:66-113`
- Modify: `reviewer/tasks/boards/asana.py:86-128`
- Modify: `reviewer/tasks/boards/yandex_tracker.py:81-139`
- Modify: `reviewer/tasks/boards/kaiten.py:99-148`
- Modify: `reviewer/tasks/boards/weeek.py:96-143`
- Test: `tests/tasks/boards/test_registry.py`

- [ ] **Step 1: Add a failing exact provider matrix test**

```python
# tests/tasks/boards/test_registry.py
ACCESS_MARKERS = {
    "yougile": ("company", "tasks", "create", "validate_connection"),
    "youtrack": ("YouTrack service", "issues", "create", "validate_connection"),
    "jira": ("Jira Cloud", "issues", "transition", "validate_connection"),
    "github": ("Issues: Read and write", "issues", "close", "validate_connection"),
    "trello": ("account", "cards", "move", "validate_connection"),
    "linear": ("Read and Write", "issues", "create", "validate_connection"),
    "clickup": ("workspace", "tasks", "create", "validate_connection"),
    "asana": ("project", "tasks", "complete", "validate_connection"),
    "yandex_tracker": ("tracker:read", "issues", "transition", "validate_connection"),
    "kaiten": ("board", "cards", "move", "validate_connection"),
    "weeek": ("project", "tasks", "complete", "validate_connection"),
}


def test_every_board_provider_documents_access_contract():
    registry = default_board_registry()
    for board_type, markers in ACCESS_MARKERS.items():
        access = registry.get(board_type).setup.access
        rendered = " ".join(
            (
                access.minimum_permissions,
                *access.read_operations,
                *access.write_operations,
                access.validation,
            )
        )
        for marker in markers:
            assert marker.casefold() in rendered.casefold(), (board_type, marker)
```

- [ ] **Step 2: Run the matrix test and verify RED**

Run: `.venv/bin/pytest tests/tasks/boards/test_registry.py::test_every_board_provider_documents_access_contract -q`

Expected: FAIL because production setup specs do not yet pass `access`.

- [ ] **Step 3: Add exact access metadata to all specs**

Import `ProviderAccessSpec` in each provider module and use these values:

| Provider | minimum_permissions | read_operations | write_operations | validation |
|---|---|---|---|---|
| YouGile | `API-capable account with company/task read and write; admin role is not required` | `read companies, boards, columns, tasks, chats and attachments` | `create/update/complete tasks and native subtasks` | `validate_connection checks identity, project visibility and lifecycle capabilities` |
| YouTrack | `permanent token with YouTrack service scope and project issue read/write` | `read issues, fields, links and attachments` | `create issues, update status and append PR links` | `validate_connection checks current user and project visibility` |
| Jira | `Jira Cloud user with Browse Projects, Create Issues, Edit Issues and Transition Issues` | `read projects, issues, fields, transitions and attachments` | `create/edit/transition issues and append PR links` | `validate_connection checks identity, project and reported permissions` |
| GitHub Issues | `fine-grained PAT with Issues: Read and write for the selected repository` | `read repository issues, labels, milestones and comments` | `create/update/close issues and append PR links` | `validate_connection checks identity, repository access and write capability` |
| Trello | `API key and account token with access to the selected board` | `read boards, lists, cards, checklists and attachments` | `create/move/update/archive cards and append PR links` | `validate_connection checks member identity and board visibility` |
| Linear | `personal API key with Read and Write for the selected team` | `read teams, workflow states, issues, relations and attachments metadata` | `create/update issues, change state and append PR links` | `validate_connection checks viewer identity and team visibility` |
| ClickUp | `personal token for a member with access to the selected workspace/list` | `read teams, lists, statuses, tasks, relations and attachments` | `create/update/complete tasks and append PR links` | `validate_connection checks user identity and list visibility` |
| Asana | `personal access token for a member with project read/write access` | `read workspaces, projects, sections, tasks, dependencies and attachments` | `create/update/complete tasks and append PR links` | `validate_connection checks user identity and project visibility` |
| Yandex Tracker | `OAuth tracker:read + tracker:write or IAM role with equivalent queue access` | `read queues, issues, fields, links and attachments` | `create/update/transition issues and append PR links` | `validate_connection checks identity, organization and queue visibility` |
| Kaiten | `API key of a user with read/write access to the selected space and board` | `read spaces, boards, columns, cards, links and attachments` | `create/update/move cards and append PR links` | `validate_connection checks user identity and board visibility` |
| Weeek | `workspace token owner with read/write access to the selected project and board` | `read workspaces, projects, boards, columns, tasks and attachments` | `create/update/complete tasks and append PR links` | `validate_connection checks user identity and project visibility` |

Represent operations as one-element tuples containing the exact table strings.

- [ ] **Step 4: Run all board contract tests**

Run: `.venv/bin/pytest tests/tasks/boards -q`

Expected: PASS.

- [ ] **Step 5: Commit the green access model and all board metadata**

```bash
git add reviewer/config/provider_access.py reviewer/tasks/boards tests/config/test_provider_access.py tests/tasks/boards tests/test_install_wizard.py
git commit -m "feat(boards): описывает права и операции providers"
```

---

### Task 3: Structured Board Setup Output and YouGile Guidance

**Files:**
- Modify: `reviewer/tasks/boards/setup.py:262-292`
- Modify: `reviewer/tasks/boards/yougile.py:110-118`
- Test: `tests/install/test_board_setup.py`

- [ ] **Step 1: Add failing output-order and YouGile tests**

```python
# tests/install/test_board_setup.py
def test_setup_prints_access_contract_before_first_secret_prompt():
    provider = FakeProvider({"status": "ok", "warnings": []})
    spec = replace(jira_provider_spec(), factory=lambda _context: provider)
    io = FakeIO(
        answers={
            "Jira Cloud site URL": "https://acme.atlassian.net",
            "Atlassian account email": "bot@example.test",
            "Atlassian API token": "jira-secret",
        },
        confirms=[False],
    )

    configure_board_provider(spec, io)

    rendered = "\n".join(io.messages)
    assert "Минимальные права:" in rendered
    assert "Чтение:" in rendered
    assert "Запись:" in rendered
    assert "Проверка:" in rendered
    assert io.events.index("echo") < next(
        index
        for index, event in enumerate(io.events)
        if event == "prompt:Atlassian API token"
    )


def test_yougile_access_does_not_require_admin_role():
    from reviewer.config.provider_access import render_provider_access
    from reviewer.tasks.boards.yougile import provider_spec as yougile_provider_spec

    spec = yougile_provider_spec()
    text = render_provider_access(
        label=spec.setup.label,
        help_text=spec.setup.help_text,
        help_url=spec.setup.help_url,
        access=spec.setup.access,
    )
    assert "admin role is not required" in text
    assert "allowOnlyOpenId" in text
    assert "API-capable" in text
```

Update the existing `FakeIO.echo` method without changing message storage:

```python
    def echo(self, text: str) -> None:
        self.events.append("echo")
        self.messages.append(text)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest tests/install/test_board_setup.py -q -k "access_contract or admin_role"`

Expected: FAIL because setup still echoes only `help_text + help_url`.

- [ ] **Step 3: Render structured access**

Replace line 267 in `configure_board_provider` with:

```python
    _echo(
        io,
        render_provider_access(
            label=spec.setup.label,
            help_text=spec.setup.help_text,
            help_url=spec.setup.help_url,
            access=spec.setup.access,
        ),
    )
```

Keep acquisition, manual fallback, validation and close paths unchanged.

- [ ] **Step 4: Run setup tests**

Run: `.venv/bin/pytest tests/install/test_board_setup.py tests/install/test_install_wizard.py -q`

Expected: PASS.

- [ ] **Step 5: Commit setup output**

```bash
git add reviewer/tasks/boards/setup.py reviewer/tasks/boards/yougile.py tests/install/test_board_setup.py
git commit -m "feat(init): показывает права выбранной доски"
```

---

### Task 4: VCS Setup Catalog and Canonical Env Schema

**Files:**
- Modify: `reviewer/install.py:115-242,260-276`
- Modify: `tests/test_install_wizard.py`
- Modify: `tests/install/test_install_wizard.py`

- [ ] **Step 1: Write failing VCS catalog tests**

```python
# tests/test_install_wizard.py
from reviewer.install import VCS_SETUPS, vcs_env_group


def test_vcs_catalog_declares_github_and_gitlab_access():
    assert tuple(VCS_SETUPS) == ("github", "gitlab")
    assert [field.key for field in VCS_SETUPS["github"].credential_fields] == [
        "GITHUB_TOKEN"
    ]
    assert [field.key for field in VCS_SETUPS["gitlab"].credential_fields] == [
        "GITLAB_URL",
        "GITLAB_TOKEN",
    ]
    assert "Pull requests: Read and write" in VCS_SETUPS["github"].access.minimum_permissions
    assert "Contents: Read" in VCS_SETUPS["github"].access.minimum_permissions
    assert VCS_SETUPS["gitlab"].access.minimum_permissions == "PAT/project token with api scope"


def test_vcs_group_is_canonical_union_with_provider_fallback():
    keys = [field.key for field in vcs_env_group().fields]
    assert keys == ["VCS_PROVIDER", "GITHUB_TOKEN", "GITLAB_URL", "GITLAB_TOKEN"]
    assert len(keys) == len(set(keys))
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest tests/test_install_wizard.py -q -k "vcs_catalog or vcs_group"`

Expected: import error for `VCS_SETUPS` / `vcs_env_group`.

- [ ] **Step 3: Implement VCS specs**

Add to `reviewer/install.py` after `EnvGroup`:

```python
@dataclass(frozen=True)
class VcsSetupSpec:
    provider: str
    label: str
    credential_fields: tuple[EnvField, ...]
    help_url: str
    help_text: str
    access: ProviderAccessSpec


VCS_SETUPS = {
    "github": VcsSetupSpec(
        provider="github",
        label="GitHub",
        credential_fields=(
            EnvField("GITHUB_TOKEN", "GitHub personal access token", secret=True),
        ),
        help_url="https://github.com/settings/personal-access-tokens/new",
        help_text="Создайте fine-grained PAT для репозитория reviewer.",
        access=ProviderAccessSpec(
            minimum_permissions="Pull requests: Read and write; Contents: Read",
            read_operations=("PR metadata, files, comments, contents and compare",),
            write_operations=("review comments/summary and PR body backlink",),
            validation="reviewer check authenticates /user; repository rights are exercised by review",
        ),
    ),
    "gitlab": VcsSetupSpec(
        provider="gitlab",
        label="GitLab",
        credential_fields=(
            EnvField("GITLAB_URL", "GitLab base URL", default="https://gitlab.com"),
            EnvField("GITLAB_TOKEN", "GitLab personal/project access token", secret=True),
        ),
        help_url="https://docs.gitlab.com/user/profile/personal_access_tokens/",
        help_text="Создайте PAT или project access token для GitLab API v4.",
        access=ProviderAccessSpec(
            minimum_permissions="PAT/project token with api scope",
            read_operations=("MR metadata, changes, notes, repository files and compare",),
            write_operations=("MR discussions/notes and description backlink",),
            validation="reviewer check authenticates /api/v4/user; project rights are exercised by review",
        ),
    ),
}


def vcs_env_group() -> EnvGroup:
    fields = [EnvField("VCS_PROVIDER", "VCS fallback provider", default="github")]
    for spec in VCS_SETUPS.values():
        fields.extend(spec.credential_fields)
    return EnvGroup(title="VCS", fields=fields, optional=True)
```

Make `WIZARD_GROUPS` use a Voyage-only required group plus `vcs_env_group()`; remove the old separate GitLab group and GitHub field from `Обязательные`. Update `_GROUP_HEADERS["VCS"]` with the existing auto-detect/fallback explanation.

- [ ] **Step 4: Preserve render/redaction contracts**

Update old tests to assert:

```python
assert "GITHUB_TOKEN=" in rendered
assert "GITLAB_URL=https://gitlab.com" in rendered
assert "GITLAB_TOKEN=" in rendered
assert "VCS_PROVIDER=github" in rendered
```

Do not remove any key from `ENV_TEMPLATE`, `.env.example`, Settings, or `extra` handling.

- [ ] **Step 5: Run install tests**

Run: `.venv/bin/pytest tests/test_install_wizard.py tests/install/test_install_wizard.py -q`

Expected: PASS.

- [ ] **Step 6: Commit catalog/schema**

```bash
git add reviewer/install.py tests/test_install_wizard.py tests/install/test_install_wizard.py
git commit -m "refactor(init): описывает VCS setup catalog"
```

---

### Task 5: Provider-Scoped VCS Prompting in `reviewer init`

**Files:**
- Modify: `reviewer/install.py` (small prompt helpers)
- Modify: `reviewer/entrypoints/cli.py:1149-1231`
- Modify: `tests/install/test_install_wizard.py:303-374`
- Modify: `tests/entrypoints/test_cli_init_onboarding.py:46-60,168-210`

- [ ] **Step 1: Write failing scoped prompt tests**

```python
# tests/install/test_install_wizard.py
@pytest.mark.parametrize(
    ("selected", "prompted", "not_prompted"),
    [
        ("github", {"GITHUB_TOKEN"}, {"GITLAB_URL", "GITLAB_TOKEN"}),
        ("gitlab", {"GITLAB_URL", "GITLAB_TOKEN"}, {"GITHUB_TOKEN"}),
    ],
)
def test_init_prompts_only_selected_vcs_provider(
    selected, prompted, not_prompted, tmp_path, monkeypatch
):
    dest = tmp_path / ".env"
    seen = []
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli._select_vcs_provider",
        lambda *_args, **_kwargs: selected,
    )
    monkeypatch.setattr(
        "reviewer.entrypoints.cli._prompt_vcs_provider",
        lambda _inst, spec, current: seen.extend(field.key for field in spec.credential_fields)
        or {field.key: current.get(field.key, "") or field.default for field in spec.credential_fields},
    )
    monkeypatch.setattr("click.confirm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("reviewer.entrypoints.cli._shutil.which", lambda _name: None)

    result = CliRunner().invoke(cli, ["init", "--scope", "global"])

    assert result.exit_code == 0, result.output
    assert set(seen) == prompted
    assert set(seen).isdisjoint(not_prompted)


def test_unselected_existing_vcs_credentials_survive(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    dest.write_text("GITLAB_TOKEN=keep-me\n", encoding="utf-8")
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    monkeypatch.setattr("reviewer.entrypoints.cli._select_vcs_provider", lambda *_a, **_k: "github")
    monkeypatch.setattr(
        "reviewer.entrypoints.cli._prompt_vcs_provider",
        lambda *_a, **_k: {"GITHUB_TOKEN": "new-github"},
    )
    monkeypatch.setattr("click.confirm", lambda *_a, **_k: True)
    monkeypatch.setattr("reviewer.entrypoints.cli._shutil.which", lambda _name: None)

    result = CliRunner().invoke(cli, ["init", "--scope", "global"])

    assert result.exit_code == 0, result.output
    assert "GITLAB_TOKEN=keep-me" in dest.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py -q -k "selected_vcs or unselected_existing_vcs"`

Expected: FAIL because selection/prompt helpers do not exist.

- [ ] **Step 3: Add small CLI seams**

Add near init helpers in `cli.py`:

```python
def _select_vcs_provider(inst, current: Mapping[str, str]) -> str:
    detected = derive_vcs_from_remote(remote_url(repo_root(".") or ".") or "")
    default = detected[0] if detected is not None else current.get("VCS_PROVIDER", "github")
    choices = tuple(inst.VCS_SETUPS)
    return click.prompt("Выберите VCS provider", type=click.Choice(choices), default=default)


def _prompt_vcs_provider(inst, spec, current: Mapping[str, str]) -> dict[str, str]:
    click.echo(render_provider_access(
        label=spec.label,
        help_text=spec.help_text,
        help_url=spec.help_url,
        access=spec.access,
    ))
    if click.confirm(f"Открыть официальную инструкцию {spec.help_url}?", default=False):
        click.launch(spec.help_url)
    group = inst.EnvGroup(title=spec.label, fields=list(spec.credential_fields))
    return inst.prompt_groups([group], current=dict(current), yes=False)
```

Import `Mapping`, `derive_vcs_from_remote`, and `render_provider_access` at module scope. Pass the
existing lazy `reviewer.install` module as `inst` from `init`; do not add a second eager installer
import.

- [ ] **Step 4: Filter dynamic provider groups in init**

In the interactive global stage:

1. Locate `vcs_group` and `board_group`.
2. Pass ordinary groups plus only the two common board fields to `prompt_groups`.
3. Initialize all omitted VCS/board fields from `current` or default.
4. On `Подключить VCS provider?` (default true), select one VCS, set `VCS_PROVIDER`, and update only selected fields.
5. Keep existing board confirm/selection/retry unchanged.

The core initialization must be:

```python
for group in (vcs_group, board_group):
    for field in group.fields:
        values.setdefault(field.key, current.get(field.key, "") or field.default)
```

- [ ] **Step 5: Extend noninteractive side-effect guards**

Add `_select_vcs_provider` and `_prompt_vcs_provider` to `_forbid_noninteractive_side_effects` in `tests/entrypoints/test_cli_init_onboarding.py`; both `--yes` and `--dry-run` must still pass.

- [ ] **Step 6: Run init tests**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py tests/entrypoints/test_cli_init_onboarding.py -q`

Expected: PASS.

- [ ] **Step 7: Commit scoped prompts**

```bash
git add reviewer/install.py reviewer/entrypoints/cli.py tests/install/test_install_wizard.py tests/entrypoints/test_cli_init_onboarding.py
git commit -m "feat(init): спрашивает credentials выбранного VCS"
```

---

### Task 6: GitHub/GitLab-Aware `reviewer check`

**Files:**
- Modify: `reviewer/entrypoints/cli.py:609-712`
- Create: `tests/entrypoints/test_check_vcs.py`

- [ ] **Step 1: Write failing VCS check tests**

```python
# tests/entrypoints/test_check_vcs.py
import httpx

from reviewer.config.settings import Settings
from reviewer.entrypoints.cli import _check_vcs_providers


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_check_accepts_gitlab_only_and_uses_configured_base(monkeypatch, capsys):
    seen = []

    def get(url, **kwargs):
        seen.append((url, kwargs["headers"]))
        return _Response(200, {"username": "bot"})

    monkeypatch.setattr(httpx, "get", get)
    failed = _check_vcs_providers(Settings(
        _env_file=None,
        github_token="",
        gitlab_token="gl-secret",
        gitlab_url="https://gitlab.example",
    ))
    output = capsys.readouterr().out

    assert failed is False
    assert seen == [("https://gitlab.example/api/v4/user", {"PRIVATE-TOKEN": "gl-secret"})]
    assert "GitLab API: аутентификация OK" in output
    assert "gl-secret" not in output


def test_check_requires_at_least_one_vcs_token(capsys):
    failed = _check_vcs_providers(Settings(
        _env_file=None,
        github_token="",
        gitlab_token="",
    ))
    output = capsys.readouterr().out

    assert failed is True
    assert "не настроен ни один VCS token" in output


def test_check_validates_both_configured_tokens(monkeypatch, capsys):
    monkeypatch.setattr(httpx, "get", lambda url, **_kwargs: _Response(200, {"login": "gh", "username": "gl"}))

    failed = _check_vcs_providers(Settings(
        _env_file=None,
        github_token="gh-secret",
        gitlab_token="gl-secret",
    ))
    output = capsys.readouterr().out

    assert failed is False
    assert "GitHub API: аутентификация OK" in output
    assert "GitLab API: аутентификация OK" in output
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest tests/entrypoints/test_check_vcs.py -q`

Expected: FAIL because GitHub is still mandatory and GitLab is not checked.

- [ ] **Step 3: Implement `_check_vcs_providers`**

```python
def _check_vcs_providers(settings: Settings) -> bool:
    configured = []
    if settings.github_token:
        configured.append((
            "GitHub",
            "https://api.github.com/user",
            {"Authorization": f"Bearer {settings.github_token}"},
            "login",
        ))
    if settings.gitlab_token:
        configured.append((
            "GitLab",
            f"{settings.gitlab_url.rstrip('/')}/api/v4/user",
            {"PRIVATE-TOKEN": settings.gitlab_token},
            "username",
        ))
    if not configured:
        click.echo("✗ VCS: не настроен ни один VCS token; запустите reviewer init --scope global")
        return True

    failed = False
    for label, url, headers, identity_key in configured:
        try:
            response = httpx.get(url, headers=headers, timeout=10)
            if response.status_code != 200:
                click.echo(f"✗ {label} API: HTTP {response.status_code} — проверьте token/base URL")
                failed = True
                continue
            identity = response.json().get(identity_key, "?")
            click.echo(f"✓ {label} API: аутентификация OK (identity: {identity})")
        except Exception as exc:  # noqa: BLE001 — health check reports safe type only
            click.echo(f"✗ {label} API: {type(exc).__name__}")
            failed = True
    return failed
```

Call it after infrastructure checks and before boards. Remove the old mandatory `GITHUB_TOKEN` key loop entry and old GitHub-only block.

- [ ] **Step 4: Add failure/redaction coverage**

Add parameterized HTTP 401 and exception tests asserting neither configured secret nor credential-bearing URL appears in output.

- [ ] **Step 5: Run check/CLI tests**

Run: `.venv/bin/pytest tests/entrypoints/test_check_vcs.py tests/entrypoints/test_cli.py tests/entrypoints/test_check_boards.py -q`

Expected: PASS.

- [ ] **Step 6: Commit VCS check**

```bash
git add reviewer/entrypoints/cli.py tests/entrypoints/test_check_vcs.py
git commit -m "fix(check): поддерживает GitLab-only окружение"
```

---

### Task 7: Part C Verification

**Files:**
- Modify only if verification exposes scoped defects.

- [ ] **Step 1: Run focused Part C suite**

Run:

```bash
.venv/bin/pytest \
  tests/config/test_provider_access.py \
  tests/tasks/boards \
  tests/install/test_board_setup.py \
  tests/install/test_install_wizard.py \
  tests/test_install_wizard.py \
  tests/entrypoints/test_cli_init_onboarding.py \
  tests/entrypoints/test_check_vcs.py \
  tests/entrypoints/test_check_boards.py -q
```

Expected: all pass.

- [ ] **Step 2: Run changed-file lint**

Run:

```bash
.venv/bin/ruff check reviewer/config/provider_access.py reviewer/install.py reviewer/entrypoints/cli.py reviewer/tasks/boards tests/config/test_provider_access.py tests/install tests/entrypoints/test_check_vcs.py tests/tasks/boards
```

Expected: `All checks passed!`

- [ ] **Step 3: Audit provider scoping mechanically**

Run: `git diff dev...HEAD -- reviewer/install.py reviewer/entrypoints/cli.py reviewer/tasks/boards/setup.py`

Expected: interactive paths prompt only selected VCS/board specs; noninteractive path still uses canonical full schema; no credential values appear in messages.

- [ ] **Step 4: Commit verification fixes if needed**

Stage only files from this plan and commit:

```bash
git commit -m "fix(init): закрывает регрессии provider credentials"
```

Do not create an empty commit.
