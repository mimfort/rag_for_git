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


def test_global_fallback_resolves_base_without_import():
    """База без импорта резолвится глобально по простому имени.

    Последний шаг приоритета — сознательное приближение: без него терялось бы
    наследование при импорте, который резолвер модулей не разбирает.
    """
    files = {
        "pkg/base.py": "class Base:\n    pass\n",
        "pkg/adapter.py": "class Child(Base):\n    pass\n",
    }
    _, edges = build_graph_from_files(files)
    assert ("pkg/adapter.py#Child", "IMPLEMENTS", "pkg/base.py#Base") in edges


def test_unresolvable_base_yields_no_edge():
    """База, не совпавшая ни с одним индексированным символом, ребра не даёт."""
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


def test_inheritance_edge_for_decorated_class():
    """Декоратор не ломает сопоставление класса с чанком.

    Чанкер начинает чанк со строки декоратора, tree-sitter — со строки
    class; сопоставление по точной строке здесь бы промахнулось.
    """
    files = {
        "pkg/base.py": "class Base:\n    pass\n",
        "pkg/deco.py": (
            "from dataclasses import dataclass\n"
            "from pkg.base import Base\n\n\n"
            "@dataclass(frozen=True)\n"
            "class Child(Base):\n    x: int = 0\n"
        ),
    }
    _, edges = build_graph_from_files(files)
    assert ("pkg/deco.py#Child", "IMPLEMENTS", "pkg/base.py#Base") in edges
