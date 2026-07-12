from __future__ import annotations
import logging
import platform as _platform
import shutil as _shutil
from typing import TYPE_CHECKING

import click
import httpx
import psycopg

from reviewer.config.settings import Settings
from reviewer.app import build_components
from reviewer.gitutil import file_at_ref, list_python_files, rev_parse, remote_url
from reviewer.graph.backend import build_code_graph
from reviewer.graph.store import GraphStore
from reviewer.index.freshness import update_base
from reviewer.index.pathfilter import is_ignored
from reviewer.index.refs import base_ref
from reviewer.policy.policy import ReviewPolicy
from reviewer.index.store import ChunkStore
from reviewer.services.status import build_status_report, render_status, render_status_json

if TYPE_CHECKING:
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


@cli.command()
def check() -> None:
    """Проверить готовность окружения (ключи, Postgres, Neo4j, GitHub)."""
    s = Settings()
    failed = False

    # 1. Ключи
    for label, val in (
        ("VOYAGE_API_KEY", s.voyage_api_key),
        ("GITHUB_TOKEN", s.github_token),
    ):
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

    # 5. GitHub (только если токен задан)
    if s.github_token:
        try:
            resp = httpx.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {s.github_token}"},
                timeout=10,
            )
            if resp.status_code == 200:
                login = resp.json().get("login", "?")
                click.echo(f"✓ GitHub API: аутентификация OK (логин: {login})")
            else:
                click.echo(f"✗ GitHub API: HTTP {resp.status_code} — проверьте токен")
                failed = True
        except Exception as e:
            click.echo(f"✗ GitHub API: {e}")
            failed = True
    else:
        click.echo("  GitHub API: токен не задан, проверка пропущена")

    # 6. Свежесть установленных скилов (информационно, не влияет на exit-code)
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
              help="git-ref для чтения файлов; по умолчанию первичная ветка "
                   "(ключ хранения, если --branch не задан)")
@click.option("--branch", "branch_opt", default=None,
              help="имя ветки для хранения индекса; по умолчанию = --ref")
@click.option("--repo", "repo_tag", default=None,
              help="owner/name тег индекса; по умолчанию из git remote origin")
def index(repo: str, ref: str | None, branch_opt: str | None, repo_tag: str | None) -> None:
    """Построить/обновить base-индекс целевой ветки из локального репо."""
    s = Settings()
    c = build_components(s)
    repo_id = _resolve_repo(repo_tag, repo, s)
    ref = ref or s.primary_branch()
    branch = branch_opt or ref
    bref = base_ref(branch)
    try:
        c.store.init_schema()
        files = list_python_files(repo, ref)
        review_yml = file_at_ref(repo, ".review.yml", ref)
        ignore = ReviewPolicy.from_yaml(review_yml).ignore if review_yml else []
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
def migrate_branches() -> None:
    """Один раз после апгрейда: перенести legacy base-индекс на первичную ветку."""
    s = Settings()
    c = build_components(s)
    primary = s.primary_branch()
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
              help="ветка base-индекса; по умолчанию первичная (REVIEW_BRANCHES)")
def search(query: str, repo_tag: str | None, branch_opt: str | None) -> None:
    """Гибридный поиск по base-индексу ветки (диагностика)."""
    from reviewer.services.repo_id import normalize_repo
    s = Settings()
    repo_id = normalize_repo(repo_tag or s.default_repo) if (repo_tag or s.default_repo) else None
    if repo_id is None:
        raise click.ClickException("Укажите --repo owner/name (или DEFAULT_REPO в .env)")
    if branch_opt and branch_opt not in s.review_branches_list():
        raise click.ClickException(
            f"Ветка {branch_opt!r} не в REVIEW_BRANCHES ({s.review_branches_list()})")
    branch = branch_opt or s.primary_branch()
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
              help="одна ветка; по умолчанию все из REVIEW_BRANCHES")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="машиночитаемый JSON вместо текста")
def status(path: str, repo_tag: str | None, branch_opt: str | None,
           as_json: bool) -> None:
    """Показать здоровье/свежесть base-индекса по веткам (не тратит Voyage)."""
    s = Settings()
    repo = _resolve_repo(repo_tag, path, s)
    branches = [branch_opt] if branch_opt else s.review_branches_list()
    store = ChunkStore(s.pg_dsn, min_size=s.pg_pool_min_size, max_size=s.pg_pool_max_size)
    graph = GraphStore(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
    try:
        report = build_status_report(store, graph, repo, branches, path)
    except psycopg.OperationalError as e:
        raise click.ClickException(f"Postgres недоступен: {e}")
    finally:
        store.close()
        graph.close()
    if as_json:
        click.echo(render_status_json(report))
        return
    backend = ("scip-python (точный)" if _shutil.which("scip-python")
               else "tree-sitter (fallback, scip-python не найден)")
    click.echo(render_status(report, backend))


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Хост для uvicorn")
@click.option("--port", default=8000, show_default=True, type=int, help="Порт для uvicorn")
def serve(host: str, port: int) -> None:
    """Запустить веб-админку наблюдаемости (FastAPI + uvicorn)."""
    import uvicorn
    from reviewer.web.app import create_app

    s = Settings()
    app = create_app(s)
    click.echo(f"Запуск веб-сервера на http://{host}:{port} ...")
    uvicorn.run(app, host=host, port=port)


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

    if list_clients:
        click.echo("Поддерживаемые клиенты (reviewer install <client>):")
        for c in inst.CLIENTS.values():
            tag = f" [{c.scope}]" if c.scope != "user" else ""
            skills = " +скилы" if c.skills_fn else ""
            click.echo(f"  {c.key:<15} {c.label}{tag}{skills}")
        return

    version = "" if no_latest else (pin or "latest")
    if all_clients:
        targets = inst.detect_installed()
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

    codex_errors: list[str] = []
    codex_mcp_only = no_skills and any(c.key == "codex" for c in targets)
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
                codex_errors.append(f"Codex CLI: {exc.format_message()}")
            continue
        plan = inst.build_plan(c, version=version, path_override=path_opt)
        if dry_run:
            click.echo(f"# {c.label} → {plan.path}")
            click.echo(plan.content)
            if c.key == "claude-code":
                al_plan = inst.build_allowlist_plan(inst.claude_user_settings_path())
                click.echo(f"# {c.label} allowlist (глобально, для всех проектов) → {al_plan.path}")
                click.echo(al_plan.content)
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
        if c.key == "claude-code":
            al_plan = inst.build_allowlist_plan(inst.claude_user_settings_path())
            al_backup = inst.apply_allowlist_plan(al_plan)
            if al_plan.already:
                al_status = "правило уже есть"
            elif al_plan.created:
                al_status = "создан settings.json"
            else:
                al_status = "добавлено правило"
            click.echo(f"  allowlist (глобально, для всех проектов): {al_status} → "
                       f"{al_plan.path} ({inst.REVIEWER_PERMISSION_RULE})")
            if al_backup:
                click.echo(f"  бэкап: {al_backup}")
        _ensure_skills(c)

    if codex_errors:
        raise click.ClickException("; ".join(codex_errors))
    if not dry_run and codex_mcp_only:
        click.echo("Codex MCP обновлён. Откройте New Chat/new CLI session; "
                   "в IDE выполните Reload Window.")
    if not dry_run:
        click.echo("Готово. Перезапустите клиент. Ключи: reviewer init && reviewer check.")


@cli.command()
@click.option("--path", "path_opt", default=None,
              help="куда писать .env (по умолчанию ~/.config/rag-reviewer/.env)")
@click.option("--yes", "yes", is_flag=True,
              help="принять все дефолты без интерактива (CI-режим)")
def init(path_opt: str | None, yes: bool) -> None:
    """Интерактивный мастер настройки .env для rag-reviewer."""
    import subprocess
    from pathlib import Path
    from reviewer import install as inst

    dest = Path(path_opt).expanduser() if path_opt else inst.default_env_path()
    dest.parent.mkdir(parents=True, exist_ok=True)

    current = inst.read_env(dest)
    wizard_keys = {f.key for g in inst.WIZARD_GROUPS for f in g.fields}

    if not yes:
        click.echo(f"Настройка rag-reviewer: {dest}")
        click.echo("─" * 52)

    try:
        values = inst.prompt_groups(inst.WIZARD_GROUPS, current=current, yes=yes)
    except click.Abort:
        click.echo("\nОтменено — файл не изменён.")
        return

    extra = {k: v for k, v in current.items() if k not in wizard_keys}
    content = inst.render_env(values, extra)
    dest.write_text(content, encoding="utf-8")
    click.echo(f"\n✓ Записан {dest}")

    if not yes and click.confirm("\nЗапустить reviewer check сейчас?", default=True):
        subprocess.run(["reviewer", "check"], check=False)
    elif not yes:
        click.echo("Запустите: reviewer check")
    else:
        click.echo("Готово. Запустите: reviewer check")

    if not yes and _shutil.which("codex") and click.confirm(
        "\nУстановить или обновить rag-reviewer для Codex?", default=True
    ):
        result = _run_codex_target(include_mcp=True, dry_run=False)
        _print_codex_result(result)


@cli.command()
def update() -> None:
    """Проверить наличие новой версии rag-reviewer на PyPI."""
    import json
    import subprocess
    import urllib.request
    from importlib import metadata

    def _ver_tuple(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0,)

    try:
        cur = metadata.version("rag-reviewer")
    except Exception:
        cur = "?"

    # Определяем режим установки
    try:
        dist = metadata.Distribution.from_name("rag-reviewer")
        direct_url_text = dist.read_text("direct_url.json")
        direct_url = json.loads(direct_url_text) if direct_url_text else {}
        is_editable = direct_url.get("dir_info", {}).get("editable", False)
    except Exception:
        is_editable = False

    uv = _shutil.which("uv")
    is_tool = False
    if uv and not is_editable:
        tool_list = subprocess.run([uv, "tool", "list"], capture_output=True, text=True)
        is_tool = "rag-reviewer" in tool_list.stdout

    if is_editable:
        click.echo(f"Режим: dev (editable) | Версия: {cur}")
        click.echo("Для обновления: git pull && pip install -e .")
        return

    mode = "uv tool (постоянная)" if is_tool else "uvx (временная)"
    click.echo(f"Режим: {mode} | Версия: {cur}")

    # Получаем latest с PyPI
    latest_ver: str | None = None
    try:
        req = urllib.request.Request(
            "https://pypi.org/pypi/rag-reviewer/json",
            headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            latest_ver = json.loads(resp.read())["info"]["version"]
    except Exception:
        pass

    if latest_ver is None:
        click.echo("Не удалось получить информацию с PyPI. Проверьте сеть.")
        return

    if cur != "?" and _ver_tuple(latest_ver) <= _ver_tuple(cur):
        click.echo(f"Версия актуальна: {cur}.")
        if not is_tool:
            click.echo("MCP-сервер обновляется автоматически — в конфиге клиента прописан @latest.")
        return

    click.echo(f"Доступна новая версия: {cur} → {latest_ver}")

    if is_tool:
        if not uv:
            click.echo("uv не найден в PATH. Запустите: uv tool upgrade rag-reviewer")
            return
        result = subprocess.run([uv, "tool", "upgrade", "rag-reviewer"], capture_output=True)
        if result.returncode == 0:
            click.echo("Обновлено. Перезапустите MCP-сервер.")
        else:
            click.echo(f"Ошибка uv tool upgrade: {result.stderr.decode().strip()}")
    else:
        # uvx: MCP-сервер обновится сам при следующем запуске (конфиг содержит @latest).
        # Для CLI-команд достаточно использовать @latest явно.
        click.echo(
            "MCP-сервер подхватит обновление автоматически при следующем запуске (@latest в конфиге).\n"
            "Для CLI: uvx --from rag-reviewer@latest reviewer <команда>"
        )


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
