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


def test_prose_criteria_and_multiline_items_keep_their_text():
    task = {
        "title": "Парсинг карточки",
        "description": (
            "## Что сделать\n\n"
            "- Добавить проверку\n"
            "  с подробным режимом.\n\n"
            "  Результат сохранять в отчёте.\n\n"
            "## Критерии приемки\n\n"
            "Запуск возвращает код успеха,\nкогда проверка завершена.\n\n"
            "1. Сохраняется итог\nв многострочном отчёте.\n\n"
            "## Контекст\n\nЭту строку пропускаем."
        ),
    }
    assert build_subqueries(task, "база") == [
        "база",
        "Добавить проверку с подробным режимом. Результат сохранять в отчёте.",
        "Запуск возвращает код успеха, когда проверка завершена.",
        "Сохраняется итог в многострочном отчёте.",
    ]


def test_fenced_markdown_does_not_change_the_section_or_create_criteria():
    task = {
        "description": (
            "## Контекст\n\n~~~~md\n## Acceptance\n- Ложный критерий\n~~~~\n"
            "## Acceptance\n- Проверка пройдена\n"
        ),
    }
    assert build_subqueries(task, "база") == ["база", "Проверка пройдена"]


def test_criteria_duplicated_across_sources_keep_their_priority():
    steps = "\n".join(f"- Шаг {number}" for number in range(MAX_SUBQUERIES))
    task = {
        "description": (
            f"## Что сделать\n{steps}\n- Тесты проходят.\n\n"
            "## Acceptance\n- Тесты\n  проходят.\n"
        ),
        "criteria": ["тесты   проходят.", "  Отчёт приложен. "],
    }

    queries = build_subqueries(task, "база")
    assert len(queries) == MAX_SUBQUERIES
    assert queries.count("Тесты проходят.") == 1
    assert "Отчёт приложен." in queries
    assert "тесты проходят." not in queries


def test_overflow_groups_related_criteria_and_keeps_unrelated_criteria():
    steps = [f"Шаг {number}" for number in range(MAX_SUBQUERIES - 3)]
    criteria = [
        "Настройки проекта сохраняются после инициализации.",
        "Настройки проекта загружаются при повторном запуске.",
        "Вывод CLI содержит номер версии.",
    ]
    task = {
        "description": "## Что сделать\n" + "\n".join(f"- {step}" for step in steps),
        "criteria": criteria,
    }

    queries = build_subqueries(task, "база")
    assert len(queries) == MAX_SUBQUERIES
    assert any(all(criterion in query for criterion in criteria[:2]) for query in queries)
    assert criteria[2] in queries
    assert queries[:MAX_SUBQUERIES - 2] == ["база", *steps]


def test_criteria_continue_under_deeper_headings_until_peer_section():
    task = {
        "description": (
            "## Acceptance\n"
            "### CLI\n"
            "- Exits with status 4.\n"
            "### Configuration\n"
            "- Remembers the project.\n"
            "## Context\n"
            "### Follow-up\n"
            "- Consider more options.\n"
        )
    }

    assert build_subqueries(task, "base") == [
        "base", "Exits with status 4.", "Remembers the project.",
    ]


def test_fenced_output_after_blank_line_is_part_of_criterion():
    task = {
        "description": (
            "## Acceptance\n\n"
            "The command prints\n\n"
            "```text\nexpected output\n```\n\n"
            "The log stays empty.\n"
        )
    }

    assert build_subqueries(task, "base") == [
        "base", "The command prints expected output", "The log stays empty.",
    ]


def test_nested_list_items_remain_part_of_their_acceptance_criterion():
    task = {
        "description": (
            "## Requirements\n"
            "1. Add tests\n"
            "   - Adapt fixtures.\n"
            "## Acceptance\n"
            "1. Report includes\n"
            "   - parsed headers\n"
            "   - decoded payload\n"
            "2. Other output stays the same.\n"
        )
    }

    assert build_subqueries(task, "base") == [
        "base", "Add tests", "Adapt fixtures.", "Report includes parsed headers decoded payload",
        "Other output stays the same.",
    ]
