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
