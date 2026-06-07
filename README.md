# rag_for_git

Агент для автоматического ревью pull/merge request'ов на основе **RAG + графа кода + LLM**.

На событие «появился/обновился PR» агент собирает релевантный контекст по всему
репозиторию (гибридный поиск + граф связей кода), прогоняет его через LLM с
инструментами и постит результат обратно: inline-комментарии на строки диффа +
сводный комментарий.

## Статус

🟡 Фаза дизайна. Реализация ещё не начата.

Полный дизайн-документ: [`docs/superpowers/specs/2026-06-07-mr-review-agent-design.md`](docs/superpowers/specs/2026-06-07-mr-review-agent-design.md)

## Ключевые решения

| Область | Выбор |
|---|---|
| Запуск | Ядро-библиотека → CLI сейчас, webhook-сервис позже |
| VCS v1 | GitHub (за интерфейсом `VCSProvider`, под GitLab/др. заложена абстракция) |
| Свежесть RAG | Стабильная база + content-hash дедуп + overlay на PR |
| Вектор/текст | Postgres + pgvector (HNSW) + ParadeDB `pg_search` (BM25), слияние через RRF |
| Граф кода | Neo4j; рёбра — tree-sitter + резолвер, апгрейд `scip-python` |
| Эмбеддинги/реранк | Voyage (`voyage-code-3`, `rerank-2.5`), модели из env |
| LLM | OpenRouter (модель, потолок цены USD/1M, роутинг провайдера — из env) |
| Оркестрация | LangGraph, map-reduce по diff с фазой verify |

## Стек

Python · LangGraph/LangChain · Postgres (pgvector + pg_search) · Neo4j · Voyage AI · OpenRouter · Docker
