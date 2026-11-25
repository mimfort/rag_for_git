from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import platform as _platform
import re
import shlex
import shutil as _shutil
from typing import TYPE_CHECKING, TextIO

import click
import httpx
import psycopg
import yaml

from reviewer.config.settings import Settings
from reviewer.app import build_components
from reviewer.config.branches import migrate_repo_branches, resolve_repo_branches
from reviewer.config.committed import (
    SOURCE_LOCAL,
    CommittedLayerFetcher,
    worktree_drift,
)
from reviewer.config.layers import (
    HomeConfigError,
    build_config_report,
    leaves_of,
    migrate_repo_config,
    resolve_policy_data,
    source_of,
)
from reviewer.config.onboarding import (
    RepositoryConfigPlan,
    RepositoryDetection,
    apply_repository_config,
    detect_repository,
    parse_branch_csv,
    plan_repository_config,
)
from reviewer.config.provider_access import render_provider_access
from reviewer.compose_lifecycle import (
    COMPOSE_PROJECT,
    ComposeResult,
    ComposeStatus,
    start_services,
    stop_services,
)
from reviewer.gitutil import file_at_ref, list_python_files, repo_root, rev_parse, remote_url
from reviewer.graph.backend import build_code_graph
from reviewer.graph.metrics import (
    count_edges_by_rel,
    detect_edge_regression,
    format_edge_counts,
)
from reviewer.graph.store import GraphStore
from reviewer.index.freshness import update_base
from reviewer.index.pathfilter import is_ignored
from reviewer.index.refs import base_ref
from reviewer.metrics.brief_quality import ground_truth
from reviewer.metrics.brief_quality.config import BriefQualityConfig
from reviewer.metrics.brief_quality.corpus import measure_corpus
from reviewer.metrics.client_usage import SOURCES as USAGE_SOURCES
from reviewer.metrics.client_usage import measure_usage
from reviewer.policy.policy import ReviewPolicy
from reviewer.index.store import ChunkStore, ConcurrentBaseUpdateError
from reviewer.index.summary_store import SummaryStore
from reviewer.mcp.session_store import SessionStore
from reviewer.services.branch import resolve_branch
from reviewer.services.gc import purge_orphaned_overlays
from reviewer.services.review_service import ReviewService
from reviewer.services.status import build_status_report, render_status, render_status_json
from reviewer.storage_health import (
    DETAIL_AUTH_FAILED, DETAIL_MISSING_DATABASE, DETAIL_POOL_EXHAUSTED,
    classify_storage_failure, mask_endpoint,
)
from reviewer.tasks.boards.errors import sanitize_provider_text
from reviewer.update_lifecycle import (
    download_compose,
    find_uv_tool_python,
    run_fresh_artifact_refresh,
    sync_compose_file,
)
from reviewer.versioning import InstallMode, check_latest, detect_installation, upgrade_uv_tool
from reviewer.web.history import ReviewHistory
from reviewer.web.serve import DEFAULT_HOST, DEFAULT_PORT

if TYPE_CHECKING:
    from reviewer.install_claude import ClaudeInstallResult
    from reviewer.install_codex import CodexInstallResult

log = logging.getLogger(__name__)


_SUBSTITUTED = "env:DEFAULT_REPO"

# Сообщения CLI отдельны от формулировок MCP-payload: у них разные адресаты —
# терминал оператора против брифа, который собирает LLM.
_STORAGE_DETAIL_MESSAGES = {
    DETAIL_AUTH_FAILED: "хранилище отвергло учётные данные — проверьте пароль в .env",
    DETAIL_MISSING_DATABASE: "базы данных не существует — проверьте имя базы в PG_DSN",
    DETAIL_POOL_EXHAUSTED: (
        "свободных соединений в пуле не осталось — поднимите PG_POOL_MAX_SIZE"
        " или снизьте параллелизм"
    ),
}


def _resolve_repo(repo_opt: str | None, path: str, settings):
    """Резолв repo-тега с происхождением: --repo → git remote → DEFAULT_REPO → ошибка.

    Возвращает `RepoResolution(repo, source)`: источник нужен вызывающим, чтобы
    отличить имя, выведенное из текущего клона, от подставленного из env.
    """
    from reviewer.services.repo_id import resolve_repo_id
    resolution = resolve_repo_id(repo_opt, remote_url(path), settings.default_repo)
    if resolution is None:
        raise click.ClickException(
            "Не удалось определить repo: укажите --repo owner/name "
            "(или задайте DEFAULT_REPO в .env)")
    return resolution


def _substitution_note(path: str, repo: str) -> str:
    """Текст про подстановку имени из env вместо вывода из origin."""
    origin = remote_url(path) or "отсутствует"
    return (f"repo не выведен из git remote origin клона {path} (origin: {origin}) — "
            f"подставлено DEFAULT_REPO={repo}")


@click.group()
@click.version_option(package_name="rag-reviewer", prog_name="reviewer")
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


def _echo_source_leaves(sources: Mapping[str, str], key: str) -> None:
    """`source`: одна строка, если все листья указывают на один слой.

    Если слои расходятся — общая строка была бы неразличимой ложью
    («какой-то один слой»), поэтому заголовок ``mixed`` и строка на лист.
    `source_of` — общий с `mcp/service.py` хелпер (`reviewer/config/layers.py`):
    диагностика верхнего ключа по листовым `sources` не должна дублироваться
    литералом на каждом потребителе (см. фикс summary_cluster_depth_overrides).
    """
    leaves = leaves_of(sources, key)
    if not leaves:
        return
    resolved = source_of(sources, key)
    if resolved != "mixed":
        click.echo(f"  source: {resolved}")
        return
    click.echo("  source: mixed")
    for path in sorted(leaves):
        click.echo(f"    {path}: {leaves[path]}")


def _echo_shadowed_leaves(key: str, leaves: Mapping[str, list[str]]) -> None:
    """`shadowed`: одна строка ТОЛЬКО когда затенён сам верхний ключ.

    Два листа одного ключа обычно затенены ОДНИМ слоем — общая свёртка (как у
    `source`) вернула бы неразличимое `shadowed: .review.yml` и скрыла бы
    именно то, что PRI-260 делает видимым: какая подсекция затенена, а не
    затенён ли ключ целиком.
    """
    if not leaves:
        return
    if set(leaves) == {key}:
        click.echo(f"  shadowed: {', '.join(leaves[key])}")
        return
    click.echo("  shadowed:")
    for path in sorted(leaves):
        click.echo(f"    {path}: {', '.join(leaves[path])}")


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
    committed_source = report.get("committed_source")
    if committed_source is not None:
        # Способ чтения коммиченного слоя (PRI-235): git-blob = коммиченный
        # объект git из локального клона без сети, vcs = через API хостинга.
        click.echo(f"committed: {committed_source}")
    drift = report.get("worktree_drift")
    if isinstance(drift, Mapping) and drift["status"] not in ("clean", "unknown"):
        # «Правка есть, но её не видно» — исходный дефект PRI-260, поэтому
        # причина пропуска сравнения (`ref_not_head`, `absent_*`) называется
        # явно. Молчит только `unknown`: сбой самой диагностики — fail-soft,
        # и строка о нём сказала бы о состоянии политики ровно ничего.
        click.echo(f"worktree_drift: {drift['status']}")
        for key in drift["keys"]:
            click.echo(f"  {key}")
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
        _echo_source_leaves(sources, key)
        _echo_shadowed_leaves(key, leaves_of(shadowed, key))
    for warning in report["warnings"]:
        click.echo(f"warning: {warning}")
    for item in report.get("skipped") or ():
        assert isinstance(item, Mapping)
        click.echo(
            f"skipped: {item['layer']} (repo={item['repo']}, ref={item['ref']}, "
            f"category={item['category']}, transport={item['transport']}, "
            f"http_status={item['http_status']})"
        )


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


def _resolve_clone_path(explicit: str | None) -> str:
    """Кандидат на локальный клон для CLI: --path, иначе текущий каталог.

    Путь из индекса (`repo_clone`) здесь намеренно не читается: у CLI есть cwd —
    более прямой и всегда актуальный кандидат, а поход в Postgres ради пути
    сделал бы диагностику политики зависимой от живой БД. Запись в индексе нужна
    MCP, у которого рабочего каталога нет.

    Годность кандидата (git-репо + сверка remote) проверяет CommittedLayerFetcher:
    посторонний клон в cwd отсекается сверкой remote, а не доверием к каталогу.
    """
    return explicit or "."


def _resolve_config_repo(repo: str) -> str:
    from reviewer.services.repo_id import normalize_repo

    return normalize_repo(repo)


@config_group.command("show")
@click.option("--repo", required=True, help="owner/name репозитория")
@click.option("--branch", default=None, help="ветка policy; по умолчанию первичная")
@click.option("--path", "clone_path", default=None,
              help="путь к локальному клону: коммиченный .review.yml читается "
                   "из него без сети (по умолчанию — текущий каталог)")
@click.option("--json", "as_json", is_flag=True, default=False)
def config_show(repo: str, branch: str | None, clone_path: str | None,
                as_json: bool) -> None:
    """Показать effective policy и происхождение её верхних ключей.

    Секция веток печатается всегда, даже если VCS недоступен (нет сети, нет
    токена) — резолв веток чисто локальный и не зависит от policy-части.
    Недоступный коммиченный `.review.yml` тоже больше не обнуляет вывод:
    печатаются домашние слои, а сам пропуск попадает в `skipped` (PRI-234).
    Код возврата при этом остаётся ненулевым.

    Коммиченный слой читается из локального клона, если он найден и подходит
    репозиторию, и только иначе — через API хостинга (PRI-235). Способ чтения
    печатается строкой `committed:`.
    """
    try:
        with _config_context(repo, branch) as ctx:
            settings, _components, vcs, repo_id, ref, branches, vcs_error = ctx
            payload = _branches_report(branches)

            def vcs_fetch_factory():
                # Недоступный VCS-провайдер — такой же сбой доставки слоя, как
                # сетевая ошибка: он уходит в общий механизм skipped, а не в
                # отдельную ветку policy_error (PRI-234).
                if vcs_error is not None:
                    raise vcs_error
                return lambda selected_ref: vcs.get_file_at_ref(".review.yml", selected_ref)

            fetch_committed = CommittedLayerFetcher(
                repo_id,
                clone_path=_resolve_clone_path(clone_path),
                vcs_fetch_factory=vcs_fetch_factory,
            )

            try:
                data, meta = resolve_policy_data(
                    repo_id,
                    ref,
                    fetch_committed,
                    strict_home=True,
                )
                payload.update(build_config_report(
                    repo_id, ref, settings, data, meta,
                    committed_source=fetch_committed.source,
                ))
                clone_root = fetch_committed.clone_root
                if clone_root is not None and fetch_committed.source == SOURCE_LOCAL:
                    # Дрейф сравнивает рабочее дерево с тем, что реально
                    # прочитано. Если слой пришёл через API хостинга (ветка в
                    # клоне не выкачана), сравнение было бы не про показанную
                    # эффективную политику.
                    payload["worktree_drift"] = worktree_drift(
                        clone_root, ref
                    ).as_dict()
            except (HomeConfigError, yaml.YAMLError) as exc:
                # Тот же санитайзер, что и у остальных config-команд: не эхоить
                # сырой YAML/normalization payload исключения.
                payload["policy_error"] = _config_error_message(exc)
            except Exception as exc:  # noqa: BLE001 — диагностика не должна падать целиком
                # Прочие сбои — без текста исключения: он может содержать
                # URL/токены из VCS-клиента.
                payload["policy_error"] = type(exc).__name__
    except (HomeConfigError, yaml.YAMLError) as exc:
        raise click.ClickException(_config_error_message(exc)) from exc
    if as_json:
        click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        _render_config_report(payload)
    if "policy_error" in payload or payload.get("skipped"):
        # Эффективная политика неполная (или не собралась вовсе): вывод уже
        # напечатан, но код возврата обязан сигналить внешним скриптам
        # (`config show; echo $?`).
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
@click.option("--env-file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Проверить выбранный .env.")
def check(board_project_values: tuple[str, ...], env_file: Path | None = None) -> None:
    """Проверить готовность окружения и настроенных внешних providers."""
    s = Settings(_env_file=env_file) if env_file is not None else Settings()
    board_projects = _parse_board_projects(board_project_values)
    failed = False
    storage_remedy_hint: str | None = None

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
        click.echo(f"✓ Postgres ({mask_endpoint(s.pg_dsn)}): подключение и таблица chunks — OK")
        try:
            store.check_vector_roundtrip()
            click.echo("✓ pgvector: вектор читается из БД в list[float] — OK")
        except Exception as e:
            # Отдельная ветка, а не общий except Postgres: подключение здесь
            # исправно, сломана совместимость типа вектора — это бьёт только по
            # ревью PR, и подсказка должна вести к версии pgvector (PRI-236).
            click.echo(
                f"✗ pgvector: вектор из БД не читается в list[float] ({e}) — "
                "ревью PR работать не будет; проверьте версию пакета pgvector"
            )
            failed = True
    except Exception as e:
        err = str(e)
        diagnosis = classify_storage_failure(e, s.pg_dsn)
        if diagnosis.detail:
            # Первым делом: «database ... does not exist» несут и несуществующая
            # БД, и отсутствующая таблица chunks, но лечатся они разным.
            click.echo(f"✗ Postgres: {_STORAGE_DETAIL_MESSAGES[diagnosis.detail]}")
        elif "chunks" in err or "does not exist" in err:
            click.echo(
                "✗ Postgres: схема не инициализирована — выполните reviewer index"
            )
        else:
            click.echo(f"✗ Postgres: {err}")
        failed = True
        storage_remedy_hint = storage_remedy_hint or diagnosis.remedy
    finally:
        if store is not None:
            store.close()

    # 3. Neo4j
    try:
        graph = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password,
                           connection_timeout=s.neo4j_connection_timeout,
                           acquisition_timeout=s.neo4j_acquisition_timeout,
                           max_retry_time=s.neo4j_max_retry_time)
        try:
            graph._driver.verify_connectivity()
            click.echo(f"✓ Neo4j ({mask_endpoint(s.neo4j_uri)}): подключение — OK")
        finally:
            graph.close()
    except Exception as e:
        click.echo(f"✗ Neo4j: {e}")
        failed = True
        storage_remedy_hint = (
            storage_remedy_hint or classify_storage_failure(e, s.neo4j_uri).remedy
        )

    if storage_remedy_hint:
        click.echo(
            f"  Подсказка: локальные хранилища не отвечают — запустите {storage_remedy_hint}"
        )

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


def _report_compose_failure(result: ComposeResult) -> None:
    """Печатает русское объяснение неуспеха и завершает процесс кодом 1."""
    if result.status is ComposeStatus.COMPOSE_MISSING:
        click.echo(f"✗ {result.compose_path} не найден — выполните reviewer update")
    elif result.status is ComposeStatus.DOCKER_MISSING:
        click.echo("✗ docker не найден в PATH — установите Docker")
    elif result.status is ComposeStatus.DAEMON_UNAVAILABLE:
        click.echo(
            "✗ docker установлен, но демон не отвечает — запустите Docker и повторите"
        )
    else:
        click.echo(f"✗ docker compose завершился с кодом {result.returncode}")
        if result.stderr.strip():
            click.echo(result.stderr.strip())
    raise SystemExit(1)


@cli.command()
def start() -> None:
    """Запустить локальную инфраструктуру (ParadeDB + Neo4j)."""
    click.echo(
        f"Запускаю инфраструктуру (проект {COMPOSE_PROJECT}); первая загрузка "
        "образов может занять несколько минут…"
    )
    result = start_services()
    if result.status is ComposeStatus.OK:
        click.echo(
            f"✓ Инфраструктура запущена (проект {COMPOSE_PROJECT}): ParadeDB, Neo4j"
        )
        return
    _report_compose_failure(result)


@cli.command()
def stop() -> None:
    """Остановить локальную инфраструктуру, сохранив тома и индекс."""
    click.echo(f"Останавливаю инфраструктуру (проект {COMPOSE_PROJECT})…")
    result = stop_services()
    if result.status is ComposeStatus.OK:
        click.echo("✓ Инфраструктура остановлена; тома и индекс сохранены")
        return
    _report_compose_failure(result)


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
    resolution = _resolve_repo(repo_tag, repo, s)
    if resolution.source == _SUBSTITUTED:
        # Fail-closed: индекс под чужим тегом обнаруживается только по странной
        # выдаче поиска, поэтому лучше отказаться до единой записи в хранилища.
        raise click.ClickException(
            _substitution_note(repo, resolution.repo)
            + "; укажите --repo owner/name или почините origin")
    repo_id = resolution.repo
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
        expected_meta = c.store.get_index_meta_row(repo_id, bref)
        sha = rev_parse(repo, f"{ref}^{{commit}}")
        files = list_python_files(repo, sha)
        # strict_committed: неполный paths.ignore залил бы в индекс файлы,
        # которые репозиторий исключил; фетчер здесь локальный (file_at_ref),
        # его сбой означает битый клон, а не недоступную сеть.
        policy_data, policy_meta = resolve_policy_data(
            repo_id,
            ref,
            lambda _: file_at_ref(repo, ".review.yml", sha),
            strict_committed=True,
        )
        policy = ReviewPolicy.load_data(s, policy_data)
        ignore = policy.ignore
        for warning in policy_meta.warnings:
            log.warning("Слой policy пропущен: %s", warning)
        if ignore:
            files = [f for f in files if not is_ignored(f, ignore)]
        try:
            update_base(
                c.store, c.embedder, repo_id, branch, files,
                read=lambda p: file_at_ref(repo, p, sha), ignore=ignore,
                indexed_sha=sha, expected_meta=expected_meta, full_snapshot=True,
            )
        except ConcurrentBaseUpdateError as exc:
            raise click.ClickException(str(exc)) from exc
        # Платформа VCS репо (PRI-133): auto-derive из git remote локального
        # клона → repo_vcs. Читается при ревью (API-only) для выбора провайдера.
        from reviewer.services.repo_id import derive_vcs_from_remote
        vcs = derive_vcs_from_remote(remote_url(repo) or "")
        if vcs:
            c.store.set_repo_vcs(repo_id, vcs[0], vcs[1])
            click.echo(f"VCS: {vcs[0]}{(' @ ' + vcs[1]) if vcs[1] else ''}")
        # Путь к клону (PRI-235): индексация и так идёт из клона, а MCP потом
        # читает коммиченный .review.yml локально вместо API хостинга.
        # Fail-soft: запись пути — побочный факт, ронять индексацию она не вправе.
        clone_root = repo_root(repo)
        if clone_root:
            try:
                c.store.set_repo_clone(repo_id, clone_root)
            except Exception:  # noqa: BLE001 — индекс уже построен, путь вторичен
                log.warning("Не удалось записать путь к клону для %s", repo_id)
        # --- граф кода (в рамках ветки) ---
        src_by_path = {p: file_at_ref(repo, p, sha) for p in files}
        src_by_path = {p: v for p, v in src_by_path.items() if v is not None}
        # Предыдущий замер полноты графа читается ДО перестройки: сравнивать
        # нужно с тем, что стояло в индексе этой ветки раньше (PRI-252).
        # Fail-soft: недоступный замер значит «сравнивать не с чем», а не отказ.
        try:
            prev_edges = c.store.get_graph_edge_counts(repo_id, bref)
        except Exception:  # noqa: BLE001 — диагностика вторична к индексации
            log.warning("Не удалось прочитать предыдущие счётчики рёбер для %s", repo_id)
            prev_edges = None
        gnodes, gedges, backend = build_code_graph(
            repo, sha, files, src_by_path, s.graph_backend,
        )
        c.graph.init_schema()
        c.graph.clear(repo_id, branch=branch)   # rebuild только этой ветки репо
        c.graph.upsert_nodes(repo_id, list(gnodes), branch=branch)
        c.graph.upsert_edges(repo_id, gedges, branch=branch)
        edge_counts = count_edges_by_rel(gedges)
        try:
            c.store.set_graph_edge_counts(repo_id, bref, edge_counts)
        except Exception:  # noqa: BLE001 — граф уже записан, счётчики вторичны
            log.warning("Не удалось записать счётчики рёбер для %s", repo_id)
        click.echo(
            f"Проиндексировано [{repo_id}@{branch}] файлов: {len(files)} @ {sha[:7]}; "
            f"граф [{backend}]: узлов {len(gnodes)}, рёбер {len(gedges)} "
            f"({format_edge_counts(edge_counts)})"
        )
        for message in detect_edge_regression(prev_edges, edge_counts):
            click.echo(f"⚠ Просадка полноты графа против предыдущего индекса ветки: {message}")
            log.warning("Просадка полноты графа [%s@%s]: %s", repo_id, branch, message)
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
    resolution = _resolve_repo(repo_tag, ".", s)
    if resolution.source == _SUBSTITUTED:
        # Fail-open: миграция ничего не индексирует, но мигрировать чужой тег
        # молча она тоже не должна.
        click.echo("⚠ " + _substitution_note(".", resolution.repo), err=True)
    repo_id = resolution.repo
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
    resolution = _resolve_repo(repo_tag, path, s)
    repo = resolution.repo
    try:
        repo_branches = resolve_repo_branches(repo, settings=s)
    except HomeConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    branches = [branch_opt] if branch_opt else list(repo_branches.index)
    store = ChunkStore(s.pg_dsn, min_size=s.pg_pool_min_size, max_size=s.pg_pool_max_size)
    graph = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password,
                       connection_timeout=s.neo4j_connection_timeout,
                       acquisition_timeout=s.neo4j_acquisition_timeout,
                       max_retry_time=s.neo4j_max_retry_time)
    summary_store = SummaryStore(s.pg_dsn, min_size=s.pg_pool_min_size,
                                 max_size=s.pg_pool_max_size)
    try:
        report = build_status_report(store, graph, repo, branches, path,
                                     summary_store=summary_store,
                                     repo_source=resolution.source)
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


def _render_measure(summary: Mapping[str, object]) -> str:
    """Человекочитаемый отчёт по сводке `measure_corpus` (для click.echo)."""
    fixed = {"briefs", "skipped_no_key", "skipped_no_merges", "rows", "write_failed", "diff_failed"}
    lines = [
        f"Брифов обработано: {summary['briefs']}",
        f"Строк записано: {summary['rows']}",
        f"Пропущено без ключа задачи: {summary['skipped_no_key']}",
        f"Пропущено без PR-мержей: {summary['skipped_no_merges']}",
    ]
    if summary.get("write_failed"):
        lines.append(f"Сбоев записи в БД: {summary['write_failed']}")
    if summary.get("diff_failed"):
        lines.append(f"Сбоев чтения Git diff: {summary['diff_failed']}")
    statuses = {k: v for k, v in summary.items() if k not in fixed}
    if statuses:
        lines.append("По статусам:")
        lines.extend(f"  {status}: {statuses[status]}" for status in sorted(statuses))
    return "\n".join(lines)


@cli.command("brief-provenance")
@click.argument("brief", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--path", default=".", help="Локальный клон")
@click.option("--indexed-sha", default=None, help="Сохранить ссылки к указанному снимку")
@click.option("--repo", "repo_tag", default=None, help="Репозиторий из предполёта")
@click.option("--branch", default=None, help="Ветка из предполёта")
def brief_provenance(
    brief: Path, path: str, indexed_sha: str | None, repo_tag: str | None, branch: str | None,
) -> None:
    """Сверить сохранённые ссылки с HEAD локального клона и вывести результат в JSON."""
    from reviewer.services import brief_provenance as provenance

    if (indexed_sha is None and (repo_tag is not None or branch is not None)
        or indexed_sha is not None and not (repo_tag and branch)):
        raise click.UsageError("Укажите --indexed-sha, --repo и --branch вместе для записи")

    root = repo_root(path)
    if root is None:
        result = {"status": "unavailable", "reason": "checkout_unavailable"}
    else:
        try:
            brief = brief.resolve(strict=True)
            text = brief.read_bytes().decode("utf-8")
            git = provenance.GitCheckout(root)
            if indexed_sha is None:
                result = provenance.revalidate(git, text)
            else:
                result, updated = provenance.capture(git, text, repo=repo_tag or "", branch=branch or "",
                                          indexed_sha=indexed_sha)
                if updated != text:
                    provenance.replace_brief(brief, text, updated)
        except (OSError, UnicodeError):
            result = {"status": "unavailable", "reason": "brief_io_error"}
    click.echo(json.dumps(result, ensure_ascii=False))


@cli.command("measure-briefs")
@click.argument("path", default=".")
@click.option("--repo", "repo_tag", default=None,
              help="owner/name тег строк метрики; по умолчанию из git remote origin")
@click.option("--briefs-dir", "briefs_dir", default=None,
              help="каталог брифов относительно клона; по умолчанию docs/superpowers/briefs")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="машиночитаемый JSON вместо текста")
def measure_briefs(path: str, repo_tag: str | None, briefs_dir: str | None,
                    as_json: bool) -> None:
    """Пересчитать качество брифов по PR-мержам клона (не тратит Voyage)."""
    s = Settings()
    resolution = _resolve_repo(repo_tag, path, s)
    if resolution.source == _SUBSTITUTED:
        # Fail-open, как migrate-branches: команда ничего не индексирует, но
        # молчать про чужой repo-тег в строках brief_quality тоже не должна.
        click.echo("⚠ " + _substitution_note(path, resolution.repo), err=True)
    # Слоистый резолв (env/home/committed), как у finish_task/publish_review —
    # голое чтение файла клона молча теряло бы core_paths/key_pattern,
    # заданные через home:review.yml или home:repos/<owner>/<name>.yml, и три
    # точки съёма считали бы по разным линейкам для одного и того же PR.
    # Committed-слой — из HEAD клона (git show, без сети, PRI-235); fail-soft
    # по умолчанию (strict_committed=False) — недоступный/битый .review.yml не
    # должен обнулять домашние слои и уж тем более ронять весь пересчёт корпуса.
    #
    # Почему ref = "HEAD", а не base_ref конкретного PR (как у finish_task/
    # publish_review): эта команда считает КОРПУС, а не один PR, и её строки
    # относятся к разным PR с разными base_ref — единственного «правильного»
    # ref для всех строк не существует. Брать историческую политику каждого
    # PR нельзя: это сетевой вызов на каждую строку, а команда обязана
    # работать офлайн, и сделало бы собственный вывод внутренне несравнимым —
    # строки считались бы разными линейками внутри одного прогона. Поэтому
    # bulk-пересчёт сознательно берёт ОДНУ текущую линейку клона — HEAD;
    # CommittedLayerFetcher проверяет ref через rev_parse до чтения, поэтому
    # HEAD резолвится и на detached-клоне. Следствие: если оператор запускает
    # команду с фичевой ветки со своим .review.yml, строки посчитаются её
    # линейкой — это цена самосогласованности пересчёта, а не недосмотр.
    fetch_committed = CommittedLayerFetcher(resolution.repo, clone_path=path)
    policy_data, _policy_meta = resolve_policy_data(resolution.repo, "HEAD", fetch_committed)
    config = BriefQualityConfig.from_review_yaml(policy_data, briefs_dir=briefs_dir)
    history = ReviewHistory(s.pg_dsn, min_size=s.pg_pool_min_size, max_size=s.pg_pool_max_size)
    try:
        # Preflight: record_brief_quality гасит DB-сбои сама (fail-soft для
        # publish_review/finish_task) — без явной проверки соединения ДО
        # цикла команда молча отчиталась бы об успехе на лежащем Postgres.
        # init_schema — единственный метод ReviewHistory без своего try/except.
        history.init_schema()
        summary = measure_corpus(path, resolution.repo, config,
                                 ground_truth.git_runner(Path(path)), history)
    except psycopg.OperationalError as e:
        raise click.ClickException(f"Postgres недоступен: {e}")
    finally:
        history.close()
    click.echo(json.dumps(summary, ensure_ascii=False) if as_json else _render_measure(summary))
    write_failed = summary.get("write_failed", 0)
    if write_failed:
        # Postgres умер на середине прогона (после preflight) — исключение
        # уже погашено внутри measure_and_record, сигналим по счётчику.
        click.echo(f"⚠ Не записано в БД строк: {write_failed}", err=True)
    if summary.get("diff_failed"):
        click.echo(f"⚠ Не удалось прочитать Git diff для PR: {summary['diff_failed']}", err=True)
    if write_failed or summary.get("diff_failed"):
        raise SystemExit(1)


@cli.command("measure-client-usage")
@click.argument("events", type=click.File("r", encoding="utf-8"), default="-")
@click.option("--source", type=click.Choice(USAGE_SOURCES), required=True, help="Формат JSONL")
@click.option("--model", default=None, help="Модель для событий без имени модели")
def measure_client_usage(events: TextIO, source: str, model: str | None) -> None:
    """Суммировать расход токенов из JSONL локально, включая чтение из stdin."""
    try:
        result = measure_usage(events, source, model=model)
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(result, ensure_ascii=False))


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
        if _shutil.which("codex") and not any(target.key == "codex" for target in targets):
            targets.append(inst.CLIENTS["codex"])
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
    *,
    compact: bool = False,
) -> None:
    """Показать все target-планы до первой записи, не раскрывая env secrets."""
    if compact:
        actions = {
            "create": "будет создан", "update": "будет обновлён",
            "append": "будет дополнен", "noop": "уже настроен",
        }
        click.echo("\nПредпросмотр настройки")
        if global_plan is not None:
            click.echo(f"Общий .env — {global_plan.path} ({actions[global_plan.action]})")
            click.echo(global_plan.preview)
        if repo_plan is not None:
            click.echo(f"Профиль {repo_plan.repo} — {repo_plan.path} ({actions[repo_plan.action]})")
            click.echo(f"Основная ветка — {repo_plan.primary}")
            click.echo(f"Ветки для индекса — {', '.join(repo_plan.index)}")
        return
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


def _quick_init_values(inst, current: dict[str, str]) -> tuple[dict[str, str], str, bool]:
    """Collect missing keys for local onboarding using the existing defaults."""
    from reviewer.services.repo_id import derive_vcs_from_remote

    values = inst.prompt_groups(inst.WIZARD_GROUPS, current=current, yes=True)
    defaults = values.copy()
    if not values["VOYAGE_API_KEY"]:
        values["VOYAGE_API_KEY"] = click.prompt(
            "Ключ Voyage для индекса (Enter — позже)",
            default="", hide_input=True, show_default=False,
        ).strip()

    detected = derive_vcs_from_remote(remote_url(".") or "")
    provider = detected[0] if detected else current.get("VCS_PROVIDER")
    spec = inst.VCS_SETUPS.get(provider)
    storage = "сохранённые параметры" if {"PG_DSN", "NEO4J_URI"} & current.keys() else (
        "локальные ParadeDB и Neo4j"
    )
    statuses = [
        "Voyage: ключ задан" if values["VOYAGE_API_KEY"] else "Voyage: ключ нужен для индекса",
        f"Хранилища: {storage}",
    ]
    if detected and provider == "gitlab" and detected[1] != values["GITLAB_URL"].rstrip("/"):
        statuses.append("GitLab: подтвердите адрес сервера и ключ через reviewer init --advanced")
    elif spec is not None:
        if not current.get("VCS_PROVIDER"):
            values["VCS_PROVIDER"] = spec.provider
        for token in (field for field in spec.credential_fields if field.secret):
            if not values[token.key]:
                click.echo(f"Инструкция для ключа {spec.label} — {spec.help_url}")
                values[token.key] = click.prompt(
                    f"Ключ {spec.label} для ревью (Enter — позже)",
                    default="", hide_input=True, show_default=False,
                ).strip()
            statuses.append(
                f"{spec.label}: " + ("ключ задан" if values[token.key] else "ключ пропущен")
            )
    return values, "\n".join(statuses), values != defaults


def _prompt_repository_branches(
    detection: RepositoryDetection, plan: RepositoryConfigPlan, settings: Settings,
) -> RepositoryConfigPlan:
    while True:
        primary = click.prompt("Primary branch", default=plan.primary).strip()
        if primary:
            break
        click.echo("Основная ветка должна иметь имя.")
    default_index = plan.index if primary == plan.primary else (primary,)
    while True:
        raw_index = click.prompt("Index branches (CSV)", default=",".join(default_index))
        try:
            index = parse_branch_csv(raw_index, primary)
        except ValueError as exc:
            click.echo(f"Некорректные ветки: {exc}")
            continue
        return plan_repository_config(
            detection, settings=settings,
            primary=primary if primary != detection.primary else None,
            index=index,
        )


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
@click.option("--advanced", is_flag=True,
              help="Настроить хранилища, VCS, доску задач и ветки по шагам.")
def init(
    path_opt: str | None,
    yes: bool,
    dry_run: bool,
    scope: str,
    repo_opt: str | None,
    advanced: bool,
) -> None:
    """Быстро настроить reviewer и показать изменения перед записью."""
    import subprocess
    from reviewer import install as inst
    from reviewer.tasks.boards import setup as board_setup
    from reviewer.tasks.boards.errors import BoardProviderError
    from reviewer.tasks.boards.registry import default_board_registry

    run_global = scope in {"all", "global"}
    run_repo = scope in {"all", "repo"}
    interactive = not yes and not dry_run
    quick = interactive and not advanced
    if run_repo and repo_opt is not None:
        try:
            repo_opt = _resolve_config_repo(repo_opt)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    global_plan: _GlobalInitPlan | None = None
    repo_plan: RepositoryConfigPlan | None = None
    detection: RepositoryDetection | None = None
    settings: Settings | None = None
    try:
        if run_global:
            env_path = path_opt or os.environ.get("REVIEWER_ENV_FILE")
            dest = Path(env_path).expanduser() if env_path else inst.default_env_path()
            current = inst.read_env(dest)
            wizard_keys = {field.key for group in inst.WIZARD_GROUPS for field in group.fields}
            groups = inst.WIZARD_GROUPS

            if quick:
                click.echo("Быстрая настройка reviewer")
                values, quick_preview, quick_changed = _quick_init_values(inst, current)
            elif interactive:
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
            for warning in inst.publish_port_warnings(values):
                click.echo(f"⚠ {warning}")
            content = inst.render_env(values, extra)
            existing = dest.read_text(encoding="utf-8") if dest.is_file() else None
            if existing is not None and (existing == content or (quick and not quick_changed)):
                action = "noop"
            else:
                action = "update" if existing is not None else "create"
            global_plan = _GlobalInitPlan(
                path=dest,
                content=content,
                preview=quick_preview if quick else inst.render_env_preview(values, extra),
                source=f"existing:{dest}" if existing is not None else "wizard defaults/input",
                action=action,
            )

        if run_repo:
            if scope == "repo":
                settings = Settings(_env_file=None)
            elif path_opt or os.environ.get("REVIEWER_ENV_FILE"):
                assert global_plan is not None
                settings = Settings(_env_file=global_plan.path)
            else:
                settings = Settings()
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
                if initial_plan.action == "noop" or not interactive or (quick and run_global):
                    repo_plan = initial_plan
                else:
                    click.echo(
                        f"\nОбнаружен repo {detection.repo} ({detection.repo_source})"
                    )
                    click.echo(
                        f"Primary candidate: {detection.primary} "
                        f"({detection.primary_source})"
                    )
                    repo_plan = _prompt_repository_branches(detection, initial_plan, settings)

        _render_init_preview(global_plan, repo_plan, compact=quick and run_global)
        if dry_run:
            return
        plans = [plan for plan in (global_plan, repo_plan) if plan is not None]
        has_changes = any(plan.action != "noop" for plan in plans)
        if interactive and has_changes:
            if quick and run_global and repo_plan is not None and repo_plan.action != "noop":
                assert detection is not None and settings is not None
                while True:
                    action = click.prompt(
                        "\n1 — сохранить · 2 — изменить ветки · 0 — отменить",
                        type=click.Choice(("1", "2", "0")), default="1", show_choices=False,
                    )
                    if action != "2":
                        confirmed = action == "1"
                        break
                    repo_plan = _prompt_repository_branches(detection, repo_plan, settings)
                    _render_init_preview(global_plan, repo_plan, compact=True)
            else:
                confirmed = click.confirm("\nЗаписать показанные изменения?", default=True)
            if not confirmed:
                click.echo("Отменено — файлы не изменены.")
                return
        if interactive and plans and not has_changes:
            click.echo("Изменений нет. Текущая конфигурация сохранена.")
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
                inst.write_env(global_plan.path, global_plan.content)
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

    if global_plan is None:
        return
    check_command = ["reviewer", "check"]
    if path_opt or os.environ.get("REVIEWER_ENV_FILE"):
        check_command.extend(("--env-file", str(global_plan.path.resolve())))
    if interactive and advanced and click.confirm(
        "\nЗапустить reviewer check сейчас?",
        default=True,
    ):
        subprocess.run(check_command, check=False)
    else:
        click.echo(f"Проверка после запуска хранилищ и индексации — {shlex.join(check_command)}")

    if quick:
        click.echo("Подключение клиента — reviewer install --list")
        click.echo("Хранилища, доска и дополнительные параметры — reviewer init --advanced")
    if interactive and advanced and _shutil.which("codex") and click.confirm(
        "\nУстановить или обновить rag-reviewer для Codex?", default=True
    ):
        result = _run_codex_target(include_mcp=True, dry_run=False)
        _print_codex_result(result)


def _has_detected_clients() -> bool:
    from reviewer import install as inst

    return bool(inst.detect_installed()) or any(
        _shutil.which(executable) is not None for executable in ("claude", "codex")
    )


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
            errors.append(
                f"Integrations: {type(exc).__name__} "
                "(подробности: reviewer install --all)"
            )
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
    if upgrade_tool and installation.mode is InstallMode.UVX:
        result = upgrade_uv_tool(installation)
        if result.returncode != 0:
            raise click.ClickException(f"Ошибка uv tool upgrade: {result.stderr}")
        click.echo("✓ Persistent uv tool installation обновлена.")
        tool_python = find_uv_tool_python(installation.uv_executable)
        if tool_python is None:
            raise click.ClickException(
                "Persistent tool обновлён, но его Python executable не найден"
            )
        fresh = run_fresh_artifact_refresh(python_executable=tool_python)
        if fresh.stdout:
            click.echo(fresh.stdout, nl=not fresh.stdout.endswith("\n"))
        if fresh.returncode != 0:
            detail = fresh.stderr.strip() or "неизвестная ошибка"
            raise click.ClickException(
                f"Persistent tool обновлён, artifact refresh завершился ошибкой: {detail}"
            )
        return

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
