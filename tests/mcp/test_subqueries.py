"""Извлечение подзапросов ретрива из структуры и сущностей задачи (PRI-255)."""
from reviewer.mcp.subqueries import MAX_SUBQUERIES, build_subqueries

SMALL = {
    "title": "Починить дрейф индекса",
    "description": "## Проблема\n\nСтатус врёт про дрейф.\n",
}

BULK = {
    "title": "Реестр досок",
    "description": (
        "## Проблема\n\nПровайдеры ветвятся по типу.\n\n"
        "## Что сделать\n\n"
        "1. Завести BoardProviderRegistry в reviewer/tasks/registry.py.\n"
        "2. Перенести yougile на RestBoardBase.\n"
        "3. Перенести youtrack на общий транспорт.\n"
        "4. Добавить пагинацию по Link-заголовку.\n"
        "5. Вымарывать секреты на границе.\n"
        "6. Описать матрицу в docs/board-providers.md.\n"
        "7. Закрыть contract-фикстурой каждый тип.\n"
        "8. Прокинуть provider_options до фабрики.\n"
        "9. Синхронизировать таблицу tasks с новым ключом.\n"
        "10. Обновить sync_board на generic lifecycle.\n"
    ),
}


def test_base_query_is_always_first():
    assert build_subqueries(SMALL, "база")[0] == "база"


def test_board_less_input_yields_only_base_query():
    assert build_subqueries(None, "свободная формулировка") == ["свободная формулировка"]


def test_small_task_yields_few_subqueries():
    assert len(build_subqueries(SMALL, "база")) <= 3


def test_bulk_task_yields_one_subquery_per_item():
    queries = build_subqueries(BULK, "база")
    assert 10 <= len(queries) <= 15
    assert any("BoardProviderRegistry" in q for q in queries)
    assert any("generic lifecycle" in q for q in queries), "хвостовой пункт обязан попасть"


def test_identifiers_are_bundled_into_one_subquery():
    task = {
        "title": "T",
        "description": "Правится search_codebase и TaskStore в reviewer/mcp/service.py, таблица tasks.",
    }
    queries = build_subqueries(task, "база")
    bundle = queries[-1]
    for identifier in ("search_codebase", "TaskStore", "reviewer/mcp/service.py"):
        assert identifier in bundle
    assert " и " not in bundle, "предлоги в пул идентификаторов не попадают"


def test_criteria_items_become_subqueries():
    task = {"title": "T", "description": "D", "criteria": ["Метрика растёт", "Отчёт зафиксирован"]}
    queries = build_subqueries(task, "база")
    assert "Метрика растёт" in queries


def test_duplicates_are_dropped_preserving_order():
    task = {"title": "T", "description": "## Что сделать\n\n1. база\n2. другое\n"}
    assert build_subqueries(task, "база").count("база") == 1


def test_degenerate_text_is_capped():
    items = "\n".join(f"{i}. пункт номер {i} про symbol_{i}" for i in range(1, 61))
    task = {"title": "T", "description": f"## Что сделать\n\n{items}\n"}
    assert len(build_subqueries(task, "база")) == MAX_SUBQUERIES
