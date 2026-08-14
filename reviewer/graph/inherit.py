"""Извлечение наследования классов из синтаксиса (tree-sitter).

Зачем отдельный источник, если есть SCIP: scip-python 0.6.6 не эмитит
``SymbolInformation`` для класса, упомянутого в файле ВЫШЕ своего определения,
а значит и ``si.relationships`` у такого класса нет — читать нечего. В этот
провал попадают все 11 адаптеров досок: каждый регистрируется в
``provider_spec()``, объявленной до класса. Синтаксический разбор такого
класса не теряет: ``class X(Y)`` виден всегда.

Модуль эмитит ТОЛЬКО рёбра наследования. CALLS остаются за SCIP (там они
type-aware) и за ``builder.build_graph_from_files``.
"""
from __future__ import annotations

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from reviewer.graph.scip import build_fqn_resolver

_PY = Language(tspython.language())
_PARSER = Parser(_PY)


def _iter_class_definitions(node):
    """Обход всех ``class_definition`` дерева, включая вложенные."""
    if node.type == "class_definition":
        yield node
    for child in node.children:
        yield from _iter_class_definitions(child)


def _base_names(class_node) -> list[str]:
    """Простые имена суперклассов: ``class C(a.B, D)`` -> ``['B', 'D']``.

    Keyword-аргументы списка баз (``metaclass=…``, ``total=False``) пропускаются:
    это не базы. Сложные выражения (``Generic[T]``) сводятся к имени слева.
    """
    args = class_node.child_by_field_name("superclasses")
    if args is None:
        return []
    names: list[str] = []
    for child in args.children:
        if child.type in ("(", ")", ",", "comment", "keyword_argument"):
            continue
        names.append(_leading_name(child))
    return [n for n in names if n]


def _leading_name(node) -> str:
    """Имя из узла-базы: ``B`` -> ``B``; ``a.B`` -> ``B``; ``G[T]`` -> ``G``."""
    if node.type == "identifier":
        return node.text.decode("utf-8")
    if node.type == "attribute":
        attr = node.child_by_field_name("attribute")
        return attr.text.decode("utf-8") if attr is not None else ""
    if node.type == "subscript":
        value = node.child_by_field_name("value")
        return _leading_name(value) if value is not None else ""
    return ""


def extract_inheritance_edges(files, chunks_by_path, name_to_nodes,
                              name_to_nodes_by_path) -> list[tuple[str, str, str]]:
    """Рёбра ``(class_node_id, "IMPLEMENTS", base_node_id)`` по всем файлам.

    Резолвинг имени базы повторяет приоритет ``_resolve_call``: локальные
    определения файла → импортированные имена → star-импорты → глобальный
    fallback по простому имени. Неразрешённая база ребра не даёт — догадки
    здесь дороже пропуска.

    Класс сопоставляется с чанком через ``build_fqn_resolver`` (самый узкий
    чанк, покрывающий строку ``class``), а не по точному ``start_line``:
    чанкер начинает чанк декорированного класса со строки декоратора, а
    tree-sitter ``class_definition.start_point`` указывает на строку
    ``class`` — точное совпадение строк промахнулось бы мимо каждого
    декорированного класса.
    """
    from reviewer.graph.builder import _parse_imports, _resolve_module, _symbols_named

    edges: list[tuple[str, str, str]] = []
    resolve = build_fqn_resolver(chunks_by_path)

    for path, src in files.items():
        imports = _parse_imports(src)
        tree = _PARSER.parse(src.encode("utf-8"))
        for cls in _iter_class_definitions(tree.root_node):
            child_fqn = resolve(path, cls.start_point[0] + 1)
            if not child_fqn:
                continue
            child = f"{path}#{child_fqn}"
            for name in _base_names(cls):
                for base in _resolve_base(name, path, imports, files,
                                          name_to_nodes, name_to_nodes_by_path,
                                          _resolve_module, _symbols_named):
                    if base != child:
                        edges.append((child, "IMPLEMENTS", base))
    return list(dict.fromkeys(edges))


def _resolve_base(name, path, imports, files, name_to_nodes,
                  name_to_nodes_by_path, resolve_module, symbols_named) -> list[str]:
    """Имя базы -> список node_id (приоритет от точного к размытому)."""
    local = symbols_named(name, path, name_to_nodes_by_path)
    if local:
        return local
    if name in imports.names:
        module, orig = imports.names[name]
        target = resolve_module(module, path, files)
        if target is not None:
            return symbols_named(orig, target, name_to_nodes_by_path)
        return []
    for module in imports.star_modules:
        target = resolve_module(module, path, files)
        if target is None:
            continue
        hits = symbols_named(name, target, name_to_nodes_by_path)
        if hits:
            return hits
    return list(name_to_nodes.get(name, []))
