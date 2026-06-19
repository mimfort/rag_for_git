# PRI-139 — Авто-привязка PR к задаче при sync-tasks (IMPLEMENTED_BY из URL в description)

**Статус:** утверждён · **Задача:** PRI-139 / ID-139 (родитель PRI-114) · **Дата:** 2026-06-19

## Проблема

`sync-tasks` индексирует текст задач, но PR-ссылки (`github.com/owner/repo/pull/N`) в
`description` остаются просто текстом — рёбер `IMPLEMENTED_BY` между `:Task` и `:PR` нет.
Поэтому `get_task_context` не видит PR задачи, а `solve-task` не может показать «связанная
задача → её PR → изменённый код». Сейчас рёбра создаются **только** в
`publish_review(task_key=…)` (ревью живого PR); исторические смерженные PR не покрыты.

## Критерии приёмки (из задачи)

- После `sync-tasks` задачи с PR-ссылками в `description` получают рёбра `IMPLEMENTED_BY`.
- `get_task_context("ID-N")` показывает PR связанных задач.
- `solve-task` видит PR и затронутый код у related/similar задач.
- Повторный `sync-tasks` без изменений `description` — 0 лишних Voyage-вызовов, скорость не страдает.
- Существующие тесты не ломаются.

## Решения (зафиксированы при брейнсторме)

1. **Где парсить:** серверно, в `TaskService.index_task`/`index_batch` (не отдельный MCP-тул).
   Скилл `sync-tasks` не меняется; парсинг и идемпотентность — в одном месте.
2. **Затронутый код — лениво.** На синке создаём только `IMPLEMENTED_BY` (без `TOUCHES`).
   Дифф PR подтягивается **по запросу LLM** в `solve-task` через новый тул `get_pr_diff`,
   только если связанная/похожая задача прошла фильтр релевантности.
3. **Гейтинг рёбер:** строго `embedded=True` (буквально по тексту задачи). Бэкфилл уже
   проиндексированного корпуса — одноразовая операционная заметка (см. ниже), не код.

## Архитектура

Три части + общий sha-фикс:

| # | Что | Где | Действие |
|---|---|---|---|
| A | Линковка на синке | `reviewer/tasks/` | парсинг PR-URL + `link_review` для `embedded=True` задач |
| Б | Ленивый дифф | `reviewer/mcp/`, `entrypoints/mcp_server.py` | новый тул `get_pr_diff(repo, number)` |
| В | Guidance | `plugin/skills/solve-task/SKILL.md` | «PR релевантен → зови `get_pr_diff`» |
| — | sha-фикс | `reviewer/tasks/graph.py` | `link_pr` не затирает непустой sha |

### Часть A — линковка на синке

**Новый модуль `reviewer/tasks/pr_links.py`** — чистая функция:

```python
def extract_pr_refs(text: str) -> list[PRRef]:
    """PRRef из всех GitHub PR-URL в тексте; дедуп по (repo, number); sha=''."""
```

- Regex: `https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)` (без учёта регистра хоста).
- Строит `PRRef(repo=f"{owner}/{name}", number=int(n), url=<совпавший URL>, sha="")`.
- Дедуп по `(repo, number)`, порядок первого появления.
- Изолированно тестируется (не зависит от Neo4j).

**`TaskService.index_task`:** после успешного эмбеддинга (`embedded=True`) — для каждого
`PRRef` из `extract_pr_refs(description)` вызвать существующий fail-soft
`self.link_review(key, pr_ref, [])` (`touched=[]`). Гейт строго `embedded=True`.

**`TaskService.index_batch`:** то же в шаге 4 (обработка `to_embed`-задач), после успешного
`upsert_task`. Для `meta_only` (неизменившихся) — не линкуем.

**Результат:** в dict каждой задачи добавляется поле `prs_linked: int` (число успешно
обработанных PR). Рядом с `embedded`/`links_upserted`/`warnings`. `sync-tasks` может это
показать в отчёте (добавление поля аддитивно, существующие потребители не ломаются).

**Fail-soft:** `link_review` — no-op без графа/ключа и логирует сбой без проброса (Neo4j down
не валит синк). Парсинг — чистая функция без I/O.

### Часть Б — `get_pr_diff` (session-less тул)

**`MCPReviewService.get_pr_diff(repo: str, number: int) -> str`:**

- `normalize_repo(repo)` → `owner/name`. Пустой/некорректный repo → нота (как в
  `_resolve_repo_branch`). Здесь repo обязателен (PR может быть в другом репо — граф задач глобален).
- VCS строится тем же паттерном, что в `prepare_review`:
  `vcs = self._vcs_factory(owner, name) if self._vcs_factory else <GitHubProvider из settings.github_token>`.
  Внутренне созданный провайдер закрывается в `finally` (fail-soft), factory-созданный — нет
  (его жизненным циклом владеет фабрика; ровно как в `prepare_review`).
- `vcs.get_changed_files(number)` (уже существует, возвращает `list[ChangedFile]` с патчами —
  **новый метод провайдера не нужен**).
- Форматирование: на файл `--- {path} [{status}]` + `patch`; `patch is None` (слишком большой
  файл) → пометка `(patch недоступен: файл слишком большой)`. Общий вывод — с символьным капом
  (константа, в стиле существующих усечений; при превышении — суффикс `… (truncated)`).
- Любая ошибка (нет токена, сеть, 404) → fail-soft нота `(diff PR недоступен)`.

**MCP-тул** в `entrypoints/mcp_server.py`:
```python
def get_pr_diff(repo: str, number: int) -> str: ...
```
с англоязычным докстрингом (как остальные тулы) — для ленивой подтяжки диффа исторического PR.

### Часть В — скилл `solve-task`

В шаге «Gather context» (блок графовых тулов) добавить guidance: `get_task_context` отдаёт PR
(id вида `owner/name#N`) связанных задач; **если** связанная/похожая задача прошла фильтр
релевантности и её PR полезен для реализации — распарсить `repo`/`number` из id и вызвать
`get_pr_diff(repo, number)`, чтобы увидеть, что менял PR. Fail-open: нота `(diff … недоступен)`
не фатальна — продолжаем.

### sha-фикс в `link_pr`

`link_pr` сейчас делает безусловный `SET p.sha=$sha`. Линковка исторического PR (`sha=""`)
затёрла бы реальный sha, проставленный `publish_review`. Меняем на условное:

```
SET p.repo=$repo, p.number=$number, p.url=$url,
    p.sha = CASE WHEN $sha <> '' THEN $sha ELSE coalesce(p.sha, '') END
```

- Создание узла с `sha=""` → `coalesce(null,'')=''`.
- Существующий реальный sha + новый `""` → сохраняется реальный.
- Новый реальный sha (ревью после синка) → перезаписывает.
- `repo/number/url` остаются безусловным SET (стабильны для данного PR id).
- `test_link_pr_params` не ломается (проверяет переданные params, не текст Cypher).

## Идемпотентность и перформанс

- `link_pr` — `MERGE` по `(:Task)`, `(:PR {id})`, ребру `IMPLEMENTED_BY` → повторный вызов не
  плодит дубликаты. Один PR ↔ много задач: общий `:PR {id="repo#number"}`.
- Voyage-вызовы не добавляются: линковка — только Neo4j-`MERGE`, и лишь для уже-эмбеднутых
  (`embedded=True`) задач. Повторный синк без изменений → `embedded=False` → линковка не
  выполняется → 0 лишних вызовов (критерий выполнен).

## Тестирование

- `tests/tasks/test_pr_links.py` (new): извлечение URL — одна/несколько ссылок, дедуп,
  игнор не-PR github-ссылок (`/issues/`, `/blob/`), посторонний текст, пустой ввод.
- `tests/tasks/test_graph.py`: sha не затирается (`abc` → `link_pr(sha="")` → остаётся `abc`;
  create с `sha=""` → `""`); существующий `test_link_pr_params` зелёный.
- `tests/tasks/test_service.py` + `tests/tasks/test_service_batch.py`: линковка при
  `embedded=True`; пропуск при `embedded=False`; fail-soft без графа; корректный `prs_linked`.
- `tests/mcp/test_server_tools.py` + сервис-тест: `get_pr_diff` форвардит/форматирует/fail-soft
  через fake `vcs_factory`; регистрация тула в сервере.

## Открытый момент — бэкфилл существующего корпуса

При гейте `embedded=True` рёбра для **уже** проиндексированных задач появятся только после
изменения их `description` (меняется `content_hash` → `embedded=True`) или полного ре-индекса.
Для текущего репо (корпус тёплый) бэкфилл выполняется одноразово: тронуть `description` задач
с PR-ссылками на доске → следующий `sync-tasks` создаст рёбра. Это операционный шаг, не код
(зафиксировано как явное требование пользователя после реализации).

## Вне объёма (YAGNI)

- Жадный сбор `touched_node_ids` на синке (GitHub compare-API на каждый PR) — заменён ленивым `get_pr_diff`.
- Отдельный MCP-тул `link_task_to_pr` — отвергнут в пользу серверного парсинга.
- Парсинг не-GitHub VCS-URL — целевой VCS проекта GitHub.
