# Brief — PRI-174 search_codebase: различать причины пустого результата
https://ru.yougile.com/team/686c049c8af8/#PRI-174

## Task

Вердикт: **rewrite** — исходная идея актуальна, но деление только на `db`/`embeddings` слишком узкое и не задаёт наблюдаемого контракта для глобального плагина.
Новая узкая формулировка: «Сделать публичный контракт `search_codebase` трёхсостоянием: подлинно пустой результат, временно недоступный поиск и неизвестная внутренняя ошибка; различать только безопасные для пользователя категории и сохранять fail-open».
Источник задачи — reviewer store после sync; `criteria=[]`, отдельного блока критериев нет.
Измеримые критерии: пустой `ContextPack` возвращает ровно `(ничего не найдено)`; сбой `embed_query` возвращает `(поиск недоступен — embeddings)`, а `hybrid_search` — `(поиск недоступен — storage)`; произвольный сбой возвращает безопасное `(поиск недоступен — внутренняя ошибка)` и логируется; `ask` распознаёт unavailable-note и включает локальный fallback.
Не включать локальный поиск или изменение ранжирования: это отдельная ценность PRI-194.

## Related work

- ID-194 — не смешивать с локальным lexical fallback: PRI-174 только делает причину деградации явной, а не возвращает результаты без RAG.
- ID-138 — сохранить установленный формат line-numbered `search_codebase`-выдачи и прежнее значение для подлинно пустого результата.

(dropped 27: остальные семантически близкие задачи относятся к лимитам, графу, сводкам или общему workflow и не задают контракт ошибки поиска.)

## Subsystems

- reviewer/retrieval — `Retriever.search_base` выполняет embedding и hybrid search, тогда как граф и reranker уже fail-soft.
- reviewer/entrypoints — MCP-обёртка публикует сервисный контракт глобальному плагину.
- tests/index — тестовые doubles уже изолируют storage и embeddings для unit-проверок.

## Relevant code

- reviewer/retrieval/retriever.py:111 — `search_base` вызывает `embed_query` и `hybrid_search` до локальных fail-soft веток; здесь нужен узкий typed-error boundary.
- reviewer/mcp/service.py:570 — публичный MCP метод сейчас ловит любой `Exception` и превращает отказ поиска в `(ничего не найдено)`.

(dropped 5: entrypoint wrapper, context-limit wiring и graph/rerank детали не меняют контракт первичного поиска.)

## Test exemplars

- tests/mcp/test_service.py:555 — существующий тест намеренно склеивает пустой ответ и `RuntimeError`; заменить на таблицу контрактных сценариев на уровне MCP.
- tests/retrieval/test_search_base.py:113 — пустой `ContextPack` уже зафиксирован отдельно; сохранить этот back-compat case и добавить independent checks для embedding/store failures.

(dropped 22: branch, ANN, graph, rerank и integration cases не проверяют классификацию отказа первичного поиска.)

## Constraints / open questions

- Категории должны быть безопасными и конечными (`embeddings`, `storage`/`db`, `unknown`); не прокидывать тексты исключений или секреты в MCP-ответ.
- Не оборачивать graph expansion и reranker: они уже намеренно fail-soft и не означают, что базовый поиск недоступен.
- Обновить payload глобального плагина `plugin/skills/ask/SKILL.md`: его fallback сейчас перечисляет только `(ничего не найдено)` и `(граф недоступен)`.
- Успех считать по тестам контракта, а эксплуатационно — по отсутствию лишнего повторного semantic-search после unavailable-note; точной telemetry для подсчёта повторов в задаче нет.
- `get_task_context` не вернул связанных задач или PR; связь с ID-194 определена по семантическому поиску, PR diff не нужен.
- Собран на: средний tier (gpt-5.6-terra), режим: subagent
