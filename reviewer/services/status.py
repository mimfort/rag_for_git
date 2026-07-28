"""Сбор и рендер статуса base-индекса (команда `reviewer status`).

Чистый слой без эмбеддера/Settings: данные берутся только из стора чанков,
графа и git — поэтому команда не тратит Voyage и легко тестируется на фейках.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from reviewer.gitutil import commits_behind
from reviewer.index.refs import base_ref


@dataclass
class BranchStatus:
    branch: str
    ref: str
    indexed_sha: str | None
    updated_at: datetime | None
    chunks: int
    graph_nodes: int | None
    drift: int | None
    summaries: int | None = None


@dataclass
class OverlayStatus:
    ref: str
    chunks: int


@dataclass
class RepoStatus:
    repo: str
    branches: list[BranchStatus]
    overlays: list[OverlayStatus]


def _drift(repo_path: str, sha: str, branch: str) -> int | None:
    """Дрейф ветки: пробуем локальный ref, затем origin/<branch>; иначе None."""
    for cand in (branch, f"origin/{branch}"):
        n = commits_behind(repo_path, sha, cand)
        if n is not None:
            return n
    return None


def build_status_report(store, graph, repo: str, branches: list[str],
                        repo_path: str, *, summary_store=None) -> RepoStatus:
    """Собрать RepoStatus по веткам. Neo4j и стор сводок fail-soft (поле=None при сбое)."""
    branch_statuses: list[BranchStatus] = []
    for branch in branches:
        ref = base_ref(branch)
        row = store.get_index_meta_row(repo, ref)
        sha = row[0] if row else None
        updated_at = row[1] if row else None
        chunks = store.count_chunks(repo, ref)
        try:
            graph_nodes = graph.count_nodes(repo, branch)
        except Exception:  # noqa: BLE001 — Neo4j недоступен, граф недоступен
            graph_nodes = None
        try:
            summaries = summary_store.count_summaries(repo, branch) if summary_store else None
        except Exception:  # noqa: BLE001 — стор сводок недоступен
            summaries = None
        drift = _drift(repo_path, sha, branch) if sha else None
        branch_statuses.append(BranchStatus(
            branch=branch, ref=ref, indexed_sha=sha, updated_at=updated_at,
            chunks=chunks, graph_nodes=graph_nodes, drift=drift, summaries=summaries))
    overlays = [
        OverlayStatus(ref=r, chunks=store.count_chunks(repo, r))
        for r in store.list_refs(repo)
        if not r.startswith("base:")
    ]
    return RepoStatus(repo=repo, branches=branch_statuses, overlays=overlays)


def render_status_json(report: RepoStatus) -> str:
    """Машиночитаемый JSON по RepoStatus (для скилов-потребителей).

    Полный SHA (не усечён — потребитель машинный), datetime → ISO 8601,
    None → null. `backend` в JSON не включается: это подсказка только для
    текстового вывода, в список требуемых полей не входит.
    """
    payload = {
        "repo": report.repo,
        "branches": [
            {
                "branch": b.branch,
                "ref": b.ref,
                "indexed_sha": b.indexed_sha,
                "updated_at": b.updated_at.isoformat() if b.updated_at else None,
                "chunks": b.chunks,
                "graph_nodes": b.graph_nodes,
                "drift": b.drift,
            }
            for b in report.branches
        ],
        "overlays": [{"ref": o.ref, "chunks": o.chunks} for o in report.overlays],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


def render_status(report: RepoStatus, backend: str) -> str:
    """Человекочитаемый отчёт по RepoStatus (для click.echo)."""
    lines = [
        f"Репозиторий: {report.repo}",
        f"Граф (бэкенд для индексации): {backend}",
        "",
    ]
    for b in report.branches:
        lines.append(f"Ветка {b.branch}   [{b.ref}]")
        if b.indexed_sha is None:
            lines.append("  SHA:    — (не проиндексирована)")
            lines.append("")
            continue
        lines.append(
            f"  SHA:    {b.indexed_sha[:7]}  (проиндексировано {_fmt_dt(b.updated_at)})")
        if b.drift is None:
            lines.append("  Статус: дрейф неизвестен (нет git-клона)")
        elif b.drift == 0:
            lines.append("  Статус: ✓ свежо")
        else:
            lines.append(f"  Статус: ↗ отстаёт на {b.drift} коммитов")
        nodes = "—  (Neo4j недоступен)" if b.graph_nodes is None else str(b.graph_nodes)
        lines.append(f"  Чанки:  {b.chunks}   Узлы графа: {nodes}")
        lines.append("")
    if report.overlays:
        lines.append("Overlay:")
        for o in report.overlays:
            lines.append(f"  {o.ref}   {o.chunks} чанков")
    return "\n".join(lines).rstrip() + "\n"
