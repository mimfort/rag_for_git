# PRI-165 — Сводки подсистем (C): freshness по структуре + cap/выбор модели — дизайн

**Задача:** PRI-165 (ID-165), оценка M. Слой: Движок (`reviewer/graph/summaries.py` + store + скилл
`summarize-subsystems`) — стоимость/надёжность под прод-масштаб.
**Порядок программы:** первая из трёх связанных задач (**C (эта) → B (PRI-166) → A (PRI-167)**).
**Связи:** PRI-159 (community summaries — фича, которую правим), PRI-154 (`python_skeleton` — переиспользуем).
**Ветка работы:** фича-ветка от `dev` (напр. `feat/pri-165-summary-skeleton-freshness`), PR → `dev`.

## Проблема

`source_hash` кластера сводки (`reviewer/graph/summaries.py:44,87`) = `sha256` от множества пар
`(node_id, content_hash)` всех членов, где `content_hash` (`reviewer/index/models.py:19`) — хэш
**полного** нормализованного текста символа (заголовок + тело). → **любая** правка тела (багфикс,
рефактор внутри функции) меняет `content_hash` → ре-стейлит **весь** грубый кластер (при `depth=2`
кластер = целый пакет) → LLM-скилл пересобирает сводку, хотя публичная поверхность подсистемы не
менялась. На проде с авто-реиндексом и частыми коммитами это даёт постоянный LLM-churn (стоимость +
латентность). Сейчас спасает только ручной запуск `/summarize-subsystems`, но на проде его привяжут
к реиндексу/крону.

**Критерии приёмки (из задачи):**
1. body-only правка члена **НЕ** ре-стейлит кластер; add/remove члена или смена сигнатуры — ре-стейлит.
2. cap ограничивает число LLM-пересборок за проход и репортит число отложенных.
3. unit на новой freshness-функции: смена сигнатуры vs смена только тела.

## Решения (зафиксированы на brainstorming)

1. **Freshness по скелету, а не по телу.** Ключ свежести члена считать от **структурного скелета**
   (сигнатуры `def`/`class` до `:`, первая строка docstring) через существующий `python_skeleton`
   (PRI-154, `reviewer/index/chunker.py:46`), а не от полного `content_hash`. Body-only правка →
   `skeleton_hash` тот же → не ре-стейлит.
2. **Skeleton-хэш — на лету из текста чанка** (а не колонка в БД): `list_base_members` добавляет
   `text` в `SELECT`, новый чистый хелпер считает `skeleton_hash` из текста символа. **Без миграции
   схемы, без реиндекса, без расхода Voyage.** Цена — tree-sitter-парс на члена при вызове
   `list_subsystem_clusters` (только путь скилла `summarize-subsystems`, не горячий).
3. **Cap — server-side**, в `list_subsystem_clusters` (детерминированно, токен-дёшево, в духе
   `sync_board`), а не дисциплиной LLM. Параметр `cap`, дефолт из `Settings` (env `SUMMARY_REBUILD_CAP`),
   **дефолт `None` = безлимит** (текущее поведение без явной настройки не меняется).
4. **Порядок stale для cap:** кластеры **без сохранённой сводки** (`updated_at IS NULL`) — первыми
   (пропущенные хуже устаревших), затем по `updated_at` по возрастанию (дольше всех не обновлялись).
5. **Выбор модели для генерации сводок — спросить при запуске, дешёвый дефолт.** Скилл спрашивает
   тир модели (дефолт — дешёвый: Haiku/Sonnet/Fable; сводка — грубый приор, точность не критична) и,
   где харнесс умеет per-subagent override (Claude Code), диспатчит per-cluster summary-субагентов на
   выбранной модели; где не умеет — пишет inline на модели сессии с пометкой. Формулировки в `SKILL.md`
   — харнес-нейтральные («dispatch a subagent»). cap × модель = двойной контроль стоимости.
6. **Гранулярность — атомарная пересборка кластера** (одна сводка на кластер, при stale — целиком).
   Дельта-суммаризация (патч сводки по изменённым членам) — **не делаем** (риск дрейфа, scope > M);
   радиус пересборки независимо дробит PRI-166 (depth).
7. **Кадэнс (пункт 3 задачи) — вне объёма** (оркестрация запуска: крон/триггер), follow-up.

## Инвариант: skeleton-хэш позиционно- и порядко-независим

Ключевая корректность фичи: сдвиг/перенос символа **не должен** ре-стейлить. Гарантируется тем, что:
- `node_id = path#fqn` (`models.py:15`) — **без номера строки**; `start_line` **не входит** в
  `compute_source_hash`;
- `symbol_skeleton_hash` хэширует **нормализованный текст** строк скелета (rstrip, как `content_hash`),
  **а не их номера** — позиция символа в файле в хэш не протекает;
- `compute_source_hash` **сортирует** пары перед хэшированием (`summaries.py:49`) → порядок членов в
  кластере не важен (множество).

| Изменение | Ре-стейлит? | Почему |
|---|---|---|
| Сдвиг символа по файлу (пустые строки/комменты вокруг) | нет | текст скелета и `fqn` те же |
| Реордеринг символов | нет | `compute_source_hash` сортирует множество |
| Правка тела / переформатирование тела | нет | строки скелета не изменились |
| Смена сигнатуры / 1-й строки docstring | **да** | строка скелета изменилась |
| Add/remove символа | **да** | изменился состав множества `node_id` |
| Перенос символа в другой файл / в класс (fqn `Class.method`) | **да** | `node_id` изменился |

## Разграничение: вектор-поиск НЕ затрагивается

PRI-165 трогает **только** ключ свежести сводок. Эмбеддинги/ретрив остаются как есть:

| Механизм | Над чем | Меняет PRI-165? |
|---|---|---|
| Эмбеддинг / вектор-поиск | полный текст чанка (заголовок+тело) | нет |
| `content_hash` (дедуп/реюз эмбеддингов) | полный текст чанка | нет |
| `node_id = path#fqn` (связь RAG↔граф) | — | нет |
| **`skeleton_hash`** (новый, на лету) | только строки скелета | **да (только это)** |

Body-only правка по-прежнему переэмбеддит этот один чанк при реиндексе (вектор-поиск свеж), но
сводку кластера не дёргает.

## Компоненты

| Компонент | Тип | Роль |
|---|---|---|
| `reviewer/index/chunker.py` | правка | новый чистый хелпер `symbol_skeleton_hash(text: str) -> str` рядом с `python_skeleton` (там tree-sitter; `summaries.py` остаётся без tree-sitter) |
| `reviewer/index/store.py` | правка | `ChunkStore.list_base_members` — добавить `text` в `SELECT`, вернуть 5-кортеж со `skeleton_hash` |
| `reviewer/graph/summaries.py` | правка | `Member.skeleton_hash`; `build_clusters` подаёт `skeleton_hash` в `compute_source_hash`; докстринг `compute_source_hash` |
| `reviewer/mcp/service.py` | правка | `list_subsystem_clusters`: cap + порядок + `deferred` + `updated_at`; `index_subsystem_summary`: consistency-check на `skeleton_hash` |
| `reviewer/index/summary_store.py` | правка | выдать `updated_at` по `cluster_key` для упорядочивания stale |
| `reviewer/config/settings.py` (Settings) | правка | `summary_rebuild_cap: int \| None = None` (env `SUMMARY_REBUILD_CAP`), рядом с `summary_cluster_depth` |
| `plugin/skills/summarize-subsystems/SKILL.md` | правка | репорт `deferred` (cap); выбор модели при запуске + диспатч summary-субагентов |

## Контракты

### `reviewer/index/chunker.py` — `symbol_skeleton_hash(text: str) -> str`

```python
def symbol_skeleton_hash(text: str) -> str:
    """Хэш структурного скелета символа: сигнатуры def/class (до ':') + 1-я строка docstring.
    Позиция/порядок в хэш не входят — берётся ТЕКСТ строк скелета, не их номера.
    Пустой скелет (битый код / нет определений) → fallback на нормализованный полный текст."""
    nums = python_skeleton(text.encode("utf-8"))     # 1-based строки скелета относительно text
    lines = text.splitlines()
    if nums:
        body = "\n".join(lines[n - 1].rstrip() for n in nums if 1 <= n <= len(lines))
    else:
        body = "\n".join(l.rstrip() for l in lines)  # fallback — как content_hash
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()
```
Нормализация (`rstrip` строк + `strip`) — та же, что у `Chunk.content_hash` (`models.py:20`), чтобы
скелет-ключ был согласованным подмножеством. `python_skeleton` не падает на битом коде → fallback
покрывает редкий случай чанка без распознанных определений (для def/class-чанков не наступает).

### `ChunkStore.list_base_members(repo, branch) -> list[tuple[path, fqn, content_hash, start_line, skeleton_hash]]`

`SELECT path, symbol_fqn, content_hash, start_line, text FROM chunks WHERE repo=%s AND ref=%s`
(`ref=base:<branch>`). Для каждой строки `skeleton_hash = symbol_skeleton_hash(text)`. Возврат —
**5-кортеж**: существующие позиции 0–3 без изменений (обратная совместимость распаковки), `skeleton_hash`
добавлен 5-м элементом. `text` наружу не отдаётся (только источник хэша). `content_hash` сохранён в
кортеже (читатели вне freshness; в самом freshness больше не участвует).

### `reviewer/graph/summaries.py`

- `@dataclass Member`: добавить `skeleton_hash: str` (после `content_hash`).
- `build_clusters` (строка 87): `source_hash=compute_source_hash([(m.node_id, m.skeleton_hash) for m in ms])`.
- `compute_source_hash` — **код без изменений** (хэширует пары `id:hash`); обновить докстринг
  («content_hash» → «skeleton_hash — ключ свежести по структуре»).

### `reviewer/mcp/service.py`

- `list_subsystem_clusters(repo, branch=None, depth=None, min_size=None, cap=None) -> dict`:
  - `cap` дефолтится в `self.settings.summary_rebuild_cap` (если параметр `None`).
  - `Member(... skeleton_hash=sk ...)` из 5-кортежа `list_base_members`.
  - `stored = summary_store.get_source_hashes(...)`, `updated = summary_store.get_updated_ats(...)`
    (новый метод, см. ниже). `stale = stored.get(key) != c.source_hash`.
  - **Cap:** если `cap` задан (не `None`) и число stale > `cap` — отсортировать stale по
    `(updated_at is not None, updated_at)` (None/нет-сводки первыми, затем `updated_at` возр.), взять
    первые `cap`, остальные stale **исключить** из `clusters`; `deferred = len(stale) - cap`.
    Свежие (`stale=false`) кластеры в `clusters` остаются всегда. `cap=None` → ничего не исключаем,
    `deferred=0`.
  - Возврат: `{"branch": resolved, "clusters": [...], "deferred": <int>}` (каждый кластер как сейчас:
    `cluster_key, num_members, files, top_symbols, source_hash, stale`).
- `index_subsystem_summary` (строки 466–470): `members` строить из **`skeleton_hash`** (5-й элемент
  5-кортежа), а не `content_hash`: `members = [(f"{p}#{s}", sk) for p, s, _h, _sl, sk in raw if
  cluster_key_of(p, depth) == cluster_key]`. **Синхронно** с `build_clusters` — иначе consistency-check
  (`compute_source_hash(members) == source_hash`) разъедется и `member_node_ids` всегда будут `[]`.

### `reviewer/index/summary_store.py`

- Новый `get_updated_ats(repo, branch) -> dict[cluster_key, datetime]` (или расширить
  `get_source_hashes` до `dict[cluster_key, tuple[source_hash, updated_at]]`). `updated_at` уже хранится
  (`subsystem_summaries.updated_at`). Отсутствие таблицы → `{}` (fail-soft, как `get_source_hashes`).

### `reviewer/config/settings.py` (Settings)

- `summary_rebuild_cap: int | None = None` (env `SUMMARY_REBUILD_CAP`). Рядом с существующим
  `summary_cluster_depth` (`settings.py:71`). `None`/0 трактуем как безлимит (нормализовать:
  `cap if cap and cap > 0 else None`).

### Скилл `plugin/skills/summarize-subsystems/SKILL.md`

- **Шаг 2 (list):** `list_subsystem_clusters` теперь применяет cap server-side; в ответе поле `deferred`.
- **Выбор модели (новый шаг, только если есть stale-кластеры):** спросить пользователя тир модели для
  написания сводок; дефолт — дешёвый (с пояснением: сводка — высокоуровневый приор). Харнес-нейтрально.
- **Шаг 3 (summarize):** для каждого stale-кластера — где харнесс умеет, **dispatch summary-субагента**
  на выбранной модели: субагент `Read`'ит представительные файлы (`files`/`top_symbols`), возвращает
  `{title, summary}` (RU, grounded; `_common/anti-hallucination.md` применяется к субагенту);
  оркестратор персистит `index_subsystem_summary(..., source_hash)`. Где override нет — писать inline
  на модели сессии, пометить в репорте.
- **Шаг 4 (report, RU):** «N просуммировано, K пропущено как свежие, M отложено по cap (`deferred`)» —
  без молчаливого усечения.

## Обработка ошибок / краевые случаи

- **Одноразовый «шторм» при апгрейде:** существующие сводки имеют `source_hash` от старого
  (`content_hash`) входа. Первый проход после деплоя пересчитает `source_hash` от `skeleton_hash` → все
  кластеры разово окажутся stale → одноразовая полная пересборка. Ожидаемо и приемлемо (далее —
  стабильность по скелету). **cap естественно растягивает шторм** на несколько проходов; скилл репортит
  `deferred`. Зафиксировать в `SKILL.md`-репорте.
- Чанк без распознанных определений → `symbol_skeleton_hash` fallback на полный текст (безопасно: любая
  правка ре-стейлит). Для def/class-чанков не наступает.
- `depth`-mismatch consistency-check в `index_subsystem_summary` сохраняется (теперь над `skeleton_hash`).
- Пустой base-индекс / нет таблицы summary → как сейчас (`{"clusters": [], "note": ...}`, fail-soft).
- Всё per `(repo, branch)` — мульти-бранч/мульти-репо изоляция сохранена.
- Где харнесс не даёт выбрать модель субагента — деградирует на модель сессии, без ошибки.

## Тестирование

- **Unit (ядро, критерий 3) — `tests/index/test_chunker.py`:** `symbol_skeleton_hash`:
  - смена сигнатуры (новый параметр/аннотация) ⇒ **другой** хэш;
  - правка только тела ⇒ **тот же** хэш;
  - сдвиг символа (пустые строки вокруг тела) / переформатирование тела ⇒ **тот же** хэш
    (позиционная независимость);
  - смена 1-й строки docstring ⇒ другой хэш; fallback на чанке без определений.
- **Unit — `tests/graph/test_summaries.py`:** `build_clusters` `source_hash` из `skeleton_hash`:
  body-only (тот же `skeleton_hash`) ⇒ тот же `source_hash`; смена сигнатуры ⇒ другой; add/remove
  члена ⇒ другой; реордеринг ⇒ тот же. Обновить конструирование `Member` (поле `skeleton_hash`).
- **Unit — `tests/mcp/test_subsystem_summaries.py`:** моки `list_base_members` → 5-кортежи;
  `index_subsystem_summary` консистентен на `skeleton_hash`; **cap**: при `cap=N` и >N stale — ровно N в
  `clusters`, `deferred = stale-N`; **порядок**: кластер без сводки (нет в `get_source_hashes`/
  `updated_at`) идёт раньше устаревшего; `cap=None` → ничего не отложено.
- **Unit — `tests/index/test_summary_store.py`:** `list_base_members` отдаёт 5-кортеж со
  `skeleton_hash`; `get_updated_ats` (или расширенный `get_source_hashes`).
- **Guard-тест скилла** (`tests/skills/`): сборка промпта `summarize-subsystems` с раскрытием
  `_common`-include'ов остаётся валидной после правок шагов.
- **Integration** (маркер `integration`): round-trip `list_subsystem_clusters` с cap на реальном
  Postgres — body-only правка не ре-стейлит, смена сигнатуры ре-стейлит.

## Вне объёма (YAGNI / follow-up)

- **Дельта-суммаризация** (патч сводки по изменённым членам, чтение только изменённых файлов) — отдельный
  follow-up; риск дрейфа + scope > M.
- **Кадэнс** (пункт 3: пересборка по расписанию/триггеру) — оркестрация запуска, follow-up.
- **Колонка `skeleton_hash` в `chunks`** — не вводим; считаем на лету (без миграции/реиндекса).
- **Embedding-экономия при body-only правках** — другой рычаг, другая задача.
- **depth кластеризации** (радиус пересборки) — PRI-166 (B).
