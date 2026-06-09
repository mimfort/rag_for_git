from __future__ import annotations
import logging
import click

from reviewer.config.settings import Settings
from reviewer.app import build_components
from reviewer.gitutil import file_at_ref, list_python_files, rev_parse
from reviewer.index.freshness import update_base, build_overlay

log = logging.getLogger(__name__)

@click.group()
def cli() -> None: ...


@cli.command()
def check() -> None:
    """Проверить готовность окружения (ключи, Postgres, Neo4j, GitHub)."""
    import httpx
    from reviewer.index.store import ChunkStore
    from reviewer.graph.store import GraphStore

    s = Settings()
    failed = False

    # 1. Ключи
    for label, val in (
        ("OPENROUTER_API_KEY", s.openrouter_api_key),
        ("VOYAGE_API_KEY", s.voyage_api_key),
        ("GITHUB_TOKEN", s.github_token),
    ):
        if val:
            click.echo(f"✓ {label} задан")
        else:
            click.echo(f"✗ {label}: не задан (добавьте в .env)")
            failed = True

    # 2. Postgres
    try:
        store = ChunkStore(s.pg_dsn)
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

    # 4. GitHub (только если токен задан)
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
def index(repo: str, ref: str) -> None:
    """Построить/обновить base-индекс целевой ветки из локального репо."""
    s = Settings()
    c = build_components(s)
    c.store.init_schema()
    files = list_python_files(repo, ref)
    update_base(c.store, c.embedder, repo, ref, files,
                read=lambda p: file_at_ref(repo, p, ref))
    # Чистим чанки файлов, которых больше нет в ветке (гигиена base-индекса)
    c.store.delete_paths_except("base", files)
    # Запоминаем SHA проиндексированного ref — нужен для синхронизации на review
    sha = rev_parse(repo, ref)
    c.store.set_index_meta("base", sha)
    # --- граф кода ---
    from reviewer.graph.builder import build_graph_from_files
    src_by_path = {p: file_at_ref(repo, p, ref) for p in files}
    src_by_path = {p: v for p, v in src_by_path.items() if v is not None}
    gnodes, gedges = build_graph_from_files(src_by_path)
    c.graph.init_schema()
    c.graph.upsert_nodes(list(gnodes))
    c.graph.upsert_edges(gedges)
    c.graph.close()
    click.echo(f"Проиндексировано файлов: {len(files)} @ {sha[:7]}; "
               f"граф: узлов {len(gnodes)}, рёбер {len(gedges)}")

@cli.command()
@click.argument("query")
def search(query: str) -> None:
    """Гибридный поиск по base-индексу (диагностика)."""
    s = Settings()
    c = build_components(s)
    try:
        qvec = c.embedder.embed_query(query)
        hits = c.store.hybrid_search(query_text=query, query_embedding=qvec,
                                     overlay_ref="", changed_paths=[], top_k=10)
        for h in hits:
            click.echo(f"{h.score:.3f}  {h.node_id}  ({h.path}:{h.start_line})")
    finally:
        c.graph.close()

@cli.command()
@click.argument("slug")
@click.argument("pr", type=int)
@click.option("--dry-run", is_flag=True, help="Посчитать ревью и вывести в консоль, не публикуя")
def review(slug: str, pr: int, dry_run: bool) -> None:
    """Отревьюить PR на GitHub и запостить inline+сводку."""
    from reviewer.vcs.github import GitHubProvider
    from reviewer.agent.graph import build_graph
    from reviewer.agent.state import Deps, ReviewUnit
    from reviewer.agent.analyzer import LLMAnalyzer, LLMVerifier, LLMSynthesizer
    from reviewer.policy.policy import ReviewPolicy
    from reviewer.index.chunker import chunk_python
    from reviewer.llm.usage import UsageLog
    # JSONL-лог решений ревью для анализа ложных срабатываний; выключен по умолчанию
    from reviewer.llm.verdicts import VerdictLog

    s = Settings()
    # Fail-fast: проверяем ключи до любых сетевых вызовов, перечисляем все отсутствующие
    missing = [name for name, val in (
        ("OPENROUTER_API_KEY", s.openrouter_api_key),
        ("VOYAGE_API_KEY", s.voyage_api_key),
        ("GITHUB_TOKEN", s.github_token),
    ) if not val]
    if missing:
        raise click.ClickException(
            "Не задан " + ", ".join(missing) + " (.env)")

    c = build_components(s)
    owner, repo = slug.split("/")
    vcs = None
    try:
        vcs = GitHubProvider(owner, repo, token=s.github_token)
        prq = vcs.get_pull_request(pr)
        # Драфт-PR пропускаем (overlay ещё не построен — ничего чистить не нужно)
        if prq.draft and s.review_skip_drafts:
            click.echo("PR — драфт, ревью пропущено (REVIEW_SKIP_DRAFTS=true).")
            return
        files = vcs.get_changed_files(pr)
        # Только существующие .py-файлы попадают в индекс и ревью
        changed = [f.path for f in files if f.path.endswith(".py") and f.status != "removed"]

        # Свежесть base-индекса: подтягиваем чанки файлов, изменённых после
        # последней индексации (граф кода обновляется только на reviewer index).
        indexed = c.store.get_index_meta("base")
        if indexed and indexed != prq.base_sha:
            try:
                diff_files = vcs.compare_files(indexed, prq.base_sha)
                update_base(c.store, c.embedder, "", prq.base_ref,
                            [f.path for f in diff_files if f.status != "removed"],
                            read=lambda p: vcs.get_file_at_ref(p, prq.base_sha),
                            removed_files=[f.path for f in diff_files if f.status == "removed"])
                c.store.set_index_meta("base", prq.base_sha)
                click.echo(f"Base-индекс синхронизирован: {len(diff_files)} файлов "
                           f"({indexed[:7]}..{prq.base_sha[:7]}).")
            except Exception as e:
                log.warning("Не удалось синхронизировать base-индекс: %s", e)
                click.echo("Внимание: base-индекс может быть устаревшим "
                           "(синхронизация не удалась).")
        elif not indexed:
            click.echo("Внимание: SHA base-индекса неизвестен (выполните reviewer index) "
                       "— индекс может быть устаревшим.")

        build_overlay(c.store, c.embedder, pr, changed,
                      read_head=lambda p: vcs.get_file_at_ref(p, prq.head_sha))

        units = []
        for f in files:
            if not f.path.endswith(".py"):
                continue
            # Пропускаем удалённые файлы и файлы, у которых head-версия пуста:
            # на удалённый/пустой файл нечего ревьюить, экономим полный tool-loop.
            if f.status == "removed":
                continue
            src = vcs.get_file_at_ref(f.path, prq.head_sha) or ""
            if not src:
                continue
            node_ids = [ch.node_id for ch in chunk_python(f.path, src.encode())]
            units.append(ReviewUnit(f.path, node_ids, f.patch or "", new_source=src))

        # Кап файлов на ревью: лишние уходят в сводку как пропущенные
        skipped_paths = None
        if len(units) > s.review_max_files:
            skipped_paths = [u.path for u in units[s.review_max_files:]]
            units = units[:s.review_max_files]
            click.echo(f"Внимание: файлов в PR больше лимита (review_max_files="
                       f"{s.review_max_files}); пропущено {len(skipped_paths)}.")

        # sources нужны верификатору для проверки наличия символов в актуальном коде
        sources = {u.path: u.new_source for u in units}

        usage = UsageLog()
        verdicts = VerdictLog(s.review_verdict_log, pr=pr) if s.review_verdict_log else None

        policy = ReviewPolicy.load(s, vcs.get_file_at_ref(".review.yml", prq.base_ref))
        changed_status = {f.path: f.status for f in files}

        # Кросс-файловый синтез имеет смысл только при ≥2 файлах в PR
        synthesizer = (
            LLMSynthesizer(c.llm_provider, prompt_cache=s.openrouter_prompt_cache)
            if s.review_synthesis and len(changed) >= 2
            else None
        )

        deps = Deps(
            vcs=vcs,
            retriever=c.retriever,
            graph=c.graph,
            policy=policy,
            analyzer=LLMAnalyzer(
                c.llm_provider,
                s.review_max_tool_iterations,
                prompt_cache=s.openrouter_prompt_cache,
            ),
            verifier=LLMVerifier(
                c.llm_provider,
                agentic=s.review_agentic_verify,
                max_iterations=s.review_verify_max_iterations,
                min_severity=s.review_verify_min_severity,
                model=(s.openrouter_model_verify or None),
                prompt_cache=s.openrouter_prompt_cache,
            ),
            pr_number=pr,
            head_sha=prq.head_sha,
            overlay_ref=f"pr:{pr}",
            changed_paths=changed,
            patches={f.path: f.patch for f in files},
            suggestions_mode=s.review_suggestions,
            pr_title=prq.title,
            pr_body=prq.body,
            changed_status=changed_status,
            synthesizer=synthesizer,
            sources=sources,
            usage=usage,
            verdicts=verdicts,
            skipped_paths=skipped_paths,
        )
        state = build_graph(deps, publish=not dry_run).invoke(
            {"review_units": units, "findings": [], "failed_units": [],
             "verified": [], "summary": "", "inline_comments": []},
            config={"max_concurrency": s.review_max_parallel_files},
        )
        if dry_run:
            click.echo(state["summary"])
            for ic in state["inline_comments"]:
                click.echo(f"\n{ic.path}:{ic.line}\n{ic.body[:200]}")
            click.echo("\nDry-run: ревью НЕ опубликовано.")
        else:
            click.echo("Ревью опубликовано.")
        rep = usage.report()
        if rep:
            click.echo(rep)
    finally:
        # Удаляем эфемерный overlay pr:N — он не нужен после завершения ревью
        try:
            c.store.delete_ref(f"pr:{pr}")
        except Exception:
            log.warning("Не удалось очистить overlay pr:%s", pr, exc_info=True)
        if c.graph:
            c.graph.close()
        if vcs:
            vcs.close()
