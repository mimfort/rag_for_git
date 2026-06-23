from __future__ import annotations
from collections.abc import Callable

from reviewer.index.chunker import chunk_python
from reviewer.index.pathfilter import is_ignored
from reviewer.index.refs import base_ref
from reviewer.index.store import ChunkRow


def _rows_for_file(repo: str, path: str, source: str, ref: str) -> list[ChunkRow]:
    chunks = chunk_python(path, source.encode("utf-8"))
    return [ChunkRow(repo=repo, ref=ref, content_hash=c.content_hash, path=c.path,
                     lang=c.lang, symbol_fqn=c.symbol_fqn, kind=c.kind,
                     start_line=c.start_line, end_line=c.end_line,
                     text=c.text, embedding=[]) for c in chunks]


def _embed_and_upsert(store, embedder, repo: str, rows: list[ChunkRow]) -> None:
    if not rows:
        return
    # cross-branch reuse: готовые векторы из других ветвей того же репо по content_hash
    cached = store.find_embeddings_by_hashes(repo, [r.content_hash for r in rows])
    to_embed = [r for r in rows if r.content_hash not in cached]
    if to_embed:
        vecs = embedder.embed_documents([r.text for r in to_embed])
        for r, v in zip(to_embed, vecs):
            r.embedding = v
    for r in rows:
        if r.content_hash in cached:
            r.embedding = cached[r.content_hash]
    store.upsert(rows)


def build_overlay(store, embedder, repo: str, pr_number: int, changed_files: list[str],
                  head_sources: dict[str, str], ignore: list[str] = ()) -> None:
    """Чанкует изменённые файлы PR head в (repo, ref='pr:<n>'). Дедуп по content_hash.

    Аргументы:
        head_sources: словарь path → содержимое head-версии файла.
                      Файлы без содержимого или не-.py пропускаются.
        ignore: список ignore-паттернов; совпадающие пути пропускаются без эмбеддинга.
    """
    ref = f"pr:{pr_number}"
    seen = store.existing_hashes(repo, ref)
    batch: list[ChunkRow] = []
    for path in changed_files:
        if not path.endswith(".py"):
            continue
        if is_ignored(path, list(ignore)):
            continue
        src = head_sources.get(path)
        if not src:
            continue
        for row in _rows_for_file(repo, path, src, ref):
            if row.content_hash not in seen:
                seen.add(row.content_hash)
                batch.append(row)
    _embed_and_upsert(store, embedder, repo, batch)


def update_base(store, embedder, repo: str, target_ref: str,
                changed_files: list[str],
                read: Callable[[str], str | None],
                removed_files: list[str] | tuple[str, ...] = (),
                ignore: list[str] = ()) -> None:
    """Инкрементально обновляет (repo, ref='base:<target_ref>') по изменённым файлам.

    removed_files — пути файлов, удалённых из репо; их чанки вычищаются из индекса.
    ignore — список ignore-паттернов; совпадающие пути пропускаются и вычищаются из base.
    Для каждого обработанного файла удаляются символы, исчезнувшие из новой версии.
    """
    ref = base_ref(target_ref)
    py_removed = [p for p in removed_files if p.endswith(".py")]
    store.delete_paths(repo, ref, py_removed)

    seen = store.existing_hashes(repo, ref)
    batch: list[ChunkRow] = []
    for path in changed_files:
        if not path.endswith(".py"):
            continue
        if is_ignored(path, list(ignore)):
            store.delete_paths(repo, ref, [path])   # путь стал игнор — вычищаем из base
            continue
        src = read(path)
        if src is None:
            # Файл недоступен/удалён — вычищаем его чанки из индекса
            store.delete_paths(repo, ref, [path])
            continue
        rows = _rows_for_file(repo, path, src, ref)
        # Удаляем символы, исчезнувшие из новой версии файла
        store.delete_missing_symbols(repo, ref, path, [r.symbol_fqn for r in rows])
        for row in rows:
            if row.content_hash not in seen:
                seen.add(row.content_hash)
                batch.append(row)
    _embed_and_upsert(store, embedder, repo, batch)
