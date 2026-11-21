from __future__ import annotations

import os
from pathlib import Path

import click
from click.testing import CliRunner
import pytest
import yaml

import reviewer.entrypoints.cli as cli_module
from reviewer.config.onboarding import RepositoryDetection
from reviewer.entrypoints.cli import cli


def _isolate(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    env = tmp_path / "global" / ".env"
    config_home = tmp_path / "xdg"
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: env)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    monkeypatch.delenv("REVIEWER_ENV_FILE", raising=False)
    return env, config_home / "rag-reviewer"


def _detection(
    tmp_path: Path,
    repo: str = "o/r",
    *,
    primary: str = "dev",
    repo_source: str = "git:origin",
) -> RepositoryDetection:
    return RepositoryDetection(
        root=tmp_path,
        repo=repo,
        repo_source=repo_source,
        primary=primary,
        primary_source="git:origin/HEAD",
    )


def _repo_path(config_root: Path, repo: str = "o/r") -> Path:
    owner, name = repo.split("/", 1)
    return config_root / "repos" / owner / f"{name}.yml"


def _forbid_noninteractive_side_effects(monkeypatch) -> None:
    def fail(name):
        return lambda *_args, **_kwargs: pytest.fail(f"{name} called")

    monkeypatch.setattr(click, "prompt", fail("prompt"))
    monkeypatch.setattr(click, "confirm", fail("confirm"))
    monkeypatch.setattr(click, "launch", fail("browser"))
    monkeypatch.setattr(
        "reviewer.tasks.boards.setup.configure_board_provider",
        fail("provider setup"),
    )
    monkeypatch.setattr(cli_module, "_select_vcs_provider", fail("VCS selection"))
    monkeypatch.setattr(cli_module, "_prompt_vcs_provider", fail("VCS setup"))
    monkeypatch.setattr(cli_module, "_run_codex_target", fail("Codex install"))
    monkeypatch.setattr("subprocess.run", fail("reviewer check"))
    monkeypatch.setattr(cli_module, "_config_context", fail("full config show"))


def test_init_scope_global_never_detects_or_reads_repo(monkeypatch, tmp_path):
    env, config_root = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: pytest.fail("repo detection called"),
    )
    monkeypatch.setattr(
        cli_module,
        "Settings",
        lambda *_args, **_kwargs: pytest.fail("repo settings constructed"),
    )

    result = CliRunner().invoke(cli, ["init", "--scope", "global", "--yes"])

    assert result.exit_code == 0, result.output
    assert env.is_file()
    assert not config_root.exists()


def test_quick_init_prompts_for_missing_keys_and_shows_compact_preview(monkeypatch, tmp_path):
    env, _ = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(cli_module, "remote_url", lambda _repo: "git@github.com:owner/repo.git")

    result = CliRunner().invoke(
        cli, ["init", "--scope", "global"], input="voyage-example\ngithub-example\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert "Быстрая настройка" in result.output
    assert "Предпросмотр настройки" in result.output
    assert "Хранилища (Postgres / Neo4j)?" not in result.output
    assert "Подключить provider доски задач" not in result.output
    assert "VOYAGE_API_KEY=" not in result.output
    assert "voyage-example" not in result.output and "github-example" not in result.output
    assert "reviewer check" in result.output
    assert "VOYAGE_API_KEY=voyage-example" in env.read_text()
    assert "GITHUB_TOKEN=github-example" in env.read_text()
    if os.name == "posix":
        assert env.stat().st_mode & 0o777 == 0o600


def test_quick_init_reuses_existing_env_without_prompts(monkeypatch, tmp_path):
    env, _ = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(cli_module, "remote_url", lambda _repo: "git@github.com:owner/repo.git")
    env.parent.mkdir()
    env.write_text("# Custom comment\nVOYAGE_API_KEY=saved-voyage\nGITHUB_TOKEN=saved-github\n")
    runner = CliRunner()
    original = env.read_bytes()
    _forbid_noninteractive_side_effects(monkeypatch)

    result = runner.invoke(cli, ["init", "--scope", "global"])

    assert result.exit_code == 0, result.output
    assert "Изменений нет" in result.output
    assert env.read_bytes() == original
    assert "saved-voyage" not in result.output and "saved-github" not in result.output


def test_quick_init_uses_env_override_and_can_cancel_before_writing(monkeypatch, tmp_path):
    default_env, _ = _isolate(monkeypatch, tmp_path)
    selected = tmp_path / "another profile" / ".env"
    monkeypatch.setenv("REVIEWER_ENV_FILE", str(selected))
    monkeypatch.setattr(cli_module, "remote_url", lambda _repo: None)
    runner = CliRunner()

    cancelled = runner.invoke(cli, ["init", "--scope", "global"], input="token\nn\n")
    assert cancelled.exit_code == 0, cancelled.output
    assert not selected.parent.exists()

    saved = runner.invoke(cli, ["init", "--scope", "global"], input="token\ny\n")
    assert saved.exit_code == 0, saved.output
    assert f"reviewer check --env-file '{selected.resolve()}'" in saved.output
    assert selected.is_file() and not default_env.exists()


def test_quick_init_defers_unconfigured_self_hosted_gitlab_to_advanced(monkeypatch, tmp_path):
    env, _ = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(cli_module, "remote_url", lambda _repo: "git@gitlab.example:team/repo.git")

    result = CliRunner().invoke(cli, ["init", "--scope", "global"], input="voyage\ny\n")

    assert result.exit_code == 0, result.output
    assert "GitLab: подтвердите адрес сервера и ключ через reviewer init --advanced" in result.output
    assert "Ключ GitLab для ревью" not in result.output
    assert "gitlab.example" not in env.read_text()


def test_check_reads_explicit_env_file(monkeypatch, tmp_path):
    selected = tmp_path / "profile.env"
    selected.touch()
    calls = []

    def settings_factory(**kwargs):
        calls.append(kwargs)
        raise click.ClickException("Переданный профиль обнаружен")

    monkeypatch.setattr(cli_module, "Settings", settings_factory)
    result = CliRunner().invoke(cli, ["check", "--env-file", str(selected)])

    assert calls == [{"_env_file": selected}]
    assert "Переданный профиль обнаружен" in result.output


def test_init_scope_repo_does_not_read_or_rewrite_global_env(monkeypatch, tmp_path):
    env, config_root = _isolate(monkeypatch, tmp_path)
    env.parent.mkdir(parents=True)
    env.write_text("SENTINEL=unchanged\n", encoding="utf-8")
    monkeypatch.setattr(
        "reviewer.install.read_env",
        lambda _path: pytest.fail("global env read"),
    )
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )
    real_settings = cli_module.Settings
    settings_calls: list[dict[str, object]] = []

    def settings_factory(**kwargs):
        settings_calls.append(kwargs)
        return real_settings(**kwargs)

    monkeypatch.setattr(cli_module, "Settings", settings_factory)

    result = CliRunner().invoke(cli, ["init", "--scope", "repo", "--yes"])

    assert result.exit_code == 0, result.output
    assert settings_calls == [{"_env_file": None}]
    assert env.read_text(encoding="utf-8") == "SENTINEL=unchanged\n"
    assert _repo_path(config_root).is_file()


def test_init_scope_repo_keeps_second_repository_isolated(monkeypatch, tmp_path):
    env, config_root = _isolate(monkeypatch, tmp_path)
    env.parent.mkdir(parents=True)
    env.write_text("SENTINEL=unchanged\n", encoding="utf-8")

    def detect(_path, repo_override, *, settings):
        del settings
        primary = "main" if repo_override == "one/service" else "release"
        return _detection(
            tmp_path,
            repo_override,
            primary=primary,
            repo_source="cli",
        )

    monkeypatch.setattr("reviewer.entrypoints.cli.detect_repository", detect)
    runner = CliRunner()

    first = runner.invoke(
        cli,
        ["init", "--scope", "repo", "--repo", "one/service", "--yes"],
    )
    first_path = _repo_path(config_root, "one/service")
    first_bytes = first_path.read_bytes() if first_path.exists() else b""
    second = runner.invoke(
        cli,
        ["init", "--scope", "repo", "--repo", "two/service", "--yes"],
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert first_path.read_bytes() == first_bytes
    assert _repo_path(config_root, "two/service").is_file()
    assert env.read_text(encoding="utf-8") == "SENTINEL=unchanged\n"


def test_init_default_all_previews_global_and_repo_targets(monkeypatch, tmp_path):
    env, config_root = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda path, *_args, **_kwargs: _detection(tmp_path)
        if path == "."
        else pytest.fail("--path leaked into repo stage"),
    )

    result = CliRunner().invoke(
        cli,
        ["init", "--path", str(env), "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert "# reviewer init preview" in result.output
    assert f"file: {env}" in result.output
    assert f"file: {_repo_path(config_root)}" in result.output
    assert "repo: o/r (git:origin)" in result.output
    assert "primary: dev (git:origin/HEAD)" in result.output


def test_init_dry_run_previews_both_targets_without_prompt_write_or_network(
    monkeypatch,
    tmp_path,
):
    env, config_root = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.apply_repository_config",
        lambda _plan: pytest.fail("repo write called"),
    )
    _forbid_noninteractive_side_effects(monkeypatch)

    result = CliRunner().invoke(cli, ["init", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert f"file: {env}" in result.output
    assert f"file: {_repo_path(config_root)}" in result.output
    assert "action: create" in result.output
    assert not env.exists()
    assert not env.parent.exists()
    assert not config_root.parent.exists()


def test_init_yes_writes_after_preview_without_prompt_provider_check_or_network(
    monkeypatch,
    tmp_path,
):
    env, config_root = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )
    _forbid_noninteractive_side_effects(monkeypatch)

    result = CliRunner().invoke(cli, ["init", "--yes"])

    assert result.exit_code == 0, result.output
    assert result.output.index("# reviewer init preview") < result.output.index("Записан")
    assert env.is_file()
    assert _repo_path(config_root).is_file()


def test_init_preview_redacts_removed_and_unknown_secrets(monkeypatch, tmp_path):
    env, _config_root = _isolate(monkeypatch, tmp_path)
    env.parent.mkdir(parents=True)
    env.write_text(
        "WEB_ADMIN_PASSWORD=legacy-admin-secret\n"
        "TASK_BOARD_MCP=legacy-board-name\n"
        "CUSTOM_TOKEN=unknown-token-secret\n"
        "DATABASE_URL=postgresql://user:url-secret@db.example/reviewer\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        ["init", "--scope", "global", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "WEB_ADMIN_PASSWORD=" in result.output
    assert "TASK_BOARD_MCP=legacy-board-name" in result.output
    assert "CUSTOM_TOKEN=" in result.output
    assert "DATABASE_URL=" in result.output
    for secret in ("legacy-admin-secret", "unknown-token-secret", "url-secret"):
        assert secret not in result.output


def test_init_interactive_retries_owner_name_for_unrecognized_remote(monkeypatch, tmp_path):
    _env, config_root = _isolate(monkeypatch, tmp_path)
    calls: list[str | None] = []

    def detect(_path, repo_override, *, settings):
        del settings
        calls.append(repo_override)
        if repo_override is None:
            return None
        return _detection(tmp_path, repo_override, repo_source="cli")

    answers = iter(["o/r", "dev", "dev"])
    monkeypatch.setattr("reviewer.entrypoints.cli.detect_repository", detect)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(click, "confirm", lambda *_args, **_kwargs: True)

    result = CliRunner().invoke(cli, ["init", "--scope", "repo"])

    assert result.exit_code == 0, result.output
    assert calls == [None, "o/r"]
    assert "repo: o/r (cli)" in result.output
    assert _repo_path(config_root).is_file()


def test_init_interactive_can_correct_detected_branches(monkeypatch, tmp_path):
    _env, config_root = _isolate(monkeypatch, tmp_path)
    answers = iter(["release", "release,main"])
    confirms: list[str] = []
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(
        click,
        "confirm",
        lambda prompt, **_kwargs: confirms.append(prompt) or True,
    )

    result = CliRunner().invoke(cli, ["init", "--scope", "repo"])

    assert result.exit_code == 0, result.output
    assert len(confirms) == 1
    assert "Обнаружен repo o/r (git:origin)" in result.output
    data = yaml.safe_load(_repo_path(config_root).read_text(encoding="utf-8"))
    assert data["repository"] == {
        "primary_branch": "release",
        "index_branches": ["release", "main"],
    }


def test_init_interactive_retries_invalid_branch_csv(monkeypatch, tmp_path):
    _env, config_root = _isolate(monkeypatch, tmp_path)
    answers = iter(["dev", "dev,dev", "main", "dev,main"])
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(click, "confirm", lambda *_args, **_kwargs: True)

    result = CliRunner().invoke(cli, ["init", "--scope", "repo"])

    assert result.exit_code == 0, result.output
    assert result.output.count("Некорректные ветки:") == 2
    data = yaml.safe_load(_repo_path(config_root).read_text(encoding="utf-8"))
    assert data["repository"]["index_branches"] == ["dev", "main"]


def test_init_existing_repo_block_is_noop_and_skips_branch_prompts(monkeypatch, tmp_path):
    _env, config_root = _isolate(monkeypatch, tmp_path)
    path = _repo_path(config_root)
    path.parent.mkdir(parents=True)
    original = "repository:\n  primary_branch: trunk\n  index_branches: [trunk, main]\n"
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: pytest.fail("branch prompt"))
    confirmations: list[str] = []
    monkeypatch.setattr(
        click,
        "confirm",
        lambda prompt, **_kwargs: confirmations.append(prompt) or True,
    )

    result = CliRunner().invoke(cli, ["init", "--scope", "repo"])

    assert result.exit_code == 0, result.output
    assert confirmations == []
    assert "action: noop" in result.output
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("abort", [False, True])
def test_init_final_rejection_or_abort_happens_before_first_write(
    monkeypatch,
    tmp_path,
    abort,
):
    env, config_root = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )
    monkeypatch.setattr(
        "reviewer.install.prompt_groups",
        lambda groups, current, yes: {
            field.key: current.get(field.key, "") or field.default
            for group in groups
            for field in group.fields
        },
    )
    monkeypatch.setattr(click, "prompt", lambda _text, default, **_kwargs: default)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.apply_repository_config",
        lambda _plan: pytest.fail("repo write called"),
    )
    confirmations = iter([False, False, "final"])

    def confirm(*_args, **_kwargs):
        answer = next(confirmations)
        if answer == "final" and abort:
            raise click.Abort()
        return False if answer == "final" else answer

    monkeypatch.setattr(click, "confirm", confirm)

    result = CliRunner().invoke(cli, ["init", "--advanced"])

    assert result.exit_code == 0, result.output
    assert "Отменено" in result.output
    assert not env.exists()
    assert not env.parent.exists()
    assert not config_root.parent.exists()


@pytest.mark.parametrize(
    ("scope", "expected_code", "global_written"),
    [("all", 0, True), ("repo", 1, False)],
)
def test_init_missing_detection_all_skips_but_repo_fails_with_guidance(
    monkeypatch,
    tmp_path,
    scope,
    expected_code,
    global_written,
):
    env, config_root = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: None,
    )

    result = CliRunner().invoke(cli, ["init", "--scope", scope, "--yes"])

    assert result.exit_code == expected_code, result.output
    assert "--repo owner/name" in result.output
    assert env.exists() is global_written
    assert not config_root.exists()


def test_init_invalid_repo_fails_before_prompt_preview_or_write(monkeypatch, tmp_path):
    env, config_root = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(click, "prompt", lambda *_args, **_kwargs: pytest.fail("prompt called"))
    monkeypatch.setattr(click, "confirm", lambda *_args, **_kwargs: pytest.fail("confirm called"))

    result = CliRunner().invoke(cli, ["init", "--repo", "invalid", "--yes"])

    assert result.exit_code != 0
    assert "owner/name" in result.output
    assert "# reviewer init preview" not in result.output
    assert not env.exists()
    assert not env.parent.exists()
    assert not config_root.exists()


def test_init_repo_prints_effective_branch_source_and_exact_follow_up(monkeypatch, tmp_path):
    _env, _config_root = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )
    monkeypatch.setattr(
        cli_module,
        "_config_context",
        lambda *_args, **_kwargs: pytest.fail("full config show called"),
    )

    result = CliRunner().invoke(cli, ["init", "--scope", "repo", "--yes"])

    assert result.exit_code == 0, result.output
    assert "branches:" in result.output
    assert "home:repos/o/r.yml" in result.output
    assert "reviewer config show --repo o/r" in result.output


def test_init_repo_write_failure_is_explicit_and_nonzero(monkeypatch, tmp_path):
    _env, _config_root = _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.detect_repository",
        lambda *_args, **_kwargs: _detection(tmp_path),
    )
    monkeypatch.setattr(
        "reviewer.entrypoints.cli.apply_repository_config",
        lambda _plan: (_ for _ in ()).throw(OSError("secret detail")),
    )

    result = CliRunner().invoke(cli, ["init", "--scope", "repo", "--yes"])

    assert result.exit_code != 0
    assert "repos/o/r.yml" in result.output
    assert "OSError" in result.output
    assert "secret detail" not in result.output
