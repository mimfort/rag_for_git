"""Оркестрация выбора бэкенда построения графа кода.

Поддерживает два бэкенда:
- ``scip``       — точный граф через scip-python (CALLS + IMPLEMENTS);
- ``treesitter`` — быстрый граф через tree-sitter (только CALLS, резолвинг по имени).

Режим ``auto`` (по умолчанию) выбирает SCIP при наличии ``scip-python`` в PATH,
иначе молча откатывается на tree-sitter.
"""
from __future__ import annotations

import logging
import shutil

from reviewer.graph.builder import build_graph_from_files
from reviewer.graph.scip import build_fqn_resolver, chunks_by_path_for, parse_scip, run_scip_python
from reviewer.gitutil import add_worktree, remove_worktree

log = logging.getLogger(__name__)


def scip_available() -> bool:
    """Вернуть True, если scip-python найден в PATH."""
    return shutil.which("scip-python") is not None


def graph_from_scip_bytes(data: bytes, chunks_by_path: dict) -> tuple[set[str], list]:
    """Разобрать index.scip-байты и вернуть (nodes, edges).

    ``chunks_by_path`` — {path: list[Chunk]} (результат ``chunks_by_path_for``).

    Импорт protobuf-класса Index выполняется локально, чтобы зависимость
    требовалась только на SCIP-ветке кода.
    """
    from reviewer.graph.scip_pb2 import Index  # локальный импорт (protobuf нужен только здесь)

    idx = Index()
    idx.ParseFromString(data)

    resolve = build_fqn_resolver(chunks_by_path)
    nodes, edges = parse_scip(idx, resolve)

    # Добавляем все node_id из чанков — листовые символы, которых SCIP не обходил,
    # тоже должны быть в графе (паритет с tree-sitter-бэкендом).
    all_nodes = set(nodes) | {c.node_id for cs in chunks_by_path.values() for c in cs}
    return all_nodes, edges


def build_with_scip(repo: str, ref: str, src_by_path: dict[str, str]) -> tuple[set, list]:
    """Построить граф через SCIP.

    Создаёт временный git worktree на ``ref``, запускает scip-python в нём
    и возвращает граф из полученного index.scip.

    :raises Exception: при любой ошибке (subprocess, parse и т.д.)
    """
    chunks_by_path = chunks_by_path_for(src_by_path)
    wt = add_worktree(repo, ref)
    try:
        data = run_scip_python(wt)
    finally:
        remove_worktree(repo, wt)
    return graph_from_scip_bytes(data, chunks_by_path)


def build_code_graph(
    repo: str,
    ref: str,
    files: list[str],
    src_by_path: dict[str, str],
    backend: str = "auto",
) -> tuple[set, list, str]:
    """Построить граф кода выбранным бэкендом.

    :param repo: путь к локальному репозиторию.
    :param ref: git-ref (ветка, SHA), который индексируется.
    :param files: список путей Python-файлов (не используется напрямую; передаётся
                  для обратной совместимости с сигнатурой CLI).
    :param src_by_path: {path: source} — содержимое файлов.
    :param backend: ``"auto"`` | ``"scip"`` | ``"treesitter"``.
    :returns: ``(nodes, edges, backend_used)``
    """
    want_scip = backend == "scip" or (backend == "auto" and scip_available())

    if want_scip:
        try:
            nodes, edges = build_with_scip(repo, ref, src_by_path)
            return nodes, edges, "scip"
        except Exception as e:
            if backend == "scip":
                # Явный выбор бэкенда: пробрасываем ошибку, не деградируем молча.
                raise
            log.warning("SCIP-индексация не удалась, откат на tree-sitter: %s", e)

    # tree-sitter fallback
    nodes, edges = build_graph_from_files(src_by_path)
    return nodes, edges, "treesitter"
