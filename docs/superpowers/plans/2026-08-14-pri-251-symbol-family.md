# PRI-251 Symbol Family Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать агенту способ перечислить семейство однотипных символов целиком (все 11 адаптеров досок, оба VCS-провайдера) вместо одного представителя, и сделать неполный ответ явно помеченным.

**Architecture:** Наследование классов извлекается tree-sitter'ом синтаксически и сливается с рёбрами SCIP (SCIP теряет `SymbolInformation` у forward-referenced классов — все 11 адаптеров попадают в этот провал). Поверх надёжного наследования считается семейство по структурному покрытию Protocol-контракта с учётом унаследованных методов. Результат отдаётся новым session-less MCP-тулом `family`, который всегда сообщает, какие сигналы сработали.

**Tech Stack:** Python 3.11+, tree-sitter (`tree_sitter_python`), Neo4j (Cypher), FastMCP, pytest.

**Spec:** `docs/superpowers/specs/2026-08-14-pri-251-symbol-family-design.md`

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, сообщения CLI и MCP-ответов.
- Коммиты: Conventional Commits на русском, **без self-attribution** (никаких `Co-Authored-By`, упоминаний Claude).
- Unit-тесты запрещают внешние и localhost-сокеты. Любой тест с реальной сетью обязан иметь `@pytest.mark.integration`.
- Запуск unit-тестов: `.venv/bin/pytest -q`. Integration: `.venv/bin/pytest -q -m integration`.
- `node_id = "path#fqn"` — единый ключ связи RAG↔граф. Не менять.
- Граф скоупится `(repo, branch)`: составная уникальность `(repo, branch, id)`.
- Любая правка контента под `plugin/` требует прогона `scripts/update_codex_plugin_manifest.py`, иначе install-тесты краснеют.
- `ruff` не чист на репозитории целиком — приводить в порядок только затронутые файлы, не гнаться за repo-wide clean.

---

### Task 1: Наследование классов из tree-sitter

Синтаксический источник рёбер `IMPLEMENTS`, независимый от SCIP. Резолвинг имени базы переиспользует существующий import-aware механизм `builder.py`.

**Files:**
- Create: `reviewer/graph/inherit.py`
- Modify: `reviewer/graph/builder.py:158-199` (`build_graph_from_files`)
- Modify: `reviewer/graph/backend.py:49-63` (`build_with_scip`)
- Test: `tests/graph/test_inherit.py`

**Interfaces:**
- Consumes: `reviewer.graph.builder._parse_imports`, `_resolve_module`, `_symbols_named` (существующие приватные хелперы), `reviewer.index.chunker.chunk_python`, `reviewer.graph.scip.build_fqn_resolver`.
- Produces: `extract_inheritance_edges(files: dict[str, str], chunks_by_path: dict[str, list], name_to_nodes: dict[str, list[str]], name_to_nodes_by_path: dict[str, dict[str, list[str]]]) -> list[tuple[str, str, str]]` — рёбра вида `(child_node_id, "IMPLEMENTS", base_node_id)`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/graph/test_inherit.py`:

```python
from reviewer.graph.builder import build_graph_from_files


def test_inheritance_edge_from_imported_base():
    """Наследование от базы в соседнем модуле даёт ребро IMPLEMENTS."""
    files = {
        "pkg/base.py": "class RestBase:\n    def close(self) -> None:\n        pass\n",
        "pkg/adapter.py": (
            "from pkg.base import RestBase\n\n\n"
            "class Adapter(RestBase):\n    board_type = 'x'\n"
        ),
    }
    _, edges = build_graph_from_files(files)
    assert ("pkg/adapter.py#Adapter", "IMPLEMENTS", "pkg/base.py#RestBase") in edges


def test_inheritance_edge_for_forward_referenced_class():
    """Класс, упомянутый выше своего определения, наследование не теряет.

    Ровно этот случай теряет scip-python 0.6.6: SymbolInformation для такого
    класса он не эмитит, и рёбра наследования из SCIP не приходит.
    """
    files = {
        "pkg/base.py": "class RestBase:\n    pass\n",
        "pkg/adapter.py": (
            "from pkg.base import RestBase\n\n\n"
            "def provider_spec():\n    return Adapter\n\n\n"
            "class Adapter(RestBase):\n    pass\n"
        ),
    }
    _, edges = build_graph_from_files(files)
    assert ("pkg/adapter.py#Adapter", "IMPLEMENTS", "pkg/base.py#RestBase") in edges


def test_multiple_bases_give_one_edge_each():
    """Множественное наследование даёт по ребру на каждую резолвленную базу."""
    files = {
        "pkg/a.py": "class A:\n    pass\n",
        "pkg/b.py": "class B:\n    pass\n",
        "pkg/c.py": (
            "from pkg.a import A\nfrom pkg.b import B\n\n\n"
            "class C(A, B):\n    pass\n"
        ),
    }
    _, edges = build_graph_from_files(files)
    assert ("pkg/c.py#C", "IMPLEMENTS", "pkg/a.py#A") in edges
    assert ("pkg/c.py#C", "IMPLEMENTS", "pkg/b.py#B") in edges


def test_local_base_wins_over_global_name_collision():
    """Локальное определение базы перекрывает одноимённый символ другого файла."""
    files = {
        "pkg/other.py": "class Base:\n    pass\n",
        "pkg/local.py": "class Base:\n    pass\n\n\nclass Child(Base):\n    pass\n",
    }
    _, edges = build_graph_from_files(files)
    assert ("pkg/local.py#Child", "IMPLEMENTS", "pkg/local.py#Base") in edges
    assert ("pkg/local.py#Child", "IMPLEMENTS", "pkg/other.py#Base") not in edges


def test_unresolvable_base_yields_no_edge():
    """База из непроиндексированного модуля рёбер не даёт (без догадок)."""
    files = {
        "pkg/adapter.py": "from httpx import Client\n\n\nclass Adapter(Client):\n    pass\n",
    }
    _, edges = build_graph_from_files(files)
    assert not [e for e in edges if e[1] == "IMPLEMENTS"]


def test_calls_edges_are_unchanged():
    """Извлечение наследования не ломает существующие рёбра CALLS."""
    files = {
        "pkg/m.py": "def helper():\n    pass\n\n\ndef caller():\n    helper()\n",
    }
    _, edges = build_graph_from_files(files)
    assert ("pkg/m.py#caller", "CALLS", "pkg/m.py#helper") in edges
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `.venv/bin/pytest tests/graph/test_inherit.py -v`
Expected: FAIL — рёбра `IMPLEMENTS` не эмитятся (проходит только `test_unresolvable_base_yields_no_edge` и `test_calls_edges_are_unchanged`).

- [ ] **Step 3: Написать `reviewer/graph/inherit.py`**

```python
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
    """
    from reviewer.graph.builder import _parse_imports, _resolve_module, _symbols_named

    edges: list[tuple[str, str, str]] = []
    fqn_by_start: dict[str, dict[int, str]] = {}
    for path, chunks in chunks_by_path.items():
        fqn_by_start[path] = {c.start_line: c.symbol_fqn for c in chunks}

    for path, src in files.items():
        imports = _parse_imports(src)
        tree = _PARSER.parse(src.encode("utf-8"))
        for cls in _iter_class_definitions(tree.root_node):
            line1 = cls.start_point[0] + 1
            child_fqn = fqn_by_start.get(path, {}).get(line1)
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
```

- [ ] **Step 4: Подключить извлечение в `build_graph_from_files`**

В `reviewer/graph/builder.py` заменить финальный `return` функции `build_graph_from_files` (строка 199) на вызов с наследованием:

```python
    edges += extract_inheritance_edges(files, chunks_by_path,
                                       name_to_nodes, name_to_nodes_by_path)
    return nodes, list(dict.fromkeys(edges))
```

И добавить импорт в шапку файла (после `from reviewer.graph.scip import build_fqn_resolver`):

```python
from reviewer.graph.inherit import extract_inheritance_edges
```

Обновить докстринг `build_graph_from_files`: после предложения про CALLS добавить строку —

```
    Рёбра IMPLEMENTS — наследование классов, извлечённое синтаксически
    (см. :mod:`reviewer.graph.inherit`).
```

- [ ] **Step 5: Прогнать тест, убедиться что проходит**

Run: `.venv/bin/pytest tests/graph/test_inherit.py -v`
Expected: PASS (6 тестов)

- [ ] **Step 6: Написать падающий тест на слияние в SCIP-ветке**

Дописать в `tests/graph/test_backend.py`:

```python
def test_scip_branch_merges_tree_sitter_inheritance(monkeypatch):
    """build_with_scip добавляет наследование из синтаксиса к рёбрам SCIP.

    SCIP теряет наследование forward-referenced класса; слияние это чинит,
    не трогая CALLS.
    """
    from reviewer.graph import backend

    src = {
        "pkg/base.py": "class RestBase:\n    pass\n",
        "pkg/adapter.py": (
            "from pkg.base import RestBase\n\n\n"
            "def spec():\n    return Adapter\n\n\n"
            "class Adapter(RestBase):\n    pass\n"
        ),
    }
    # SCIP «прислал» только CALLS — ровно как на реальном репозитории.
    monkeypatch.setattr(backend, "add_worktree", lambda repo, ref: "/tmp/wt")
    monkeypatch.setattr(backend, "remove_worktree", lambda repo, path: None)
    monkeypatch.setattr(backend, "run_scip_python", lambda wt: b"")
    monkeypatch.setattr(
        backend, "graph_from_scip_bytes",
        lambda data, chunks: ({"pkg/adapter.py#spec"},
                              [("pkg/adapter.py#spec", "CALLS", "pkg/base.py#RestBase")]),
    )

    nodes, edges = backend.build_with_scip("/repo", "dev", src)

    assert ("pkg/adapter.py#Adapter", "IMPLEMENTS", "pkg/base.py#RestBase") in edges
    assert ("pkg/adapter.py#spec", "CALLS", "pkg/base.py#RestBase") in edges


def test_scip_branch_deduplicates_inheritance_edges(monkeypatch):
    """Если SCIP уже прислал ребро наследования, дубликата не возникает."""
    from reviewer.graph import backend

    src = {
        "pkg/base.py": "class RestBase:\n    pass\n",
        "pkg/adapter.py": "from pkg.base import RestBase\n\n\nclass Adapter(RestBase):\n    pass\n",
    }
    edge = ("pkg/adapter.py#Adapter", "IMPLEMENTS", "pkg/base.py#RestBase")
    monkeypatch.setattr(backend, "add_worktree", lambda repo, ref: "/tmp/wt")
    monkeypatch.setattr(backend, "remove_worktree", lambda repo, path: None)
    monkeypatch.setattr(backend, "run_scip_python", lambda wt: b"")
    monkeypatch.setattr(backend, "graph_from_scip_bytes",
                        lambda data, chunks: (set(), [edge]))

    _, edges = backend.build_with_scip("/repo", "dev", src)

    assert edges.count(edge) == 1
```

- [ ] **Step 7: Прогнать тест, убедиться что падает**

Run: `.venv/bin/pytest tests/graph/test_backend.py -k merges_tree_sitter -v`
Expected: FAIL — ребра `IMPLEMENTS` в результате `build_with_scip` нет.

- [ ] **Step 8: Реализовать слияние в `build_with_scip`**

В `reviewer/graph/backend.py` заменить тело `build_with_scip` (строки 49-63) на:

```python
def build_with_scip(repo: str, ref: str, src_by_path: dict[str, str]) -> tuple[set, list]:
    """Построить граф через SCIP, добрав наследование классов синтаксически.

    Создаёт временный git worktree на ``ref``, запускает scip-python в нём
    и возвращает граф из полученного index.scip.

    Наследование классов приходит НЕ из SCIP: scip-python 0.6.6 не эмитит
    ``SymbolInformation`` для класса, упомянутого выше своего определения
    (в этот провал попадают все адаптеры досок), поэтому рёбра ``IMPLEMENTS``
    класс→база добираются tree-sitter'ом и сливаются с рёбрами SCIP.
    SCIP остаётся источником точных ``CALLS`` и метод-уровневых ``IMPLEMENTS``.

    :raises Exception: при любой ошибке (subprocess, parse и т.д.)
    """
    chunks_by_path = chunks_by_path_for(src_by_path)
    wt = add_worktree(repo, ref)
    try:
        data = run_scip_python(wt)
    finally:
        remove_worktree(repo, wt)
    nodes, edges = graph_from_scip_bytes(data, chunks_by_path)
    edges = list(dict.fromkeys(edges + inheritance_edges(src_by_path, chunks_by_path)))
    return nodes, edges
```

Добавить в шапку `backend.py` импорт и функцию-хелпер:

```python
from reviewer.graph.inherit import extract_inheritance_edges


def inheritance_edges(src_by_path: dict[str, str], chunks_by_path: dict) -> list:
    """Рёбра наследования из синтаксиса — индексы имён строятся здесь же."""
    name_to_nodes: dict[str, list[str]] = {}
    name_to_nodes_by_path: dict[str, dict[str, list[str]]] = {}
    for path, chunks in chunks_by_path.items():
        per_file = name_to_nodes_by_path.setdefault(path, {})
        for c in chunks:
            simple = c.symbol_fqn.split(".")[-1]
            name_to_nodes.setdefault(simple, []).append(c.node_id)
            per_file.setdefault(simple, []).append(c.node_id)
    return extract_inheritance_edges(src_by_path, chunks_by_path,
                                     name_to_nodes, name_to_nodes_by_path)
```

- [ ] **Step 9: Прогнать оба набора тестов**

Run: `.venv/bin/pytest tests/graph/ -q`
Expected: PASS — новые тесты зелёные, существующие тесты графа не сломаны.

- [ ] **Step 10: Прогнать линт по затронутым файлам**

Run: `.venv/bin/ruff check reviewer/graph/inherit.py reviewer/graph/builder.py reviewer/graph/backend.py tests/graph/test_inherit.py`
Expected: без ошибок.

- [ ] **Step 11: Коммит**

```bash
git add reviewer/graph/inherit.py reviewer/graph/builder.py reviewer/graph/backend.py tests/graph/test_inherit.py tests/graph/test_backend.py
git commit -m "feat(graph): наследование классов из tree-sitter, слияние с рёбрами SCIP"
```

---

### Task 2: Семейство символов поверх графа

Чистая логика семейства + запросы к Neo4j. Структурное сопоставление контракта считает методы с учётом унаследованных — без Task 1 это невозможно.

**Files:**
- Create: `reviewer/graph/family.py`
- Modify: `reviewer/graph/store.py` (после `implementations_detailed`, строка 120)
- Test: `tests/graph/test_family.py`

**Interfaces:**
- Consumes: `GraphStore.implementations_detailed(repo, node_ids, *, branch)` из Task 1-контекста (существующий).
- Produces:
  - `GraphStore.members_by_prefix(repo, prefixes: list[str], *, branch: str = "") -> dict[str, list[str]]` — для каждого префикса `path#Class.` список id членов.
  - `GraphStore.bases_of(repo, node_ids, *, branch: str = "") -> dict[str, list[str]]` — исходящие `IMPLEMENTS` (класс → его базы).
  - `GraphStore.class_nodes(repo, *, branch: str = "") -> list[str]` — id всех узлов-классов (без точки в fqn-части).
  - `reviewer.graph.family.effective_methods(node_id, own, bases_map, own_by_node) -> set[str]`
  - `reviewer.graph.family.structural_matches(contract: str, contract_methods: set[str], candidates: dict[str, set[str]]) -> list[str]`
  - `reviewer.graph.family.FamilyResult` — dataclass `{members: list[str], signals: list[str], complete: bool, note: str}`

- [ ] **Step 1: Написать падающий тест чистой логики**

Создать `tests/graph/test_family.py`:

```python
from reviewer.graph.family import (
    FamilyResult,
    effective_methods,
    merge_signals,
    structural_matches,
)


def test_effective_methods_includes_inherited():
    """Методы класса = собственные ∪ унаследованные по цепочке IMPLEMENTS.

    Ровно этот учёт отличает 6 найденных адаптеров от 11: остальные не
    переопределяют close(), он достаётся им от базы.
    """
    own_by_node = {
        "b.py#Base": {"close", "secrets"},
        "a.py#Adapter": {"fetch", "normalize"},
    }
    bases_map = {"a.py#Adapter": ["b.py#Base"]}
    got = effective_methods("a.py#Adapter", own_by_node, bases_map)
    assert got == {"fetch", "normalize", "close", "secrets"}


def test_effective_methods_follows_multi_level_chain():
    """Цепочка наследования обходится до конца."""
    own_by_node = {
        "c.py#Root": {"close"},
        "b.py#Mid": {"secrets"},
        "a.py#Leaf": {"fetch"},
    }
    bases_map = {"a.py#Leaf": ["b.py#Mid"], "b.py#Mid": ["c.py#Root"]}
    assert effective_methods("a.py#Leaf", own_by_node, bases_map) == {
        "fetch", "secrets", "close"
    }


def test_effective_methods_survives_inheritance_cycle():
    """Цикл в рёбрах не вешает обход (граф строится эвристиками)."""
    own_by_node = {"a.py#A": {"x"}, "b.py#B": {"y"}}
    bases_map = {"a.py#A": ["b.py#B"], "b.py#B": ["a.py#A"]}
    assert effective_methods("a.py#A", own_by_node, bases_map) == {"x", "y"}


def test_structural_matches_requires_full_contract_coverage():
    """Класс попадает в семейство, только покрыв ВЕСЬ набор методов контракта."""
    contract_methods = {"fetch", "normalize", "close"}
    candidates = {
        "a.py#Full": {"fetch", "normalize", "close", "extra"},
        "b.py#Partial": {"fetch", "close"},
    }
    got = structural_matches("p.py#Proto", contract_methods, candidates)
    assert got == ["a.py#Full"]


def test_structural_matches_excludes_the_contract_itself():
    """Сам Protocol в список своих реализаций не попадает."""
    contract_methods = {"fetch", "close"}
    candidates = {
        "p.py#Proto": {"fetch", "close"},
        "a.py#Impl": {"fetch", "close"},
    }
    assert structural_matches("p.py#Proto", contract_methods, candidates) == ["a.py#Impl"]


def test_structural_matches_with_empty_contract_returns_nothing():
    """Пустой контракт не делает семейством весь репозиторий."""
    assert structural_matches("p.py#Proto", set(), {"a.py#X": {"f"}}) == []


def test_merge_signals_dedupes_and_records_sources():
    """Слияние сигналов: члены уникальны, источники перечислены."""
    result = merge_signals(
        node_id="b.py#Base",
        inheritance=["a.py#Adapter", "c.py#Other"],
        structural=["c.py#Other", "d.py#Legacy"],
    )
    assert isinstance(result, FamilyResult)
    assert result.members == ["a.py#Adapter", "c.py#Other", "d.py#Legacy"]
    assert result.signals == ["inheritance", "structural"]
    assert result.complete is True


def test_merge_signals_marks_empty_result_incomplete():
    """Пустой ответ помечается неполным — молчаливой пустоты быть не должно."""
    result = merge_signals(node_id="b.py#Base", inheritance=[], structural=[])
    assert result.members == []
    assert result.complete is False
    assert "не найдено" in result.note
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `.venv/bin/pytest tests/graph/test_family.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'reviewer.graph.family'`

- [ ] **Step 3: Написать `reviewer/graph/family.py`**

```python
"""Семейство однотипных символов: «кто ещё такой же» для узла графа.

Два независимых сигнала:

- **inheritance** — подклассы и сиблинги по рёбрам ``IMPLEMENTS``. Надёжен
  после того, как наследование стало приходить из синтаксиса
  (:mod:`reviewer.graph.inherit`).
- **structural** — покрытие полного набора методов контракта с учётом
  унаследованных. Нужен потому, что ``typing.Protocol`` рёбер наследования
  не даёт ни при каком бэкенде: структурная типизация не выражается рёбрами.

Молчаливая пустота хуже ошибки: она неотличима от «семейства нет». Поэтому
результат всегда несёт список сработавших сигналов и признак полноты.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FamilyResult:
    """Семейство узла: члены, сработавшие сигналы, полнота ответа."""

    members: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    complete: bool = True
    note: str = ""


def effective_methods(node_id: str, own_by_node: dict[str, set[str]],
                      bases_map: dict[str, list[str]]) -> set[str]:
    """Простые имена методов класса: собственные ∪ унаследованные.

    Обход по цепочке ``IMPLEMENTS`` защищён от циклов: рёбра строятся
    эвристиками резолвинга имён, и цикл там возможен.
    """
    seen: set[str] = set()
    methods: set[str] = set()
    stack = [node_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        methods |= own_by_node.get(current, set())
        stack.extend(bases_map.get(current, []))
    return methods


def structural_matches(contract: str, contract_methods: set[str],
                       candidates: dict[str, set[str]]) -> list[str]:
    """Классы, покрывающие ВЕСЬ набор методов контракта (сам контракт исключён).

    Полное покрытие, а не частичное: девяти методов ``TaskBoardProvider``
    достаточно, чтобы совпадение не давало шума, а частичное покрытие
    притянуло бы любой класс с ``close()``.
    """
    if not contract_methods:
        return []
    return sorted(
        node for node, methods in candidates.items()
        if node != contract and contract_methods <= methods
    )


def merge_signals(node_id: str, inheritance: list[str],
                  structural: list[str]) -> FamilyResult:
    """Слить сигналы в один ответ, сохранив порядок и назвав источники."""
    members: list[str] = []
    for item in list(inheritance) + list(structural):
        if item != node_id and item not in members:
            members.append(item)
    signals: list[str] = []
    if inheritance:
        signals.append("inheritance")
    if structural:
        signals.append("structural")
    if not members:
        return FamilyResult(
            members=[], signals=signals, complete=False,
            note="семейство не найдено ни по наследованию, ни по структуре "
                 "контракта — это может значить и что его нет, и что сигналы слепы",
        )
    return FamilyResult(members=members, signals=signals, complete=True)
```

- [ ] **Step 4: Прогнать тест, убедиться что проходит**

Run: `.venv/bin/pytest tests/graph/test_family.py -v`
Expected: PASS (8 тестов)

- [ ] **Step 5: Добавить запросы в `GraphStore`**

В `reviewer/graph/store.py` после `implementations_detailed` (строка 120) добавить:

```python
    def bases_of(self, repo: str, node_ids: list[str], *,
                 branch: str = "") -> dict[str, list[str]]:
        """Базы символов — ИСХОДЯЩИЕ IMPLEMENTS (класс → его базы).

        Обратное направление к :meth:`implementations_detailed`; нужно для
        подсчёта унаследованных методов при структурном сопоставлении.
        """
        if not node_ids:
            return {}
        records, _, _ = self._driver.execute_query(
            "UNWIND $ids AS sid "
            "MATCH (s:Symbol {repo: $repo, branch: $branch, id: sid})-[:IMPLEMENTS]->"
            "(b:Symbol {repo: $repo, branch: $branch}) "
            "RETURN sid AS id, collect(DISTINCT b.id) AS bases",
            ids=list(node_ids), repo=repo, branch=branch)
        return {r["id"]: sorted(r["bases"]) for r in records}

    def class_members(self, repo: str, *, branch: str = "") -> dict[str, set[str]]:
        """Собственные методы каждого класса: {'path#Class': {'m1', 'm2'}}.

        Класс и его члены различаются формой node_id: 'path#Class' против
        'path#Class.method'. Вложенные уровни глубже одного не учитываются —
        в этом репозитории их нет.
        """
        records, _, _ = self._driver.execute_query(
            "MATCH (s:Symbol {repo: $repo, branch: $branch}) "
            "WHERE s.id CONTAINS '.' "
            "RETURN s.id AS id",
            repo=repo, branch=branch)
        out: dict[str, set[str]] = {}
        for r in records:
            node_id = r["id"]
            path, _, fqn = node_id.partition("#")
            if "." not in fqn:
                continue
            cls, _, method = fqn.rpartition(".")
            if "." in cls:
                continue
            out.setdefault(f"{path}#{cls}", set()).add(method)
        return out
```

- [ ] **Step 6: Написать тест на запросы стора**

Дописать в `tests/graph/test_family.py`:

```python
class _FakeDriver:
    """Драйвер-заглушка: отдаёт заранее заданные записи на любой запрос."""

    def __init__(self, records):
        self._records = records

    def execute_query(self, *args, **kwargs):
        return self._records, None, None


def _store(records):
    from reviewer.graph.store import GraphStore

    store = GraphStore.__new__(GraphStore)
    store._driver = _FakeDriver(records)
    return store


def test_class_members_groups_methods_by_class():
    """class_members складывает 'path#Class.method' в набор методов класса."""
    store = _store([
        {"id": "a.py#Adapter.fetch"},
        {"id": "a.py#Adapter.close"},
        {"id": "b.py#Base.secrets"},
    ])
    got = store.class_members("owner/name", branch="dev")
    assert got == {"a.py#Adapter": {"fetch", "close"}, "b.py#Base": {"secrets"}}


def test_class_members_ignores_nested_deeper_than_one_level():
    """Символы глубже 'Class.method' в набор методов не попадают."""
    store = _store([{"id": "a.py#Outer.Inner.method"}])
    assert store.class_members("owner/name", branch="dev") == {}


def test_bases_of_returns_outgoing_implements():
    """bases_of отдаёт исходящие IMPLEMENTS, отсортированные."""
    store = _store([{"id": "a.py#Adapter", "bases": ["b.py#Base", "a.py#Mixin"]}])
    got = store.bases_of("owner/name", ["a.py#Adapter"], branch="dev")
    assert got == {"a.py#Adapter": ["a.py#Mixin", "b.py#Base"]}


def test_bases_of_with_no_ids_skips_the_query():
    """Пустой вход не ходит в базу."""
    store = _store([])
    assert store.bases_of("owner/name", [], branch="dev") == {}
```

- [ ] **Step 7: Прогнать тесты**

Run: `.venv/bin/pytest tests/graph/test_family.py -v`
Expected: PASS (12 тестов)

- [ ] **Step 8: Прогнать линт**

Run: `.venv/bin/ruff check reviewer/graph/family.py reviewer/graph/store.py tests/graph/test_family.py`
Expected: без ошибок.

- [ ] **Step 9: Коммит**

```bash
git add reviewer/graph/family.py reviewer/graph/store.py tests/graph/test_family.py
git commit -m "feat(graph): семейство символов по наследованию и структурному контракту"
```

---

### Task 3: MCP-тул `family` и неглухой `implementations`

Публичная поверхность. Тул отдаёт семейство целиком; `implementations` перестаёт молча возвращать пустоту при существующем семействе.

**Files:**
- Modify: `reviewer/mcp/service.py:1791-1813` (`implementations`), добавить метод `family` рядом
- Modify: `reviewer/entrypoints/mcp_server.py:366-372` (регистрация тулов)
- Test: `tests/mcp/test_server_tools.py`
- Test: `tests/mcp/test_family_service.py`

**Interfaces:**
- Consumes: `reviewer.graph.family.{FamilyResult, effective_methods, structural_matches, merge_signals}`, `GraphStore.{implementations_detailed, bases_of, class_members}`, `reviewer.tools.graph_format.format_neighbors`.
- Produces: `MCPReviewService.family(repo: str, node_id: str, branch: str | None = None) -> str` и MCP-тул `family(repo, node_id, branch)`.

- [ ] **Step 1: Написать падающий тест сервисного слоя**

Создать `tests/mcp/test_family_service.py`:

```python
import pytest

from reviewer.graph.family import merge_signals


class _Graph:
    """Граф-заглушка с заданными наследниками, базами и членами классов."""

    def __init__(self, impls=None, bases=None, members=None):
        self._impls = impls or {}
        self._bases = bases or {}
        self._members = members or {}

    def implementations_detailed(self, repo, node_ids, *, branch=""):
        out = []
        for nid in node_ids:
            out += [{"id": i, "rel": "IMPLEMENTS"} for i in self._impls.get(nid, [])]
        return out

    def bases_of(self, repo, node_ids, *, branch=""):
        return {n: self._bases.get(n, []) for n in node_ids}

    def class_members(self, repo, *, branch=""):
        return dict(self._members)


def test_family_finds_subclasses_by_inheritance():
    """Класс с наследниками отдаёт их как семейство по сигналу inheritance."""
    result = merge_signals("b.py#Base", ["a.py#One", "c.py#Two"], [])
    assert result.members == ["a.py#One", "c.py#Two"]
    assert result.signals == ["inheritance"]


def test_family_finds_protocol_implementers_structurally():
    """Protocol без номинальных наследников находит реализации структурно."""
    result = merge_signals("p.py#Proto", [], ["a.py#One", "b.py#Legacy"])
    assert result.members == ["a.py#One", "b.py#Legacy"]
    assert result.signals == ["structural"]
```

- [ ] **Step 2: Прогнать тест, убедиться что проходит частично**

Run: `.venv/bin/pytest tests/mcp/test_family_service.py -v`
Expected: PASS — это заготовка фикстур; сервисные тесты добавляются на шаге 4.

- [ ] **Step 3: Реализовать `MCPReviewService.family`**

В `reviewer/mcp/service.py` сразу после метода `implementations` (после строки 1813) добавить:

```python
    def family(self, repo: str, node_id: str,
               branch: str | None = None) -> str:
        """Семейство однотипных символов: «кто ещё такой же» для node_id.

        Два сигнала: наследование (подклассы и сиблинги по IMPLEMENTS) и
        структурное соответствие контракту (полное покрытие набора методов
        с учётом унаследованных — так находятся реализации typing.Protocol,
        у которых рёбер наследования нет и быть не может).

        Ответ всегда называет сработавшие сигналы: молчаливая пустота
        неотличима от «семейства нет» и потому запрещена.
        """
        rb = self._resolve_repo_branch(repo, branch)
        if isinstance(rb, str):
            return rb
        repo, resolved = rb
        if self.components.graph is None:
            return "(граф недоступен)"
        cl = self._resolve_context_limits(repo, resolved)
        try:
            result = self._compute_family(repo, node_id, resolved)
        except Exception:
            log.warning("family: сбой графа", exc_info=True)
            return "(семейство не определено: сбой графа)"
        header = self._family_header(result)
        if not result.members:
            return header
        body = format_neighbors(
            [{"id": m, "rel": "FAMILY"} for m in result.members],
            store=self.components.store, repo=repo, branch=resolved,
            overlay_ref=None, changed_paths=[],
            empty_msg="(семейство пусто)", cap=cl.graph.callers_topk)
        return f"{header}\n{body}"

    def _compute_family(self, repo: str, node_id: str, branch: str):
        """Собрать семейство узла из обоих сигналов."""
        from reviewer.graph.family import (
            effective_methods,
            merge_signals,
            structural_matches,
        )

        graph = self.components.graph
        inheritance = [n["id"] for n in graph.implementations_detailed(
            repo, [node_id], branch=branch)]
        own = graph.class_members(repo, branch=branch)
        bases = graph.bases_of(repo, list(own), branch=branch)
        contract_methods = effective_methods(node_id, own, bases)
        candidates = {
            cls: effective_methods(cls, own, bases) for cls in own
        }
        structural = structural_matches(node_id, contract_methods, candidates)
        return merge_signals(node_id, inheritance, structural)

    @staticmethod
    def _family_header(result) -> str:
        """Шапка ответа: сигналы и полнота — до списка членов."""
        if not result.members:
            return f"(семейство не найдено; {result.note})"
        signals = ", ".join(result.signals)
        return (f"// семейство из {len(result.members)} членов "
                f"(сигналы: {signals})")
```

- [ ] **Step 4: Написать тест на неглухой `implementations`**

Дописать в `tests/mcp/test_family_service.py`:

```python
def test_implementations_points_at_family_when_it_exists():
    """Пустые прямые наследники + существующее семейство → не голая пустота.

    Именно этот случай воспроизводится на RestBoardBase: прямых наследников
    в графе может не быть, а семейство есть.
    """
    from reviewer.mcp.service import MCPReviewService

    msg = MCPReviewService._implementations_empty_message(family_size=8)
    assert "8" in msg
    assert "family" in msg


def test_implementations_stays_terse_when_no_family_either():
    """Ни наследников, ни семейства — прежний короткий ответ."""
    from reviewer.mcp.service import MCPReviewService

    msg = MCPReviewService._implementations_empty_message(family_size=0)
    assert msg == "(implementations не найдены)"
```

- [ ] **Step 5: Прогнать тест, убедиться что падает**

Run: `.venv/bin/pytest tests/mcp/test_family_service.py -k implementations -v`
Expected: FAIL с `AttributeError: type object 'MCPReviewService' has no attribute '_implementations_empty_message'`

- [ ] **Step 6: Реализовать неглухой `implementations`**

В `reviewer/mcp/service.py` добавить статический метод рядом с `family`:

```python
    @staticmethod
    def _implementations_empty_message(family_size: int) -> str:
        """Сообщение при отсутствии прямых наследников.

        Если семейство всё же существует, молчать нельзя: пустой ответ
        неотличим от «семейства нет» и уводит агента в ложный вывод.
        """
        if family_size > 0:
            return (f"(прямых наследников нет, но семейство из {family_size} "
                    f"членов существует — получить его: тул family)")
        return "(implementations не найдены)"
```

И заменить в методе `implementations` финальный `return` (строки 1810-1813) на:

```python
        if not found:
            try:
                family = self._compute_family(repo, node_id, resolved)
                size = len(family.members)
            except Exception:
                log.warning("implementations: подсчёт семейства не удался", exc_info=True)
                size = 0
            return self._implementations_empty_message(size)
        return format_neighbors(
            found, store=self.components.store, repo=repo, branch=resolved,
            overlay_ref=None, changed_paths=[], empty_msg="(implementations не найдены)",
            cap=cl.graph.callers_topk)
```

Обновить докстринг `implementations`: заменить последнюю строку `Точны после полного \`reviewer index\` с SCIP.` на

```
        Наследование классов приходит из tree-sitter (SCIP теряет его у
        forward-referenced классов); метод-уровневые override-ы — из SCIP.
        Пустой ответ при существующем семействе помечен явно (см. тул family).
```

- [ ] **Step 7: Прогнать тест, убедиться что проходит**

Run: `.venv/bin/pytest tests/mcp/test_family_service.py -v`
Expected: PASS

- [ ] **Step 8: Зарегистрировать MCP-тул**

В `reviewer/entrypoints/mcp_server.py` сразу после регистрации `implementations` (после строки 372) добавить:

```python
    @mcp.tool()
    def family(repo: str, node_id: str, branch: str | None = None) -> str:
        """Symbols of the same family as node_id 'path#fqn' — "who else is like
        this" over the base index (no PR session). Combines two signals:
        inheritance (subclasses/siblings via IMPLEMENTS) and structural contract
        match (full coverage of a Protocol's method set, inherited methods
        included — Protocol implementers have no inheritance edges by design).
        The answer always names which signals fired: a silent empty result is
        indistinguishable from "no family exists" and is never returned.
        Use it for bulk tasks ("add a field to every provider") where one hit
        is a single member of a family of N. branch defaults to the primary
        tracked branch."""
        return service.family(repo, node_id, branch)
```

- [ ] **Step 9: Написать тест форвардинга тула**

Дописать в `tests/mcp/test_server_tools.py` после `test_implementations_tool_forwards` (строка 241):

```python
def test_family_tool_registered():
    import asyncio

    svc = _service()
    svc.family.return_value = "// семейство из 8 членов (сигналы: inheritance)"
    server = create_server(svc)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "family" in names


def test_family_tool_forwards():
    import asyncio

    svc = _service()
    svc.family.return_value = "// семейство из 8 членов (сигналы: inheritance)"
    server = create_server(svc)
    asyncio.run(server.call_tool(
        "family", {"repo": "owner/name", "node_id": "base.py#Base"}))
    svc.family.assert_called_once_with("owner/name", "base.py#Base", None)
```

Файл использует helper `_service()` и `create_server(svc)` + `asyncio.run(server.call_tool(...))` — фикстур pytest здесь нет.

- [ ] **Step 10: Прогнать тесты MCP**

Run: `.venv/bin/pytest tests/mcp/ -q`
Expected: PASS

- [ ] **Step 11: Прогнать линт**

Run: `.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_family_service.py`
Expected: без ошибок.

- [ ] **Step 12: Коммит**

```bash
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_family_service.py tests/mcp/test_server_tools.py
git commit -m "feat(mcp): тул family и явная пометка неполноты в implementations"
```

---

### Task 4: Приёмка на реальных семействах репозитория

Критерий приёмки 5 требует закрепить поведение на настоящих семействах, а не на синтетике. Тест работает на реальном исходном коде репозитория без внешних сервисов.

**Files:**
- Create: `tests/graph/test_real_families.py`
- Test: тот же файл

**Interfaces:**
- Consumes: `reviewer.graph.inherit.extract_inheritance_edges`, `reviewer.graph.family.{effective_methods, structural_matches}`, `reviewer.index.chunker.chunk_python`.

- [ ] **Step 1: Написать тест на реальные семейства**

Создать `tests/graph/test_real_families.py`:

```python
"""Приёмка на настоящих семействах этого репозитория.

Синтетика не ловит регрессию, ради которой делалась задача: провал
воспроизводился именно на 11 адаптерах досок, где SCIP молчит, а три
адаптера вдобавок не имеют общей базы.

Тесты читают исходники с диска — внешних сервисов не требуется.
"""
from __future__ import annotations

import pathlib

import pytest

from reviewer.graph.builder import build_graph_from_files
from reviewer.graph.family import effective_methods, structural_matches
from reviewer.index.chunker import chunk_python

REPO = pathlib.Path(__file__).resolve().parents[2]

REST_BASE = "reviewer/tasks/boards/restbase.py#RestBoardBase"
REST_ADAPTERS = {
    "reviewer/tasks/boards/asana.py#AsanaBoard",
    "reviewer/tasks/boards/clickup.py#ClickUpBoard",
    "reviewer/tasks/boards/github.py#GitHubIssuesBoard",
    "reviewer/tasks/boards/kaiten.py#KaitenBoard",
    "reviewer/tasks/boards/linear.py#LinearBoard",
    "reviewer/tasks/boards/trello.py#TrelloBoard",
    "reviewer/tasks/boards/weeek.py#WeeekBoard",
    "reviewer/tasks/boards/yandex_tracker.py#YandexTrackerBoard",
}
LEGACY_ADAPTERS = {
    "reviewer/tasks/boards/jira.py#JiraCloudBoard",
    "reviewer/tasks/boards/yougile.py#YougileBoard",
    "reviewer/tasks/boards/youtrack.py#YouTrackBoard",
}
BOARD_CONTRACT = "reviewer/tasks/boards/base.py#TaskBoardProvider"
VCS_CONTRACT = "reviewer/vcs/base.py#VCSProvider"


@pytest.fixture(scope="module")
def sources() -> dict[str, str]:
    """Исходники пакета reviewer/, ключ — путь относительно корня репозитория."""
    return {
        str(p.relative_to(REPO)): p.read_text(encoding="utf-8")
        for p in (REPO / "reviewer").rglob("*.py")
    }


@pytest.fixture(scope="module")
def graph(sources):
    """Граф из tree-sitter по реальным исходникам."""
    return build_graph_from_files(sources)


@pytest.fixture(scope="module")
def own_and_bases(sources, graph):
    """Собственные методы классов и карта баз по рёбрам IMPLEMENTS."""
    own: dict[str, set[str]] = {}
    for path, src in sources.items():
        for c in chunk_python(path, src.encode("utf-8")):
            if "." not in c.symbol_fqn:
                continue
            cls, _, method = c.symbol_fqn.rpartition(".")
            if "." in cls:
                continue
            own.setdefault(f"{path}#{cls}", set()).add(method)
    bases: dict[str, list[str]] = {}
    for src_id, rel, dst_id in graph[1]:
        if rel == "IMPLEMENTS":
            bases.setdefault(src_id, []).append(dst_id)
    return own, bases


def test_rest_board_base_yields_all_eight_subclasses(graph):
    """Критерий 1: восемь наследников RestBoardBase видны как IMPLEMENTS."""
    subclasses = {
        src for src, rel, dst in graph[1]
        if rel == "IMPLEMENTS" and dst == REST_BASE
    }
    assert REST_ADAPTERS <= subclasses


def test_board_contract_family_covers_all_eleven_adapters(own_and_bases):
    """Критерий 2: семейство перечисляется целиком, включая три легаси."""
    own, bases = own_and_bases
    contract_methods = effective_methods(BOARD_CONTRACT, own, bases)
    candidates = {cls: effective_methods(cls, own, bases) for cls in own}
    found = set(structural_matches(BOARD_CONTRACT, contract_methods, candidates))
    assert REST_ADAPTERS <= found
    assert LEGACY_ADAPTERS <= found


def test_board_contract_family_has_no_false_positives(own_and_bases):
    """Структурный фильтр не притягивает посторонние классы."""
    own, bases = own_and_bases
    contract_methods = effective_methods(BOARD_CONTRACT, own, bases)
    candidates = {cls: effective_methods(cls, own, bases) for cls in own}
    found = set(structural_matches(BOARD_CONTRACT, contract_methods, candidates))
    assert found <= (REST_ADAPTERS | LEGACY_ADAPTERS)


def test_vcs_protocol_family_covers_both_providers(own_and_bases):
    """Оба VCS-провайдера находятся, хотя Protocol наследования не даёт."""
    own, bases = own_and_bases
    contract_methods = effective_methods(VCS_CONTRACT, own, bases)
    candidates = {cls: effective_methods(cls, own, bases) for cls in own}
    found = set(structural_matches(VCS_CONTRACT, contract_methods, candidates))
    assert any(f.endswith("#GitHubProvider") for f in found)
    assert any(f.endswith("#GitLabProvider") for f in found)
```

- [ ] **Step 2: Прогнать тест**

Run: `.venv/bin/pytest tests/graph/test_real_families.py -v`
Expected: PASS. Имена классов провайдеров сверены заранее: `GitHubProvider` (`reviewer/vcs/github.py:10`) и `GitLabProvider` (`reviewer/vcs/gitlab.py:69`).

- [ ] **Step 3: Написать регрессионный integration-тест на upstream-дефект**

Дописать в `tests/graph/test_backend_integration.py`:

```python
@pytest.mark.integration
@pytest.mark.skipif(shutil.which("scip-python") is None,
                    reason="scip-python не установлен")
def test_scip_still_drops_forward_referenced_class_symbol(tmp_path):
    """Фиксирует дефект scip-python 0.6.6, ради которого наследование берётся
    из tree-sitter: для класса, упомянутого выше своего определения,
    SymbolInformation не эмитится вовсе.

    Если будущая версия начнёт его эмитить — тест покраснеет, и это сигнал
    пересмотреть комментарии, а не поломка: слияние в build_with_scip
    дедуплицирует рёбра и останется корректным.
    """
    import subprocess

    from reviewer.graph.scip_pb2 import Index

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "base.py").write_text("class Base:\n    pass\n")
    (pkg / "adapter.py").write_text(
        "from pkg.base import Base\n\n\n"
        "def spec():\n    return Adapter\n\n\n"
        "class Adapter(Base):\n    pass\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "probe"\nversion = "0.0.1"\n'
    )
    subprocess.run(["scip-python", "index", ".", "--project-name=probe"],
                   cwd=tmp_path, check=True, capture_output=True)

    idx = Index()
    idx.ParseFromString((tmp_path / "index.scip").read_bytes())
    symbols = {
        si.symbol for doc in idx.documents for si in doc.symbols
        if doc.relative_path.endswith("adapter.py")
    }
    assert not [s for s in symbols if s.endswith("/Adapter#")]
```

Сверить, что в шапке файла уже импортированы `shutil` и `pytest`; если нет — добавить.

- [ ] **Step 4: Прогнать integration-тест**

Run: `.venv/bin/pytest tests/graph/test_backend_integration.py -q -m integration -k forward_referenced`
Expected: PASS (или SKIP, если `scip-python` не в PATH).

- [ ] **Step 5: Прогнать весь unit-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS — ничего не сломано.

- [ ] **Step 6: Коммит**

```bash
git add tests/graph/test_real_families.py tests/graph/test_backend_integration.py
git commit -m "test(graph): приёмка семейств на адаптерах досок и VCS-провайдерах"
```

---

### Task 5: bulk-подвыборка в харнессе метрик

Критерий приёмки 4 требует подтверждённого числами роста core-recall на bulk-подвыборке. Понятия подвыборки в харнессе нет — вводим.

**Files:**
- Modify: `eval/solve_task_metrics/recall.py:28-72` (`QualityAggregate`, `aggregate`)
- Modify: `eval/solve_task_metrics/snapshot.py:109-140` (сборка блока `quality`)
- Modify: `eval/solve_task_metrics/report.py:54-64` (раздел «Качество ретрива»)
- Test: `tests/eval/test_bulk_subsample.py`

**Interfaces:**
- Consumes: `TaskQuality` (существующий dataclass с полями `core_recall`, `expected_core`).
- Produces: `BULK_CORE_THRESHOLD = 10`; поля `QualityAggregate.bulk_core_recall_median`, `.bulk_n_measured`; ключи снапшота `quality.bulk_core_recall_median`, `quality.bulk_n_measured`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/eval/test_bulk_subsample.py`:

```python
"""bulk-подвыборка: задачи с широким знаменателем ядра.

Порог взят из анализа PRI-246: на нём разделялись выборки, и в него
попадают все четыре задачи-развёртки, давшие провал core-recall
(PRI-223 — 25 файлов ядра, PRI-225 — 18, PRI-215 — 14, PRI-196 — 10).
"""
from eval.solve_task_metrics.recall import BULK_CORE_THRESHOLD, TaskQuality, aggregate


def _row(key: str, expected_core: int, core_recall: float | None) -> TaskQuality:
    row = TaskQuality(task_key=key, expected=expected_core, expected_core=expected_core,
                      predicted=5, hit_core=0)
    row.core_recall = core_recall
    return row


def test_threshold_is_ten_core_files():
    """Порог зафиксирован явно, а не подобран под текущие данные."""
    assert BULK_CORE_THRESHOLD == 10


def test_bulk_subsample_takes_only_wide_tasks():
    """В подвыборку попадают задачи со знаменателем ядра >= порога."""
    rows = [_row("A", 25, 0.24), _row("B", 4, 0.80), _row("C", 10, 0.50)]
    agg = aggregate(rows)
    assert agg.bulk_n_measured == 2
    assert agg.bulk_core_recall_median == 0.37


def test_bulk_subsample_ignores_tasks_without_measurement():
    """Задача с пустым ядром в подвыборку не попадает даже при широком diff."""
    rows = [_row("A", 25, None), _row("B", 12, 0.40)]
    agg = aggregate(rows)
    assert agg.bulk_n_measured == 1
    assert agg.bulk_core_recall_median == 0.40


def test_empty_bulk_subsample_is_none_not_zero():
    """Нет ни одной широкой задачи — метрика не определена, а не равна нулю."""
    agg = aggregate([_row("A", 3, 0.9)])
    assert agg.bulk_n_measured == 0
    assert agg.bulk_core_recall_median is None
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `.venv/bin/pytest tests/eval/test_bulk_subsample.py -v`
Expected: FAIL с `ImportError: cannot import name 'BULK_CORE_THRESHOLD'`

- [ ] **Step 3: Реализовать подвыборку в `recall.py`**

В `eval/solve_task_metrics/recall.py` добавить константу после импортов:

```python
BULK_CORE_THRESHOLD = 10
"""Порог знаменателя ядра, с которого задача считается задачей-развёрткой.

Значение из анализа PRI-246: на нём разделялись выборки (при expected_core >= 10
медиана predicted 5.0, при expected_core < 10 — 6.0), и в него попадают все
четыре задачи, давшие провал core-recall.
"""
```

В `QualityAggregate` добавить поля:

```python
    bulk_core_recall_median: float | None = None
    bulk_n_measured: int = 0
```

В `aggregate` перед `raw_values` добавить:

```python
    bulk = [r for r in measured if r.expected_core >= BULK_CORE_THRESHOLD]
    agg.bulk_n_measured = len(bulk)
    if bulk:
        agg.bulk_core_recall_median = statistics.median([r.core_recall for r in bulk])
```

- [ ] **Step 4: Прогнать тест, убедиться что проходит**

Run: `.venv/bin/pytest tests/eval/test_bulk_subsample.py -v`
Expected: PASS (4 теста)

- [ ] **Step 5: Пробросить в снапшот и отчёт**

В `eval/solve_task_metrics/snapshot.py` в словарь `"quality"` (строки 131-138) добавить два ключа:

```python
            "bulk_core_recall_median": aggregate.bulk_core_recall_median,
            "bulk_n_measured": aggregate.bulk_n_measured,
```

В `eval/solve_task_metrics/report.py` в раздел «Качество ретрива» после строки про медианный размер знаменателя ядра добавить:

```python
        f"- core-recall на bulk-подвыборке (ядро ≥ 10 файлов): "
        f"медиана {_pct(quality.get('bulk_core_recall_median'))}, "
        f"N={quality.get('bulk_n_measured', 0)}",
```

Использовать `.get`, а не индексацию: старые срезы в `solve_task_metrics_history.jsonl` этих ключей не имеют, и `compare` не должен падать на них.

- [ ] **Step 6: Прогнать харнесс и снять срез «до»**

Run: `.venv/bin/python -m eval.solve_task_metrics snapshot`
Expected: отчёт содержит новую строку про bulk-подвыборку; команда завершается без ошибки.

- [ ] **Step 7: Прогнать линт и весь набор тестов**

Run: `.venv/bin/ruff check eval/solve_task_metrics/recall.py eval/solve_task_metrics/snapshot.py eval/solve_task_metrics/report.py tests/eval/test_bulk_subsample.py && .venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 8: Коммит**

```bash
git add eval/solve_task_metrics/recall.py eval/solve_task_metrics/snapshot.py eval/solve_task_metrics/report.py tests/eval/test_bulk_subsample.py eval/solve_task_metrics_report.md eval/solve_task_metrics_history.jsonl
git commit -m "feat(eval): bulk-подвыборка core-recall для задач-развёрток"
```

---

### Task 6: Скилл, документация и манифесты

Тул бесполезен, если агент о нём не знает, и опасен, если докстринги обещают не то, что он делает. Критерий приёмки 6 требует, чтобы ожидания совпадали с реальностью.

**Files:**
- Modify: `plugin/skills/solve-task/SKILL.md` (секция «Deepen via the code graph» в шаге 3)
- Modify: `CLAUDE.md` (раздел «Неочевидные факты»)
- Modify: `README.md`, `README.ru.md`
- Modify: `reviewer/graph/store.py` (докстринг `implementations_detailed`)
- Test: `tests/skills/` (существующие guard-тесты)

**Interfaces:**
- Consumes: MCP-тул `family` из Task 3.
- Produces: правок интерфейсов нет — только текст.

- [ ] **Step 1: Обновить скилл `solve-task`**

В `plugin/skills/solve-task/SKILL.md` в шаге 3, в пункте «Deepen via the code graph», заменить абзац про `implementations` (начинающийся «For OO/registry/dispatch tasks…») на:

```
     For OO/registry/dispatch tasks («add a new provider / handler») and for any
     task that smells like a roll-out («add a field to every provider»), call
     `family(node_id)` on the symbols central to the task. It answers «who else is
     like this» from two signals — inheritance and structural contract match — and
     always says which fired, so an empty answer is never mistaken for «no family
     exists». Prefer it over the undirected `related_symbols`, which mixes
     callers/tests/implements.
     A family is the unit the brief must carry: when `family` returns N members,
     the brief names all N, not the one member retrieval happened to surface. This
     is a structural signal, not a textual one — do not try to infer roll-out tasks
     from the wording of the description.
     `implementations(node_id)` remains the directed «who subclasses X» query.
     Fail-soft notes are non-fatal — continue.
```

- [ ] **Step 2: Прогнать guard-тесты скиллов**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS. Если тест сверяет список тулов, упомянутых в скиллах, — добавить `family` в его ожидания.

- [ ] **Step 3: Обновить `CLAUDE.md`**

В раздел «Неочевидные факты» добавить пункт после пункта про два бэкенда графа:

```markdown
- **Наследование классов в графе приходит из tree-sitter, а не из SCIP (PRI-251).**
  scip-python 0.6.6 не эмитит `SymbolInformation` для класса, упомянутого в файле
  ВЫШЕ своего определения, — а значит и `si.relationships` у такого класса нет,
  читать нечего. В этот провал попадают все 11 адаптеров досок: каждый
  регистрируется в `provider_spec()`, объявленной до класса. Измерено на
  репозитории: из 185 классов с `SymbolInformation` forward-referenced — 0, из 14
  без неё — 13. Поэтому `reviewer/graph/inherit.py` извлекает `class X(Y)`
  синтаксически, а `build_with_scip` сливает эти рёбра с рёбрами SCIP
  (дедупликация). SCIP остаётся источником точных `CALLS` и метод-уровневых
  `IMPLEMENTS`. Дефект upstream закреплён integration-тестом: если новая версия
  scip-python начнёт эмитить символ, тест покраснеет.
- **Семейство символов (`family`) — не то же, что `implementations`.**
  `implementations` отвечает «кто наследует X» по рёбрам графа. `family` отвечает
  «кто ещё такой же» и добавляет второй сигнал — структурное покрытие набора
  методов контракта с учётом унаследованных. Он нужен потому, что `typing.Protocol`
  (`TaskBoardProvider`, `VCSProvider`) рёбер наследования не даёт ни при каком
  бэкенде: структурная типизация не выражается рёбрами. На этом репозитории
  структурный сигнал находит все 11 адаптеров (включая три легаси без общей базы)
  и оба VCS-провайдера, без ложных срабатываний. Пустой ответ при существующем
  семействе запрещён: `implementations` в этом случае явно отсылает к `family`.
```

- [ ] **Step 4: Обновить оба README**

В `README.md` и `README.ru.md` в описании session-less тулов графа добавить строку про `family` рядом с `implementations` — на языке соответствующего файла. В `README.ru.md`:

```markdown
- `family(repo, node_id, branch)` — семейство однотипных символов («кто ещё такой
  же»): наследование + структурное соответствие контракту. Для задач-развёрток
  («добавить поле во все провайдеры»), где один найденный файл — представитель
  семейства из N.
```

В `README.md` — то же по-английски.

- [ ] **Step 5: Поправить докстринг `implementations_detailed`**

В `reviewer/graph/store.py` в докстринге `implementations_detailed` (строки 111-114) заменить строки

```
        Класс → его подклассы; метод → его override-ы (SCIP эмитит и то, и то).
        Элементы: {"id": <node_id>, "rel": "IMPLEMENTS"}, упорядочены по id.
        Точны после полного `reviewer index` с SCIP (см. инвариант графа).
```

на

```
        Класс → его подклассы; метод → его override-ы.
        Элементы: {"id": <node_id>, "rel": "IMPLEMENTS"}, упорядочены по id.
        Наследование классов эмитит tree-sitter (SCIP теряет его у
        forward-referenced классов), override-ы методов — SCIP.
```

- [ ] **Step 6: Пересобрать манифесты плагина**

Контент под `plugin/` изменён, значит payload-digest сменился.

Run: `.venv/bin/python scripts/update_codex_plugin_manifest.py`
Expected: манифесты обновлены.

- [ ] **Step 7: Прогнать весь набор тестов**

Run: `.venv/bin/pytest -q`
Expected: PASS — включая install-тесты манифестов.

- [ ] **Step 8: Коммит**

```bash
git add plugin/ CLAUDE.md README.md README.ru.md reviewer/graph/store.py
git commit -m "docs(graph): семейство символов и источник наследования в графе"
```

---

### Task 7: Полная переиндексация и замер эффекта

Рёбра появляются в Neo4j только после перестроения графа. Без этого шага критерии 1-3 не проверяются на живой системе, а критерий 4 не измеряется.

**Files:**
- Modify: `eval/solve_task_metrics_report.md`, `eval/solve_task_metrics_history.jsonl` (результат прогона)

**Interfaces:**
- Consumes: MCP-тул `family`, `implementations` из Task 3; харнесс из Task 5.

- [ ] **Step 1: Перестроить граф ветки**

`reviewer index` полностью пересобирает граф (clear + upsert), поэтому рёбра разных бэкендов не смешиваются.

Run: `uvx --from rag-reviewer reviewer index . --ref dev --repo mimfort/rag_for_git`
Expected: вывод сообщает число узлов и рёбер; рёбер должно стать больше прежних 33922 — добавились класс-уровневые `IMPLEMENTS`.

Примечание: индексация идёт минутами и упирается в лимиты Voyage (3 RPM / 10K TPM) — троттлинг с ретраями это норма, а не ошибка.

- [ ] **Step 2: Проверить критерий приёмки 1 на живом графе**

Вызвать MCP-тул `implementations` с `repo="mimfort/rag_for_git"`, `node_id="reviewer/tasks/boards/restbase.py#RestBoardBase"`, `branch="dev"`.

Expected: восемь адаптеров (`AsanaBoard`, `ClickUpBoard`, `GitHubIssuesBoard`, `KaitenBoard`, `LinearBoard`, `TrelloBoard`, `WeeekBoard`, `YandexTrackerBoard`), а не `(implementations не найдены)`.

- [ ] **Step 3: Проверить критерии приёмки 2 и 3 на живом графе**

Вызвать `family` с `node_id="reviewer/tasks/boards/base.py#TaskBoardProvider"`, `branch="dev"`.

Expected: все 11 адаптеров, включая `JiraCloudBoard`, `YougileBoard`, `YouTrackBoard`; шапка называет сработавшие сигналы.

Затем вызвать `family` с заведомо одиночным узлом (например `reviewer/gitutil.py#add_worktree`).

Expected: ответ явно говорит, что семейство не найдено и почему, — не пустая строка.

- [ ] **Step 4: Снять срез «после» и сравнить**

Run: `.venv/bin/python -m eval.solve_task_metrics snapshot && .venv/bin/python -m eval.solve_task_metrics compare --back 1`
Expected: сравнение печатает дельты, включая строку bulk-подвыборки.

Зафиксировать честно: core-recall считается по брифам, а брифы задач-развёрток появятся только после того, как задачи будут решаться уже с `family`. Если дельта на текущем корпусе нулевая — это ожидаемо и должно быть записано как «замер отложен до накопления bulk-подвыборки», а не выдано за рост.

- [ ] **Step 5: Коммит результата замера**

```bash
git add eval/solve_task_metrics_report.md eval/solve_task_metrics_history.jsonl
git commit -m "chore(eval): срез метрик после включения семейств символов"
```

---

## Self-Review

**Spec coverage:**

| Требование спеки | Задача |
|---|---|
| Слой 1 — наследование из синтаксиса, слияние со SCIP | Task 1 |
| Слой 2 — семейство по структурному контракту с унаследованными методами | Task 2 |
| Слой 3 — явная полнота ответа, тул `family` | Task 3 |
| `implementations` не молчит при существующем семействе | Task 3 |
| Использование в solve-task, `Retriever` не трогаем | Task 6 |
| Тесты: юнит парсера, tree-sitter, слияние, структурное семейство, MCP-тул | Tasks 1-3 |
| Тесты на реальных семействах (11 адаптеров, 2 VCS-провайдера) | Task 4 |
| Integration-тест на upstream-дефект scip-python | Task 4 |
| bulk-подвыборка с порогом 10, замер до/после | Tasks 5, 7 |
| Документация: CLAUDE.md, оба README, докстринги | Task 6 |

Критерии приёмки задачи: 1 → Task 1 + Task 7 шаг 2; 2 → Task 2 + Task 7 шаг 3; 3 → Task 3 + Task 7 шаг 3; 4 → Tasks 5, 7; 5 → Task 4; 6 → Task 6. Пробелов нет.

**Placeholder scan:** плейсхолдеров нет; каждый шаг несёт исполняемый код или точную команду. Имена классов VCS-провайдеров и стиль тестов MCP-сервера сверены с кодом до написания плана, поэтому мест «уточнить по факту» не осталось. Единственное место, где ожидание задано условно, — Task 6 шаг 2: если guard-тест скиллов сверяет перечень тулов, в него добавляется `family`.

**Type consistency:** `extract_inheritance_edges` (Task 1) вызывается из `builder.build_graph_from_files` и из `backend.inheritance_edges` с одинаковой сигнатурой из четырёх аргументов. `effective_methods(node_id, own_by_node, bases_map)` и `structural_matches(contract, contract_methods, candidates)` (Task 2) вызываются в `MCPReviewService._compute_family` (Task 3) и в `tests/graph/test_real_families.py` (Task 4) в том же порядке аргументов. `FamilyResult` полями `members`/`signals`/`complete`/`note` используется в `_family_header` и в тестах согласованно. `BULK_CORE_THRESHOLD`, `bulk_core_recall_median`, `bulk_n_measured` (Task 5) названы одинаково в `recall.py`, `snapshot.py`, `report.py` и тесте.
