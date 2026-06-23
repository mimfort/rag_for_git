# PRI-161 (расширенная) — настраиваемый контекст-слой: ignore-на-входе + per-prefix depth + приор сводок в solve-task

**Дата:** 2026-06-23
**Задача:** PRI-161 (приор сводок подсистем в solve-task), расширенная по запросу пользователя двумя связанными настройками контекст-слоя.
**Оценка:** L (три части, один цикл).

## Контекст и проблема

Три независимых по коду, но тематически связанных улучшения «контекст-слоя» — всё настраивается через `.review.yml` целевой/индексируемой ветки:

1. **Приор сводок в solve-task (ядро PRI-161).** `plugin/skills/solve-task/SKILL.md` шаг 3 «Gather context» собирает контекст задачи (`search_tasks`, `search_codebase`, граф, `get_pr_diff`), но **не** подмешивает сводки подсистем. `get_subsystem_summaries(query, top_k)` с top-k отбором готов (PRI-167, PR #55 смержен) — осталось подключить как архитектурный приор.

2. **Per-prefix depth кластеризации сводок.** Сейчас глубина кластера глобальна на репо: env `SUMMARY_CLUSTER_DEPTH` + per-repo override `summary_cluster_depth` в `.review.yml` (PRI-166). В гетерогенных репозиториях одни поддеревья хочется дробить глубже, другие держать крупно — глобальный depth везде компромиссен.

3. **Ignore-папок на входе (как .gitignore).** `paths.ignore` в `.review.yml` уже есть (`policy.py:16/36/79-81`), но применяется **только** как пост-фильтр находок на гейте (`policy.py:108`, `fnmatch(finding.file, pat)`). Содержимое игнор-папок всё равно чанкуется, **эмбеддится (Voyage тратится)**, идёт в граф/ретрив/сводки. Нужно сдвинуть игнор на **вход** (индексация) — не индексировать игнор-папки вовсе.

## Цель и критерии приёмки

- **Приор:** в solve-task появляется секция brief `## Подсистемы` — top-k релевантных подсистем для задачи (через `get_subsystem_summaries(query=…)`); fail-open.
- **Per-prefix depth:** `.review.yml` принимает `summary_cluster_depth_overrides` — карту `префикс пути → depth`; `cluster_key` резолвится longest-prefix-match; глобальный `summary_cluster_depth` остаётся дефолтом.
- **Ignore-на-входе:** пути под `paths.ignore` не чанкуются/не эмбеддятся (overlay + base) и не попадают в граф; уже проиндексированные под новый ignore вычищаются на `reviewer index`. Voyage не тратится на игнор-папки.
- **Deliverable:** корневой `./.review.yml` обновлён с наглядными примерами всех опций (русские комментарии); раздел политики в README дополнен.

## Принятые решения (brainstorming)

1. **Объём — один спек, три части, один SDD-цикл.** Связаны конфигом `.review.yml` и общим deliverable; не platform-scale. План разобьёт на задачи с per-task review.
2. **Ignore-синтаксис — `fnmatch`** (без новой зависимости; `pathspec` отклонён). Нормализация: голый паттерн без glob-метасимволов (`dir`, `a/b`) матчит и сам путь, и поддерево (`dir/*`), чтобы «папка и всё внутри» работало без явного `/*`.
3. **Приор — отдельная секция brief `## Подсистемы`**, перед «Relevant code» (сначала архитектурная карта → потом конкретные символы).
4. **Единый ключ `paths.ignore`** для гейта и индексации; **единый матчер** `is_ignored` — гейт переводится на него.

## Архитектура

### Часть 1 — ignore-на-входе

**Единый матчер.** Новая чистая функция (модуль `reviewer/policy/` или `reviewer/index/`):

```python
def is_ignored(path: str, patterns: list[str]) -> bool:
    """Путь под одним из ignore-паттернов (fnmatch). Нормализация: паттерн без
    glob-метасимволов (* ? [) — голое 'dir' или 'a/b' — матчит и сам путь, и его
    поддерево (эквив. fnmatch против pat и pat + '/*'). Паттерны с glob ('vendor/*',
    '*.gen.py') матчатся fnmatch как есть."""
```

`ReviewPolicy.gate` (`policy.py:108`) переводится с инлайн-`fnmatch` на `is_ignored` — поведение не ухудшается, матчер единый.

**Точка фильтра — `reviewer/index/freshness.py`.** В `build_overlay` (overlay PR, `:44-45`) и `update_base` (base-индекс, `:72`) рядом с фильтром `.py` пропускать пути под ignore — файлы не чанкуются и **не эмбеддятся**. Сигнатуры расширяются параметром `ignore: list[str] = ()`.

**Проброс паттернов:**
- `build_overlay` ← `ReviewService.prepare`: policy уже резолвится из base-ветки → передать `policy.ignore`.
- `update_base` ← `reviewer index` (CLI): читать `.review.yml` индексируемой ветки с диска клона → `policy.ignore`.

**Граф.** `reviewer index` строит граф (builder/SCIP) и перестраивает его целиком (clear+upsert). Применить тот же `is_ignored`-фильтр к множеству файлов/узлов графа на входе — игнор-узлы не попадут; полная перестройка означает, что чистки графа не нужно. Инвариант `node_id = "path#fqn"` (чанк↔граф) сохраняется: ignore одинаков для чанков и графа.

**Чистка уже проиндексированного (асимметрия чанки vs граф).** Чанки индексируются инкрементально (дедуп по `content_hash`), поэтому при *добавлении* пути в ignore старые чанки надо удалить явно. На `reviewer index`: собрать проиндексированные пути ветки, вычислить попавшие под ignore, `store.delete_paths(repo, ref, ignored_paths)`. Осиротевшие сводки подсистем подчистит существующий `prune` (PRI-166). Граф — без явной чистки (перестройка).

**Экономия Voyage.** Главный мотив: на free tier (3 RPM / 10K TPM) исключение `vendor/`, `migrations/`, сгенерированного кода снимает основную часть эмбеддингов.

### Часть 2 — per-prefix depth

**Формат `.review.yml`:**

```yaml
summary_cluster_depth: 2               # дефолт
summary_cluster_depth_overrides:
  reviewer/index: 3                    # дробить глубже
  vendor: 1                            # держать крупно
```

**Резолв depth per-path.** Новая чистая функция в `reviewer/graph/summaries.py`:

```python
def depth_for(path: str, default: int, overrides: dict[str, int]) -> int:
    """depth самого длинного ключа-префикса (по сегментам директории),
    под который попадает path; иначе default."""
```

`build_clusters` (`summaries.py:55`) принимает `depth_overrides: dict[str,int] | None = None` и группирует по `cluster_key(m.path, depth_for(m.path, depth, overrides))` для каждого члена (вместо одного глобального `depth`).

**Policy.** Поле `summary_cluster_depth_overrides: dict[str,int] = field(default_factory=dict)` + чтение в `from_yaml`/`load` (зеркало `summary_cluster_depth`). Из env не задаётся (только `.review.yml`) — env остаётся глобальным дефолтом depth.

**Согласованность инварианта PRI-167.** `cluster_key` и `source_hash` зависят от depth, поэтому `list_subsystem_clusters` и re-derive `member_node_ids` в `index_subsystem_summary` (`service.py:543-550`) обязаны использовать **одну и ту же** per-path depth-логику. Резолв overrides — рядом с `_resolve_summary_depth` (новый/расширенный хелпер, возвращающий и default-depth, и карту overrides).

**Смена overrides** меняет `cluster_key` затронутых подсистем → пересбор затронутых + `prune` осиротевших (механика `prune_subsystem_summaries`, uncapped прогон, PRI-166).

### Часть 3 — приор сводок в solve-task

- **`plugin/skills/solve-task/SKILL.md`, шаг 3 (начало):** вызвать `get_subsystem_summaries(repo, branch, query="<title>. <первые строки описания>", top_k=8)`. Порог масштаба встроен (PRI-167: ниже порога → все сводки).
- **Новая секция brief `## Подсистемы`** — top-k подсистем (`cluster_key` + однострочная суть), перед «Relevant code».
- **branch** — тот же, что для `search_codebase` (резолв в шаге 0/3). **Fail-open:** пусто/`(… недоступно)`/ошибка → секция опускается с пометкой, как прочие источники.
- **Guard-тест** (аналог `tests/skills/test_ask_uses_summaries.py`): SKILL.md содержит вызов `get_subsystem_summaries(... query=...)`.

## Поток данных и инварианты

- Все настройки — из `.review.yml` целевой/индексируемой ветки (PR не ослабляет ревью).
- `node_id`-консистентность: ignore применяется к чанкам **и** графу одинаково.
- Voyage-экономия: ignore-файлы не эмбеддятся; смена depth/overrides → пересбор только затронутых сводок (дедуп эмбеддингов сводок по `source_hash`, PRI-167).

## Тестирование

- **ignore:** unit `is_ignored` (вкл. нормализацию `dir`→`dir/*`, отрицательные кейсы); `build_overlay`/`update_base` пропускают ignore-пути; чистка проиндексированного под ignore на `reviewer index` (`delete_paths`); существующие тесты гейта (`tests/policy/`) зелёные на новом матчере.
- **depth:** unit `depth_for` longest-prefix (корень, вложенные/конфликтные префиксы, нет совпадения → default); `build_clusters` с `depth_overrides` даёт корректные `cluster_key`; резолв из `.review.yml` (policy).
- **приор:** guard-тест solve-task (вызов `get_subsystem_summaries` с `query=`).

## Deliverable — пример `.review.yml` (+README)

Обновить корневой `./.review.yml`: добавить с русскими комментариями блок `paths.ignore` (пояснение «папка и всё внутри: `dir/*`»), `summary_cluster_depth` + `summary_cluster_depth_overrides`, `summary_topk_threshold`. Дополнить раздел политики в README (он уже документирует `.review.yml`).

## Предварительная декомпозиция (детализирует writing-plans)

1. `is_ignored` + перевод гейта находок на неё (unit).
2. ignore-фильтр в `freshness` (overlay + base) + проброс `policy.ignore` из `prepare`/`reviewer index`.
3. ignore для графа на `reviewer index` + чистка проиндексированного под ignore (`delete_paths`).
4. `summary_cluster_depth_overrides` (policy) + `depth_for` + `build_clusters(depth_overrides=…)` + резолв в service (list/index — единая depth-логика).
5. приор в solve-task SKILL.md + guard-тест.
6. обновить `./.review.yml` (+README) — финал.

## Вне области (YAGNI)

- `pathspec`/полная gitignore-семантика (`**`, отрицания `!`) — отклонено в пользу `fnmatch`.
- Per-prefix `summary_topk_threshold` — не требуется (порог глобальный достаточен).
- Env-форма `summary_cluster_depth_overrides` — overrides только per-repo через `.review.yml`.
- Автоматический реиндекс при смене ignore/overrides без `reviewer index` — пересбор по-прежнему запускается явным прогоном.

## Открытые вопросы / оговорки

- Точный текст/критерии PRI-161 с доски подтвердить не удалось (store-first промах: `get_task("PRI-161")`/`get_task("ID-161")` = null; корпус задач, похоже, без эмбеддингов — `sync_board` дал `embedded:0`, `search_tasks` деградировал). Суть взята из дизайна PRI-167 («Потребитель — PRI-161 (приор сводок в solve-task)»). Не блокер; точный текст можно дочитать через board-MCP при необходимости.
