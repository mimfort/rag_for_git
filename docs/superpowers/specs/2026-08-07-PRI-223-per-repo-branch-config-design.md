# PRI-223 (часть A) — Per-repo branch config: слой, резолвер, диагностика

Задача: https://ru.yougile.com/team/686c049c8af8/#PRI-223
Бриф: `docs/superpowers/briefs/2026-08-07-PRI-223-redesign-reviewer-init.md`

## Скоуп

PRI-223 распадается на четыре самостоятельных куска. Эта спека покрывает **только A —
слой branch-конфига**; B (передел мастера `init`), C (provider-scoped credentials),
D (`configure-review` + документация) идут отдельными циклами спека→план→PR и опираются
на результат A.

Покрываемые пункты задачи: 3, 4, 6, 12 и та часть 8, что касается `REVIEW_BRANCHES`
(runtime-фолбэк), плюс branch-часть 13.
Покрываемые критерии приёмки: 4, 6, 7 (в применении к новому блоку), 12, 13 (branch-часть).

Вне скоупа A: вопросы мастера `init`, автодетект remote в onboarding, выпиливание
`DEFAULT_REPO`/`WEB_ADMIN_*`/`TASK_BOARD_MCP` из wizard, credential-UX, правки
`configure-review` и README. A не удаляет ни одного env-поля — только перестаёт быть от
них единственно зависимым.

## Проблема

Ветки резолвятся из глобального `Settings` и потому одинаковы для всех репозиториев одного
деплоя: `settings.review_branches_list()` / `primary_branch()`
(`reviewer/config/settings.py:97,134-138`) читают единственный CSV `REVIEW_BRANCHES`.
Два репозитория с разными основными ветками в одном деплое настроить нельзя.

PRI-221 уже дала домашние слои конфига (`reviewer/config/layers.py`:
`reviewer_config_root`, `home_repo_path`, `resolve_policy_data`, `build_config_report`,
`migrate_repo_config`) — но только для policy.

Прямо переиспользовать `resolve_policy_data` для веток нельзя: её сигнатура
(`layers.py:257-264`) требует `fetch_repo_yaml(ref)`, то есть чтения committed `.review.yml`
из ветки. Чтобы узнать ветку, понадобилось бы уже знать ветку. Цикл виден в живом коде:
`_config_context` (`reviewer/entrypoints/cli.py:120`) берёт `settings.primary_branch()`
до создания VCS-провайдера, которым потом читает `.review.yml`.

Дополнительно обнаружено при разборе: `resolve_branch`
(`reviewer/services/branch.py:15`) не вызывается нигде в продакшн-коде — только в
`tests/services/test_branch_resolve.py`. Логика валидации ветки продублирована по месту
вызова в `cli.py:711-714` и `mcp/service.py:1346-1349`.

## Решение

### Модель конфигурации

Ветки задаются блоком `repository` в домашних YAML-слоях:

```yaml
# ~/.config/rag-reviewer/repos/<owner>/<name>.yml
repository:
  primary_branch: dev          # необязателен; дефолт = index_branches[0]
  index_branches: [dev, main]
```

Координат репозитория внутри файла нет: они закодированы путём (`home_repo_path`,
`layers.py:69`). Отсюда следует и ответ на «куда мигрировать `DEFAULT_REPO`» — он
определяет имя файла, а не его содержимое.

Порядок слоёв, первый непустой выигрывает, блок берётся **целиком** (поключевого мержа нет —
то же правило замены верхнего ключа, что и в `resolve_policy_data`):

1. `~/.config/rag-reviewer/repos/<owner>/<name>.yml` → `repository:`
2. `~/.config/rag-reviewer/review.yml` → `repository:`
3. env `REVIEW_BRANCHES` (CSV, первая = primary) — compat-фолбэк
4. `["main"]`

Committed `.review.yml` веток **не задаёт**. Ключ `repository` в нём — запрещённый:
игнорируется с warning, значение не эхоится (тот же приём, что `_credential_path`,
`layers.py:126`). Так цикл bootstrap↔`.review.yml` устраняется конструктивно, а не
договорённостью: функция резолва веток физически не имеет параметра для чтения committed-файла.

### Резолвер

Новый модуль `reviewer/config/branches.py`, не зависящий от `resolve_policy_data`:

```python
@dataclass(frozen=True)
class RepoBranches:
    primary: str
    index: tuple[str, ...]
    source: str                  # "home:repos/<owner>/<name>.yml" | "home:review.yml" | "env" | "default"
    warnings: tuple[str, ...]

def resolve_repo_branches(
    repo: str,
    *,
    settings: Settings,
    config_root: Path | None = None,
    strict_home: bool = False,
) -> RepoBranches: ...
```

Читает только локальные файлы и env: ни VCS, ни сети, ни БД. `config_root` существует ради
тестов через `tmp_path` — ровно как у `resolve_policy_data`.

Валидация (стиль и тип ошибки — `HomePolicyError`, как `_validate_known_policy_data`,
`layers.py:189`):

- `repository` — mapping; неизвестные подключи сохраняются и игнорируются (правило future keys)
- `index_branches` — непустой список непустых строк без дублей
- `primary_branch` — строка; если задана, обязана входить в `index_branches`; иначе ошибка,
  называющая ветку, список и файл
- битый YAML / нечитаемый файл: при `strict_home=True` — исключение, иначе warning и переход
  к следующему слою (как `merge_home`, `layers.py:296-312`)

Инвариант: **невалидный существующий home-конфиг не деградирует молча до env**. Тихий откат
привёл бы к индексации не той ветки, а узнал бы об этом пользователь по пустому поиску.

### Точки перехода

Все 14 мест чтения веток получают `repo` (он в каждом контексте уже есть) и зовут резолвер:

| Место | Изменение |
|---|---|
| `cli.py:120` (`_config_context`) | ветка для policy берётся из branch-резолвера до создания VCS — разрыв цикла |
| `cli.py:630` (`index --ref`) | дефолт `--ref` = per-repo primary; явный ref валидируется против per-repo index |
| `cli.py:684,711-714,743` (`status`, `search`) | allowlist и primary — per-repo; текст ошибки больше не ссылается на `REVIEW_BRANCHES` |
| `mcp/service.py:1346-1349` (`_resolve_repo_branch`) | session-less тулы резолвят ветку по репо запроса |
| `services/review_service.py:202` | гейт «PR в неотслеживаемую ветку → skipped» считается по per-repo index |
| `services/branch.py:15` (`resolve_branch`) | параметризуется `RepoBranches` и **применяется** в `cli.py:711-714` и `mcp/service.py:1346-1349` вместо дублей |

`Settings.review_branches_list()` / `primary_branch()` сохраняются, но вызываются только
внутри `branches.py` как env-фолбэк. Отсутствие внешних вызовов фиксируется guard-тестом.

Для деплоя без home-файлов (`REVIEW_BRANCHES` в env) поведение остаётся бит-в-бит прежним —
это и есть критерий 12.

### Диагностика: `config show`

Отчёт получает секцию веток:

```
branches:
  primary: dev        (home:repos/mimfort/rag_for_git.yml)
  index:   dev, main  (home:repos/mimfort/rag_for_git.yml)
```

Ветки резолвятся до VCS и печатаются, даже если policy-часть упала (нет сети, нет токена,
репозиторий недоступен). Сейчас `config show` падает целиком на `_create_vcs_provider`;
после правки диагностика веток остаётся доступной именно тогда, когда она нужнее всего.

### Миграция: `config migrate`

Существующая команда (`cli.py:159-182`) дополнительно переносит `REVIEW_BRANCHES` в
`repository.index_branches` файла `home_repo_path(repo)`:

- репозиторий берётся из `--repo` (required, как сейчас)
- `.env` не изменяется: `REVIEW_BRANCHES` остаётся рабочим фолбэком
- если в home-файле уже есть `repository:` — noop с сообщением, не перезапись
  (переиспользуется семантика `noop` / `conflicting_keys`)
- эффективные ветки до и после миграции идентичны

Отдельной команды не заводим: имя `migrate-branches` уже занято миграцией legacy
base-индекса, вторая команда с тем же смыслом путала бы.

## Тестирование

Всё unit: без Postgres, Neo4j, сети и localhost-сокетов.

`tests/config/test_branches.py` (новый), по шаблону `tests/config/test_layers.py:27-52`
(запись `tmp_path/review.yml` и `tmp_path/repos/o/r.yml`, вызов с `config_root=tmp_path`,
проверка значений и источника):

- приоритет слоёв: per-repo > global > env > дефолт `main`
- блок заменяется целиком: global задаёт `index_branches`, per-repo только `primary_branch`
  → `index` из global не наследуется
- `primary_branch` по умолчанию равен `index_branches[0]`
- `primary_branch` вне `index_branches` → `HomePolicyError` с именем файла
- пустой `index_branches`, дубли, не-строки → ошибка
- битый YAML в существующем файле → ошибка, а не откат на env
- owner-подгруппы и точки в имени репозитория (`test_layers.py:110-136`)
- `repository:` в committed `.review.yml` → warning и игнор, значение не эхоится

`tests/config/test_review_branches.py` — дополняется кейсами per-repo override поверх env.

Изоляция репозиториев: два файла `repos/*.yml` дают непересекающиеся `index_branches`,
команды выбирают ветки по репозиторию (критерий 4).

Guard-тест: греп по `reviewer/` не находит вызовов `review_branches_list` / `primary_branch`
вне `reviewer/config/branches.py`.

Миграция (`test_layers.py:365-380` как образец): создаёт файл; второй вызов — noop;
эффективные `RepoBranches` до и после совпадают; существующий `repository:` не перезаписывается.

`tests/services/test_branch_resolve.py` — переводится на `RepoBranches`; добавляются тесты
того, что `cli` и MCP используют именно `resolve_branch`, а не собственные копии проверки.

## Риски

- **Тихая смена поведения гейта PR.** `review_service.py:202` решает, ревьюить ли PR. Ошибка
  в резолве приведёт к молчаливому пропуску ревью. Смягчение: env-фолбэк даёт прежний список
  при отсутствии home-файлов, и это покрыто отдельным тестом.
- **`config show` без сети.** Разделение отчёта на branch-часть (локальную) и policy-часть
  (сетевую) требует аккуратности в обработке ошибок, чтобы падение второй не съедало первую.
- **Тела `migrate_repo_config` / `build_config_report` длинные** (`layers.py:440-950`); при
  реализации их нужно прочитать целиком, а не полагаться на сигнатуры из тестов.
