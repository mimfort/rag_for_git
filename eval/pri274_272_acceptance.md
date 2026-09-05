# Приёмка PRI-274 и PRI-272 на живом деплое

Дата замера: 2026-09-05. Ветка PRI-274 + PRI-272, база `ebfa663`.

## Окружение

Оба хранилища подняты и здоровы на протяжении всех опытов — это условие обеих
приёмок, а не фон: и «пул исчерпан», и «эмбеддер не отвечает» суть случаи,
в которых контейнеры **работают**, и вся задача в том, чтобы перестать выдавать
их за «хранилище лежит».

| Параметр | Значение |
|---|---|
| ParadeDB | `127.0.0.1:5433`, up 35 h (healthy) |
| Neo4j | `127.0.0.1:7687`, up 35 h (healthy) |
| Репозиторий / ветка | `mimfort/rag_for_git` / `dev` |
| `indexed_sha` | `ebfa663cb96004d5c6ce49e9817233ff35100b9c`, `drift = 0` |
| Корпус | chunks 7877, graph_nodes 8128, summaries 40, задач в сторе 186 |
| `pg_pool_min_size` / `pg_pool_max_size` | 1 / 4 |
| Таймаут пула psycopg | 30.0 с |
| Клиент Voyage | `voyageai 0.4.1` |

Замеры выполнялись в одиночку: параллельная сессия reviewer заняла бы те же
соединения и исказила бы опыт 1.

## Опыт 1 — PRI-274: исчерпанный пул называется по имени

**Постановка.** Заняты все 4 соединения пула `ChunkStore` (`pool.getconn()` ×4),
после чего вызван `prepare_task_context(repo, key="PRI-274", branch="dev",
warm_board=True)`. Контейнеры при этом работают.

**Наблюдаемое исключение** — до классификатора доходит
`psycopg_pool.PoolTimeout: couldn't get a connection after 30.00 sec`, брошенный
в `store.get_repo_clone` внутри `_TaskContextDeps._clone_path` (то есть в первой
же точке preflight, как и предписывает PRI-275).

**Время ответа: 30.30 с и 30.13 с** в двух прогонах — ровно один таймаут пула,
не восемь. Восемь пропущенных секций стоили 0.1–0.3 с сверх первого таймаута.

**Полный список `gaps`** (второй прогон, воспроизводится дословно):

```json
{"section": "preflight",       "reason": "свободных соединений в пуле не осталось",            "cause": "storage_unavailable", "cause_detail": "pool_exhausted", "remedy": null}
{"section": "task_board",      "reason": "пропущено: свободных соединений в пуле не осталось", "cause": "storage_unavailable", "cause_detail": "pool_exhausted", "remedy": null}
{"section": "warm_board",      "reason": "пропущено: свободных соединений в пуле не осталось", "cause": "storage_unavailable", "cause_detail": "pool_exhausted", "remedy": null}
{"section": "task",            "reason": "пропущено: свободных соединений в пуле не осталось", "cause": "storage_unavailable", "cause_detail": "pool_exhausted", "remedy": null}
{"section": "related.similar", "reason": "пропущено: свободных соединений в пуле не осталось", "cause": "storage_unavailable", "cause_detail": "pool_exhausted", "remedy": null}
{"section": "subsystems",      "reason": "пропущено: свободных соединений в пуле не осталось", "cause": "storage_unavailable", "cause_detail": "pool_exhausted", "remedy": null}
{"section": "code",            "reason": "пропущено: свободных соединений в пуле не осталось", "cause": "storage_unavailable", "cause_detail": "pool_exhausted", "remedy": null}
{"section": "test_exemplars",  "reason": "пропущено: свободных соединений в пуле не осталось", "cause": "storage_unavailable", "cause_detail": "pool_exhausted", "remedy": null}
```

**Критерии PRI-274 выполнены:** `cause == "storage_unavailable"` (класс не
изменился — шаг 0a скилла ищет именно его), `cause_detail == "pool_exhausted"`,
`remedy is null` (`reviewer start` не предлагается: поднимать нечего), время —
один таймаут.

**Побочно подтверждена раздельность замыкания по бэкендам (PRI-276):** секция
`related.linked` при мёртвом для нас Postgres **собралась** — граф жив и ответил
`Task ID-330 [Запуск / CI / хуки]: Исчерпание пула соединений выдаётся за
недоступность хранилища…`. Один общий флаг на два хранилища потерял бы и её.

## Опыт 2 — PRI-272: недоступный эмбеддер при живых хранилищах

**Постановка.** Хранилища живы, `Settings` настоящий, ключ Voyage в конфиге не
тронут: подменён только базовый URL клиента на `http://127.0.0.1:9/v1` (порт
discard, connection refused). Вызов — `prepare_task_context(repo,
key="PRI-99999", branch="dev", warm_board=True)`.

**Наблюдаемое исключение** — `voyageai.error.APIConnectionError` (за 0.02 с).
Отдельной проверкой предиката на этом самом объекте:
`is_embedder_unavailable → True`, `is_storage_unavailable → False`.

**Полученные `gaps`:**

```json
{"section": "task",            "reason": "задачи нет в сторе",              "cause": "unknown",              "cause_detail": null, "remedy": null}
{"section": "related.similar", "reason": "эмбеддер не отвечает",            "cause": "embedder_unavailable", "cause_detail": null, "remedy": null}
{"section": "subsystems",      "reason": "пропущено: эмбеддер не отвечает", "cause": "embedder_unavailable", "cause_detail": null, "remedy": null}
{"section": "code",            "reason": "пропущено: эмбеддер не отвечает", "cause": "embedder_unavailable", "cause_detail": null, "remedy": null}
{"section": "test_exemplars",  "reason": "пропущено: эмбеддер не отвечает", "cause": "embedder_unavailable", "cause_detail": null, "remedy": null}
```

**Хранилищные секции собрались нормально**, как и требует критерий:

```
preflight:  {"branch": "dev", "indexed_sha": "ebfa663…", "drift": 0, "chunks": 7877, "graph_nodes": 8128, "summaries": 40}
task_board: {"type": "yougile", "project": "PRI", "key_pattern": "PRI-\\d+", "done_target": "Готово"}
```

Четыре секции несут `embedder_unavailable` с пустым `remedy`; `cause_detail`
у них `null` — уточнение относится только к `storage_unavailable`, как и решено
в спеке. Первая точка отказа даёт голое «эмбеддер не отвечает», остальные —
«пропущено: …»: замыкание сработало, второго обращения к Voyage не было.

Секция `task` здесь сказала «задачи нет в сторе» (`cause: unknown`) — и это
**правильно**: ключ `PRI-99999` не существует, а синк в этом прогоне был
инкрементальным на неизменившейся доске (`changed: 0, embedded: 0,
embedder_failed: false`) и до эмбеддера не дошёл. Признак не залипает.

## Опыт 3 — структурный признак отказа в `index_batch`

**Постановка.** Живые Postgres/Neo4j и настоящий `TaskStore`; недоступен только
Voyage. Задача синтетическая (`ACCEPT-PRI272-SYNTHETIC`) и в сторе отсутствует —
поэтому попадает в `to_embed`, чего инкрементальный синк на неизменившейся доске
не даёт.

Строка результата (сокращён только текст исключения внутри `warnings`):

```json
{"key": "ACCEPT-PRI272-SYNTHETIC", "embedded": false, "links_upserted": 0,
 "links_stored": null, "prs_linked": 0,
 "warnings": ["embedder: APIConnectionError: Error communicating with VoyageAI: …Connection refused…"],
 "retry_required": true, "failure": "embedder"}
```

`content_hash` этого ключа до опыта — `None`, после опыта — `None`: при сбое
эмбеддера запись в стор не выполняется вовсе (upsert стоит в ветке `else`), и
задача остаётся помеченной к повтору.

## Опыт 4 — сквозняк до ветки секции `task`

Ветке нужен реальный батч к эмбеддингу **внутри** `warm_board`, а для этого
задача обязана одновременно отсутствовать в сторе и быть новее watermark.
Инкрементальный синк такого сам не создаёт (это и показал опыт 2), поэтому
случай поставлен двумя обратимыми локальными правками: удалением строки
`ID-328` (задача PRI-272; ключи стора этой доски — `ID-N`, `PRI-N` живёт в
`aliases`) и обнулением курсора `tasks:yougile:PRI`. Внешние системы не
затрагивались: доска не правилась.

**Ответ за 36.10 с** (перечисление 132 задач доски; Voyage отказывает мгновенно):

```json
{"section": "task",            "reason": "задача не проиндексирована: эмбеддер не отвечает", "cause": "embedder_unavailable", "cause_detail": null, "remedy": null}
{"section": "related.similar", "reason": "пропущено: эмбеддер не отвечает",                  "cause": "embedder_unavailable", "cause_detail": null, "remedy": null}
{"section": "subsystems",      "reason": "пропущено: эмбеддер не отвечает",                  "cause": "embedder_unavailable", "cause_detail": null, "remedy": null}
{"section": "code",            "reason": "пропущено: эмбеддер не отвечает",                  "cause": "embedder_unavailable", "cause_detail": null, "remedy": null}
{"section": "test_exemplars",  "reason": "пропущено: эмбеддер не отвечает",                  "cause": "embedder_unavailable", "cause_detail": null, "remedy": null}
```

Свод синка в том же ответе:
`{"enumerated": 132, "changed": 132, "embedded": 0, "failed": 1,
"embedder_failed": true, "cursor_advanced": false}`.

Это и есть критерий PRI-272 для секции `task`: пользователю сказано, что задача
**не проиндексирована из-за эмбеддера**, а не что её «нет в сторе». Курсор при
неудаче не продвинут — задача не потеряна.

**Восстановление проверено, а не заявлено.** Полный синк с живым Voyage вернул
`{"enumerated": 132, "changed": 132, "embedded": 1, "failed": 0,
"embedder_failed": false, "cursor_advanced": true}`; `content_hash` строки
`ID-328` — `e55f1618…dc57` до опыта и он же после; курсор — `1787947092961` до
опыта и он же после.

## Опыт 5 — контрольный: остановленный контейнер против занятого пула

**Этого опыта в первом круге приёмки не было, и его отсутствие пропустило
дефект.** Оба случая приходят к классификатору ОДНИМ типом
`psycopg_pool.PoolTimeout`: через пул остановленный Postgres не отдаёт отказ
соединения — фоновые воркеры пула молча ретраятся, а `getconn` ждёт полный
таймаут. Опыты 1–4 ставили «пул занят» и «эмбеддер мёртв», но не ставили
«контейнер не поднят», а на входе они неразличимы.

Остановленный контейнер моделируется закрытым портом `127.0.0.1:5999`: для
драйвера это тот же connection refused, а живые контейнеры соседних сессий при
этом не трогаются.

**До фикса** — вердикты побайтово одинаковые, то есть критерий PRI-274 «случай
отличим по наблюдаемым полям» не выполнен, и лежачий контейнер вдобавок теряет
единственное лекарство:

| Случай | Тип, время | `cause_detail` | `remedy` |
|---|---|---|---|
| Postgres не поднят | `PoolTimeout`, 30.0 с | `pool_exhausted` | `null` |
| Postgres поднят, пул занят | `PoolTimeout`, 30.0 с | `pool_exhausted` | `null` |

**После фикса** — тип дополнен наблюдением (одноразовая проба
`psycopg.connect(connect_timeout=2)` мимо пула):

| Случай | Тип, время | `cause_detail` | `remedy` | `redacted` |
|---|---|---|---|---|
| Postgres не поднят | `PoolTimeout`, 30.0 с | `null` | `"reviewer start"` | `connection failed: connection to server at "[REDACTED]", port [REDACTED] failed: … Connection refused` |
| Postgres поднят, пул занят | `PoolTimeout`, 30.0 с | `pool_exhausted` | `null` | `null` |

Хост и порт в отрывке вымараны — диагностика осталась бессекретной.

**Цена пробы измерена, а не оценена:** 0.00 с на закрытом порту (connection
refused немедленный) и 0.02 с на живом сервере. Опыт 1 после фикса переснят и
дал **30.09 с** — тот же один таймаут пула.

## Что этими опытами не проверено

- **Мержа и удалённого MCP** приёмка не касается: канал файловый и локальный.
- **Локализация сообщений libpq.** `auth_failed` и `missing_database` различаются
  по тексту сервера, поэтому на не-английской локали `lc_messages` уедут в
  нераспознанную ветку. Здесь наблюдался только `pool_exhausted`, который
  решается **типом** исключения и локали не подвержен.
- **Одновременный отказ обоих источников** живьём не ставился: опыты 1 и 2
  разводят Postgres и Voyage намеренно, чтобы каждый класс наблюдался чисто.
  Текст шага 0a про несколько классов сразу опирается на раздельность замыкания,
  подтверждённую в опыте 1 сборкой `related.linked` при мёртвом Postgres.
