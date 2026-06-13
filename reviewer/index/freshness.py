from __future__ import annotations
from collections.abc import Callable

from reviewer.index.chunker import chunk_python
from reviewer.index.store import ChunkRow


def _rows_for_file(repo: str, path: str, source: str, ref: str) -> list[ChunkRow]:
    chunks = chunk_python(path, source.encode("utf-8"))
    return [ChunkRow(repo=repo, ref=ref, content_hash=c.content_hash, path=c.path,
                     lang=c.lang, symbol_fqn=c.symbol_fqn, kind=c.kind,
                     start_line=c.start_line, end_line=c.end_line,
                     text=c.text, embedding=[]) for c in chunks]


def _embed_and_upsert(store, embedder, rows: list[ChunkRow]) -> None:
    if not rows:
        return
    vecs = embedder.embed_documents([r.text for r in rows])
    for r, v in zip(rows, vecs):
        r.embedding = v
    store.upsert(rows)


def build_overlay(store, embedder, repo: str, pr_number: int, changed_files: list[str],
                  head_sources: dict[str, str]) -> None:
    """Чанкует изменённые файлы PR head в (repo, ref='pr:<n>'). Дедуп по content_hash.

    Аргументы:
        head_sources: словарь path → содержимое head-версии файла.
                      Файлы без содержимого или не-.py пропускаются.
    """
    ref = f"pr:{pr_number}"
    seen = store.existing_hashes(repo, ref)
    batch: list[ChunkRow] = []
    for path in changed_files:
        if not path.endswith(".py"):
            continue
        src = head_sources.get(path)
        if not src:
            continue
        for row in _rows_for_file(repo, path, src, ref):
            if row.content_hash not in seen:
                seen.add(row.content_hash)
                batch.append(row)
    _embed_and_upsert(store, embedder, batch)


def update_base(store, embedder, repo: str, target_ref: str,
                changed_files: list[str],
                read: Callable[[str], str | None],
                removed_files: list[str] | tuple[str, ...] = ()) -> None:
    """Инкрементально обновляет (repo, ref='base') по изменённым файлам целевой ветки.

    removed_files — пути файлов, удалённых из репо; их чанки вычищаются из индекса.
    Для каждого обработанного файла удаляются символы, исчезнувшие из новой версии.
    """
    py_removed = [p for p in removed_files if p.endswith(".py")]
    store.delete_paths(repo, "base", py_removed)

    seen = store.existing_hashes(repo, "base")
    batch: list[ChunkRow] = []
    for path in changed_files:
        if not path.endswith(".py"):
            continue
        src = read(path)
        if src is None:
            # Файл недоступен/удалён — вычищаем его чанки из индекса
            store.delete_paths(repo, "base", [path])
            continue
        rows = _rows_for_file(repo, path, src, "base")
        # Удаляем символы, исчезнувшие из новой версии файла
        store.delete_missing_symbols(repo, "base", path, [r.symbol_fqn for r in rows])
        for row in rows:
            if row.content_hash not in seen:
                seen.add(row.content_hash)
                batch.append(row)
    _embed_and_upsert(store, embedder, batch)
