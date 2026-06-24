# PRI-133 — GitLab-провайдер + деплой-уровневый резолв VCS (мульти-платформа)

**Статус:** дизайн утверждён · **Дата:** 2026-06-24 · **Оценка:** M
**Слой:** Движок (reviewer CLI/MCP), Claude Code не нужен.

## Проблема

`VCSProvider` — чистый Protocol (`reviewer/vcs/base.py`), единственная реализация —
`GitHubProvider` (`reviewer/vcs/github.py`, httpx). `_create_vcs_provider` жёстко создаёт
`GitHubProvider`, токен живёт только в ENV (`GITHUB_TOKEN`). Это не позволяет одному деплою
обслуживать репозитории на разных платформах (GitHub + GitLab).

## Ключевое архитектурное напряжение (и его снятие)

Движок reviewer-mcp работает **только через API** и не имеет локального клона: чтобы прочитать
`.review.yml`, он вызывает `vcs.get_file_at_ref(".review.yml", base_sha)` — провайдер уже должен
существовать. Но какой провайдер создавать (GitHub/GitLab) — свойство репозитория, нужное **до**
первого API-вызова. Значит тип провайдера принципиально не может приходить из `.review.yml`,
прочитанного по API.

**Решение (утверждено):** деплой-уровневый резолв, секретов в `.review.yml` нет.

1. **Auto-derive при индексации.** `reviewer index` работает на локальном клоне, где виден
   git-remote. Из `git remote get-url origin` определяем платформу и хост (новая функция
   `derive_vcs_from_remote(url) -> (provider, base_url) | None`, обобщает текущий github-only
   `_REMOTE_RE` на `github.com` и `gitlab.*`). Результат пишем в новую таблицу `repo_vcs`.
2. **Резолв при ревью.** `_create_vcs_provider(owner, repo)` сначала делает дешёвое чтение
   `repo_vcs` по `repo` (DB-рид до любого API), получает `provider` + `base_url`, берёт токен из
   ENV по платформе и строит нужный провайдер.
3. **Фолбэк.** Нет строки (репо не индексирован / remote не распознан) → ENV `VCS_PROVIDER`
   (дефолт `github`). Обратная совместимость 1-в-1.

Почему отдельная таблица `repo_vcs`, а не колонка в `index_meta`: `index_meta` ключуется
`(repo, ref)`, а `_create_vcs_provider` вызывается **до** того, как известна ветка/ref (она нужна,
чтобы получить PR). Платформа же — свойство репо, не ветки. Ключ по `repo`.

## Конфиг (только ENV деплоя)

| ENV | Дефолт | Назначение |
|---|---|---|
| `VCS_PROVIDER` | `github` | фолбэк-платформа, когда `repo_vcs` пуст / remote не распознан |
| `GITHUB_TOKEN` | `""` | токен платформы github (как сейчас) |
| `GITLAB_TOKEN` | `""` | токен платформы gitlab |
| `GITLAB_URL` | `https://gitlab.com` | дефолт base-url; фолбэк для self-hosted, когда не выведен из remote |

`VOYAGE_API_KEY` и прочие секреты остаются только в ENV. В `.review.yml` блок `vcs:` **не вводится**.

## Компоненты

### 1. `reviewer/vcs/_http.py` (новый) — общий retry-транспорт
Вынести `_RetryTransport` (экспоненциальный backoff на 429/502/503/504, Retry-After) из
`github.py` в общий модуль. `github.py` импортирует его оттуда. Используется обоими провайдерами.

### 2. `reviewer/vcs/gitlab.py` (новый) — `GitLabProvider`
Реализует `VCSProvider` Protocol (7 методов). Эталон — `GitHubProvider`. GitLab API v4:

- **Клиент:** `base_url = {gitlab_url}/api/v4`, заголовок `PRIVATE-TOKEN: <token>`, общий
  `_RetryTransport`. Путь проекта URL-энкодится: `owner/name` → `owner%2Fname` (как `:id`).
  «number» = MR `iid` (per-project счётчик — прямой аналог номера PR).
- **`get_pull_request(iid)`** → `GET /projects/:id/merge_requests/:iid`. Маппинг:
  `base_sha`/`head_sha`/`start_sha` ← `diff_refs`; `base_ref` ← `target_branch`;
  `head_ref` ← `source_branch`; `body` ← `description`; `draft` ← `draft`.
- **`get_changed_files(iid)`** → `GET /projects/:id/merge_requests/:iid/changes` →
  `ChangedFile(path=new_path, status←{new_file/deleted_file/renamed_file→added/removed/renamed,
  иначе modified}, patch=diff)`.
- **`get_file_at_ref(path, ref)`** → `GET /projects/:id/repository/files/{path%2Fenc}?ref=:ref` →
  base64-декод `content`; 404 → `None`.
- **`list_existing_fingerprints(iid)`** → `GET /projects/:id/merge_requests/:iid/notes`
  (пагинация) → скан тел на `<!-- ai-review:hash -->` (общий маркер с GitHub).
- **`publish_review(iid, head_sha, summary, comments)`** — у GitLab нет объекта «review»:
  - сводка → `POST .../notes` (один общий нот);
  - каждый inline → `POST .../discussions` с `position = {position_type:"text", base_sha,
    start_sha, head_sha, new_path, old_path, new_line|old_line}` (RIGHT→`new_line`,
    LEFT→`old_line`). Тройку SHA берём из `diff_refs` MR (кэш из `get_pull_request` либо один
    доп. GET). Мультистрочные (`start_line`) → `line_range` в position; при недоступности —
    деградация в однострочный комментарий.
- **`compare_files(base, head)`** → `GET /projects/:id/repository/compare?from=:base&to=:head` →
  `diffs` → `ChangedFile`.
- **`close()`** → закрыть httpx-клиент.

### 3. `reviewer/services/repo_id.py` — `derive_vcs_from_remote`
Новая функция рядом с `derive_repo_from_remote`. Парсит remote-URL → `(provider, base_url)`:
- хост `github.com` → `("github", "")` (base_url не нужен — github.com зашит в провайдере);
- хост содержит `gitlab` (`gitlab.com` или self-hosted `gitlab.mycompany.com`) →
  `("gitlab", "https://<host>")`;
- иначе → `None`.

### 4. `reviewer/index/store.py` + `schema.sql` — `repo_vcs`
```sql
CREATE TABLE IF NOT EXISTS repo_vcs (
    repo       text        PRIMARY KEY,
    provider   text        NOT NULL,
    base_url   text        NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);
```
Методы стора: `get_repo_vcs(repo) -> tuple[str, str] | None` (provider, base_url) и
`set_repo_vcs(repo, provider, base_url)` (upsert по PK, forward-only, fail-soft на отсутствие
таблицы — паттерн `get_index_meta`).

### 5. `reviewer/config/settings.py`
Добавить: `vcs_provider: str = "github"`, `gitlab_token: str = ""`,
`gitlab_url: str = "https://gitlab.com"`. `github_token` остаётся.

### 6. `reviewer/services/review_service.py` — `_create_vcs_provider`
```
def _create_vcs_provider(self, owner, repo) -> VCSProvider:   # тип возврата: Protocol
    full = normalize_repo(f"{owner}/{repo}")
    row = self.components.store.get_repo_vcs(full)             # DB-рид до API
    provider, base_url = row or (self.settings.vcs_provider, "")
    if provider == "gitlab":
        return GitLabProvider(owner, repo,
                              token=self.settings.gitlab_token,
                              base_url=base_url or self.settings.gitlab_url, ...retry...)
    return GitHubProvider(owner, repo, token=self.settings.github_token, ...retry...)
```
5 call-site'ов (`prepare` + 4× в `mcp/service.py`) не меняются — сигнатура `(owner, name)` та же.

### 7. `reviewer/entrypoints/cli.py` — команда `index`
После `c.store.set_index_meta(repo_id, bref, sha)` (:167):
```
vcs = derive_vcs_from_remote(remote_url(path) or "")
if vcs:
    c.store.set_repo_vcs(repo_id, vcs[0], vcs[1])
```
Fail-soft: не падать, если remote не распознан.

### 8. `.env.example`
Добавить `VCS_PROVIDER`, `GITLAB_TOKEN`, `GITLAB_URL` рядом с `GITHUB_TOKEN`.

## Сознательные отклонения от исходной задачи PRI-133

- **Блок `vcs:` в `.review.yml` НЕ вводится** (решение: секретов в yml нет). Поэтому **часть Г
  задачи отпадает**: `ReviewPolicy.from_yaml` не парсит `vcs:`; скиллу `configure-review`
  (PRI-168) клоберить нечего — он и так сохраняет все ключи вне контекст-слоя, изменений в нём
  не требуется. Утверждение задачи «git log чёрн-анализ провайдер-агностичен» уже выполнено
  (локальный git), отдельной работы нет.
- **Два критерия приёмки меняются осознанно:**
  - ~~«В `.review.yml` можно указать `vcs: {provider, token}` — ревью работает без ENV-токена»~~
    → креды деплой-уровневые (ENV), не в yml.
  - ~~«Если `vcs.token` в yml не задан — подтягивается из ENV»~~ → токен всегда из ENV по платформе.
- **Сохранены** критерии: MR на GitLab ревьюится inline без дублей; self-hosted через
  `GITLAB_URL`/derived base-url; обратная совместимость (github по умолчанию, существующий
  `GITHUB_TOKEN` без изменений).

## Тестирование

- **Unit (mock httpx)** `tests/vcs/test_gitlab.py` — зеркалит `test_github.py`: все 7 методов;
  упор на `publish_review` (payload `discussions` + `position`) и `get_file_at_ref` 404.
- **Unit** `tests/services/test_repo_id.py` (или существующий) — `derive_vcs_from_remote`
  (github / gitlab.com / self-hosted / нераспознанный / пустой).
- **Unit** — `_create_vcs_provider` резолв (repo_vcs hit→GitLab; miss→ENV-дефолт github);
  `get/set_repo_vcs` (upsert, отсутствие таблицы → None).
- **Реальный GitLab E2E** требует живого MR + токена → integration/manual (gated маркером
  `integration`). Планка этой итерации — мокнутые unit-тесты; критерий «MR реально ревьюится»
  проверяется отдельным прогоном на GitLab-репо пользователя.
- Линт `ruff check .` (line-length 100), `pytest -q` зелёные.

## Известные ограничения

- **Вложенные группы GitLab** (`group/subgroup/repo`, >2 сегментов): `normalize_repo` /
  `derive_repo_from_remote` рассчитаны на `owner/name`. URL-энкод полного пути проекта в
  `GitLabProvider` обрабатывается, но `repo`-дискриминатор остаётся двусегментным. Полная
  поддержка вложенных групп — отдельная задача при необходимости.
- Реальная проверка публикации на GitLab вне CI (нужен живой инстанс/токен).
