from __future__ import annotations
import click

from reviewer.config.settings import Settings
from reviewer.app import build_components
from reviewer.gitutil import changed_files, file_at_ref, list_python_files
from reviewer.index.freshness import update_base, build_overlay

@click.group()
def cli() -> None: ...

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
    # --- граф кода ---
    from reviewer.graph.builder import build_graph_from_files
    src_by_path = {p: file_at_ref(repo, p, ref) for p in files}
    src_by_path = {p: v for p, v in src_by_path.items() if v is not None}
    gnodes, gedges = build_graph_from_files(src_by_path)
    c.graph.init_schema()
    c.graph.upsert_nodes(list(gnodes))
    c.graph.upsert_edges(gedges)
    c.graph.close()
    click.echo(f"Проиндексировано файлов: {len(files)}; граф: узлов {len(gnodes)}, рёбер {len(gedges)}")

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
def review(slug: str, pr: int) -> None:
    """Отревьюить PR на GitHub и запостить inline+сводку."""
    from reviewer.vcs.github import GitHubProvider
    from reviewer.agent.graph import build_graph
    from reviewer.agent.state import Deps, ReviewUnit
    from reviewer.agent.analyzer import LLMAnalyzer, LLMVerifier
    from reviewer.policy.policy import ReviewPolicy
    from reviewer.index.chunker import chunk_python

    s = Settings()
    c = build_components(s)
    owner, repo = slug.split("/")
    vcs = None
    try:
        vcs = GitHubProvider(owner, repo, token=s.github_token)
        prq = vcs.get_pull_request(pr)
        files = vcs.get_changed_files(pr)
        changed = [f.path for f in files if f.path.endswith(".py")]

        build_overlay(c.store, c.embedder, pr, changed,
                      read_head=lambda p: vcs.get_file_at_ref(p, prq.head_sha))

        units = []
        for f in files:
            if not f.path.endswith(".py"):
                continue
            src = vcs.get_file_at_ref(f.path, prq.head_sha) or ""
            node_ids = [ch.node_id for ch in chunk_python(f.path, src.encode())]
            units.append(ReviewUnit(f.path, node_ids, f.patch or ""))

        policy = ReviewPolicy.from_yaml(vcs.get_file_at_ref(".review.yml", prq.base_ref))
        deps = Deps(vcs=vcs, retriever=c.retriever, graph=c.graph, policy=policy,
                    analyzer=LLMAnalyzer(c.llm_provider, s.review_max_tool_iterations),
                    verifier=LLMVerifier(c.llm_provider), pr_number=pr,
                    head_sha=prq.head_sha, overlay_ref=f"pr:{pr}",
                    changed_paths=changed, patches={f.path: f.patch for f in files})
        build_graph(deps).invoke({"review_units": units, "findings": [],
                                  "verified": [], "summary": "", "inline_comments": []})
        click.echo("Ревью опубликовано.")
    finally:
        if c.graph:
            c.graph.close()
        if vcs:
            vcs.close()
