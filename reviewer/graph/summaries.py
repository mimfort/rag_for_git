"""Кластеризация графа кода по модулям/пути и расчёт ключа свежести.

Чистая логика без I/O: членство и центральность приходят аргументами, поэтому
покрывается unit-тестами без Postgres/Neo4j. Кластер = пакет/директория
(префикс пути node_id="path#fqn"); summary каждого кластера пишет LLM-скилл.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Sequence


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


def depth_for(path: str, default: int, overrides: dict[str, int]) -> int:
    """Глубина для пути: depth самого длинного ключа-префикса ``overrides``
    (совпадение по сегментам ДИРЕКТОРИИ), иначе ``default``.

    Ключ ``"reviewer/index"`` матчит ``"reviewer/index/store.py"``, но НЕ
    ``"reviewer/indexer/x.py"`` (сравнение посегментно, не по строке-префиксу).
    """
    if not overrides:
        return default
    dir_parts = path.split("/")[:-1]
    best_depth, best_len = default, -1
    for key, d in overrides.items():
        kparts = key.strip("/").split("/")
        if dir_parts[:len(kparts)] == kparts and len(kparts) > best_len:
            best_depth, best_len = d, len(kparts)
    return best_depth


def normalize_depth_overrides(overrides: dict[str, int]) -> dict[str, int]:
    """Нормализовать aliases префиксов и отклонить противоречивые depth."""
    normalized: dict[str, int] = {}
    for raw_prefix, raw_depth in overrides.items():
        prefix = str(raw_prefix).strip("/")
        depth = int(raw_depth)
        previous = normalized.get(prefix)
        if previous is not None and previous != depth:
            raise ValueError(
                f"Конфликтующие summary depth overrides для {prefix!r}: "
                f"{previous} и {depth}"
            )
        normalized[prefix] = depth
    return dict(sorted(normalized.items()))


def normalize_summary_paths_ignore(patterns: Sequence[str] | None) -> list[str]:
    """Канонизировать ignore-паттерны кластеризации сводок.

    strip пробелов и '/', отбрасывание пустых, дедуп, сортировка — чтобы
    порядок и написание в .review.yml не меняли layout_token.
    """
    cleaned = {str(p).strip().strip("/") for p in (patterns or []) if p is not None}
    return sorted(p for p in cleaned if p)


def canonicalize_layout(
    default_depth: int,
    overrides: dict[str, int],
    summary_paths_ignore: Sequence[str] | None = None,
) -> tuple[dict[str, int], list[str], str]:
    """Вернуть единые normalized overrides, normalized ignore и token.

    ``summary_paths_ignore`` входит в payload token'а намеренно (PRI-245):
    смена состава кластеров обязана инвалидировать layout, иначе штатный
    prune не соберёт осиротевшие сводки.
    """
    normalized = normalize_depth_overrides(overrides)
    ignore = normalize_summary_paths_ignore(summary_paths_ignore)
    payload = json.dumps(
        {
            "default_depth": int(default_depth),
            "overrides": list(normalized.items()),
            "summary_paths_ignore": ignore,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    token = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return normalized, ignore, token


def compute_layout_token(
    default_depth: int,
    overrides: dict[str, int],
    summary_paths_ignore: Sequence[str] | None = None,
) -> str:
    """Вернуть canonical identity effective layout policy."""
    return canonicalize_layout(default_depth, overrides, summary_paths_ignore)[2]


def compute_source_hash(items: list[tuple[str, str]]) -> str:
    """sha256 от sorted("node_id:skeleton_hash") — детерминированный ключ свежести.

    Меняется при изменении состава кластера или СТРУКТУРЫ его членов (сигнатуры/docstring),
    но НЕ при правке тела (PRI-165: вход — skeleton_hash, не content_hash). Сортировка пар
    делает ключ независимым от порядка членов."""
    joined = "\n".join(sorted(f"{nid}:{h}" for nid, h in items))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def compute_file_fingerprints(members: list[Member]) -> dict[str, str]:
    """Вернуть детерминированные структурные fingerprint для каждого файла.

    Отпечаток зависит только от ``node_id`` и ``skeleton_hash`` его символов;
    изменение тела символа не требует пересборки пофайловой сводки.
    """
    grouped: dict[str, list[tuple[str, str]]] = {}
    for member in members:
        grouped.setdefault(member.path, []).append((member.node_id, member.skeleton_hash))
    return {path: compute_source_hash(items) for path, items in sorted(grouped.items())}


def build_clusters(
    members: list[Member],
    in_degree_fn: Callable[[list[str]], dict[str, int]] | None,
    *,
    depth: int = 2,
    min_size: int = 1,
    top_n: int = 10,
    depth_overrides: dict[str, int] | None = None,
) -> list[Cluster]:
    """Сгруппировать членов по cluster_key; top_symbols — по in_degree (fail-soft).

    ``depth_overrides`` задаёт per-prefix глубину: longest-prefix-match по
    сегментам директории; при отсутствии совпадения используется ``depth``.
    """
    overrides = normalize_depth_overrides(depth_overrides or {})
    groups: dict[str, list[Member]] = {}
    for m in members:
        groups.setdefault(cluster_key(m.path, depth_for(m.path, depth, overrides)), []).append(m)

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
