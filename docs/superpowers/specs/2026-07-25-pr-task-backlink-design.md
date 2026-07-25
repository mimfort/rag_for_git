# Кликабельная ссылка на задачу в теле PR (обратный линк `finish_task`)

Дата: 2026-07-25
Статус: спека согласована, план не написан

## Проблема

`finish_task` уже связывает PR и задачу **в одну сторону**: ссылка на PR дописывается в описание
задачи на доске (`TaskBoardProvider.finish`). Обратной связи нет — в теле PR ключ задачи остаётся
голым текстом (`PRI-216`), некликабельным. Чтобы попасть из PR в задачу, ревьюер копирует ключ
и ищет его на доске руками.

URL задачи в системе уже есть: `.review.yml` → `task_board.url_template` → провайдер подставляет
код задачи → `TaskBrief["url"]`. Он доходит до стора и графа, но в PR не попадает.

## Решение

`finish_task` после успешной записи в доску **fail-soft** дописывает в начало тела PR строку
с markdown-ссылкой на задачу. Server-side, детерминированно, обе платформы (GitHub, GitLab).

Почему server-side, а не инструкция в скилле: скилл — это промпт, а не `try/finally`. Гарантию
даёт только сервер (тот же довод, что и для GC overlay в `reviewer/services/gc.py`).

Функция включена всегда, отдельного переключателя в `.review.yml` нет.

## Поток данных

```
finish_task(key, pr_url, …)
  ├─ provider.finish(key, pr_url, …)        # как сейчас: PR-ссылка → в задачу, done-таргет
  ├─ brief = _write_through(provider, key)  # теперь возвращает TaskBrief-dict, а не bool
  │     └─ brief["url"]  ← url_template.replace("{code}", key), уже нормализован
  └─ _backlink_pr(pr_url, key, brief["url"])   # НОВОЕ, fail-soft
        ├─ parse_pr_url(pr_url) → (platform, owner, repo, number)
        ├─ vcs = review_service._create_vcs_provider(owner, repo, platform=platform)
        ├─ body = vcs.get_pull_request(number).body
        ├─ new = apply_backlink(body, key, task_url)   # None → писать не надо
        └─ vcs.update_pull_request_body(number, new)
```

Источник URL — нормализованный бриф из уже выполняемого write-through, а **не** возврат
`provider.finish()`. Поэтому контракт `TaskBoardProvider` не меняется: ни `finish()`, ни
контракт-фикстура `tests/tasks/boards/contract.py`, ни реализации провайдеров досок.
Работает для любого зарегистрированного типа доски автоматически.

Единственная правка внутреннего метода: `MCPReviewService._write_through` возвращает
`dict | None` (нормализованный бриф) вместо `bool`. В payload поле `reindexed` остаётся
булевым (`brief is not None`) — внешний контракт тула не ломается.

## Компоненты

### `reviewer/tasks/pr_backlink.py` (новый)

Чистые функции без I/O, юнит-тестируются без сети — по образцу соседнего
`reviewer/tasks/pr_links.py`.

```python
def parse_pr_url(url: str) -> tuple[str, str, str, int] | None:
    """(platform, owner, repo, number) или None, если ссылка не распознана."""

def apply_backlink(body: str, key: str, task_url: str) -> str | None:
    """Новое тело PR или None, если писать не надо (ссылка уже есть)."""
```

### `VCSProvider.update_pull_request_body(number, body) -> None`

Добавляется в `Protocol` (`reviewer/vcs/base.py`) и в обе реализации:

| Платформа | Запрос |
|---|---|
| GitHub | `PATCH /repos/{owner}/{repo}/pulls/{number}` c `{"body": …}` |
| GitLab | `PUT /projects/{owner%2Frepo}/merge_requests/{iid}` c `{"description": …}` |

Симметрично уже существующему `get_pull_request`, который на обеих платформах отдаёт `body`.

### `MCPReviewService.finish_task`

Оркестрация: парсинг URL → провайдер → чтение тела → вставка → запись. Весь блок обёрнут
в fail-soft (см. ниже). Ответ тула получает новое поле `task_link_added: bool`; причины
пропуска уходят в существующий `warnings`.

## Формат строки и идемпотентность

Тело PR после правки:

```markdown
Задача: [PRI-216](https://ru.yougile.com/team/686c049c8af8/#PRI-216)
<!-- reviewer:task-link -->

## Задача

сервисы test-профиля пингуются healthcheck'ом…
```

Правила `apply_backlink(body, key, task_url)`:

| Состояние тела PR | Результат |
|---|---|
| маркер `<!-- reviewer:task-link -->` уже есть | `None` — повторный `finish_task` не плодит строк |
| маркера нет, но `task_url` уже встречается в тексте | `None` — ручная ссылка уважается |
| тело пустое | строка + маркер |
| иначе | строка + маркер + `\n\n` + исходное тело |

Маркер выбран по образцу принятого в проекте `<!-- ai-review:hash -->`: HTML-комментарий не
рендерится ни на GitHub, ни на GitLab и даёт устойчивую идемпотентность.

Пустой `task_url` (в `.review.yml` нет `url_template`) → PR не трогаем вообще,
`task_link_added: false` + warning. Голый ключ не вставляем: смысл фичи — кликабельность,
а некликабельный номер в теле PR и так есть.

Отображаемый текст ссылки — `key`, переданный в `finish_task` (проектный код вида `PRI-216`,
не нормализованный сторовый `ID-216`).

## Резолвинг платформы и репо из `pr_url`

Платформу определяет **форма пути**, не хост — так self-hosted GitLab работает без
предварительной индексации репо:

| URL | platform | owner | repo | number |
|---|---|---|---|---|
| `https://github.com/mimfort/rag_for_git/pull/128` | github | `mimfort` | `rag_for_git` | 128 |
| `https://gitlab.example.ru/team/sub/svc/-/merge_requests/42` | gitlab | `team/sub` | `svc` | 42 |
| что-то ещё | — | — | — | `None` → warning |

`/-/merge_requests/N` — однозначная сигнатура GitLab, `/pull/N` — GitHub. Вложенные группы
GitLab ложатся в `owner` как есть: `GitLabProvider` собирает `_proj = quote(f"{owner}/{repo}")`,
поэтому путь любой глубины уже поддержан. Хвосты URL (`/files`, `?tab=…`, `#note_1`,
завершающий `/`) отбрасываются.

`_create_vcs_provider(owner, repo, platform=None)` получает необязательный явный `platform`.
Когда он передан, платформа из URL **побеждает** `repo_vcs` и ENV-фолбэк: URL — более прямое
свидетельство, чем таблица, где репо может отсутствовать (фолбэк по умолчанию — `github`,
и без этого GitLab-PR в непроиндексированном репо ушёл бы не туда). Для GitLab `base_url`
берётся из схемы+хоста самого `pr_url`, иначе из `repo_vcs` / `GITLAB_URL`. Токен, как и везде,
из env по платформе (`GITHUB_TOKEN` / `GITLAB_TOKEN`); секретов в `.review.yml` нет.

GitHub Enterprise — вне скоупа: `GitHubProvider` жёстко ходит в `api.github.com`. Это
ограничение проекта в целом, здесь оно не расширяется.

## Обработка ошибок

Доска уже закрыта к моменту правки PR, и это не откатывается. Поэтому весь блок бэклинка
fail-soft — `status` остаётся `"ok"`, в `warnings` уходит причина:

| Ситуация | Поведение |
|---|---|
| `pr_url` не распознан | `task_link_added: false` + warning |
| `url` задачи пуст | `task_link_added: false` + warning |
| write-through не удался (`brief is None`) | источника URL нет → `task_link_added: false` + warning; `reindexed: false`, как и сейчас |
| нет прав на правку PR (403/404) | `task_link_added: false` + warning |
| сеть/таймаут/любое исключение VCS | `task_link_added: false` + warning, `log.warning` с трейсом |

Ошибка правки PR никогда не превращает успешный `finish_task` в `status: "error"`.
VCS-провайдер закрывается в `finally` (как в остальных местах `MCPReviewService`).

## Тесты

| Файл | Что закрывает |
|---|---|
| `tests/tasks/test_pr_backlink.py` (новый) | `parse_pr_url`: GitHub, GitLab с вложенными группами, self-hosted хост, хвосты (`/files`, `?tab=`, `#note_1`, trailing `/`), мусор → `None`. `apply_backlink`: все четыре строки таблицы идемпотентности |
| `tests/vcs/test_github.py`, `tests/vcs/test_gitlab.py` (правка) | `update_pull_request_body` бьёт в верный метод/путь/поле: GitHub `PATCH …/pulls/N` c `body`, GitLab `PUT …/merge_requests/N` c `description` — на моке `httpx.Client`, как уже устроены эти файлы |
| `tests/mcp/test_finish_task.py` (правка) | оркестрация на фейках: PR правится ровно один раз; повторный вызов → `task_link_added: false` без записи; исключение VCS → `status: "ok"` + warning; пустой `url` задачи → skip + warning; `reindexed` в payload остаётся `bool` после смены типа возврата `_write_through` |
| `tests/skills/test_finish_task_skill.py` (правка) | guard: скилл упоминает бэклинк в offer и в отчёте |

Все тесты — unit, без сети и без Postgres/Neo4j.

## Документация

- `plugin/skills/finish-task/SKILL.md`: шаг 4 (offer) — предупредить, что в тело PR будет
  добавлена строка со ссылкой на задачу; шаг 6 (отчёт) — озвучить `task_link_added` и warnings.
  Правка контента под `plugin/` меняет codex payload-digest → обязателен прогон
  `update_codex_plugin_manifest.py`, иначе install-тесты краснеют.
- `README.md` и `README.ru.md` — синхронно оба, раздел про `finish_task`.
- `CLAUDE.md`, пункт «Закрытие задачи после PR (`finish_task`)» — связь стала двусторонней.
- `docs/board-providers.md` — ремарка у `url_template`: он же питает бэклинк в тело PR.

## Вне скоупа

- Переключатель включения/выключения в `.review.yml`.
- Правка заголовка PR.
- Вставка ссылки в момент создания PR (инструкция в скиллах) — сервер закрывает кейс сам.
- GitHub Enterprise (`api.github.com` захардкожен в провайдере).
- Автолинк-ссылки средствами настроек репозитория GitHub (autolink references) — это настройка
  репозитория, а не поведение reviewer.
