from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import platform as _platform
import re
import shutil as _shutil
from typing import TYPE_CHECKING

import click
import httpx
import psycopg
import yaml

from reviewer.config.settings import Settings
from reviewer.app import build_components
from reviewer.config.branches import migrate_repo_branches, resolve_repo_branches
from reviewer.config.layers import (
    HomeConfigError,
    build_config_report,
    migrate_repo_config,
    resolve_policy_data,
)
from reviewer.config.onboarding import (
    RepositoryConfigPlan,
    apply_repository_config,
    detect_repository,
    parse_branch_csv,
    plan_repository_config,
)
from reviewer.config.provider_access import render_provider_access
from reviewer.gitutil import file_at_ref, list_python_files, repo_root, rev_parse, remote_url
from reviewer.graph.backend import build_code_graph
from reviewer.graph.store import GraphStore
from reviewer.index.freshness import update_base
from reviewer.index.pathfilter import is_ignored
from reviewer.index.refs import base_ref
from reviewer.policy.policy import ReviewPolicy
from reviewer.index.store import ChunkStore
from reviewer.index.summary_store import SummaryStore
from reviewer.mcp.session_store import SessionStore
from reviewer.services.branch import resolve_branch
from reviewer.services.gc import purge_orphaned_overlays
from reviewer.services.review_service import ReviewService
from reviewer.services.status import build_status_report, render_status, render_status_json
from reviewer.tasks.boards.errors import sanitize_provider_text
from reviewer.update_lifecycle import (
    download_compose,
    run_fresh_artifact_refresh,
    sync_compose_file,
)
from reviewer.versioning import InstallMode, check_latest, detect_installation, upgrade_uv_tool
from reviewer.web.serve import DEFAULT_HOST, DEFAULT_PORT

if TYPE_CHECKING:
    from reviewer.install_claude import ClaudeInstallResult
    from reviewer.install_codex import CodexInstallResult

log = logging.getLogger(__name__)


def _resolve_repo(repo_opt: str | None, path: str, settings) -> str:
    """Резолв repo-тега: --repo → git remote → DEFAULT_REPO → ошибка."""
    from reviewer.services.repo_id import normalize_repo, derive_repo_from_remote
    if repo_opt:
        return normalize_repo(repo_opt)
    derived = derive_repo_from_remote(remote_url(path) or "")
    if derived:
        return derived
    if settings.default_repo:
        return normalize_repo(settings.default_repo)
    raise click.ClickException(
        "Не удалось определить repo: укажите --repo owner/name "
        "(или задайте DEFAULT_REPO в .env)")


@click.group()
def cli() -> None: ...


@cli.group("config")
def config_group() -> None:
    """Inspect and migrate layered repository policy."""


def _close_config_components(components) -> None:
    """Закрыть созданные для diagnostic CLI хранилища независимо друг от друга."""
    for name in ("store", "graph", "task_store", "summary_store"):
        component = getattr(components, name, None)
        if component is not None:
            _safe_config_close(component, name)


def _safe_config_close(resource, name: str) -> None:
    """Не дать cleanup-ошибке скрыть исход результата diagnostic команды."""
    try:
        resource.close()
    except Exception:  # noqa: BLE001 — best-effort cleanup не меняет результат команды
        log.warning("Не удалось закрыть resource config CLI: %s", name)


def _config_error_message(exc: Exception) -> str:
    """Вернуть публичный diagnostic без YAML/normalization payload."""
    if isinstance(exc, HomeConfigError):
        return str(exc)
    return "конфиг не прочитан: YAML"


def _branches_report(branches) -> dict[str, object]:
    """Общий payload effective branches для config show и repo onboarding."""
    return {
        "branches": {
            "primary": branches.primary,
            "index": list(branches.index),
            "source": branches.source,
        }
    }


def _render_config_report(report: Mapping[str, object]) -> None:
    branches = report.get("branches")
    if branches:
        assert isinstance(branches, Mapping)
        click.echo("branches:")
        click.echo(f"  primary: {branches['primary']}  ({branches['source']})")
        click.echo(f"  index:   {', '.join(branches['index'])}  ({branches['source']})")
    policy_error = report.get("policy_error")
    if policy_error is not None:
        click.echo(f"policy_error: {policy_error}")
        return
    if "effective" not in report:
        return
    effective = report["effective"]
    sources = report["sources"]
    shadowed = report["shadowed"]
    assert isinstance(effective, Mapping)
    assert isinstance(sources, Mapping)
    assert isinstance(shadowed, Mapping)
    for key in sorted(effective):
        click.echo(
            f"{key}: {json.dumps(effective[key], ensure_ascii=False, sort_keys=True)}"
        )
        click.echo(f"  source: {sources[key]}")
        if key in shadowed:
            click.echo(f"  shadowed: {', '.join(shadowed[key])}")
    for warning in report["warnings"]:
        click.echo(f"warning: {warning}")


@contextmanager
def _config_context(repo_opt: str, branch_opt: str | None):
    settings = Settings()
    # connect=False: diagnostic-командам граф не нужен, а созданный GraphStore
    # эагерли требует живой Neo4j — не должен ронять команду при недоступной сети.
    components = build_components(settings, connect=False)
    vcs = None
    try:
        repo = _resolve_config_repo(repo_opt)
        # Ветки резолвятся из домашнего слоя ДО обращения к VCS: раньше ветка
        # бралась из settings.primary_branch() именно затем, чтобы создать VCS
        # и прочитать committed .review.yml — цикл «нужна ветка, чтобы узнать
        # ветку». Домашний резолв не ходит в сеть, поэтому цикла больше нет.
        branches = resolve_repo_branches(repo, settings=settings)
        branch = branch_opt or branches.primary
        owner, name = repo.split("/", 1)
        vcs_error: Exception | None = None
        try:
            vcs = ReviewService(settings, components)._create_vcs_provider(owner, name)
        except Exception as exc:  # noqa: BLE001 — диагностика не должна падать целиком
            vcs_error = exc
        yield settings, components, vcs, repo, branch, branches, vcs_error
    finally:
        if vcs is not None:
            _safe_config_close(vcs, "vcs")
        _close_config_components(components)


def _resolve_config_repo(repo: str) -> str:
    from reviewer.services.repo_id import normalize_repo

    return normalize_repo(repo)


@config_group.command("show")
@click.option("--repo", required=True, help="owner/name репозитория")
@click.option("--branch", default=None, help="ветка policy; по умолчанию первичная")
@click.option("--json", "as_json", is_flag=True, default=False)
def config_show(repo: str, branch: str | None, as_json: bool) -> None:
    """Показать effective policy и происхождение её верхних ключей.

    Секция веток печатается всегда, даже если VCS недоступен (нет сети, нет
    токена) — резолв веток чисто локальный и не зависит от policy-части.
    """
    try:
        with _config_context(repo, branch) as ctx:
            settings, _components, vcs, repo_id, ref, branches, vcs_error = ctx
            payload = _branches_report(branches)
            try:
                if vcs_error is not None:
                    raise vcs_error
                data, meta = resolve_policy_data(
                    repo_id,
                    ref,
                    lambda selected_ref: vcs.get_file_at_ref(".review.yml", selected_ref),
                    strict_home=True,
                )
                payload.update(build_config_report(repo_id, ref, settings, data, meta))
            except (HomeConfigError, yaml.YAMLError) as exc:
                # Тот же санитайзер, что и у остальных config-команд: не эхоить
                # сырой YAML/normalization payload исключения.
                payload["policy_error"] = _config_error_message(exc)
            except Exception as exc:  # noqa: BLE001 — диагностика не должна падать целиком
                # Прочие сбои (VCS, сеть) — без текста исключения: он может
                # содержать URL/токены из VCS-клиента.
                payload["policy_error"] = type(exc).__name__
    except (HomeConfigError, yaml.YAMLError) as exc:
        raise click.ClickException(_config_error_message(exc)) from exc
    if as_json:
        click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        _render_config_report(payload)
    if "policy_error" in payload:
        # Branch-секция и диагностика уже напечатаны — не через ClickException
        # (он подавил бы вывод), но код возврата должен сигналить о проблеме
        # внешним скриптам (`config show; echo $?`).
        raise SystemExit(1)


@config_group.command("migrate")
@click.option("--repo", required=True, help="owner/name репозитория")
@click.option("--branch", default=None, help="ветка policy; по умолчанию первичная")
def config_migrate(repo: str, branch: str | None) -> None:
    """Безопасно скопировать committed policy и ветки в домашний слой репозитория.

    Порядок обязателен: policy-миграция идёт первой, потому что
    `migrate_repo_config` публикует новый домашний файл только при его
    отсутствии — если branch-миграция создаст файл раньше, policy-перенос
    уйдёт в ветку «уже перенесено» и отчитается иначе. Поэтому исключение
    policy-части не поднимается
    сразу: оно сохраняется, branch-миграция выполняется и печатается
    безусловно (перенос веток от committed `.review.yml` не зависит), и
    только потом сохранённое исключение поднимается — так пользователь
    видит перенос веток и получает ненулевой код возврата.
    """
    pending_error: click.ClickException | None = None
    try:
        with _config_context(repo, branch) as ctx:
            settings, _components, vcs, repo_id, ref, _branches, vcs_error = ctx
            try:
                if vcs_error is not None:
                    raise click.ClickException(
                        f"Не удалось подключиться к VCS: {vcs_error}"
                    ) from vcs_error
                result = migrate_repo_config(
                    repo_id,
                    ref,
                    lambda selected_ref: vcs.get_file_at_ref(".review.yml", selected_ref),
                    settings=settings,
                )
                if result.conflicting_keys:
                    keys = ", ".join(result.conflicting_keys)
                    raise click.ClickException(f"Конфликтующие ключи: {keys}")
                report = build_config_report(
                    repo_id, ref, settings, result.data, result.meta
                )
            except (HomeConfigError, yaml.YAMLError) as exc:
                pending_error = click.ClickException(_config_error_message(exc))
                click.echo(f"Policy не перенесена: {pending_error.message}")
            except click.ClickException as exc:
                pending_error = exc
                click.echo(f"Policy не перенесена: {exc.message}")
            else:
                if result.noop:
                    click.echo(f"Конфиг уже перенесён: {result.path}")
                else:
                    click.echo(f"Конфиг перенесён: {result.path}")
                _render_config_report(report)

            branch_result = migrate_repo_branches(repo_id, settings=settings)
            if branch_result.noop:
                click.echo(f"Ветки уже заданы в {branch_result.path}")
            else:
                click.echo(f"Ветки перенесены в {branch_result.path} (.env не изменён)")
    except (HomeConfigError, yaml.YAMLError) as exc:
        raise click.ClickException(_config_error_message(exc)) from exc
    if pending_error is not None:
        raise pending_error


def _run_codex_target(
    *,
    include_mcp: bool,
    dry_run: bool,
    version: str = "latest",
    path_opt: str | None = None,
) -> "CodexInstallResult":
    from pathlib import Path

    from reviewer import install_codex

    if path_opt and include_mcp:
        raise click.ClickException(
            "Codex plugin lifecycle несовместим с --path; используйте --no-skills"
        )
    options = install_codex.CodexInstallOptions(
        include_mcp=include_mcp,
        dry_run=dry_run,
        mcp_version=version,
        mcp_path=Path(path_opt).expanduser() if path_opt else None,
    )
    try:
        return install_codex.run_codex_install(
            options, legacy_migrator=install_codex.migrate_legacy_skills
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


def _print_codex_result(result) -> None:
    if result.plan.options.dry_run:
        if result.mcp_preview is not None:
            click.echo("# Codex MCP config preview")
            click.echo(result.mcp_preview)
        click.echo(f"# Codex marketplace: {result.plan.marketplace_action}")
        click.echo("# " + " ".join(result.plan.marketplace_argv))
        click.echo("# " + " ".join(result.plan.plugin_argv))
        click.echo("# legacy migration: scan after verified plugin install")
        return
    click.echo(f"✓ Codex plugin: {result.verification.version}")
    click.echo(f"  skills: {len(result.verification.skills)}")
    if result.config_backup:
        click.echo(f"  config backup: {result.config_backup}")
    if result.migration.backup_root:
        click.echo(f"  legacy backup: {result.migration.backup_root}")
    for warning in result.warnings:
        click.echo(f"  предупреждение: {warning}")
    click.echo("Откройте New Chat/new CLI session; в IDE выполните Reload Window.")


def _run_claude_target(*, dry_run: bool) -> "ClaudeInstallResult":
    from reviewer import install_claude

    try:
        return install_claude.run_claude_install(
            install_claude.ClaudeInstallOptions(dry_run=dry_run)
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


def _print_claude_result(result) -> None:
    if result.plan.options.dry_run:
        click.echo("# Claude Code marketplace")
        click.echo("# " + " ".join(result.plan.marketplace_argv))
        click.echo("# " + " ".join(result.plan.marketplace_update_argv))
        click.echo("# " + " ".join(result.plan.plugin_argv))
        return
    if result.plugin is None:
        raise click.ClickException("Claude Code plugin не прошёл post-install verification")
    click.echo(f"✓ Claude Code plugin: {result.plugin.plugin_id}")
    click.echo(f"  scope: {result.plugin.scope}; enabled: {result.plugin.enabled}")
    click.echo("Откройте New Chat/new CLI session; в IDE выполните Reload Window.")


@dataclass(frozen=True)
class _ClaudeMcpInstallResult:
    executable: str
    get_argv: tuple[str, ...]
    remove_argv: tuple[str, ...] | None
    add_argv: tuple[str, ...]
    dry_run: bool
    created: bool
    replaced: bool


def _claude_mcp_fields(output: str) -> dict[str, str]:
    """Parse only documented human-readable ``claude mcp get`` fields."""
    fields: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        for name in ("Scope", "Command", "Args"):
            prefix = f"{name}:"
            if line.startswith(prefix):
                if name in fields:
                    return {}
                fields[name] = line[len(prefix):].strip()
    return fields


def _claude_mcp_is_canonical(fields: dict[str, str], version: str) -> bool:
    package = f"rag-reviewer@{version}" if version else "rag-reviewer"
    return (
        fields.get("Scope", "").startswith("User config")
        and fields.get("Command") == "uvx"
        and fields.get("Args") == f"--from {package} reviewer-mcp"
    )


def _checked_claude_mcp_command(runner, argv: tuple[str, ...], phase: str) -> None:
    response = runner(argv)
    if response.returncode:
        detail = response.stderr.strip() or response.stdout.strip() or "command failed"
        raise click.ClickException(f"{phase}: {detail}; argv={' '.join(argv)}")


_CLAUDE_MCP_NOT_FOUND_RESPONSE = re.compile(
    r'No MCP server named "reviewer"\.(?:'
    r' Configured servers: [^\r\n]*'
    r'| Run `claude mcp add` to add one\.'
    r')?'
)


def _run_claude_mcp_target(
    *,
    dry_run: bool,
    version: str,
    runner=None,
    which=None,
) -> _ClaudeMcpInstallResult:
    """Ensure the public user-scope Claude MCP entry without reading private state."""
    from reviewer import install_claude
    from reviewer.install_codex import subprocess_runner

    runner = subprocess_runner if runner is None else runner
    which = _shutil.which if which is None else which
    try:
        executable = str(install_claude.find_claude_executable(which))
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    package = f"rag-reviewer@{version}" if version else "rag-reviewer"
    get_argv = (executable, "mcp", "get", "reviewer")
    remove_argv = (executable, "mcp", "remove", "reviewer", "--scope", "user")
    add_argv = (
        executable,
        "mcp",
        "add",
        "--scope",
        "user",
        "reviewer",
        "--",
        "uvx",
        "--from",
        package,
        "reviewer-mcp",
    )
    if dry_run:
        return _ClaudeMcpInstallResult(
            executable, get_argv, remove_argv, add_argv, True, False, False
        )

    status = runner(get_argv)
    if status.returncode:
        messages = tuple(
            message
            for message in (status.stderr.strip(), status.stdout.strip())
            if message
        )
        if (
            len(messages) != 1
            or _CLAUDE_MCP_NOT_FOUND_RESPONSE.fullmatch(messages[0]) is None
        ):
            detail = "\n".join(messages) or "command failed"
            raise click.ClickException(
                f"Claude Code MCP get: {detail}; argv={' '.join(get_argv)}"
            )
        fields = {}
    else:
        fields = _claude_mcp_fields(status.stdout)
    if _claude_mcp_is_canonical(fields, version):
        return _ClaudeMcpInstallResult(
            executable, get_argv, None, add_argv, False, False, False
        )

    replaced = fields.get("Scope", "").startswith("User config")
    if replaced:
        _checked_claude_mcp_command(runner, remove_argv, "Claude Code MCP remove")
    _checked_claude_mcp_command(runner, add_argv, "Claude Code MCP add")

    verified = runner(get_argv)
    verification = _claude_mcp_fields(verified.stdout) if not verified.returncode else {}
    if not _claude_mcp_is_canonical(verification, version):
        raise click.ClickException(
            "Claude Code MCP verification failed: expected user-scope "
            f"uvx --from {package} reviewer-mcp; argv={' '.join(get_argv)}"
        )
    return _ClaudeMcpInstallResult(
        executable,
        get_argv,
        remove_argv if replaced else None,
        add_argv,
        False,
        not replaced,
        replaced,
    )


def _print_claude_mcp_result(result: _ClaudeMcpInstallResult) -> None:
    if result.dry_run:
        click.echo("# Claude Code MCP (user scope)")
        click.echo("# " + " ".join(result.get_argv))
        if result.remove_argv is not None:
            click.echo("# " + " ".join(result.remove_argv))
        click.echo("# " + " ".join(result.add_argv))
        return
    if result.replaced:
        status = "заменена неканоническая запись"
    elif result.created:
        status = "добавлена запись"
    else:
        status = "запись уже актуальна"
    click.echo(f"✓ Claude Code MCP (user scope): {status}")


def _apply_claude_allowlist(inst, *, dry_run: bool) -> None:
    try:
        plan = inst.build_allowlist_plan(inst.claude_user_settings_path())
        if dry_run:
            click.echo("# Claude Code allowlist (глобально, для всех проектов) → " f"{plan.path}")
            click.echo(plan.content)
            return
        backup = inst.apply_allowlist_plan(plan)
    except Exception as exc:
        raise click.ClickException(f"Claude Code allowlist: {exc}") from exc
    if plan.already:
        status = "правило уже есть"
    elif plan.created:
        status = "создан settings.json"
    else:
        status = "добавлено правило"
    click.echo(
        "  allowlist (глобально, для всех проектов): "
        f"{status} → {plan.path} ({inst.REVIEWER_PERMISSION_RULE})"
    )
    if backup:
        click.echo(f"  бэкап: {backup}")


def _check_board_providers(
    settings,
    *,
    registry=None,
    credential_source=None,
    board_projects: Mapping[str, str] | None = None,
) -> bool:
    """Проверить configured registry providers; ``True`` означает хотя бы одну ошибку."""
    import json

    from reviewer.config.provider_credentials import ProviderCredentialSource
    from reviewer.tasks.boards.errors import BoardProviderError
    from reviewer.tasks.boards.reporting import sanitize_validation_report
    from reviewer.tasks.boards.registry import default_board_registry
    from reviewer.tasks.boards.runtime import resolved_provider

    registry = registry or default_board_registry()
    source = credential_source or ProviderCredentialSource.from_settings(settings)
    configured = registry.configured_types(source)
    failed = False
    for board_type in sorted(set(board_projects or {}) - set(configured)):
        click.echo(f"✗ --board-project: provider {board_type!r} не настроен")
        failed = True
    if not configured:
        click.echo("  Доски задач: configured providers не настроены, проверка пропущена")
        return failed

    for board_type in configured:
        project = (board_projects or {}).get(board_type)
        try:
            with resolved_provider(
                settings,
                board_type,
                {},
                registry=registry,
                credential_source=source,
            ) as resolved:
                report = resolved.provider.validate_connection(project)
                safe = sanitize_validation_report(report, resolved.secrets)
            click.echo(f"✓ Доска задач [{board_type}]: подключение — OK")
            if isinstance(safe, dict):
                for key in ("identity", "project", "capabilities"):
                    if key in safe:
                        rendered = json.dumps(safe[key], ensure_ascii=False, sort_keys=True)
                        click.echo(f"  {key}: {rendered}")
                for warning in safe.get("warnings") or []:
                    click.echo(f"  warning: {warning}")
        except BoardProviderError as error:
            click.echo(f"✗ Доска задач [{board_type}] ({error.category}): {error.message}")
            if error.hint:
                click.echo(f"  {error.hint}")
            failed = True
    return failed


def _check_vcs_providers(settings: Settings) -> bool:
    """Проверить authentication каждого настроенного VCS без вывода credentials."""
    secrets = tuple(
        token for token in (settings.github_token, settings.gitlab_token) if token
    )
    configured: list[tuple[str, str, dict[str, str], str]] = []
    if settings.github_token:
        configured.append(
            (
                "GitHub",
                "https://api.github.com/user",
                {"Authorization": f"Bearer {settings.github_token}"},
                "login",
            )
        )
    if settings.gitlab_token:
        configured.append(
            (
                "GitLab",
                f"{settings.gitlab_url.rstrip('/')}/api/v4/user",
                {"PRIVATE-TOKEN": settings.gitlab_token},
                "username",
            )
        )
    if not configured:
        click.echo(
            "✗ VCS: не настроен ни один VCS token; "
            "запустите reviewer init --scope global"
        )
        return True

    failed = False
    for label, url, headers, identity_key in configured:
        try:
            response = httpx.get(url, headers=headers, timeout=10)
            if response.status_code != 200:
                click.echo(
                    f"✗ {label} API: HTTP {response.status_code} — "
                    "проверьте token/base URL"
                )
                failed = True
                continue
            payload = response.json()
            identity = payload.get(identity_key) if isinstance(payload, Mapping) else None
            if not isinstance(identity, str) or not identity.strip():
                click.echo(f"✗ {label} API: identity отсутствует или некорректен")
                failed = True
                continue
            safe_identity = sanitize_provider_text(identity, secrets)
            if label == "GitHub":
                identity_valid = (
                    len(safe_identity) <= 100
                    and re.fullmatch(
                        r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:\[bot\])?",
                        safe_identity,
                    )
                    is not None
                )
            else:
                identity_valid = (
                    len(safe_identity) <= 255
                    and re.fullmatch(r"[A-Za-z0-9_.-]+", safe_identity) is not None
                )
            if safe_identity != identity or not identity_valid:
                click.echo(f"✗ {label} API: identity отсутствует или некорректен")
                failed = True
                continue
            click.echo(f"✓ {label} API: аутентификация OK (identity: {safe_identity})")
        except Exception as exc:  # noqa: BLE001 — health check сообщает только безопасный тип
            click.echo(f"✗ {label} API: {type(exc).__name__}")
            failed = True
    return failed


def _parse_board_projects(values: tuple[str, ...]) -> dict[str, str]:
    projects: dict[str, str] = {}
    for value in values:
        board_type, separator, project = value.partition("=")
        if not separator or not board_type.strip() or not project.strip():
            raise click.BadParameter(
                "ожидается TYPE=PROJECT",
                param_hint="--board-project",
            )
        board_type = board_type.strip()
        if board_type in projects:
            raise click.BadParameter(
                f"тип {board_type!r} указан повторно",
                param_hint="--board-project",
            )
        projects[board_type] = project.strip()
    return projects


@cli.command()
@click.option(
    "--board-project",
    "board_project_values",
    multiple=True,
    metavar="TYPE=PROJECT",
    help="Проект для provider validation; опцию можно повторять.",
)
def check(board_project_values: tuple[str, ...]) -> None:
    """Проверить готовность окружения и настроенных внешних providers."""
    s = Settings()
    board_projects = _parse_board_projects(board_project_values)
    failed = False

    # 1. Ключи
    for label, val in (("VOYAGE_API_KEY", s.voyage_api_key),):
        if val:
            click.echo(f"✓ {label} задан")
        else:
            click.echo(f"✗ {label}: не задан (добавьте в .env)")
            failed = True

    # 2. Postgres
    store = None
    try:
        store = ChunkStore(
            s.pg_dsn,
            min_size=s.pg_pool_min_size,
            max_size=s.pg_pool_max_size,
        )
        with store._connect() as conn:
            conn.execute("SELECT 1 FROM chunks LIMIT 1")
        click.echo(f"✓ Postgres ({s.pg_dsn}): подключение и таблица chunks — OK")
    except Exception as e:
        err = str(e)
        if "chunks" in err or "does not exist" in err:
            click.echo(
                "✗ Postgres: схема не инициализирована — выполните reviewer index"
            )
        else:
            click.echo(f"✗ Postgres: {err}")
        failed = True
    finally:
        if store is not None:
            store.close()

    # 3. Neo4j
    try:
        graph = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
        try:
            graph._driver.verify_connectivity()
            click.echo(f"✓ Neo4j ({s.neo4j_uri}): подключение — OK")
        finally:
            graph.close()
    except Exception as e:
        click.echo(f"✗ Neo4j: {e}")
        failed = True

    # 4. scip-python (информационно, не влияет на exit-code)
    if _shutil.which("scip-python"):
        click.echo("✓ scip-python: найден (точный граф)")
    else:
        click.echo("  scip-python: не найден — граф через tree-sitter (fallback)")

    # 5. Настроенные VCS providers
    failed = _check_vcs_providers(s) or failed

    # 6. Настроенные board providers
    failed = _check_board_providers(s, board_projects=board_projects) or failed

    # 7. Свежесть установленных скилов (информационно, не влияет на exit-code)
    try:
        from reviewer import install as _inst
        warns = _inst.staleness_warnings()
        for line in warns:
            click.echo(line)
        if not warns:
            click.echo("✓ Скилы клиентов: актуальны (или не установлены)")
    except Exception:  # noqa: BLE001 — детект устарелости не должен валить check
        pass

    if failed:
        raise SystemExit(1)
    click.echo("Готово к работе.")


@cli.command()
@click.argument("repo")
@click.option("--ref", default=None,
              help="git-ref для чтения файлов; по умолчанию первичная ветка. "
                   "Если --branch не задан, дополнительно валидируется как "
                   "отслеживаемая ветка (совпадает с ключом хранения)")
@click.option("--branch", "branch_opt", default=None,
              help="имя ветки для хранения индекса; по умолчанию = --ref. "
                   "Если задан явно, --ref — произвольный ref (например, тег) "
                   "без проверки по отслеживаемым веткам")
@click.option("--repo", "repo_tag", default=None,
              help="owner/name тег индекса; по умолчанию из git remote origin")
def index(repo: str, ref: str | None, branch_opt: str | None, repo_tag: str | None) -> None:
    """Построить/обновить base-индекс целевой ветки из локального репо."""
    s = Settings()
    repo_id = _resolve_repo(repo_tag, repo, s)
    try:
        branches = resolve_repo_branches(repo_id, settings=s)
        if branch_opt is None:
            # --branch не задан: --ref — это и ключ хранения, поэтому обязан
            # быть отслеживаемой веткой репозитория.
            ref = resolve_branch(ref, None, branches)
        elif ref is None:
            # --branch задан явно: --ref — произвольный git-ref для чтения
            # файлов (например, тег), пользователь сам управляет расщеплением
            # ref↔branch, поэтому validation по отслеживаемым веткам пропускаем.
            ref = branches.primary
    except (HomeConfigError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    c = build_components(s)
    branch = branch_opt or ref
    bref = base_ref(branch)
    try:
        c.store.init_schema()
        files = list_python_files(repo, ref)
        policy_data, policy_meta = resolve_policy_data(
            repo_id,
            ref,
            lambda selected_ref: file_at_ref(repo, ".review.yml", selected_ref),
        )
        policy = ReviewPolicy.load_data(s, policy_data)
        ignore = policy.ignore
        for warning in policy_meta.warnings:
            log.warning("Домашний слой policy пропущен: %s", warning)
        if ignore:
            files = [f for f in files if not is_ignored(f, ignore)]
        update_base(c.store, c.embedder, repo_id, branch, files,
                    read=lambda p: file_at_ref(repo, p, ref), ignore=ignore)
        c.store.delete_paths_except(repo_id, bref, files)
        sha = rev_parse(repo, ref)
        c.store.set_index_meta(repo_id, bref, sha)
        # Платформа VCS репо (PRI-133): auto-derive из git remote локального
        # клона → repo_vcs. Читается при ревью (API-only) для выбора провайдера.
        from reviewer.services.repo_id import derive_vcs_from_remote
        vcs = derive_vcs_from_remote(remote_url(repo) or "")
        if vcs:
            c.store.set_repo_vcs(repo_id, vcs[0], vcs[1])
            click.echo(f"VCS: {vcs[0]}{(' @ ' + vcs[1]) if vcs[1] else ''}")
        # --- граф кода (в рамках ветки) ---
        src_by_path = {p: file_at_ref(repo, p, ref) for p in files}
        src_by_path = {p: v for p, v in src_by_path.items() if v is not None}
        gnodes, gedges, backend = build_code_graph(
            repo, ref, files, src_by_path, s.graph_backend,
        )
        c.graph.init_schema()
        c.graph.clear(repo_id, branch=branch)   # rebuild только этой ветки репо
        c.graph.upsert_nodes(repo_id, list(gnodes), branch=branch)
        c.graph.upsert_edges(repo_id, gedges, branch=branch)
        click.echo(
            f"Проиндексировано [{repo_id}@{branch}] файлов: {len(files)} @ {sha[:7]}; "
            f"граф [{backend}]: узлов {len(gnodes)}, рёбер {len(gedges)}"
        )
    finally:
        c.store.close()
        if c.graph:
            c.graph.close()


@cli.command("migrate-branches")
@click.option("--repo", "repo_tag", default=None,
              help="owner/name; по умолчанию из git remote origin")
def migrate_branches(repo_tag: str | None) -> None:
    """Один раз после апгрейда: перенести legacy base-индекс на первичную ветку."""
    s = Settings()
    repo_id = _resolve_repo(repo_tag, ".", s)
    try:
        primary = resolve_repo_branches(repo_id, settings=s).primary
    except (HomeConfigError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    c = build_components(s)
    try:
        c.store.init_schema()
        n = c.store.migrate_legacy_base(primary)
        if c.graph is not None:
            c.graph.init_schema()
            c.graph.migrate_legacy_branch(primary)
        graph_msg = f"; граф → branch={primary}" if c.graph is not None else " (граф: пропущен)"
        click.echo(f"Миграция завершена: {n} чанков → base:{primary}{graph_msg}")
    finally:
        c.store.close()
        if c.graph:
            c.graph.close()


@cli.command()
@click.argument("query")
@click.option("--repo", "repo_tag", default=None, help="owner/name; по умолчанию DEFAULT_REPO")
@click.option("--branch", "branch_opt", default=None,
              help="ветка base-индекса; по умолчанию первичная ветка репозитория "
                   "(см. reviewer config show)")
def search(query: str, repo_tag: str | None, branch_opt: str | None) -> None:
    """Гибридный поиск по base-индексу ветки (диагностика)."""
    from reviewer.services.repo_id import normalize_repo
    s = Settings()
    repo_id = normalize_repo(repo_tag or s.default_repo) if (repo_tag or s.default_repo) else None
    if repo_id is None:
        raise click.ClickException("Укажите --repo owner/name (или DEFAULT_REPO в .env)")
    try:
        branches = resolve_repo_branches(repo_id, settings=s)
        branch = resolve_branch(branch_opt, None, branches)
    except (HomeConfigError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    c = build_components(s)
    try:
        qvec = c.embedder.embed_query(query)
        hits = c.store.hybrid_search(
            repo_id, query_text=query, query_embedding=qvec,
            overlay_ref="", changed_paths=[], top_k=10, base_ref=base_ref(branch),
        )
        for h in hits:
            click.echo(f"{h.score:.3f}  {h.node_id}  ({h.path}:{h.start_line})")
    finally:
        c.store.close()
        if c.graph:
            c.graph.close()


@cli.command()
@click.argument("path", default=".")
@click.option("--repo", "repo_tag", default=None,
              help="owner/name тег индекса; по умолчанию из git remote origin")
@click.option("--branch", "branch_opt", default=None,
              help="одна ветка; по умолчанию все отслеживаемые ветки репозитория "
                   "(см. reviewer config show)")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="машиночитаемый JSON вместо текста")
def status(path: str, repo_tag: str | None, branch_opt: str | None,
           as_json: bool) -> None:
    """Показать здоровье/свежесть base-индекса по веткам (не тратит Voyage)."""
    s = Settings()
    repo = _resolve_repo(repo_tag, path, s)
    try:
        repo_branches = resolve_repo_branches(repo, settings=s)
    except HomeConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    branches = [branch_opt] if branch_opt else list(repo_branches.index)
    store = ChunkStore(s.pg_dsn, min_size=s.pg_pool_min_size, max_size=s.pg_pool_max_size)
    graph = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    summary_store = SummaryStore(s.pg_dsn, min_size=s.pg_pool_min_size,
                                 max_size=s.pg_pool_max_size)
    try:
        report = build_status_report(store, graph, repo, branches, path,
                                     summary_store=summary_store)
    except psycopg.OperationalError as e:
        raise click.ClickException(f"Postgres недоступен: {e}")
    finally:
        store.close()
        graph.close()
        summary_store.close()
    if as_json:
        click.echo(render_status_json(report))
        return
    backend = ("scip-python (точный)" if _shutil.which("scip-python")
               else "tree-sitter (fallback, scip-python не найден)")
    click.echo(render_status(report, backend))


@cli.command()
def gc() -> None:
    """Вычистить осиротевшие overlay pr:N и просроченные сессии ревью.

    Overlay брошенного ревью (prepare_review без publish_review) не убирает никто:
    _cleanup срабатывает только после реальной публикации. Эта команда — явная
    уборка; та же логика оппортунистически работает при каждом prepare_review.
    """
    s = Settings()
    if not s.review_session_persist:
        # MCP (_ensure_session_store) в этом случае молча уходит в no-op — сервер
        # сам себя защищает. CLI-команда так не может: без review_sessions живость
        # overlay определить нечем, а таблица (если персист выключен) не пишется
        # никем — live_keys() вернул бы пустое множество, и GC счёл бы сиротами
        # ВСЕ overlay деплоя, включая overlay идущих прямо сейчас ревью. Поэтому
        # явно отказываем, а не тихо всё сносим.
        raise click.ClickException(
            "Персист сессий выключен (REVIEW_SESSION_PERSIST=false) — определить "
            "живые ревью нечем, GC отказано. Включите REVIEW_SESSION_PERSIST=true "
            "в конфиге reviewer-mcp и повторите."
        )
    store = ChunkStore(s.pg_dsn, min_size=s.pg_pool_min_size, max_size=s.pg_pool_max_size)
    session_store = SessionStore(
        s.pg_dsn, min_size=s.pg_pool_min_size, max_size=s.pg_pool_max_size
    )
    try:
        report = purge_orphaned_overlays(store, session_store, s.review_session_ttl_hours)
    except psycopg.OperationalError as e:
        raise click.ClickException(f"Postgres недоступен: {e}")
    except psycopg.errors.UndefinedTable:
        raise click.ClickException(
            "Таблица chunks не найдена — на этом деплое ещё ни разу не выполнялся "
            "`reviewer index`. Проиндексируйте хотя бы одну ветку и повторите gc."
        )
    finally:
        store.close()
        session_store.close()
    for ref in report["purged"]:
        click.echo(f"удалён осиротевший overlay: {ref}")
    click.echo(
        f"Overlay: удалено {len(report['purged'])}, оставлено живых {report['kept']}, "
        f"нераспознано {report['skipped']}; просроченных сессий удалено: "
        f"{report['sessions_deleted']}"
    )


@cli.command()
@click.option("--host", default=DEFAULT_HOST, show_default=True, help="Хост для uvicorn")
@click.option("--port", default=DEFAULT_PORT, show_default=True, type=int, help="Порт для uvicorn")
def serve(host: str, port: int) -> None:
    """Запустить веб-админку наблюдаемости (FastAPI + uvicorn)."""
    from reviewer.web.serve import run_server

    run_server(host, port)


@cli.command()
@click.argument("client", required=False)
@click.option("--all", "all_clients", is_flag=True,
              help="прописать во все обнаруженные клиенты")
@click.option("--list", "list_clients", is_flag=True, help="показать поддерживаемые клиенты")
@click.option("--path", "path_opt", default=None, help="переопределить путь к конфигу клиента")
@click.option("--pin", default=None, help="закрепить версию (напр. 0.1.2); по умолчанию @latest")
@click.option("--no-latest", is_flag=True, help="без @latest (брать из кэша uvx)")
@click.option("--no-skills", is_flag=True, help="не ставить скилы (только MCP-сервер)")
@click.option("--dry-run", is_flag=True, help="показать, что будет записано, без записи на диск")
def install(client: str | None, all_clients: bool, list_clients: bool,
            path_opt: str | None, pin: str | None, no_latest: bool,
            no_skills: bool, dry_run: bool) -> None:
    """Прописать MCP-сервер reviewer (и скилы) в конфиг AI-CLI/IDE (кроссплатформенно)."""
    from reviewer import install as inst

    if all_clients and path_opt:
        raise click.ClickException("--path несовместим с --all")

    if list_clients:
        click.echo("Поддерживаемые клиенты (reviewer install <client>):")
        for c in inst.CLIENTS.values():
            tag = f" [{c.scope}]" if c.scope != "user" else ""
            skills = " +скилы" if c.skills_fn else ""
            click.echo(f"  {c.key:<15} {c.label}{tag}{skills}")
        return

    version = "" if no_latest else (pin or "latest")
    if all_clients:
        targets = [c for c in inst.detect_installed() if c.key != "claude-code"]
        if _shutil.which("claude"):
            targets.insert(0, inst.CLIENTS["claude-code"])
        if not targets:
            raise click.ClickException(
                "Не обнаружено установленных клиентов. Укажите явно: reviewer install <client> "
                "(список: reviewer install --list).")
        path_opt = None  # --path несовместим с --all
    elif client:
        key = client.lower()
        if key not in inst.CLIENTS:
            raise click.ClickException(
                f"Неизвестный клиент {client!r}. Список: reviewer install --list")
        targets = [inst.CLIENTS[key]]
    else:
        raise click.ClickException(
            "Укажите клиент (reviewer install <client>), либо --all / --list.")

    native_errors: list[str] = []
    codex_mcp_only = no_skills and any(c.key == "codex" for c in targets)
    claude_mcp_only = no_skills and any(c.key == "claude-code" for c in targets)
    tar_cache: list[tuple[bytes, str | None]] = []  # тарбол+ETag качаем один раз

    def _ensure_skills(c) -> None:
        if no_skills or c.skills_fn is None:
            return
        if not tar_cache:
            click.echo("  скилы: скачиваю с GitHub…")
            try:
                tar_cache.append(inst.fetch_skills_archive())
            except Exception as exc:  # noqa: BLE001 — fail-soft, MCP уже прописан
                click.echo(f"  скилы: пропуск (не скачать тарбол: {exc})")
                tar_cache.append((b"", None))
                return
        data, etag = tar_cache[0]
        if not data:
            return
        try:
            dest, names = inst.install_skills(c, tar_bytes=data, source_etag=etag)
            click.echo(f"  скилы: {len(names)} шт. → {dest}")
        except Exception as exc:  # noqa: BLE001
            click.echo(f"  скилы: пропуск ({exc})")

    for c in targets:
        if c.key == "claude-code":
            try:
                if path_opt:
                    raise click.ClickException(
                        "Claude Code global lifecycle несовместим с --path"
                    )
                if no_skills:
                    result = _run_claude_mcp_target(
                        dry_run=dry_run,
                        version=version,
                    )
                    _print_claude_mcp_result(result)
                else:
                    if no_latest or (pin is not None and pin != "latest"):
                        raise click.ClickException(
                            "Claude Code plugin использует версию из bundled manifest; "
                            "--pin и --no-latest доступны только с --no-skills"
                        )
                    result = _run_claude_target(dry_run=dry_run)
                    _print_claude_result(result)
                _apply_claude_allowlist(inst, dry_run=dry_run)
            except click.ClickException as exc:
                if not all_clients:
                    raise
                native_errors.append(f"Claude Code CLI: {exc.format_message()}")
            continue
        if c.key == "codex" and not no_skills:
            try:
                result = _run_codex_target(
                    include_mcp=True,
                    dry_run=dry_run,
                    version=version,
                    path_opt=path_opt,
                )
                _print_codex_result(result)
            except click.ClickException as exc:
                if not all_clients:
                    raise
                native_errors.append(f"Codex CLI: {exc.format_message()}")
            continue
        plan = inst.build_plan(c, version=version, path_override=path_opt)
        if dry_run:
            click.echo(f"# {c.label} → {plan.path}")
            click.echo(plan.content)
            if c.skills_fn and not no_skills:
                click.echo(f"# {c.label} скилы → {c.skills_fn(_platform.system())}")
            continue
        backup = inst.apply_plan(plan)
        if plan.already:
            status = "обновлена запись"
        elif plan.created:
            status = "создан конфиг"
        else:
            status = "добавлена запись"
        click.echo(f"✓ {c.label}: {status} → {plan.path}")
        if backup:
            click.echo(f"  бэкап: {backup}")
        if c.note:
            click.echo(f"  прим.: {c.note}")
        _ensure_skills(c)

    if native_errors:
        raise click.ClickException("; ".join(native_errors))
    if not dry_run and codex_mcp_only:
        click.echo("Codex MCP обновлён. Откройте New Chat/new CLI session; "
                   "в IDE выполните Reload Window.")
    if not dry_run and claude_mcp_only:
        click.echo("Claude Code MCP обновлён. Откройте New Chat/new CLI session; "
                   "в IDE выполните Reload Window.")
    if not dry_run:
        click.echo("Готово. Перезапустите клиент. Ключи: reviewer init && reviewer check.")


@dataclass(frozen=True)
class _GlobalInitPlan:
    path: Path
    content: str
    preview: str
    source: str
    action: str


def _render_init_preview(
    global_plan: _GlobalInitPlan | None,
    repo_plan: RepositoryConfigPlan | None,
) -> None:
    """Показать все target-планы до первой записи, не раскрывая env secrets."""
    click.echo("# reviewer init preview")
    if global_plan is not None:
        click.echo(f"\nfile: {global_plan.path}")
        click.echo(f"action: {global_plan.action}")
        click.echo(f"source: {global_plan.source}")
        click.echo(global_plan.preview)
    if repo_plan is not None:
        click.echo(f"\nfile: {repo_plan.path}")
        click.echo(f"action: {repo_plan.action}")
        click.echo(f"repo: {repo_plan.repo} ({repo_plan.repo_source})")
        click.echo(f"primary: {repo_plan.primary} ({repo_plan.primary_source})")
        click.echo(repo_plan.preview)


def _select_vcs_provider(inst, current: Mapping[str, str]) -> str:
    """Выбрать VCS с offline default из origin, затем env fallback."""
    from reviewer.services.repo_id import derive_vcs_from_remote

    root = repo_root(".")
    detected = derive_vcs_from_remote(remote_url(root or ".") or "")
    default = detected[0] if detected is not None else current.get("VCS_PROVIDER", "github")
    choices = tuple(inst.VCS_SETUPS)
    if default not in choices:
        default = "github"
    return click.prompt(
        "Выберите VCS provider",
        type=click.Choice(choices),
        default=default,
    )


def _prompt_vcs_provider(inst, spec, current: Mapping[str, str]) -> dict[str, str]:
    """Показать access contract и запросить поля только выбранного VCS."""
    click.echo(
        render_provider_access(
            label=spec.label,
            help_text=spec.help_text,
            help_url=spec.help_url,
            access=spec.access,
        )
    )
    if click.confirm(
        f"Открыть официальную инструкцию {spec.help_url}?",
        default=False,
    ):
        click.launch(spec.help_url)
    group = inst.EnvGroup(title=spec.label, fields=list(spec.credential_fields))
    return inst.prompt_groups([group], current=dict(current), yes=False)


@cli.command()
@click.option("--path", "path_opt", default=None,
              help="куда писать .env (по умолчанию ~/.config/rag-reviewer/.env)")
@click.option("--yes", "yes", is_flag=True,
              help="принять все дефолты без интерактива (CI-режим)")
@click.option("--dry-run", "dry_run", is_flag=True,
              help="показать безопасный preview без prompt, сети и записи файла")
@click.option(
    "--scope",
    type=click.Choice(("all", "global", "repo")),
    default="all",
    show_default=True,
    help="этапы настройки: global .env, repo branch config или оба",
)
@click.option("--repo", "repo_opt", default=None, help="owner/name для repo stage")
def init(
    path_opt: str | None,
    yes: bool,
    dry_run: bool,
    scope: str,
    repo_opt: str | None,
) -> None:
    """Настроить global .env и per-repo ветки с preview до записи."""
    import subprocess
    from reviewer import install as inst
    from reviewer.tasks.boards import setup as board_setup
    from reviewer.tasks.boards.errors import BoardProviderError
    from reviewer.tasks.boards.registry import default_board_registry

    run_global = scope in {"all", "global"}
    run_repo = scope in {"all", "repo"}
    interactive = not yes and not dry_run
    if run_repo and repo_opt is not None:
        try:
            repo_opt = _resolve_config_repo(repo_opt)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    global_plan: _GlobalInitPlan | None = None
    repo_plan: RepositoryConfigPlan | None = None
    settings: Settings | None = None
    try:
        if run_global:
            dest = Path(path_opt).expanduser() if path_opt else inst.default_env_path()
            current = inst.read_env(dest)
            wizard_keys = {field.key for group in inst.WIZARD_GROUPS for field in group.fields}
            groups = inst.WIZARD_GROUPS

            if interactive:
                click.echo(f"Настройка rag-reviewer: {dest}")
                click.echo("─" * 52)
                vcs_group = next(group for group in groups if group.title == "VCS")
                board_group = next(group for group in groups if group.title == "Доска задач")
                common_fields = inst.common_board_env_fields(board_group)
                values = inst.prompt_groups(
                    [
                        (
                            inst.EnvGroup(
                                title=board_group.title,
                                fields=common_fields,
                                optional=True,
                            )
                            if group is board_group
                            else group
                        )
                        for group in groups
                        if group is not vcs_group
                    ],
                    current=current,
                    yes=False,
                )
                for group in (vcs_group, board_group):
                    for field in group.fields:
                        values.setdefault(
                            field.key,
                            current.get(field.key, "") or field.default,
                        )

                if click.confirm("\nПодключить VCS provider?", default=True):
                    vcs_type = _select_vcs_provider(inst, current)
                    values["VCS_PROVIDER"] = vcs_type
                    values.update(
                        _prompt_vcs_provider(inst, inst.VCS_SETUPS[vcs_type], current)
                    )

                if click.confirm("\nПодключить provider доски задач?", default=False):
                    registry = default_board_registry()
                    io = board_setup.ClickSetupIO()
                    board_type = io.choose(
                        "Выберите provider доски",
                        [
                            board_setup.SetupChoice(
                                value=registered_type,
                                label=registry.get(registered_type).setup.label,
                            )
                            for registered_type in registry.registered_types()
                        ],
                    )
                    while True:
                        try:
                            values.update(
                                board_setup.configure_board_provider(registry.get(board_type), io)
                            )
                            break
                        except BoardProviderError as error:
                            click.echo(f"✗ {board_type}: {error.message}")
                            if error.hint:
                                click.echo(f"  {error.hint}")
                            if not click.confirm(
                                "Исправить credentials и повторить?",
                                default=True,
                            ):
                                click.echo(
                                    "Provider пропущен; непроверенные credentials не сохранены."
                                )
                                break
            else:
                values = inst.prompt_groups(groups, current=current, yes=True)

            extra = {key: value for key, value in current.items() if key not in wizard_keys}
            content = inst.render_env(values, extra)
            if dest.is_file() and dest.read_text(encoding="utf-8") == content:
                action = "noop"
            else:
                action = "update" if dest.is_file() else "create"
            global_plan = _GlobalInitPlan(
                path=dest,
                content=content,
                preview=inst.render_env_preview(values, extra),
                source=f"existing:{dest}" if dest.is_file() else "wizard defaults/input",
                action=action,
            )

        if run_repo:
            settings = Settings(_env_file=None) if scope == "repo" else Settings()
            detection = detect_repository(
                ".",
                repo_opt,
                settings=settings,
            )
            if (
                detection is None
                and interactive
                and repo_opt is None
                and repo_root(".") is not None
            ):
                entered_repo = click.prompt(
                    "Repository (owner/name)",
                    default="",
                    show_default=False,
                ).strip()
                if entered_repo:
                    detection = detect_repository(
                        ".",
                        entered_repo,
                        settings=settings,
                    )

            if detection is None:
                message = (
                    "repo не определён: запустите из git repository с распознаваемым origin "
                    "или передайте --repo owner/name"
                )
                if scope == "repo":
                    raise click.ClickException(message)
                click.echo(f"repo stage пропущен: {message}")
            else:
                initial_plan = plan_repository_config(
                    detection,
                    settings=settings,
                )
                if initial_plan.action == "noop" or not interactive:
                    repo_plan = initial_plan
                else:
                    click.echo(
                        f"\nОбнаружен repo {detection.repo} ({detection.repo_source})"
                    )
                    click.echo(
                        f"Primary candidate: {detection.primary} "
                        f"({detection.primary_source})"
                    )
                    primary = click.prompt(
                        "Primary branch",
                        default=initial_plan.primary,
                    ).strip()
                    default_index = (
                        initial_plan.index
                        if primary == initial_plan.primary
                        else (primary,)
                    )
                    while True:
                        raw_index = click.prompt(
                            "Index branches (CSV)",
                            default=",".join(default_index),
                        )
                        try:
                            index = parse_branch_csv(raw_index, primary)
                        except ValueError as exc:
                            click.echo(f"Некорректные ветки: {exc}")
                            continue
                        break
                    repo_plan = plan_repository_config(
                        detection,
                        settings=settings,
                        primary=(primary if primary != initial_plan.primary else None),
                        index=index,
                    )

        _render_init_preview(global_plan, repo_plan)
        if dry_run:
            return
        if interactive and not click.confirm(
            "\nЗаписать показанные изменения?",
            default=True,
        ):
            click.echo("Отменено — файлы не изменены.")
            return
    except click.Abort:
        click.echo("\nОтменено — файлы не изменены.")
        return
    except (HomeConfigError, yaml.YAMLError) as exc:
        raise click.ClickException(_config_error_message(exc)) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if global_plan is not None:
        try:
            if global_plan.action != "noop":
                global_plan.path.parent.mkdir(parents=True, exist_ok=True)
                global_plan.path.write_text(global_plan.content, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — error не должен раскрыть env content
            raise click.ClickException(
                f"Не удалось записать {global_plan.path}: {type(exc).__name__}"
            ) from None
        if global_plan.action == "noop":
            click.echo(f"\n✓ Global config: noop → {global_plan.path}")
        else:
            click.echo(f"\n✓ Записан {global_plan.path} ({global_plan.action})")

    if repo_plan is not None:
        try:
            apply_repository_config(repo_plan)
        except Exception as exc:  # noqa: BLE001 — не эхоить потенциальный payload исключения
            raise click.ClickException(
                f"Не удалось записать {repo_plan.path}: {type(exc).__name__}"
            ) from None
        click.echo(f"✓ Repo config: {repo_plan.action} → {repo_plan.path}")
        assert settings is not None
        try:
            effective = resolve_repo_branches(repo_plan.repo, settings=settings)
        except (HomeConfigError, yaml.YAMLError) as exc:
            raise click.ClickException(_config_error_message(exc)) from exc
        _render_config_report(_branches_report(effective))
        click.echo(f"Полный отчёт: reviewer config show --repo {repo_plan.repo}")

    if run_global and interactive and click.confirm(
        "\nЗапустить reviewer check сейчас?",
        default=True,
    ):
        subprocess.run(["reviewer", "check"], check=False)
    elif run_global and interactive:
        click.echo("Запустите: reviewer check")
    elif run_global:
        click.echo("Готово. Запустите: reviewer check")

    if run_global and interactive and _shutil.which("codex") and click.confirm(
        "\nУстановить или обновить rag-reviewer для Codex?", default=True
    ):
        result = _run_codex_target(include_mcp=True, dry_run=False)
        _print_codex_result(result)


def _has_detected_clients() -> bool:
    from reviewer import install as inst

    return bool(inst.detect_installed()) or _shutil.which("claude") is not None


def _install_detected_clients(ctx: click.Context) -> None:
    ctx.invoke(
        install,
        client=None,
        all_clients=True,
        list_clients=False,
        path_opt=None,
        pin=None,
        no_latest=False,
        no_skills=False,
        dry_run=False,
    )


def _refresh_update_artifacts(ctx: click.Context) -> None:
    errors: list[str] = []
    try:
        compose = sync_compose_file(download_compose())
        statuses = {
            "created": "создан",
            "adopted": "принят под управление",
            "current": "актуален",
            "updated": "обновлён",
        }
        if compose.action == "preserved":
            click.echo(
                "⚠ Compose не перезаписан: обнаружены пользовательские изменения в "
                f"{compose.path}"
            )
        else:
            click.echo(f"✓ Compose {statuses[compose.action]}: {compose.path}")
    except Exception as exc:  # noqa: BLE001 - independent phases must continue
        errors.append(f"Compose: {type(exc).__name__}")
    if _has_detected_clients():
        try:
            _install_detected_clients(ctx)
        except click.ClickException as exc:
            errors.append(f"Integrations: {exc.format_message()}")
        except Exception as exc:  # noqa: BLE001 - do not expose profile paths or config content
            errors.append(f"Integrations: {type(exc).__name__}")
    else:
        click.echo("AI-клиенты не обнаружены; integration refresh пропущен.")
    if errors:
        raise click.ClickException("Обновление завершено частично: " + "; ".join(errors))
    click.echo(
        "Обновление завершено. Откройте New Chat/new CLI session; "
        "в IDE — Reload Window."
    )


@cli.command()
@click.option(
    "--upgrade-tool",
    is_flag=True,
    help="обновить существующую persistent uv tool installation из latest uvx",
)
@click.option("--refresh-artifacts", is_flag=True, hidden=True)
@click.pass_context
def update(ctx: click.Context, upgrade_tool: bool, refresh_artifacts: bool) -> None:
    """Обновить package, AI-client integrations, скилы и Compose-файл."""
    if refresh_artifacts:
        _refresh_update_artifacts(ctx)
        return
    installation = detect_installation()
    if installation.mode is InstallMode.EDITABLE:
        click.echo(f"Режим: dev (editable) | Версия: {installation.current}")
        click.echo("Для обновления: git pull && pip install -e .")
        _refresh_update_artifacts(ctx)
        return

    mode = "uv tool (постоянная)" if installation.mode is InstallMode.UV_TOOL else "uvx (временная)"
    click.echo(f"Режим: {mode} | Версия: {installation.current}")

    version_check = check_latest(installation)
    if version_check.latest is None:
        click.echo("Не удалось получить информацию с PyPI. Проверьте сеть.")
        _refresh_update_artifacts(ctx)
        return

    if not version_check.current_valid:
        click.echo("Не удалось определить корректную текущую версию. Обновление не запущено.")
        _refresh_update_artifacts(ctx)
        return

    if not version_check.update_available and installation.current != "?":
        click.echo(f"Версия актуальна: {installation.current}.")
        if installation.mode is not InstallMode.UV_TOOL:
            click.echo("MCP-сервер обновляется автоматически — в конфиге клиента прописан @latest.")
        if upgrade_tool and installation.mode is InstallMode.UVX:
            result = upgrade_uv_tool(installation)
            if result.returncode != 0:
                raise click.ClickException(f"Ошибка uv tool upgrade: {result.stderr}")
            click.echo("✓ Persistent uv tool installation обновлена.")
        _refresh_update_artifacts(ctx)
        return

    click.echo(f"Доступна новая версия: {installation.current} → {version_check.latest}")

    if installation.mode is InstallMode.UV_TOOL:
        if installation.uv_executable is None:
            click.echo("uv не найден в PATH. Запустите: uv tool upgrade rag-reviewer")
            return
        result = upgrade_uv_tool(installation)
        if result.returncode == 0:
            click.echo("✓ Python package обновлён.")
            fresh = run_fresh_artifact_refresh()
            if fresh.stdout:
                click.echo(fresh.stdout, nl=not fresh.stdout.endswith("\n"))
            if fresh.returncode != 0:
                detail = fresh.stderr.strip() or "неизвестная ошибка"
                raise click.ClickException(
                    f"Пакет обновлён, artifact refresh завершился ошибкой: {detail}"
                )
        else:
            raise click.ClickException(f"Ошибка uv tool upgrade: {result.stderr}")
    else:
        # uvx: MCP-сервер обновится сам при следующем запуске (конфиг содержит @latest).
        # Для CLI-команд достаточно использовать @latest явно.
        click.echo(
            "MCP-сервер подхватит обновление автоматически при следующем запуске (@latest в конфиге).\n"
            "Для CLI: uvx --from rag-reviewer@latest reviewer <команда>"
        )
        if upgrade_tool:
            result = upgrade_uv_tool(installation)
            if result.returncode != 0:
                raise click.ClickException(f"Ошибка uv tool upgrade: {result.stderr}")
            click.echo("✓ Persistent uv tool installation обновлена.")
        _refresh_update_artifacts(ctx)


@cli.command("install-skills")
@click.argument("client", required=False)
@click.option("--all", "all_clients", is_flag=True,
              help="поставить во все обнаруженные клиенты со скилами")
@click.option("--list", "list_clients", is_flag=True,
              help="показать клиенты с поддержкой файловых скилов")
@click.option("--path", "path_opt", default=None,
              help="переопределить каталог скилов")
@click.option("--dry-run", is_flag=True,
              help="показать plugin plan без записи и сети")
def install_skills(client: str | None, all_clients: bool,
                   list_clients: bool, path_opt: str | None, dry_run: bool) -> None:
    """Установить скилы (review-pr, solve-task и др.) в каталог клиента."""
    from pathlib import Path
    from reviewer import install as inst

    capable = [c for c in inst.CLIENTS.values() if c.skills_fn]
    if list_clients:
        click.echo("Клиенты со скилами:")
        for c in capable:
            click.echo(f"  {c.key:<15} {c.label} → {c.skills_fn(_platform.system())}")
        click.echo("  codex           Codex CLI → plugin marketplace")
        return

    if all_clients:
        targets = [
            c for c in inst.detect_installed()
            if c.skills_fn is not None or c.key == "codex"
        ]
        if not targets:
            raise click.ClickException(
                "Не обнаружено клиентов со скилами. Укажите явно или см. --list.")
    elif client:
        key = client.lower()
        if key not in inst.CLIENTS:
            raise click.ClickException(
                f"Неизвестный клиент {client!r}. Список: reviewer install-skills --list")
        targets = [inst.CLIENTS[key]]
    else:
        raise click.ClickException(
            "Укажите клиент (reviewer install-skills <client>), либо --all / --list.")

    if path_opt and any(c.key == "codex" for c in targets):
        raise click.ClickException("Codex plugin не поддерживает --path")
    if dry_run and any(c.key != "codex" for c in targets):
        raise click.ClickException("install-skills --dry-run поддерживается только для codex")

    for c in [target for target in targets if target.key == "codex"]:
        result = _run_codex_target(include_mcp=False, dry_run=dry_run)
        _print_codex_result(result)

    file_targets = [target for target in targets if target.key != "codex"]
    if not file_targets:
        return
    click.echo("Скачиваю скилы с GitHub…")
    tar, etag = inst.fetch_skills_archive()
    for c in file_targets:
        if c.skills_fn is None:
            raise click.ClickException(f"{c.label}: файловые скилы не поддерживаются")
        dest = Path(path_opt).expanduser() if path_opt else c.skills_fn(_platform.system())
        names = inst.extract_skills(tar, dest)
        inst.stamp_skills_dir(dest, source_etag=etag)
        click.echo(f"✓ {c.label}: {len(names)} скилов → {dest}")
        if c.note:
            click.echo(f"  прим.: {c.note}")
