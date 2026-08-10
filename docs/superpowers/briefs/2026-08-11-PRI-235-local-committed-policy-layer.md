# Brief — PRI-235 Читать коммиченный .review.yml из локального клона в config show и MCP
https://ru.yougile.com/team/686c049c8af8/#PRI-235

## Task
Коммиченный слой политики читается по-разному: `reviewer index` — локально (`file_at_ref` →
`git show <ref>:.review.yml`), а `config show` и MCP `_resolve_policy` — через `vcs.get_file_at_ref`,
хотя клон часто доступен локально. Для репо без рабочего remote коммиченный слой недостижим.
Нужно: (1) `config show` умеет указать путь к клону и предпочитает локальный фетчер, VCS — фолбэк;
(2) определить источник пути к клону для MCP и переиспользовать локальный фетчер; при неизвестном
пути поведение не меняется; (3) отчёт `config show` показывает способ чтения; (4) доки
(CLAUDE.md, README.md, README.ru.md); (5) тесты.

## Related work
- PRI-234 (`docs/superpowers/plans/2026-08-10-pri-234-policy-layers-fail-soft.md`) — fail-soft
  коммиченного слоя, `SkippedLayer`/`ResolutionMeta.skipped`, `strict_committed`. Эта задача
  убирает сам сетевой вызов там, где клон доступен. (dropped 0)

## Relevant code
- `reviewer/config/layers.py:298-336` — `resolve_policy_data(repo, ref, fetch_repo_yaml, ...)`:
  фетчер коммиченного слоя инъектируется вызывающим → точка расширения без правки резолвера.
- `reviewer/config/layers.py:555-578` — `build_config_report`: сюда добавляется поле источника
  коммиченного слоя.
- `reviewer/entrypoints/cli.py:196-245` — `config_show` + `fetch_committed` (замыкание на `vcs`);
  здесь появляется выбор локального фетчера и `--path`.
- `reviewer/entrypoints/cli.py:833-840` — `reviewer index`: эталон локального чтения
  (`lambda ref: file_at_ref(repo, ".review.yml", ref)`, `strict_committed=True`).
- `reviewer/entrypoints/cli.py:851-855` — `set_repo_vcs(repo_id, ...)` при индексации: прецедент
  «`reviewer index` персистит per-repo факт о клоне для более позднего API-only потребителя».
- `reviewer/index/store.py:178-205` — `get_repo_vcs`/`set_repo_vcs`; шаблон для repo→clone.
- `reviewer/index/schema.sql:128` — `repo_vcs`; сюда же аддитивная таблица клонов.
- `reviewer/mcp/service.py:1365-1400` — `_resolve_policy`: создаёт VCS ради одного файла,
  `strict_committed=False` (намеренно мягкий).
- `reviewer/gitutil.py:39-52` — `file_at_ref`, `repo_root`, `rev_parse` — локальное чтение и
  валидация клона.
(dropped 0)

## Constraints / open questions
- MCP-сервер может работать не на той машине, где клон → путь из БД обязан валидироваться
  (существует, git-репо, `repo_root(path) == path`, ref резолвится) и фолбэчиться на VCS.
- `file_at_ref` возвращает `None` и когда файла нет на ref, и (через CalledProcessError) когда
  ref/репо битые — различать: валидировать репо и ref ДО чтения, иначе «нет файла» ≠ «нет клона».
- Секреты: путь к клону — не секрет, но в диагностику попадает только метка источника
  (`local`/`vcs`), не путь.

Собран на: премиум-тир (сессионная модель), режим: inline

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 46 · out 9.2K · cache-write 173.6K · cache-read 1.6M
Всего: 1.8M токенов
