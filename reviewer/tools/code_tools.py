from __future__ import annotations
import functools
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from langchain_core.tools import StructuredTool

from reviewer.index.refs import base_ref

_DUP_STUB = "(повтор: результат уже показан выше)"

@dataclass
class ToolContext:
    retriever: Any
    graph: Any
    overlay_ref: str
    changed_paths: list[str]
    changed_node_ids: list[str] = field(default_factory=list)
    repo: str = ""
    branch: str = ""
    read_file_fn: Callable[[str], str | None] | None = None
    patches: dict = field(default_factory=dict)
    store: Any = None
    cache: dict | None = None

def _memoize(fn, ctx_sig, seen, cache):
    """Оборачивает tool-функцию: run-level кэш результатов (cache) + дедуп-заглушка
    повторов в пределах юнита (seen). Ключ = (имя, нормализованные args, ctx_sig).
    functools.wraps сохраняет имя/докстринг/сигнатуру -> StructuredTool строит ту же схему."""
    sig = inspect.signature(fn)

    def _key(args, kwargs):
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return (fn.__name__,
                json.dumps(bound.arguments, sort_keys=True, ensure_ascii=False, default=str),
                ctx_sig)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        key = _key(args, kwargs)
        if key in seen:
            return _DUP_STUB
        if cache is not None and key in cache:
            result = cache[key]
        else:
            result = fn(*args, **kwargs)
            if cache is not None:
                cache[key] = result
        seen.add(key)
        return result

    return wrapper


def make_tools(ctx: ToolContext) -> list[StructuredTool]:
    def search_code(query: str) -> str:
        """Семантико-лексический поиск релевантного кода по всему репозиторию."""
        pack = ctx.retriever.retrieve(
            ctx.repo, query=query, changed_node_ids=ctx.changed_node_ids,
            overlay_ref=ctx.overlay_ref, changed_paths=ctx.changed_paths, top_k=8,
            branch=ctx.branch)
        return pack.as_context() or "(ничего не найдено)"

    def get_related_symbols(node_id: str) -> str:
        """Связанные символы (вызовы/реализации/тесты) для node_id вида 'path#fqn'."""
        related = ctx.graph.expand(ctx.repo, [node_id], hops=2, branch=ctx.branch)
        return "\n".join(sorted(related)) or "(нет связей)"

    def read_file(path: str, start: int = 1, end: int = 400) -> str:
        """Точный исходник файла на head-ревизии PR, строки [start..end] с номерами (N|код).
        Окно ограничено 400 строками."""
        if ctx.read_file_fn is None:
            return "(чтение файлов недоступно)"
        src = ctx.read_file_fn(path)
        if src is None:
            return f"(файл не найден: {path})"
        lines = src.splitlines()
        if not lines:
            return "(файл пуст)"
        s = max(1, start)
        if s > len(lines):
            return f"(нет строки {s}; в файле {len(lines)} строк)"
        e = min(len(lines), end)
        capped = (e - s + 1 > 400)
        if capped:
            e = s + 399
        body = "\n".join(f"{i}|{lines[i - 1]}" for i in range(s, e + 1))
        if capped:
            body += "\n(…усечено)"
        return body

    def get_definition(symbol: str) -> str:
        """Где определён символ + его исходный код. Резолв имени через граф, код — через индекс.
        Фолбэк на семантический поиск, если граф не нашёл совпадений или стор недоступен."""
        ids: list[str] = []
        if ctx.graph is not None and hasattr(ctx.graph, "find_symbol"):
            ids = ctx.graph.find_symbol(ctx.repo, symbol, branch=ctx.branch)
        if ids and ctx.store is not None:
            nodes = ctx.store.fetch_nodes(ctx.repo, ids[:3], ctx.overlay_ref,
                                          ctx.changed_paths, base_ref=base_ref(ctx.branch))
            if nodes:
                return "\n\n".join(
                    f"// {n.node_id} ({n.path}:{n.start_line}-{n.end_line})\n{n.text}"
                    for n in nodes)
        pack = ctx.retriever.retrieve(
            ctx.repo, query=symbol, changed_node_ids=ctx.changed_node_ids,
            overlay_ref=ctx.overlay_ref, changed_paths=ctx.changed_paths, top_k=3,
            branch=ctx.branch)
        return pack.as_context() or "(определение не найдено)"

    def find_callers(node_id: str) -> str:
        """Кто вызывает символ node_id ('path#fqn') — направленный CALLS (impact-анализ)."""
        if ctx.graph is None or not hasattr(ctx.graph, "callers"):
            return "(граф недоступен)"
        found = ctx.graph.callers(ctx.repo, [node_id], branch=ctx.branch)
        return "\n".join(sorted(found)) or "(вызовов не найдено)"

    def get_changed_file_diff(path: str) -> str:
        """Дифф другого изменённого файла этого PR."""
        patch = (ctx.patches or {}).get(path)
        return patch or "(файл не входит в изменения PR)"

    def get_impact() -> str:
        """Радиус поражения PR: символы с ИЗМЕНЁННОЙ сигнатурой → их вызывающие вне диффа.
        Помечает места, которые могут быть не обновлены под новый контракт (кросс-файловый impact).
        Сам не выносит вердикт — подтверждай находки через read_file."""
        from reviewer.tools.impact import compute_impact, format_impact
        if ctx.graph is None or ctx.store is None:
            return "(граф или индекс недоступны)"
        items = compute_impact(
            ctx.graph, ctx.store, repo=ctx.repo, branch=ctx.branch,
            changed_node_ids=ctx.changed_node_ids, changed_paths=ctx.changed_paths,
            overlay_ref=ctx.overlay_ref)
        return format_impact(items)

    seen: set = set()
    ctx_sig = (ctx.repo, ctx.branch, ctx.overlay_ref,
               tuple(sorted(ctx.changed_paths or [])),
               tuple(sorted(ctx.changed_node_ids or [])))
    raw = [search_code, get_related_symbols, read_file,
           get_definition, find_callers, get_changed_file_diff, get_impact]
    return [StructuredTool.from_function(_memoize(fn, ctx_sig, seen, ctx.cache)) for fn in raw]
