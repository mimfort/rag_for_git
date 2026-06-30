# PRI-202 — Адаптивные лимиты контекста (cliff-cut реранкера) + per-repo конфиг

- **Задача:** PRI-202 (store: ID-202) — «Конфигурируемые лимиты контекста + интерактивное расширение»
- **URL:** https://ru.yougile.com/team/686c049c8af8/#PRI-202
- **Дата:** 2026-07-01
- **Статус:** дизайн утверждён, готов к плану
- **Бриф:** `docs/superpowers/briefs/2026-07-01-PRI-202-configurable-context-limits.md`

## Проблема

Лимиты retrieval-тулов захардкожены и едины для всех репозиториев/тасок (`search_codebase top_k=10`,
`search_tasks top_k=5`, `related_symbols hops=1`, `callers _CAP=25`). Релевантный контекст **молча
отсекается на уровне retrieval** — LLM его даже не видит. Числа не адаптируются ни к плотности репо
(монорепа vs утилита), ни к размаху таски (багфикс vs крупная фича). Цель solve-task — полноценные
брифы — страдает: на крупных тасках релевантный код теряется.

## Цель

Адаптивный охват: top_k подбирается **сам** по «обрыву» (cliff) скоров реранкера — маленький багфикс
получает 2-3 чанка, крупная фича 12-15. Параметры настраиваются **per-project** в `.review.yml`.
Никаких синхронных переспросов пользователя; ленивая текстовая заметка сигналит о хвосте за обрезом.

## Ключевые решения (утверждены на brainstorming)

1. **Главный механизм — авто-отсечка по cliff реранкера**, а не фиксированный/конфигурируемый top_k.
2. **Конфиг = предохранители** (floor/ceiling/candidate_pool) + ленивая заметка о хвосте.
   **Синхронный интерактивный interrupt — выкинут** (round-trips, зависимость от дисциплины LLM).
3. **Алгоритм cliff — гибрид:** relative-to-top (`ratio`) ∧ absolute floor (`abs_floor`), зажатый
   в рельсы `[floor, ceiling]`.
4. **Параметры только в `.review.yml`** (per-project). **Env-слоя нет.** Дефолты — константы в коде.
5. **brief-cap делаем адаптивным** в этой же работе: SKILL.md перестаёт резать бриф фиксированным
   числом — берёт столько, сколько отдал cliff/рельсы.
6. **`search_tasks` ≠ `search_codebase`** (асимметрия): у задач нет реранкера (RRF-only) → им только
   статические рельсы, без cliff.
7. **ANN-префильтр перед реранком** (защита Voyage free tier): RRF-ранг не отличает сигнал от шума,
   поэтому реранкать пул вслепую дорого. Перед реранком отбрасываем кандидатов с явно плохой
   ANN-cosine-близостью — но **только тех, кто не является BM25-совпадением** (лексический хит
   оставляем даже при плохом векторе). На запросе «релевантных нет» пул схлопывается до горстки →
   реранкаем 3-5, а не 30. `candidate_pool` дефолт снижен 40 → 30.

## Архитектура и поток данных

Cliff-cut применяется **только к session-less пути** `Retriever.search_base` (его дёргает
`search_codebase`). PR-путь `Retriever.retrieve()` (overlay-сессия) **не трогаем** — у него своя
логика и свои тесты.

Новый поток `search_base`:
1. `store.hybrid_search` → до `candidate_pool` кандидатов (дефолт 30); теперь возвращает на чанк
   сырую ANN-cosine-дистанцию и флаг BM25-совпадения.
2. graph-expansion (`hops` из конфига) подмешивает соседей (graph-items префильтр НЕ трогает —
   они здесь ради blast-radius, не близости).
3. **ANN-префильтр:** из hybrid-хитов оставляем кандидата, если он `bm25_hit ИЛИ ann_dist ≤
   ann_distance_max`; остальные (далёкий вектор И без лексического хита) — отбрасываем ДО реранка.
4. дедуп перекрытий + (по умолчанию) отсев тестов — как сейчас.
5. **реранкер прогоняется ВСЕГДА** (если доступен и кандидатов > floor) и **возвращает скоры** (0–1).
6. **cliff-селектор** режет список → `(kept, tail_meta)`.
7. `ContextPack` несёт `kept` + `tail_meta`; `as_context()` дописывает ленивую заметку.

Fail-open: реранкер `None` / упал (rate-limit Voyage) → откат на RRF-порядок + срез по `ceiling`,
заметка не пишется. Retry/backoff Voyage уже есть (`index/_retry.py`).

## Алгоритм cliff (чистая функция)

`reviewer/retrieval/cliff.py` — изолированная, тестируется без БД:

```python
def select_by_cliff(scored, *, floor_n, ceiling_n, ratio, abs_floor):
    """scored: list[(item, score)], отсортирован по score убыванием.
    Возвращает (kept_items, tail_meta)."""
    if not scored:
        return [], _empty_meta()
    top = scored[0][1]
    kept = []
    for i, (item, score) in enumerate(scored):
        if i < floor_n:                       # рельса floor: минимум всегда
            kept.append((item, score)); continue
        if len(kept) >= ceiling_n:            # рельса ceiling: жёсткий максимум
            break
        if score >= top * ratio and score >= abs_floor:
            kept.append((item, score))        # внутри cliff — берём
        else:
            break                             # обрыв — стоп
    tail = scored[len(kept):]
    return [it for it, _ in kept], _build_tail_meta(tail, abs_floor, top)
```

- `floor` побеждает `abs_floor`: на узком/мусорном запросе вернём `floor_n` элементов (не пусто),
  заметка пометит «слабые совпадения».
- `floor_n` зажимается `min(floor_n, len(scored))`; предполагается `floor ≤ ceiling` (иначе конфиг
  невалиден — `ceiling` приоритетнее, лог-варнинг).

`tail_meta` (для заметки): `beyond_relevant` = число элементов за обрезом со `score ≥ abs_floor`;
их группировка по **первому сегменту пути** (`{prefix: (count, top_score)}`); `cut_score` (скор
последнего взятого) и `top_score`. Пусто, если за обрезом нет ничего ≥ `abs_floor`.

## Поведение (примеры search_codebase)

- **Узкий багфикс** — реранкер `[0.91, 0.34, 0.12, …]`: `0.34 < 0.91×0.5` → обрыв после 1-го, но
  `floor=4` → 4 чанка.
- **Крупная фича** — `[0.88, 0.81, 0.79, 0.74, 0.71, 0.68, …, 0.22]`: все ≥ `0.44` и ≥ `0.3` →
  ~10-12 до обрыва на `0.22`.
- **Трудный/мусорный** — `[0.41, 0.38, 0.35, …]`: `abs_floor=0.3` режет хвост → верхушка + заметка.
- **Релевантных нет** — ANN-префильтр схлопывает пул до 3-5 (далёкие вектора без BM25-хитов
  отброшены до реранка) → реранкаем горстку, не 30; cliff вернёт `floor` + заметку «слабые совпадения».

## Конфиг `.review.yml` (per-project, env нет)

```yaml
context_limits:
  search_codebase:
    floor: 4              # минимум чанков всегда (даже при обрыве на 1-м)
    ceiling: 15           # потолок (токены/Voyage); монорепа → 25-30
    ratio: 0.5            # брать пока score >= 0.5×(топ); ↑строже, ↓шире
    abs_floor: 0.3        # и score >= 0.3 по абсолюту — режет шум
    candidate_pool: 30    # верхний предел кандидатов до реранка (recall↔стоимость Voyage)
    ann_distance_max: 0.65 # ANN-префильтр: отбросить кандидата с cosine-дистанцией > порога ДО
                          #   реранка, ЕСЛИ он не BM25-хит. Пермиссивный дефолт (режет явный шум,
                          #   recall не страдает); ниже → агрессивнее экономит Voyage, риск отсечь
  search_tasks:
    floor: 3            # реранкера нет → только рельсы
    ceiling: 8          # борда с 2000+ тасок → выше
  graph:
    hops: 1             # глубина обхода графа от топ-хитов
    callers_topk: 25    # cap callers на узел (было _CAP=25)
```

Без секции / без отдельных ключей → дефолт-константы из кода. `ReviewPolicy` парсит **только из yml**.

## Компоненты и файлы

| Файл | Изменение |
|---|---|
| `reviewer/retrieval/cliff.py` *(новый)* | чистая `select_by_cliff` + `_build_tail_meta`; дефолт-константы лимитов |
| `reviewer/policy/policy.py` | + поле `context_limits` (dataclass `ContextLimits`); парс в `from_yaml`/`load` **только из yml**; `from_settings` не читает env — отдаёт константы |
| `reviewer/index/reranker.py` | + `rerank_scored(query, items) → [(item, score)]`; старый `rerank()` делегирует и срезает скоры (PR-путь цел) |
| `reviewer/index/store.py` | `hybrid_search` возвращает на чанк ANN-cosine-дистанцию (`embedding <=> vec` из ann-CTE) и флаг `bm25_hit` (LEFT JOIN к bm25-CTE); `Retrieved` + поля `ann_distance: float\|None`, `bm25_hit: bool`. PR-путь `retrieve()` поля игнорирует (обратно совместимо) |
| `reviewer/retrieval/retriever.py` | `search_base`: ANN-префильтр (`bm25_hit ∨ ann_distance ≤ ann_distance_max`, graph-items не трогаем), всегда реранк (пул ≤`candidate_pool`), `hops` из конфига вместо хардкода (:128), вызов `select_by_cliff`, проброс `tail_meta` |
| `reviewer/retrieval/output_shaping.py` | `ContextPack` несёт `tail_meta`; `as_context()` дописывает заметку |
| `reviewer/mcp/service.py` | `_resolve_context_limits(repo, branch)` (зеркало `_resolve_summary_depth` :331, читает `.review.yml` ветки, fail-soft → константы); `search_codebase`/`search_tasks`/`related_symbols`/`find_callers` берут лимиты оттуда; `top_k: int=10` → `int\|None=None` (override потолка) |
| `reviewer/entrypoints/mcp_server.py` | FastMCP-регистрация тулов `search_codebase`/`search_tasks`: сигнатура `top_k` → `int\|None=None` + обновить docstring (override ceiling, не фикс top_k) |
| `reviewer/tasks/store.py` *(или service)* | `search_tasks` применяет рельсы `floor`/`ceiling` + tail-счёт за `ceiling` |
| `reviewer/tools/graph_format.py` | `_CAP=25` → параметр `cap` (дефолт-константа), пробрасывается из `context_limits.graph.callers_topk` |
| `plugin/skills/solve-task/SKILL.md` + `plugin/skills/ask/SKILL.md` | адаптивный brief-cap + ленивая инструкция перевызова |

## search_tasks — рельсы, не cliff

`TaskStore.search` (RRF-only, без реранкера) → cliff неприменим (плоский RRF без обрывов). Только
статические рельсы: показать `min(ceiling, found)`, но не меньше `floor`. Заметка: «показано N из M
(рельса ceiling) — перевызови с большим ceiling». Адаптивность для задач (Voyage-реранк в таск-путь)
— в «возможных расширениях».

## Ленивая заметка (формат)

Дописывается `as_context()` в конец строки выдачи, только если `tail_meta.beyond_relevant > 0`:

```
— контекст обрезан по cliff: 7 из 23 (скор 0.88→0.71, обрыв на 0.22). За обрезом ещё 4
релевантных (≥0.3): reviewer/retrieval (0.69), reviewer/index (0.55). Перевызови с большим
ceiling, чтобы включить.
```

Для `search_tasks` (рельсы): «— показано 8 из 14 (рельса ceiling). Перевызови с большим ceiling».

## SKILL.md (solve-task + ask)

1. **Адаптивный brief-cap.** В Relevance-фильтре убрать фиксированные числа «≤5 файлов/символов»,
   «≤3 test exemplars»: ретривал уже адаптивно ограничен server-side (cliff/рельсы) → включать
   **каждый** возвращённый элемент, который *прямо информирует* реализацию, **не до-резать до
   фиксированного числа и не раздувать искусственно**. Бинарный judgment keep/drop (directly-informs)
   и отчёт `(dropped N: reason)` — остаются (это контроль качества, не числовой кап). Related tasks
   ограничены рельсой `ceiling` поиска; дистилляция берёт прямо-информирующие.
2. **Ленивый перевызов.** Если выдача тула кончается заметкой о высокоскоровом хвосте за обрезом
   И таска выглядит широкой — LLM **может** перевызвать тул с бóльшим `ceiling` (override-параметр),
   чтобы добрать. **Без переспроса пользователя.**

**Механизм override:** session-less `search_codebase(... top_k=None ...)` — `top_k` переосмысляется
как **override потолка** для этого вызова: `None` → `ceiling` из конфига; явное значение → поднять
`ceiling`. Сигнатура меняется `top_k: int = 10` → `top_k: int | None = None` (обратно совместимо для
вызовов с явным числом). Аналогично `search_tasks`.

## Тесты (AC 8-9)

| Тест | Что проверяет |
|---|---|
| `tests/retrieval/test_cliff.py` *(новый)* | `select_by_cliff`: отсечка по `ratio`, по `abs_floor`, `floor` поднимает минимум, `ceiling` режет максимум, `tail_meta` (счёт ≥abs_floor, группировка по префиксу, диапазон), края (пусто/1 элемент) |
| `tests/index/test_reranker.py` | `rerank_scored` → `[(item, score)]` в порядке; `rerank()` отдаёт только items (PR-путь цел); Voyage замокан |
| `tests/policy/test_policy.py` | `context_limits` из `.review.yml` поверх констант; нет секции → дефолты; частичная секция → остальное дефолт; подсекции `search_tasks`/`graph` |
| `tests/retrieval/test_search_base.py` *(обновить)* | ANN-префильтр (BM25-хит остаётся при плохом векторе; далёкий не-BM25 отброшен до реранка), всегда реранк (`FakeReranker.calls`) по отфильтрованному пулу, cliff применён, заметка в `ContextPack`; fail-soft (реранкер `None`/исключение) → RRF + срез `ceiling`, без заметки |
| `tests/index/test_store_hybrid.py` | `hybrid_search` отдаёт `ann_distance`/`bm25_hit`; чанк из BM25 без ANN → `ann_distance=None, bm25_hit=True` |
| `tests/retrieval/test_retriever.py` | **не трогаем** — `retrieve()` (PR-путь) и rerank-skip тесты зелёные (AC 8) |
| `tests/tools/` + `tests/mcp/` | `graph_format` cap-параметр (срез на cap); `_resolve_context_limits` fail-soft → константы; `hops` пробрасывается в `search_base`; `top_k`-override поднимает ceiling |
| `tests/tasks/` | `search_tasks` рельсы floor/ceiling + tail-счёт |

## Границы скоупа

**В скоупе:** cliff-cut `search_codebase`; ANN-префильтр пула (BM25-aware); рельсы `search_tasks`;
`hops`+`callers_topk` как конфиг; `context_limits` только в `.review.yml`; ленивая заметка;
адаптивный brief-cap + ленивый перевызов в SKILL.md (solve-task + ask); тесты выше.

**Вне скоупа (явно):**
- Синхронный интерактивный interrupt (round-trips) — выкинут по решению.
- Voyage-реранкер для `search_tasks` (адаптивные задачи) — возможное расширение.
- PR-review путь (`retrieve()` + overlay) — потом, своя сессия.
- env-слой конфига — только `.review.yml`.
- Полный структурный мета-конверт `by_category`/`top_outliers` — заменён компактной прозо-заметкой
  (не ломаем строковую форму выдачи тула).

## Возможные расширения (не сейчас)

- Адаптивность для `search_tasks`: добавить Voyage-реранк в таск-путь → cliff и для задач.
- Cliff для PR-review (`retrieve()`), когда появится спрос.
- Структурный мета-конверт (JSON) вместо прозо-заметки, если потребители захотят машинный разбор.

## Обратная совместимость

- `retrieve()` (PR-путь) и его тесты не меняются; новые поля `Retrieved.ann_distance`/`bm25_hit`
  опциональны (дефолт `None`/`False`) — PR-путь их просто игнорирует.
- `rerank()` сохраняет сигнатуру/семантику (новый scored-путь — отдельный метод).
- Отсутствие `context_limits` в `.review.yml` → дефолт-константы (поведение близко к нынешнему, но
  top_k теперь адаптивен, а не фикс 10 — это намеренное улучшение, не регресс).
- `top_k`-override в session-less тулах обратно совместим для вызовов с явным числом.
