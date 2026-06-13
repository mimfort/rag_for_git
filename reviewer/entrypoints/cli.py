from __future__ import annotations
import logging
import shutil as _shutil

import click
import httpx

from reviewer.config.settings import Settings
from reviewer.app import build_components
from reviewer.gitutil import file_at_ref, list_python_files, rev_parse, remote_url
from reviewer.graph.backend import build_code_graph
from reviewer.graph.store import GraphStore
from reviewer.index.freshness import update_base
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
@click.option("--ref", default="HEAD")
@click.option("--repo", "repo_tag", default=None,
              help="owner/name тег индекса; по умолчанию из git remote origin")
def index(repo: str, ref: str, repo_tag: str | None) -> None:
    """Построить/обновить base-индекс целевой ветки из локального репо."""
    s = Settings()
    c = build_components(s)
    repo_id = _resolve_repo(repo_tag, repo, s)
    try:
        c.store.init_schema()
        files = list_python_files(repo, ref)
        update_base(c.store, c.embedder, repo_id, ref, files,
                    read=lambda p: file_at_ref(repo, p, ref))
        c.store.delete_paths_except(repo_id, "base", files)
        sha = rev_parse(repo, ref)
        c.store.set_index_meta(repo_id, "base", sha)
        # --- граф кода ---
        src_by_path = {p: file_at_ref(repo, p, ref) for p in files}
        src_by_path = {p: v for p, v in src_by_path.items() if v is not None}
        gnodes, gedges, backend = build_code_graph(
            repo, ref, files, src_by_path, s.graph_backend,
        )
        c.graph.init_schema()
        c.graph.clear(repo_id)   # rebuild только этого репо
        c.graph.upsert_nodes(repo_id, list(gnodes))
        c.graph.upsert_edges(repo_id, gedges)
        click.echo(
            f"Проиндексировано [{repo_id}] файлов: {len(files)} @ {sha[:7]}; "
            f"граф [{backend}]: узлов {len(gnodes)}, рёбер {len(gedges)}"
        )
    finally:
        c.store.close()
        if c.graph:
            c.graph.close()


@cli.command()
@click.argument("query")
@click.option("--repo", "repo_tag", default=None, help="owner/name; по умолчанию DEFAULT_REPO")
def search(query: str, repo_tag: str | None) -> None:
    """Гибридный поиск по base-индексу (диагностика)."""
    from reviewer.services.repo_id import normalize_repo
    s = Settings()
    repo_id = normalize_repo(repo_tag or s.default_repo) if (repo_tag or s.default_repo) else None
    if repo_id is None:
        raise click.ClickException("Укажите --repo owner/name (или DEFAULT_REPO в .env)")
    c = build_components(s)
    try:
        qvec = c.embedder.embed_query(query)
        hits = c.store.hybrid_search(
            repo_id, query_text=query, query_embedding=qvec,
            overlay_ref="", changed_paths=[], top_k=10,
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
