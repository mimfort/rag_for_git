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
    # Один вектор на текст даже при нескольких путях или символах.
    texts = {row.content_hash: row.text for row in rows}
    cached = store.find_embeddings_by_hashes(repo, list(texts))
    to_embed = {digest: text for digest, text in texts.items() if digest not in cached}
    if to_embed:
        vectors = embedder.embed_documents(list(to_embed.values()))
        cached.update(zip(to_embed, vectors, strict=True))
    for r in rows:
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
    ignore_list = list(ignore)
    seen = store.existing_hashes(repo, ref)
    batch: list[ChunkRow] = []
    for path in changed_files:
        if not path.endswith(".py"):
            continue
        if is_ignored(path, ignore_list):
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
    ignore_list = list(ignore)
    removed = [path for path in removed_files if path.endswith(".py")]
    locations = store.chunk_locations(repo, ref, changed_files)
    kept_symbols: dict[str, list[str]] = {}
    batch: list[ChunkRow] = []
    for path in changed_files:
        if not path.endswith(".py"):
            continue
        if is_ignored(path, ignore_list):
            removed.append(path)
            continue
        src = read(path)
        if src is None:
            removed.append(path)
            continue
        rows = {row.symbol_fqn: row for row in _rows_for_file(repo, path, src, ref)}
        kept_symbols[path] = list(rows)
        for symbol, row in rows.items():
            location = (row.content_hash, row.start_line, row.end_line, row.kind)
            if locations.get((path, symbol)) != location:
                batch.append(row)
    # Старые строки ещё нужны для переиспользования векторов при переносе кода.
    _embed_and_upsert(store, embedder, repo, batch)
    store.delete_paths(repo, ref, [path for path in dict.fromkeys(removed)
                                   if path not in kept_symbols])
    for path, symbols in kept_symbols.items():
        store.delete_missing_symbols(repo, ref, path, symbols)
