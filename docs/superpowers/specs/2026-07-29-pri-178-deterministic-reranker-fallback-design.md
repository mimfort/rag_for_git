# PRI-178 — детерминированный fallback search_codebase

Статус: утверждено пользователем 2026-07-29.

## Цель

Сохранить полезное graph-разнообразие в bounded-выдаче `Retriever.search_base`, когда
Voyage reranker не настроен или завершился ошибкой, и явно сообщить потребителю о
деградации качества ранжирования.

## Проблема

`search_base` получает упорядоченные hybrid/RRF hits, расширяет их соседями из графа и
добавляет graph-only chunks после hits. Успешный reranker оценивает общий пул, поэтому
источник кандидата не важен. В no-rerank ветках используется `items[:ceiling]`; если
hybrid hits заполняют лимит, graph-only хвост исчезает.

Простая долевая квота не подходит:

- graph-кандидаты не имеют semantic score и не должны массово вытеснять RRF hits;
- `GraphStore.expand` возвращает `set`, а `ChunkStore.fetch_nodes` не обещает порядок;
- результат fallback не должен меняться из-за порядка строк БД.

Fallback также не отличим от успешного ранжирования по возвращаемому `ContextPack`, хотя
его используют session-less инструменты глобального плагина.

## Scope

В scope:

- только `Retriever.search_base`;
- стабильное ранжирование graph-only кандидатов существующим `expand_detailed`;
- единый чистый selector для no-rerank путей;
- типизированная причина degraded-mode и краткая заметка в `ContextPack.as_context`;
- unit-тесты selection, provenance, ordering и output shaping.

Вне scope:

- PR-session путь `Retriever.retrieve`;
- Voyage retry `6×22s`, timeout или circuit breaker;
- изменение ANN prefilter, RRF, успешного rerank/cliff;
- новые настройки `CodebaseLimits`;
- новые зависимости или изменения схемы данных.

## Архитектура

### 1. Стабильное graph expansion

`search_base` вызывает существующий
`GraphStore.expand_detailed(repo, seeds, hops=hops, branch=branch)`. Метод уже возвращает
элементы вида `{"id": str, "rels": list[str], "dist": int}` в порядке
`(dist, node_id)`.

`fetch_nodes` может вернуть chunks в другом порядке, поэтому `search_base`:

1. сохраняет ordered `graph_ids` из `expand_detailed`;
2. строит `fetched_by_id`;
3. восстанавливает `related` обходом `graph_ids`;
4. исключает ids, уже присутствующие среди hybrid hits.

Graph error остаётся fail-soft: логируется, `graph-only=[]`, поиск продолжается по hybrid
hits.

### 2. Provenance после очистки

До объединения фиксируются `hybrid_ids` и ordered `graph_only_ids`. Общий пул сохраняет
текущий порядок `hybrid -> graph`, затем проходит существующие test filtering и
`_dedupe_overlapping`.

После очистки список снова делится по surviving `node_id`:

- `hybrid_items` — surviving items с id из `hybrid_ids`;
- `graph_items` — surviving items с id из `graph_only_ids`.

Это важно для случая, когда широкий chunk удаляет вложенный chunk другого источника:
selector видит только реально оставшиеся элементы и не резервирует пустой graph-слот.

### 3. Source-aware selector

В `reviewer/retrieval/retriever.py` добавляется чистая функция:

```python
def _select_degraded_context(
    hybrid_items: list,
    graph_items: list,
    ceiling: int,
) -> list:
    ...
```

Политика:

1. `ceiling <= 0` возвращает `[]`.
2. Если hybrid items нет, graph items заполняют выдачу до `ceiling`.
3. `ceiling == 1` возвращает первый hybrid item; graph используется только если hybrid
   пуст.
4. Если hybrid items меньше `ceiling`, они остаются первыми, а свободные слоты
   заполняются ordered graph items.
5. Если hybrid items заполняют `ceiling` и graph items существуют, selector сохраняет
   первые `ceiling - 1` hybrid items и добавляет первый graph item.
6. Если graph items нет, возвращаются первые `ceiling` hybrid items.

Таким образом лучший RRF hit сохраняется, graph получает минимальный один слот, а
результат всегда bounded и детерминирован.

Selector применяется:

- когда reranker отсутствует и пул требует ранжирования;
- когда `rerank_scored` бросает исключение;
- при малом пуле без вызова reranker только если `ceiling` действительно отсекает
  элементы; этот путь использует ту же selection policy, но не считается деградацией
  reranker.

### 4. Degraded metadata

В модуле retrieval объявляется:

```python
from typing import Literal

DegradedReason = Literal["reranker_unconfigured", "reranker_failed"]
```

`ContextPack` получает поле:

```python
degraded_reason: DegradedReason | None = None
```

`as_context` после основного текста и cliff-note добавляет одну из двух коротких заметок:

- `reranker_unconfigured` — поиск выполнен без настроенного reranker;
- `reranker_failed` — reranker недоступен, применён детерминированный резервный отбор
  hybrid+graph.

Текст исключения, credentials и provider details в заметку не попадают. Малый пул,
который штатно не требует rerank, `degraded_reason` не получает. Успешный cliff-path
также остаётся без degraded-note.

## Поток данных

```text
hybrid_search -> ANN prefilter -> ordered hybrid hits
                                 |
                                 +-> expand_detailed -> fetch_nodes -> reorder by (dist, id)
                                                          |
hybrid + graph-only -> test filter -> overlap dedup -> split surviving provenance
                                                          |
                         +--------------------------------+------------------+
                         |                                                   |
                  rerank succeeds                                    no rerank / error
                         |                                                   |
                  select_by_cliff                              _select_degraded_context
                         |                                                   |
                  ContextPack(tail_meta)                 ContextPack(degraded_reason)
```

## Ошибки и совместимость

- Graph failure: прежний hybrid-only fail-soft.
- Reranker exception: прежний fail-soft без проброса ошибки, но с deterministic selection
  и безопасной диагностической заметкой.
- Конструкторы `ContextPack(items=...)` совместимы благодаря default `None`.
- Формат code chunks и line-numbered output не меняется; добавляется только trailing note
  в реальном degraded-mode.
- Публичные MCP signatures и board/config contracts не меняются.

## Тестирование

`tests/retrieval/test_search_base.py`:

- fake graph поддерживает `expand_detailed` и фиксирует `hops`/`branch`;
- shuffled `fetch_nodes` восстанавливается в `(dist, node_id)` order;
- no-reranker и exception paths сохраняют top hybrid и один graph при полном лимите;
- свободные слоты заполняются graph items;
- `ceiling=1` сохраняет top hybrid;
- test-filtered или overlap-deduped graph item не резервирует слот;
- малый пул без rerank не получает degraded reason;
- успешный rerank/cliff не меняется.

`tests/retrieval/test_output_shaping.py`:

- обе degraded reasons рендерят ожидаемую безопасную заметку;
- обычный `ContextPack` не получает заметку.

Регрессия:

```bash
.venv/bin/pytest -q tests/retrieval
.venv/bin/pytest -q tests/mcp/test_service.py tests/mcp/test_context_limits_wiring.py
.venv/bin/ruff check reviewer/retrieval/retriever.py tests/retrieval
.venv/bin/pytest -q
```

## Решения, принятые при self-review

- Scope остаётся одной подсистемой retrieval; `expand_detailed` уже существует, поэтому
  отдельное изменение graph API не требуется.
- Фиксируется один минимальный graph-слот, а не произвольная доля.
- Штатный skip reranker на малом пуле отделён от реальной деградации и не создаёт шумной
  заметки.
- Все edge cases (`ceiling<=0`, `ceiling=1`, пустой источник, dedup/filter) имеют
  однозначную семантику.
