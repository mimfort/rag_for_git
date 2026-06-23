"""Кластеризация графа кода по модулям/пути и расчёт ключа свежести.

Чистая логика без I/O: членство и центральность приходят аргументами, поэтому
покрывается unit-тестами без Postgres/Neo4j. Кластер = пакет/директория
(префикс пути node_id="path#fqn"); summary каждого кластера пишет LLM-скилл.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable


@dataclass
class Member:
    node_id: str          # "path#fqn"
    path: str
    content_hash: str
    skeleton_hash: str    # хэш структурного скелета — ключ свежести по структуре (PRI-165)
    start_line: int


@dataclass
class Cluster:
    key: str
    member_node_ids: list[str]
    files: list[str]
    top_symbols: list[dict]   # [{"node_id", "file", "line"}], отсортированы по центральности
    num_members: int
    source_hash: str


def cluster_key(path: str, depth: int) -> str:
    """Ключ кластера = директория пути, обрезанная до первых ``depth`` сегментов.

    "reviewer/index/store.py", depth=2 -> "reviewer/index".
    Файл в корне -> "<root>"; директория короче depth -> вся директория.
    """
    dir_parts = path.split("/")[:-1]      # отбросить имя файла
    if not dir_parts:
        return "<root>"
    return "/".join(dir_parts[:depth])


def compute_source_hash(items: list[tuple[str, str]]) -> str:
    """sha256 от sorted("node_id:skeleton_hash") — детерминированный ключ свежести.

    Меняется при изменении состава кластера или СТРУКТУРЫ его членов (сигнатуры/docstring),
    но НЕ при правке тела (PRI-165: вход — skeleton_hash, не content_hash). Сортировка пар
    делает ключ независимым от порядка членов."""
    joined = "\n".join(sorted(f"{nid}:{h}" for nid, h in items))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def build_clusters(
    members: list[Member],
    in_degree_fn: Callable[[list[str]], dict[str, int]] | None,
    *,
    depth: int = 2,
    min_size: int = 1,
    top_n: int = 10,
) -> list[Cluster]:
    """Сгруппировать членов по cluster_key; top_symbols — по in_degree (fail-soft)."""
    groups: dict[str, list[Member]] = {}
    for m in members:
        groups.setdefault(cluster_key(m.path, depth), []).append(m)

    degrees: dict[str, int] = {}
    if in_degree_fn is not None:
        try:
            degrees = in_degree_fn([m.node_id for m in members]) or {}
        except Exception:
            degrees = {}                  # граф недоступен → порядок по (path, line)

    clusters: list[Cluster] = []
    for key, ms in sorted(groups.items()):
        if len(ms) < min_size:
            continue
        ranked = sorted(
            ms, key=lambda m: (-degrees.get(m.node_id, 0), m.path, m.start_line))
        top = [{"node_id": m.node_id, "file": m.path, "line": m.start_line}
               for m in ranked[:top_n]]
        clusters.append(Cluster(
            key=key,
            member_node_ids=sorted(m.node_id for m in ms),
            files=sorted({m.path for m in ms}),
            top_symbols=top,
            num_members=len(ms),
            source_hash=compute_source_hash([(m.node_id, m.skeleton_hash) for m in ms]),
        ))
    return clusters
