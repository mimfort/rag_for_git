from pathlib import Path

import pytest
from click.testing import CliRunner
from reviewer import install as inst
from reviewer.entrypoints.cli import cli
from reviewer.tasks.boards.registry import (
    BoardProviderRegistry,
    BoardProviderSpec,
    ProviderSetupSpec,
)
from tests.provider_access import FAKE_PROVIDER_ACCESS


REMOVED_STANDARD_KEYS = {
    "DEFAULT_REPO",
    "REVIEW_BRANCHES",
    "WEB_ADMIN_USER",
    "WEB_ADMIN_PASSWORD",
    "TASK_BOARD_MCP",
}


def _keys_from_text(text: str) -> set[str]:
    """Имена KEY из текста .env-вида: пропускаем комментарии и пустые строки."""
    keys = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def test_read_env_parses_key_value(tmp_path):
    f = tmp_path / ".env"
    f.write_text("VOYAGE_API_KEY=sk-abc\nGITHUB_TOKEN=ghp-xyz\n", encoding="utf-8")
    result = inst.read_env(f)
    assert result == {"VOYAGE_API_KEY": "sk-abc", "GITHUB_TOKEN": "ghp-xyz"}


def test_read_env_skips_comments_and_empty(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# комментарий\n\nFOO=bar\n", encoding="utf-8")
    result = inst.read_env(f)
    assert result == {"FOO": "bar"}
    assert len(result) == 1


def test_read_env_missing_file(tmp_path):
    result = inst.read_env(tmp_path / "nonexistent.env")
    assert result == {}


def test_read_env_value_with_equals(tmp_path):
    f = tmp_path / ".env"
    f.write_text("PG_DSN=postgresql://u:p@localhost:5433/db\n", encoding="utf-8")
    result = inst.read_env(f)
    assert result["PG_DSN"] == "postgresql://u:p@localhost:5433/db"


def test_render_env_contains_wizard_keys():
    values = {
        "VOYAGE_API_KEY": "sk-test",
        "GITHUB_TOKEN": "",
        "PG_DSN": "postgresql://reviewer:reviewer@localhost:5433/reviewer",
        "NEO4J_URI": "neo4j://localhost:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "reviewerpass",
        "TASK_BOARD_KEY_PATTERN": "",
        "TASK_BOARD_URL_TEMPLATE": "",
    }
    result = inst.render_env(values, extra={})
    assert "VOYAGE_API_KEY=sk-test" in result
    assert "PG_DSN=postgresql://reviewer:reviewer@localhost:5433/reviewer" in result


def test_render_env_extra_keys_preserved():
    values = {
        "VOYAGE_API_KEY": "sk-test",
        "GITHUB_TOKEN": "",
        "PG_DSN": "postgresql://reviewer:reviewer@localhost:5433/reviewer",
        "NEO4J_URI": "neo4j://localhost:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "reviewerpass",
        "TASK_BOARD_KEY_PATTERN": "",
        "TASK_BOARD_URL_TEMPLATE": "",
    }
    extra = {"REVIEW_MAX_COMMENTS": "30", "REVIEW_HISTORY": "true"}
    result = inst.render_env(values, extra=extra)
    assert "REVIEW_MAX_COMMENTS=30" in result
    assert "REVIEW_HISTORY=true" in result
    assert "Прочие настройки" in result


def test_render_env_no_extra_no_extra_block():
    values = {f.key: f.default for g in inst.WIZARD_GROUPS for f in g.fields}
    result = inst.render_env(values, extra={})
    assert "Прочие настройки" not in result


def test_prompt_groups_yes_uses_current_values():
    current = {"VOYAGE_API_KEY": "sk-existing", "GITHUB_TOKEN": "ghp-existing"}
    result = inst.prompt_groups(inst.WIZARD_GROUPS, current=current, yes=True)
    assert result["VOYAGE_API_KEY"] == "sk-existing"
    assert result["GITHUB_TOKEN"] == "ghp-existing"


def test_prompt_groups_yes_uses_field_default_when_no_current():
    result = inst.prompt_groups(inst.WIZARD_GROUPS, current={}, yes=True)
    assert result["PG_DSN"] == "postgresql://reviewer:reviewer@localhost:5433/reviewer"
    assert result["VOYAGE_API_KEY"] == ""
    assert result.keys().isdisjoint(REMOVED_STANDARD_KEYS)


def test_prompt_groups_yes_skips_optional_groups():
    # При yes=True опциональные группы сохраняют current или default — не вызывают confirm
    current = {"TASK_BOARD_KEY_PATTERN": "PRI-\\d+"}
    result = inst.prompt_groups(inst.WIZARD_GROUPS, current=current, yes=True)
    assert result["TASK_BOARD_KEY_PATTERN"] == "PRI-\\d+"
    assert result["TASK_BOARD_URL_TEMPLATE"] == ""


def test_fresh_wizard_omits_repo_web_and_legacy_board_mcp():
    keys = {field.key for group in inst.WIZARD_GROUPS for field in group.fields}

    assert keys.isdisjoint(REMOVED_STANDARD_KEYS)


def test_runtime_template_keeps_compatibility_keys():
    assert REMOVED_STANDARD_KEYS <= _keys_from_text(inst.ENV_TEMPLATE)


def test_render_env_preserves_removed_existing_keys_as_extra():
    values = {
        field.key: field.default
        for group in inst.WIZARD_GROUPS
        for field in group.fields
    }
    extra = {key: f"existing-{key.lower()}" for key in REMOVED_STANDARD_KEYS}

    rendered = inst.render_env(values, extra)

    for key, value in extra.items():
        assert f"{key}={value}" in rendered


def test_render_env_includes_board_api_key_and_hint():
    # init теперь пишет per-type ключи досок + подсказки
    values = {f.key: f.default for g in inst.WIZARD_GROUPS for f in g.fields}
    result = inst.render_env(values, extra={})
    assert "YOUGILE_API_KEY=" in result
    assert "YOUTRACK_TOKEN=" in result
    assert "YOUTRACK_BASE_URL=" in result
    assert "JIRA_BASE_URL=" in result
    assert "JIRA_EMAIL=" in result
    assert "JIRA_API_TOKEN=" in result
    assert "TASK_BOARD_API_KEY=" not in result
    assert "TASK_BOARD_API_BASE=" not in result
    assert "permanent token" in result      # подсказка YouTrack в prompt_text поля


def test_init_yes_creates_env_file(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--scope", "global", "--yes"])
    assert result.exit_code == 0, result.output
    assert dest.exists()
    content = dest.read_text(encoding="utf-8")
    assert "VOYAGE_API_KEY=" in content
    assert "PG_DSN=" in content
    assert "YOUGILE_API_KEY=" in content
    assert "YOUTRACK_TOKEN=" in content
    assert "JIRA_API_TOKEN=" in content
    assert all(f"{key}=" not in content for key in REMOVED_STANDARD_KEYS)


def test_init_dry_run_is_safe_preview_only(tmp_path, monkeypatch):
    dest = tmp_path / "missing" / ".env"
    secret = "must-not-appear"
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    monkeypatch.setattr(
        "reviewer.install.read_env",
        lambda _path: {"JIRA_API_TOKEN": secret},
    )
    monkeypatch.setattr(
        "click.prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("prompt called")),
    )
    result = CliRunner().invoke(cli, ["init", "--scope", "global", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "JIRA_API_TOKEN=" in result.output
    assert secret not in result.output
    assert not dest.exists()
    assert not dest.parent.exists()


def test_init_dry_run_redacts_legacy_and_unknown_extra_secrets(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    dest.write_text(
        "TASK_BOARD_API_KEY=legacy-secret\n"
        "CUSTOM_TOKEN=custom-secret\n"
        "AWS_SECRET_ACCESS_KEY=aws-secret\n"
        "PG_DSN=postgresql://reviewer:pg-secret@db.example/reviewer\n"
        "DATABASE_URL=postgresql://reviewer:db-secret@db.example/reviewer\n"
        "BROKEN_URL=https://user:ipv6-secret@[::1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)

    result = CliRunner().invoke(cli, ["init", "--scope", "global", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "TASK_BOARD_API_KEY=" in result.output
    assert "CUSTOM_TOKEN=" in result.output
    assert "legacy-secret" not in result.output
    assert "custom-secret" not in result.output
    assert "aws-secret" not in result.output
    assert "pg-secret" not in result.output
    assert "db-secret" not in result.output
    assert "ipv6-secret" not in result.output
    assert "TASK_BOARD_API_KEY=legacy-secret" in dest.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["--dry-run", "--yes"])
def test_init_noninteractive_modes_never_touch_provider_setup_stages(
    mode,
    tmp_path,
    monkeypatch,
):
    dest = tmp_path / ".env"
    calls: list[str] = []

    class SentinelClient:
        def close(self) -> None:
            calls.append("http-close")

    class SentinelProvider:
        def validate_connection(self, _project=None):
            calls.append("validation")
            return {"status": "ok"}

        def close(self) -> None:
            calls.append("provider-close")

    def acquire(io):
        calls.append("acquisition")
        io.open_url("https://sentinel.example/acquire")
        from reviewer.tasks.boards import setup

        setup.httpx.Client().close()
        return {}

    def create_provider(_context):
        calls.append("factory")
        return SentinelProvider()

    registry = BoardProviderRegistry(
        [
            BoardProviderSpec(
                board_type="sentinel",
                factory=create_provider,
                credential_fields=(),
                setup=ProviderSetupSpec(
                    label="Sentinel",
                    help_url="https://sentinel.example/setup",
                    help_text="Sentinel setup.",
                    access=FAKE_PROVIDER_ACCESS,
                    acquisition=acquire,
                ),
            )
        ]
    )
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    monkeypatch.setattr(
        "reviewer.tasks.boards.registry.default_board_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "click.confirm",
        lambda *_args, **_kwargs: calls.append("confirm") or True,
    )
    monkeypatch.setattr(
        "click.prompt",
        lambda *_args, **_kwargs: calls.append("prompt") or "",
    )
    monkeypatch.setattr(
        "click.launch",
        lambda *_args, **_kwargs: calls.append("browser"),
    )
    monkeypatch.setattr(
        "reviewer.tasks.boards.setup.httpx.Client",
        lambda *_args, **_kwargs: calls.append("http-construction") or SentinelClient(),
    )

    result = CliRunner().invoke(cli, ["init", "--scope", "global", mode])

    assert result.exit_code == 0, result.output
    assert calls == []


def test_init_interactive_configures_selected_registry_provider(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    configured: list[str] = []
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    monkeypatch.setattr(
        "reviewer.install.prompt_groups",
        lambda groups, current, yes: {
            field.key: current.get(field.key, "") or field.default
            for group in groups
            for field in group.fields
        },
    )
    monkeypatch.setattr(
        "reviewer.tasks.boards.setup.ClickSetupIO.choose",
        lambda _io, _text, choices: next(
            choice.value for choice in choices if choice.value == "jira"
        ),
    )
    monkeypatch.setattr(
        "reviewer.tasks.boards.setup.configure_board_provider",
        lambda spec, _io: configured.append(spec.board_type)
        or {
            "JIRA_BASE_URL": "https://acme.atlassian.net",
            "JIRA_EMAIL": "bot@example.test",
            "JIRA_API_TOKEN": "jira-secret",
        },
    )
    answers = iter([False, True, True, False])
    monkeypatch.setattr("click.confirm", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr("reviewer.entrypoints.cli._shutil.which", lambda _name: None)

    result = CliRunner().invoke(cli, ["init", "--scope", "global"])

    assert result.exit_code == 0, result.output
    assert configured == ["jira"]
    content = dest.read_text(encoding="utf-8")
    assert "JIRA_BASE_URL=https://acme.atlassian.net" in content
    assert "JIRA_EMAIL=bot@example.test" in content
    assert "JIRA_API_TOKEN=jira-secret" in content


@pytest.mark.parametrize(
    ("selected", "prompted", "not_prompted"),
    [
        ("github", {"GITHUB_TOKEN"}, {"GITLAB_URL", "GITLAB_TOKEN"}),
        ("gitlab", {"GITLAB_URL", "GITLAB_TOKEN"}, {"GITHUB_TOKEN"}),
    ],
)
def test_init_prompts_only_selected_vcs_provider(
    selected,
    prompted,
    not_prompted,
    tmp_path,
    monkeypatch,
):
    dest = tmp_path / ".env"
    seen = []
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    monkeypatch.setattr(
        "reviewer.install.prompt_groups",
        lambda groups, current, yes: {
            field.key: current.get(field.key, "") or field.default
            for group in groups
            for field in group.fields
        },
    )
    monkeypatch.setattr(
        "reviewer.entrypoints.cli._select_vcs_provider",
        lambda *_args, **_kwargs: selected,
        raising=False,
    )

    def prompt_vcs(_inst, spec, current):
        seen.extend(field.key for field in spec.credential_fields)
        return {
            field.key: current.get(field.key, "") or field.default
            for field in spec.credential_fields
        }

    monkeypatch.setattr(
        "reviewer.entrypoints.cli._prompt_vcs_provider",
        prompt_vcs,
        raising=False,
    )
    monkeypatch.setattr(
        "click.confirm",
        lambda text, **_kwargs: (
            "VCS provider" in text or "Записать показанные изменения" in text
        ),
    )
    monkeypatch.setattr("reviewer.entrypoints.cli._shutil.which", lambda _name: None)

    result = CliRunner().invoke(cli, ["init", "--scope", "global"])

    assert result.exit_code == 0, result.output
    assert set(seen) == prompted
    assert set(seen).isdisjoint(not_prompted)


def test_unselected_existing_vcs_credentials_survive(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    dest.write_text("GITLAB_TOKEN=keep-me\n", encoding="utf-8")
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    monkeypatch.setattr(
        "reviewer.install.prompt_groups",
        lambda groups, current, yes: {
            field.key: current.get(field.key, "") or field.default
            for group in groups
            for field in group.fields
        },
    )
    monkeypatch.setattr(
        "reviewer.entrypoints.cli._select_vcs_provider",
        lambda *_args, **_kwargs: "github",
        raising=False,
    )
    monkeypatch.setattr(
        "reviewer.entrypoints.cli._prompt_vcs_provider",
        lambda *_args, **_kwargs: {"GITHUB_TOKEN": "new-github"},
        raising=False,
    )
    monkeypatch.setattr(
        "click.confirm",
        lambda text, **_kwargs: (
            "VCS provider" in text or "Записать показанные изменения" in text
        ),
    )
    monkeypatch.setattr("reviewer.entrypoints.cli._shutil.which", lambda _name: None)

    result = CliRunner().invoke(cli, ["init", "--scope", "global"])

    assert result.exit_code == 0, result.output
    content = dest.read_text(encoding="utf-8")
    assert "GITHUB_TOKEN=new-github" in content
    assert "GITLAB_TOKEN=keep-me" in content


def test_init_interactive_common_board_fields_do_not_require_rest_provider(
    tmp_path,
    monkeypatch,
):
    dest = tmp_path / ".env"
    seen_board_group = []
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)

    def prompt_groups(groups, current, yes):
        values = {}
        for group in groups:
            if group.title == "Доска задач":
                seen_board_group.extend(field.key for field in group.fields)
            for field in group.fields:
                values[field.key] = field.default
        return values

    monkeypatch.setattr("reviewer.install.prompt_groups", prompt_groups)
    answers = iter([False, False, True, False])
    monkeypatch.setattr("click.confirm", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr("reviewer.entrypoints.cli._shutil.which", lambda _name: None)

    result = CliRunner().invoke(cli, ["init", "--scope", "global"])

    assert result.exit_code == 0, result.output
    assert seen_board_group == [
        "TASK_BOARD_KEY_PATTERN",
        "TASK_BOARD_URL_TEMPLATE",
    ]
    assert "TASK_BOARD_MCP=" not in dest.read_text(encoding="utf-8")


def test_init_yes_preserves_existing_secret(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    dest.write_text("VOYAGE_API_KEY=sk-existing\n", encoding="utf-8")
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--scope", "global", "--yes"])
    assert result.exit_code == 0, result.output
    content = dest.read_text(encoding="utf-8")
    assert "VOYAGE_API_KEY=sk-existing" in content


def test_render_env_includes_gitlab_fields():
    values = {f.key: f.default for g in inst.WIZARD_GROUPS for f in g.fields}
    values["GITLAB_TOKEN"] = "glpat-secret"
    result = inst.render_env(values, extra={})
    # отличительный текст многострочного заголовка GitLab VCS (нет в дефолтном):
    assert "автоопределяется из git remote" in result
    assert "GITLAB_TOKEN=glpat-secret" in result
    assert "GITLAB_URL=https://gitlab.com" in result
    assert "VCS_PROVIDER=github" in result
    assert "YOUGILE_API_BASE=" in result


def test_init_yes_preserves_extra_keys(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    dest.write_text(
        "VOYAGE_API_KEY=sk-x\n"
        "REVIEW_MAX_COMMENTS=42\n"
        "DEFAULT_REPO=owner/legacy\n"
        "WEB_ADMIN_USER=legacy-admin\n"
        "TASK_BOARD_MCP=legacy-board\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--scope", "global", "--yes"])
    assert result.exit_code == 0, result.output
    content = dest.read_text(encoding="utf-8")
    assert "REVIEW_MAX_COMMENTS=42" in content
    assert "DEFAULT_REPO=owner/legacy" in content
    assert "WEB_ADMIN_USER=legacy-admin" in content
    assert "TASK_BOARD_MCP=legacy-board" in content


def test_env_template_mirrors_env_example():
    repo_root = Path(__file__).resolve().parents[2]
    example_keys = _keys_from_text((repo_root / ".env.example").read_text(encoding="utf-8"))
    template_keys = _keys_from_text(inst.ENV_TEMPLATE)
    test_only = {key for key in example_keys if key.startswith("TEST_")}

    assert template_keys.isdisjoint(test_only)
    assert template_keys == example_keys - test_only, (
        f"ENV_TEMPLATE и .env.example разошлись:\n"
        f"  test-only в .env.example: {sorted(test_only)}\n"
        f"  прочие только в .env.example: "
        f"{sorted(example_keys - test_only - template_keys)}\n"
        f"  только в ENV_TEMPLATE: {sorted(template_keys - example_keys)}"
    )


def test_env_template_contains_all_wizard_keys():
    template_keys = _keys_from_text(inst.ENV_TEMPLATE)
    wizard_keys = {f.key for g in inst.WIZARD_GROUPS for f in g.fields}
    missing = wizard_keys - template_keys
    assert not missing, f"в ENV_TEMPLATE нет ключей wizard: {sorted(missing)}"
