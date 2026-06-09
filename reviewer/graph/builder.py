from __future__ import annotations
from dataclasses import dataclass, field

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from reviewer.index.chunker import chunk_python
from reviewer.graph.scip import build_fqn_resolver

_PY = Language(tspython.language())
_PARSER = Parser(_PY)


@dataclass
class Imports:
    """Таблица импортов одного файла (для import-aware резолвинга вызовов).

    - ``names`` — простое имя -> (модуль, имя_символа_в_модуле). Покрывает
      ``from a.b import foo`` и ``from a.b import foo as bar``.
    - ``module_aliases`` — локальное имя модуля -> dotted-модуль. Покрывает
      ``import a.b`` (имена ``a`` и ``a.b``) и ``import a.b as ab`` (имя ``ab``).
    - ``star_modules`` — модули из ``from a.b import *`` (star-источники файла).
    """

    names: dict[str, tuple[str, str]] = field(default_factory=dict)
    module_aliases: dict[str, str] = field(default_factory=dict)
    star_modules: list[str] = field(default_factory=list)


def _called_name(call_node) -> str | None:
    """Простое имя вызываемого: ``foo()`` -> ``foo``, ``mod.foo()`` -> ``foo``."""
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "identifier":
        return fn.text.decode("utf-8")
    if fn.type == "attribute":
        attr = fn.child_by_field_name("attribute")
        return attr.text.decode("utf-8") if attr is not None else None
    return None


def _call_receiver(call_node) -> str | None:
    """Объект-получатель для вызова через атрибут: ``mod.foo()`` -> ``mod``.

    Возвращает текст левой части (``object``) только если это простой
    identifier; для ``self.foo()`` вернёт ``self``. Иначе ``None``.
    """
    fn = call_node.child_by_field_name("function")
    if fn is None or fn.type != "attribute":
        return None
    obj = fn.child_by_field_name("object")
    if obj is not None and obj.type == "identifier":
        return obj.text.decode("utf-8")
    return None


def _iter_calls(node):
    if node.type == "call":
        yield node
    for ch in node.children:
        yield from _iter_calls(ch)


def _parse_imports(source: str) -> Imports:
    """Разбирает импорты файла в :class:`Imports` через tree-sitter."""
    imports = Imports()
    tree = _PARSER.parse(source.encode("utf-8"))
    for node in tree.root_node.children:
        if node.type == "import_statement":
            _parse_import_statement(node, imports)
        elif node.type == "import_from_statement":
            _parse_import_from(node, imports)
    return imports


def _parse_import_statement(node, imports: Imports) -> None:
    """``import a.b`` / ``import a.b as ab`` / ``import os, sys``."""
    for name_node in node.children_by_field_name("name"):
        if name_node.type == "aliased_import":
            mod_node = name_node.child_by_field_name("name")
            alias_node = name_node.child_by_field_name("alias")
            if mod_node is None or alias_node is None:
                continue
            module = mod_node.text.decode("utf-8")
            alias = alias_node.text.decode("utf-8")
            imports.module_aliases[alias] = module
        elif name_node.type == "dotted_name":
            module = name_node.text.decode("utf-8")
            # ``import a.b`` вводит имена ``a`` и ``a.b``, оба указывают на модуль
            imports.module_aliases.setdefault(module, module)
            imports.module_aliases.setdefault(module.split(".")[0], module)


def _module_name_text(module_node) -> str:
    """Текст модуля для ``from``-импорта, включая относительный (``.x``)."""
    return module_node.text.decode("utf-8") if module_node is not None else ""


def _parse_import_from(node, imports: Imports) -> None:
    """``from a.b import foo [as bar]`` / ``from .x import y`` / ``from a import *``."""
    module = _module_name_text(node.child_by_field_name("module_name"))
    if not module:
        return
    for name_node in node.children_by_field_name("name"):
        if name_node.type == "aliased_import":
            orig_node = name_node.child_by_field_name("name")
            alias_node = name_node.child_by_field_name("alias")
            if orig_node is None or alias_node is None:
                continue
            orig = orig_node.text.decode("utf-8")
            alias = alias_node.text.decode("utf-8")
            imports.names[alias] = (module, orig)
        elif name_node.type == "dotted_name":
            local = name_node.text.decode("utf-8")
            imports.names[local] = (module, local)
    # star-import: дочерний узел wildcard_import
    if any(ch.type == "wildcard_import" for ch in node.children):
        imports.star_modules.append(module)


def _resolve_module(module: str, current_path: str, files: dict[str, str]) -> str | None:
    """Резолвит dotted-модуль в существующий ключ ``files`` или ``None``.

    Поддерживает абсолютные (``a.b``) и относительные (``.x``, ``..pkg.x``)
    импорты. Для ``a.b`` ищет ``a/b.py``, затем ``a/b/__init__.py``.
    """
    if module.startswith("."):
        # relative_import: ведущие точки = подъём по директориям
        dots = len(module) - len(module.lstrip("."))
        tail = module[dots:]  # часть после точек, напр. ``pkg.x`` или ``""``
        # директория текущего файла как список сегментов
        parts = current_path.split("/")[:-1]
        # одна точка = текущий пакет; каждая лишняя точка поднимает на уровень
        up = dots - 1
        if up > len(parts):
            return None
        base = parts[: len(parts) - up] if up else parts
        rel_parts = tail.split(".") if tail else []
        segments = base + rel_parts
    else:
        segments = module.split(".")

    if not segments:
        return None
    base_path = "/".join(segments)
    for candidate in (f"{base_path}.py", f"{base_path}/__init__.py"):
        if candidate in files:
            return candidate
    return None


def _symbols_named(name: str, path: str, name_to_nodes_by_path: dict[str, dict[str, list[str]]]):
    """Узлы с простым именем ``name`` в файле ``path`` (или ``[]``)."""
    return name_to_nodes_by_path.get(path, {}).get(name, [])


def build_graph_from_files(files: dict[str, str]):
    """Строит (nodes, edges) графа кода по tree-sitter с import-aware резолвингом.

    Узлы = все символы (``path#fqn``). Рёбра CALLS резолвятся по приоритету
    от точного к размытому: локальные определения файла → импортированные имена
    → star-импорты → глобальный fallback (все символы с этим простым именем).
    """
    chunks_by_path: dict[str, list] = {}
    name_to_nodes: dict[str, list[str]] = {}                  # глобально: имя -> узлы
    name_to_nodes_by_path: dict[str, dict[str, list[str]]] = {}  # по файлу: имя -> узлы
    nodes: set[str] = set()
    for path, src in files.items():
        chunks = chunk_python(path, src.encode("utf-8"))
        chunks_by_path[path] = chunks
        per_file = name_to_nodes_by_path.setdefault(path, {})
        for c in chunks:
            nodes.add(c.node_id)
            simple = c.symbol_fqn.split(".")[-1]
            name_to_nodes.setdefault(simple, []).append(c.node_id)
            per_file.setdefault(simple, []).append(c.node_id)

    imports_by_path = {path: _parse_imports(src) for path, src in files.items()}
    resolve = build_fqn_resolver(chunks_by_path)
    edges: list[tuple[str, str, str]] = []

    for path, src in files.items():
        imports = imports_by_path[path]
        tree = _PARSER.parse(src.encode("utf-8"))
        for call in _iter_calls(tree.root_node):
            name = _called_name(call)
            if not name:
                continue
            caller_fqn = resolve(path, call.start_point[0] + 1)
            if not caller_fqn:
                continue
            caller = f"{path}#{caller_fqn}"
            for callee in _resolve_call(name, call, path, imports, files,
                                        name_to_nodes, name_to_nodes_by_path):
                if callee != caller:
                    edges.append((caller, "CALLS", callee))

    return nodes, list(dict.fromkeys(edges))


def _resolve_call(name, call, path, imports: Imports, files,
                  name_to_nodes, name_to_nodes_by_path) -> list[str]:
    """Резолвит один вызов в список callee-узлов (приоритет от точного к размытому).

    a) локальные определения файла; b) импортированные имена (и ``mod.foo()``
    через импорт модуля); c) star-импорты; d) глобальный fallback по имени.
    """
    receiver = _call_receiver(call)

    # Вызов через атрибут на импортированный модуль: ``ab.foo()`` / ``a.b.foo()``.
    if receiver is not None and receiver != "self":
        module = imports.module_aliases.get(receiver)
        if module is not None:
            target = _resolve_module(module, path, files)
            if target is not None:
                hits = _symbols_named(name, target, name_to_nodes_by_path)
                if hits:
                    return hits
            return []  # импорт модуля найден, но символ/модуль не резолвится — без рёбер

    # a) локальные определения файла F перекрывают всё остальное
    local = _symbols_named(name, path, name_to_nodes_by_path)
    if local:
        return local

    # b) импортированные имена ``from a.b import foo [as bar]``
    if name in imports.names:
        module, orig = imports.names[name]
        target = _resolve_module(module, path, files)
        if target is not None:
            hits = _symbols_named(orig, target, name_to_nodes_by_path)
            if hits:
                return hits
        return []  # имя импортировано, но цель не резолвится — без рёбер

    # c) star-импорты ``from a.b import *``
    for module in imports.star_modules:
        target = _resolve_module(module, path, files)
        if target is None:
            continue
        hits = _symbols_named(name, target, name_to_nodes_by_path)
        if hits:
            return hits

    # d) глобальный fallback — все символы с этим простым именем (поведение v1)
    return list(name_to_nodes.get(name, []))
