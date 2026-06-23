# PRI-166 — Сводки подсистем (B): depth кластеризации в `.env` + preflight-предупреждение

**Дата:** 2026-06-23
**Статус:** дизайн утверждён, готов к плану
**Задача:** PRI-166 (yougile, alias ID-166). Вторая из трёх в дизайне «GraphRAG-сводки под прод-масштаб» (C ✅ PRI-165 → **B (эта)** → A PRI-167). Связь: PRI-159 (родитель).
**Оценка:** M.

## Проблема

`summary_cluster_depth` — скрытая глобальная env-константа деплоя (дефолт 2, `reviewer/config/settings.py:71`). Её не выбирает LLM и не видит пользователь. Плоский `src/`-репо при depth=2 даёт один гигантский кластер; глубокий монорепо — мелкие обрезки. Пользователь не знает, какой depth применится к `summarize-subsystems` и сколько подсистем получится. Под гетерогенные продакшен-репо нужна управляемость и предсказуемость.

`cluster_key(path, depth)` (`reviewer/graph/summaries.py:33`) задаёт идентичность кластера через обрезку пути до `depth` сегментов. Поэтому смена depth меняет множество и состав кластеров: старые `cluster_key` больше не листаются, их сводки осиротеют, но `get_subsystem_summaries` всё равно отдаёт их потребителям (`ask` / PR-walkthrough) — смесь старого и нового depth, которая сама не залечивается.

## Цель и критерии приёмки

1. `summary_cluster_depth` берётся из env и **задокументирован** в `.env.example` (+ README/CLAUDE.md).
2. Скилл `summarize-subsystems` перед прогоном показывает **применяемый depth + число/уровень кластеров** и **ждёт подтверждения**.
3. Смена env-depth меняет кластеризацию и сопровождается **явным предупреждением о полном пересборе** сводок.
4. (Сверх критериев, по решению дизайна) per-repo override depth через `.review.yml` целевой ветки; осиротевшие при смене depth сводки **вычищаются** (purge) на полном прогоне — потребители видят только сводки актуального depth.

## Решения дизайна

- **Scope:** базовый (env-doc + preflight + warn) **+ per-repo `.review.yml` override**. Адаптивный depth — НЕ берём (YAGNI).
- **Механизм резолва depth — server-side.** Единый хелпер в MCP-сервисе резолвит depth (env → override из `.review.yml`); оба тула (`list_subsystem_clusters`, `index_subsystem_summary`) используют его. LLM не передаёт depth и не читает yml. Прецеденты: глобальный `task_board`-дефолт + override из `.review.yml` (`ReviewPolicy`), session-less VCS-фетч в `get_pr_diff`.
- **Осиротевшие сводки — purge.** Новый метод стора `delete_summaries_except` + тул `prune_subsystem_summaries`; вызывается скиллом только на полном (uncapped) прогоне. Потребители видят только сводки текущего depth.

## Инвариант (сохранить)

`list_subsystem_clusters` и `index_subsystem_summary` **обязаны** использовать один и тот же depth — иначе `cluster_key`/`source_hash` не совпадут и `member_node_ids` не сохранятся (см. `reviewer/mcp/service.py:477-484`). Централизованный резолв через единый хелпер гарантирует это.

## Компоненты

### 1. Settings + документация
**Файлы:** `reviewer/config/settings.py`, `.env.example`, `README.md`, `CLAUDE.md`

- Поле `summary_cluster_depth: int = 2` уже существует. Обогатить inline-комментарий: упомянуть `env SUMMARY_CLUSTER_DEPTH` и per-repo `.review.yml` override (зеркало doc-стиля соседнего `summary_rebuild_cap`, `settings.py:72-73`).
- `.env.example`: добавить запись в graph/summary-блок (рядом с `GRAPH_BACKEND`):
  ```
  SUMMARY_CLUSTER_DEPTH=2            # глубина пути для кластера подсистемы (summarize-subsystems); смена = полный пересбор сводок; per-repo override в .review.yml
  ```
- README.md / CLAUDE.md: короткая строка в разделе про GraphRAG-сводки подсистем — что depth настраивается env (+ override `.review.yml`) и что смена влечёт полный пересбор.

### 2. ReviewPolicy override
**Файл:** `reviewer/policy/policy.py`

- Новое поле `summary_cluster_depth: int = 2`.
- `from_settings`: `summary_cluster_depth=settings.summary_cluster_depth`.
- `load`: `if "summary_cluster_depth" in data: policy.summary_cluster_depth = int(data["summary_cluster_depth"])`.
- `from_yaml`: добавить ключ для консистентности (хотя используемый путь — `load`).

### 3. MCP-сервис
**Файл:** `reviewer/mcp/service.py`

- **`_resolve_summary_depth(repo, branch) -> tuple[int, str]`** — новый приватный хелпер. Внутри try/finally создаёт VCS через `self._review_service._create_vcs_provider(owner, name)` (или `_vcs_factory` в тестах), читает `vcs.get_file_at_ref(".review.yml", branch)`, прогоняет `ReviewPolicy.load(self.settings, yaml_text)`, возвращает `(policy.summary_cluster_depth, source)`, где `source ∈ {"env", ".review.yml"}`. Детект `source`: `.review.yml`, если распарсенный yaml содержит ключ `summary_cluster_depth`; иначе `env`. Любой сбой (нет токена/ветки/сети, кривой yml, файла нет) → `(self.settings.summary_cluster_depth, "env")`, fail-soft, провайдер закрывается в `finally` (как в `get_pr_diff`).
- **`list_subsystem_clusters`**: если caller не передал явный `depth` — взять `depth, depth_source = self._resolve_summary_depth(repo, resolved)` вместо `self.settings.summary_cluster_depth` (строка 444). В ответ добавить поля: `depth`, `depth_source`, `orphans` (= `len(set(stored) - {c.key for c in clusters})`, где `stored` уже берётся на строке 446).
- **`index_subsystem_summary`**: заменить `depth = self.settings.summary_cluster_depth` (строка 480) на `depth, _ = self._resolve_summary_depth(repo, resolved)`.
- **`prune_subsystem_summaries(repo, branch) -> dict`** — новый публичный тул. Резолвит repo/branch (`_resolve_repo_branch`), резолвит depth, пере-выводит текущие cluster_keys из `store.list_base_members` через `cluster_key_of(path, depth)`. Guard: если base пуст → `{"pruned": 0, "kept": 0, "note": "(base-индекс пуст — purge пропущен)"}` (не вайпать на транзиентной пустоте). Иначе `pruned = summary_store.delete_summaries_except(repo, resolved, keep_keys)`; вернуть `{"pruned": pruned, "kept": len(keep_keys)}`. Fail-soft.
- Зарегистрировать тул в `reviewer/entrypoints/mcp_server.py` (как остальные `*_subsystem_*`).

### 4. SummaryStore
**Файл:** `reviewer/index/summary_store.py`

- **`delete_summaries_except(repo, branch, keep_keys: list[str]) -> int`**: `DELETE FROM <summaries> WHERE repo=%s AND branch=%s AND cluster_key <> ALL(%s)` (psycopg list-параметр), вернуть `rowcount`. Пустой `keep_keys` удалит все сводки repo/branch — поэтому вызывающий (`prune_subsystem_summaries`) гейтит на непустой base.

### 5. SKILL.md preflight
**Файл:** `plugin/skills/summarize-subsystems/SKILL.md`

- **Новый шаг (после «List clusters», до «Choose model»):** Preflight echo + confirmation.
  - Показать (по-русски): применяемый `depth` + `depth_source` (env / `.review.yml`), всего кластеров, пример уровней (несколько `cluster_key`), сколько stale vs fresh, `deferred` (cap), `orphans`.
  - Если `orphans > 0`: предупредить — «depth изменился или модули удалены → N сводок осиротело; полный (uncapped) прогон пересоберёт всё и удалит осиротевшие».
  - Явно зафиксировать: `cluster_key` зависит от depth → смена depth = полный пересбор всех сводок.
  - **Спросить подтверждение** перед суммаризацией. Отказ → stop (ничего не суммаризировать, не purge).
- **Новый шаг (после суммаризации stale):** если прогон полный (`deferred == 0`, без явного depth/cap override) → вызвать `prune_subsystem_summaries(repo, branch)`, отчитать `pruned`. При частичном (deferred>0) — purge **пропустить**, сказать об этом (зеркало `sync_board --limit`, чтобы не удалить лишь отложенные кластеры).
- Обновить «Report»-шаг: добавить depth/depth_source и pruned к отчёту.

### 6. Тесты
- `tests/policy/test_policy.py` — `summary_cluster_depth`: env-дефолт через `from_settings`; override из `.review.yml` через `load`; отсутствие ключа сохраняет env-значение.
- summary_store-тест (`tests/index/` рядом с существующими store-тестами) — `delete_summaries_except` удаляет осиротевшие ключи и сохраняет переданные `keep_keys`.
- MCP-тесты (`tests/mcp/`) — на фейках (fake `vcs_factory` + fake-сторы, как в существующих unit-тестах сервиса):
  - `_resolve_summary_depth`: override из `.review.yml`; fail-soft → env при сбое VCS/отсутствии ключа.
  - `list_subsystem_clusters`: ответ содержит `depth`/`depth_source`/`orphans`; `orphans` считается верно.
  - `index_subsystem_summary`: использует резолвнутый depth (консистентность с list при override).
  - `prune_subsystem_summaries`: пере-выводит keep_keys; пустой base → no-op; удаляет осиротевшие.
- `tests/skills/test_summarize_subsystems.py` (+ при необходимости `tests/skills/test_assembled_prompts.py`) — guard: SKILL.md содержит preflight-эхо depth, подтверждение, warn об осиротевших/пересборе, шаг prune на полном прогоне.

## Поток данных (скилл `summarize-subsystems`)

1. Resolve repo/branch.
2. `list_subsystem_clusters(repo, branch)` → `{depth, depth_source, orphans, deferred, clusters[]}`.
3. **Preflight: эхо depth/уровней/stale/deferred/orphans + warn(если orphans>0) + подтверждение.** Отказ → stop.
4. Если есть stale — выбор модели сводок.
5. Суммаризировать stale-кластеры → `index_subsystem_summary(...)` (сервер резолвит тот же depth).
6. **Если полный прогон (`deferred==0`): `prune_subsystem_summaries(repo, branch)`** — удалить осиротевшие.
7. Отчёт: depth/depth_source, summarized / fresh / deferred / pruned.

## Fail-soft / краевые случаи

- VCS-сбой / нет токена / нет ветки / кривой `.review.yml` → `_resolve_summary_depth` → env-дефолт, `depth_source="env"`, не падаем.
- Пустой base → `prune_subsystem_summaries` no-op + note (не вайпать на транзиентной пустоте).
- Частичный прогон (cap/`deferred>0`/явный depth) → без purge (иначе удалили бы лишь отложенные кластеры).
- Оба тула резолвят depth единым хелпером → инвариант консистентности cluster_key/source_hash сохранён.

## Вне scope

- Адаптивный depth по целевому размеру кластера.
- Векторизация/отбор сводок (PRI-167, A — идёт после).
- Изменение алгоритма кластеризации (`build_clusters`/`cluster_key`).

## Соглашения

Язык кода/комментариев/CLI — русский. Коммиты — Conventional Commits на русском, без self-attribution.
