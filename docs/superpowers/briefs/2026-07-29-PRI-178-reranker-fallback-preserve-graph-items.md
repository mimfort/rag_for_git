# Brief — PRI-178 search_codebase: детерминированный fallback при недоступном reranker

https://ru.yougile.com/team/686c049c8af8/#PRI-178

## Task

- Данные задачи получены из reviewer store после `sync_board`; store key `ID-178`, alias `PRI-178`.
- Проблема актуальна: в `search_base` graph-элементы добавляются после RRF-hits, а no-rerank пути возвращают начало `items[:ceiling]`; при переполнении графовый хвост теряется.
- Исходное описание устарело: рабочий путь — `Retriever.search_base`, лимит называется `ceiling`, reranker вызывает `rerank_scored`, а общий fallback уже имеет базовый тест.
- Полезный результат: source-aware deterministic fallback, который сохраняет лучший hybrid hit и ограниченное graph-разнообразие, не нарушая `ceiling`, фильтрацию тестов и overlap-dedup.
- Fallback должен явно сообщать потребителю, что reranker отсутствовал или завершился ошибкой.

## Related work

- PRI-202 — использовать введённые там `CodebaseLimits`/`ceiling` и не менять cliff-policy успешной ветки.
- PRI-138 — сохранять контракт `search_codebase`: dedupe, line-numbered output и bounded context.
- PRI-194 — не смешивать с отдельным lexical fallback: PRI-178 меняет только состав уже найденных hybrid+graph кандидатов.

(dropped 17: задачи о сводках, центральности, graph-инструментах и общем RAG не задают реализацию этого fallback.)

## Subsystems

- `reviewer/retrieval` — hybrid search, graph expansion, rerank, cliff и формирование `ContextPack`.
- `reviewer/graph` — уже предоставляет `expand_detailed` с расстоянием и стабильным tie-break по `node_id`.
- `tests/retrieval` — unit-контракт выдачи, degraded-paths и output shaping.

## Relevant code

- `reviewer/retrieval/retriever.py:123` — hybrid hits и ANN prefilter задают приоритетный RRF-порядок.
- `reviewer/retrieval/retriever.py:134` — graph expansion добавляет fetched nodes после hits; provenance и стабильный порядок нужно сохранить.
- `reviewer/retrieval/retriever.py:145` — test filtering и `_dedupe_overlapping` происходят до fallback.
- `reviewer/retrieval/retriever.py:149` — ветка без reranker сейчас срезает начало списка.
- `reviewer/retrieval/retriever.py:151` — exception из `rerank_scored` ведёт в идентичный срез.
- `reviewer/graph/store.py:122` — `expand_detailed` возвращает `{id, rels, dist}` в порядке `(dist, id)`.

(dropped 1: `Retriever.retrieve` — отдельный PR-session путь без symptomного fail-soft fallback.)

## Test exemplars

- `tests/retrieval/test_search_base.py:18` — `_FakeStore` задаёт hybrid и graph nodes.
- `tests/retrieval/test_search_base.py:50` — `_FakeGraph` моделирует expansion; fake нужно перевести на `expand_detailed`.
- `tests/retrieval/test_search_base.py:178` — no-reranker сценарий.
- `tests/retrieval/test_search_base.py:188` — reranker exception сценарий.
- `tests/retrieval/test_output_shaping.py:70` — форматирование `ContextPack.as_context`.

(dropped 0: все тестовые опоры напрямую нужны.)

## Constraints / open questions

- Hybrid/RRF остаётся основным сигналом релевантности; graph получает один гарантированный слот только при `ceiling >= 2`, если hybrid уже заполнил лимит.
- При свободных слотах graph-кандидаты заполняют остаток в порядке `(distance, node_id)`.
- При `ceiling == 1` лучший hybrid hit не вытесняется.
- После filtering/dedup provenance пересчитывается по `node_id`; удалённый graph-кандидат не резервирует слот.
- Успешный `rerank_scored -> select_by_cliff`, `Retriever.retrieve`, `CodebaseLimits` и Voyage retry не меняются.
- Base-index `main` имел `chunks=0`, поэтому code retrieval вернул пусто; ссылки на код проверены прямым чтением рабочей копии.

Собран на: mid tier (gpt-5.6-terra), режим: subagent
