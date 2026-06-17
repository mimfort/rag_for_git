from __future__ import annotations
import logging
import platform as _platform
import shutil as _shutil

import click
import httpx

from reviewer.config.settings import Settings
from reviewer.app import build_components
from reviewer.gitutil import file_at_ref, list_python_files, rev_parse, remote_url
from reviewer.graph.backend import build_code_graph
from reviewer.graph.store import GraphStore
from reviewer.index.freshness import update_base
from reviewer.index.refs import base_ref
from reviewer.index.store import ChunkStore

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
        update_base(c.store, c.embedder, repo_id, branch, files,
                    read=lambda p: file_at_ref(repo, p, ref))
        c.store.delete_paths_except(repo_id, bref, files)
        sha = rev_parse(repo, ref)
        c.store.set_index_meta(repo_id, bref, sha)
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

    tar_cache: list[bytes] = []  # тарбол скилов качаем один раз на все цели

    def _ensure_skills(c) -> None:
        if no_skills or c.skills_fn is None:
            return
        if not tar_cache:
            click.echo("  скилы: скачиваю с GitHub…")
            try:
                tar_cache.append(inst.fetch_skills_bytes())
            except Exception as exc:  # noqa: BLE001 — fail-soft, MCP уже прописан
                click.echo(f"  скилы: пропуск (не скачать тарбол: {exc})")
                tar_cache.append(b"")
                return
        if not tar_cache[0]:
            return
        try:
            dest, names = inst.install_skills(c, tar_bytes=tar_cache[0])
            click.echo(f"  скилы: {len(names)} шт. → {dest}")
        except Exception as exc:  # noqa: BLE001
            click.echo(f"  скилы: пропуск ({exc})")

    for c in targets:
        plan = inst.build_plan(c, version=version, path_override=path_opt)
        if dry_run:
            click.echo(f"# {c.label} → {plan.path}")
            click.echo(plan.content)
            if c.key == "claude-code":
                al_plan = inst.build_allowlist_plan(plan.path.parent / ".claude" / "settings.json")
                click.echo(f"# {c.label} allowlist → {al_plan.path}")
                click.echo(al_plan.content)
            if c.skills_fn and not no_skills:
                click.echo(f"# {c.label} скилы → {c.skills_fn(_platform.system())}")
            continue
        backup = inst.apply_plan(plan)
        if plan.already and c.dialect == "codex":
            status = "запись уже есть (TOML не трогаю — правьте вручную при необходимости)"
        elif plan.already:
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
            al_plan = inst.build_allowlist_plan(plan.path.parent / ".claude" / "settings.json")
            al_backup = inst.apply_allowlist_plan(al_plan)
            if al_plan.already:
                al_status = "правило уже есть"
            elif al_plan.created:
                al_status = "создан settings.json"
            else:
                al_status = "добавлено правило"
            click.echo(f"  allowlist: {al_status} → {al_plan.path} "
                       f"({inst.REVIEWER_PERMISSION_RULE})")
            if al_backup:
                click.echo(f"  бэкап: {al_backup}")
        _ensure_skills(c)

    if not dry_run:
        click.echo("Готово. Перезапустите клиент. Ключи: reviewer init && reviewer check.")


@cli.command()
@click.option("--path", "path_opt", default=None,
              help="куда писать .env (по умолчанию ~/.config/rag-reviewer/.env)")
@click.option("--force", is_flag=True, help="перезаписать существующий файл")
def init(path_opt: str | None, force: bool) -> None:
    """Создать .env в стабильном месте (~/.config/rag-reviewer/.env)."""
    from pathlib import Path
    from reviewer import install as inst

    dest = Path(path_opt).expanduser() if path_opt else inst.default_env_path()
    if dest.exists() and not force:
        click.echo(f"Файл уже существует: {dest}")
        click.echo("Откройте его и заполните VOYAGE_API_KEY / GITHUB_TOKEN "
                   "(перезапись: reviewer init --force).")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(inst.ENV_TEMPLATE, encoding="utf-8")
    click.echo(f"✓ Создан {dest}")
    click.echo("Заполните VOYAGE_API_KEY и GITHUB_TOKEN, затем: reviewer check")


@cli.command()
def update() -> None:
    """Обновить rag-reviewer до последней версии с PyPI."""
    import json
    import subprocess
    from importlib import metadata

    uv = _shutil.which("uv")
    if not uv:
        raise click.ClickException(
            "uv не найден в PATH. Установите uv: https://docs.astral.sh/uv/getting-started/")
    try:
        cur = metadata.version("rag-reviewer")
    except Exception:
        cur = "?"
    click.echo(f"Текущая версия (в этом окружении): {cur}")

    # Editable-установка (pip install -e .) — dev-режим
    try:
        dist = metadata.Distribution.from_name("rag-reviewer")
        direct_url_text = dist.read_text("direct_url.json")
        is_editable = (
            bool(direct_url_text)
            and json.loads(direct_url_text).get("dir_info", {}).get("editable", False)
        )
    except Exception:
        is_editable = False

    if is_editable:
        click.echo(
            "Обнаружена editable-установка (pip install -e .). "
            "Обновите код через git pull и переустановите: pip install -e ."
        )
        return

    # Persistent uv tool — пробуем апгрейд (подавляем вывод при неудаче)
    upgraded = subprocess.run(
        [uv, "tool", "upgrade", "rag-reviewer"],
        capture_output=True,
    ).returncode == 0

    if upgraded:
        click.echo("Готово. Перезапустите MCP-сервер в редакторе/CLI, чтобы применить новую версию.")
        return

    # uvx/ephemeral: чистим кэш и сразу скачиваем свежую версию в этом же запуске
    subprocess.run([uv, "cache", "clean", "rag-reviewer"], capture_output=True)
    fetch = subprocess.run(
        [uv, "run", "--with", "rag-reviewer", "--no-project",
         "python", "-c",
         "from importlib.metadata import version; print(version('rag-reviewer'))"],
        capture_output=True, text=True,
    )
    new_ver = fetch.stdout.strip() if fetch.returncode == 0 else None
    if new_ver and new_ver != cur:
        click.echo(f"Обновлено: {cur} → {new_ver}. Перезапустите MCP-сервер.")
    elif new_ver:
        click.echo(f"Версия актуальна: {cur}.")
    else:
        click.echo(
            "Кэш очищен. Следующий запуск подхватит последнюю версию с PyPI.\n"
            "Если хотите постоянную установку: uv tool install rag-reviewer"
        )


@cli.command("install-skills")
@click.argument("client", required=False)
@click.option("--all", "all_clients", is_flag=True,
              help="поставить во все обнаруженные клиенты со скилами")
@click.option("--list", "list_clients", is_flag=True,
              help="показать клиенты с поддержкой файловых скилов")
@click.option("--path", "path_opt", default=None,
              help="переопределить каталог скилов")
def install_skills(client: str | None, all_clients: bool,
                   list_clients: bool, path_opt: str | None) -> None:
    """Установить скилы (review-pr, solve-task и др.) в каталог клиента."""
    from pathlib import Path
    from reviewer import install as inst

    capable = [c for c in inst.CLIENTS.values() if c.skills_fn]
    if list_clients:
        click.echo("Клиенты с файловыми скилами:")
        for c in capable:
            click.echo(f"  {c.key:<15} {c.label} → {c.skills_fn(_platform.system())}")
        click.echo("Прочие клиенты подхватывают скилы из плагина "
                   "(/plugin marketplace add mimfort/rag_for_git).")
        return

    if all_clients:
        targets = [c for c in inst.detect_installed() if c.skills_fn]
        if not targets:
            raise click.ClickException(
                "Не обнаружено клиентов со скилами. Укажите явно или см. --list.")
    elif client:
        key = client.lower()
        if key not in inst.CLIENTS:
            raise click.ClickException(
                f"Неизвестный клиент {client!r}. Список: reviewer install-skills --list")
        c = inst.CLIENTS[key]
        if c.skills_fn is None:
            raise click.ClickException(
                f"{c.label}: файловые скилы не поддерживаются (используйте плагин).")
        targets = [c]
    else:
        raise click.ClickException(
            "Укажите клиент (reviewer install-skills <client>), либо --all / --list.")

    click.echo("Скачиваю скилы с GitHub…")
    tar = inst.fetch_skills_bytes()
    for c in targets:
        dest = Path(path_opt).expanduser() if path_opt else c.skills_fn(_platform.system())
        names = inst.extract_skills(tar, dest)
        click.echo(f"✓ {c.label}: {len(names)} скилов → {dest}")
        if c.note:
            click.echo(f"  прим.: {c.note}")
