# PRI-223 (часть B) — Global/runtime и repo onboarding в reviewer init

Задача: https://ru.yougile.com/team/686c049c8af8/#PRI-223
Бриф: `docs/superpowers/briefs/2026-08-07-PRI-223-redesign-reviewer-init.md`

## Скоуп

Эта спека покрывает только часть B задачи PRI-223:

- разделение `reviewer init` на global runtime/credentials и repo onboarding;
- автоматическое определение repo из локального git remote и primary branch без сети;
- удаление multirepo, `DEFAULT_REPO`, `REVIEW_BRANCHES`, `WEB_ADMIN_*` и legacy
  `TASK_BOARD_MCP` из стандартных вопросов мастера;
- общий preview затрагиваемых файлов, несекретных значений и provenance до первой записи;
- неразрушающая повторная настройка второго репозитория.

Покрываемые пункты «Что сделать»: 2, 7, оставшаяся часть 8, 11.
Покрываемые критерии: 1, 2, 3, 10, 11.

Вне скоупа:

- provider-scoped credentials, минимальные права и YouGile acquisition flow — часть C;
- правки skill `configure-review`, двуязычная документация и общий добор тестов — часть D;
- изменение runtime-семантики legacy env — значения продолжают читаться `Settings`;
- изменение branch resolution — часть A уже реализована и смержена PR #159.

## Проблема

Текущий `reviewer init` — один мастер одного `.env`. `WIZARD_GROUPS`
(`reviewer/install.py:182-275`) смешивает runtime/credentials с repo-specific вопросами и
advanced web-настройками. Команда `init` (`reviewer/entrypoints/cli.py:1083-1195`) смешивает
prompting, provider setup, preview, запись и post-actions в одной функции.

Из этого следуют четыре дефекта:

1. Второй repo требует повторного прохода по глобальному `.env`, хотя branch config уже
   изолирован в `home:repos/<owner>/<name>.yml`.
2. Fresh wizard предлагает `DEFAULT_REPO` и `REVIEW_BRANCHES`, хотя repo выводится из remote, а
   ветки после части A принадлежат per-repo home config.
3. `--dry-run` показывает только `.env`; обычный интерактивный запуск вообще пишет без итогового
   preview и общего подтверждения.
4. Автодетект default branch отсутствует. Использовать сетевой `git remote show origin` нельзя:
   критерий 10 запрещает сеть и в `--yes`, и в `--dry-run`.

## Выбранный подход

Сохраняем одну публичную команду и вводим явные стадии:

```text
reviewer init                         # --scope all: global + repo
reviewer init --scope global          # только user-scope .env
reviewer init --scope repo            # только текущий git repo
reviewer init --scope repo --repo o/r # repo id явно, если remote отсутствует/нестандартен
```

Альтернативы отклонены:

- расширить текущую монолитную `init`: минимальный diff, но planning, side effects и error paths
  останутся неразделимыми и плохо тестируемыми;
- добавить `init-global`/`init-repo`: границы чистые, но это лишние top-level команды и ненужная
  миграция launcher/CLI-контракта.

`--scope` — `click.Choice(("all", "global", "repo"))`, default `all`. Существующие `--path`,
`--yes` и `--dry-run` сохраняют смысл. `--path` влияет только на global stage. Новый `--repo`
задаёт canonical repo id для repo stage; без него используется `origin` текущего git root.

## Global Stage

### Стандартные поля

Из `WIZARD_GROUPS` удаляются:

- группа «Мульти-репо / ветки» целиком (`DEFAULT_REPO`, `REVIEW_BRANCHES`);
- группа «Веб-админка» целиком (`WEB_ADMIN_USER`, `WEB_ADMIN_PASSWORD`);
- `TASK_BOARD_MCP` из общей части board group.

`TASK_BOARD_KEY_PATTERN`, `TASK_BOARD_URL_TEMPLATE` и текущие registry-driven provider fields
остаются до части C. Часть B не меняет provider selection/setup flow.

Удалённые поля не удаляются из `Settings`, `_ENV_TEMPLATE_BASE` или `.env.example`: это runtime
compatibility и advanced reference. При повторном init они не входят в `wizard_keys`, поэтому
попадают в `extra` и сохраняются с исходными значениями. Fresh generated `.env` их не содержит,
потому что `render_env` выводит только wizard groups и реальные extra.

### Preview

Global plan содержит:

- target `.env`;
- provenance `existing:<path>` или `wizard defaults/input`;
- полный будущий `.env`, пропущенный через существующий `render_env_preview`.

Secret-like keys и credentials в URL остаются пустыми в preview по текущему контракту.

## Repo Stage

### Git detection

В `reviewer/gitutil.py` добавляются две fail-soft функции без сети:

```python
def repo_root(path: str = ".") -> str | None: ...
def remote_default_branch(repo: str) -> str | None: ...
```

`repo_root` использует `git -C <path> rev-parse --show-toplevel`.
`remote_default_branch` использует локальный symbolic ref
`refs/remotes/origin/HEAD` и возвращает имя без `origin/`. Он не вызывает `fetch`, `ls-remote`
или `remote show`.

Repo id выбирается в порядке:

1. `reviewer init --repo owner/name` → provenance `cli`;
2. `remote_url(root)` + `derive_repo_from_remote(...)` → provenance `git:origin`;
3. repo stage недоступен.

`DEFAULT_REPO` намеренно не участвует в onboarding autodetect: он остаётся runtime fallback для
старых session-less вызовов, но не является владельцем нового repo config.

Если git root отсутствует, `scope=repo` завершается понятной ошибкой, а `scope=all` завершает
global stage и печатает команду повтора из git repository. Если root есть, но repo id не удалось
получить, интерактивный режим предлагает ввести `owner/name`; noninteractive режим не prompt-ит:
`scope=repo` возвращает ошибку с `--repo`, `scope=all` пропускает repo stage с той же подсказкой.

### Branch candidate

Сначала вызывается `resolve_repo_branches(repo, settings=...)` из части A.

- Если source уже `home:repos/<repo>.yml`, существующий `repository` block считается
  authoritative: stage показывает noop и ничего не переписывает.
- Иначе primary candidate = локальный `origin/HEAD`, если он существует; fallback = effective
  `RepoBranches.primary` (home global → env → default `main`).
- Если candidate входит в effective `RepoBranches.index`, default index сохраняет effective list;
  иначе default index = `(candidate,)`.

Interactive запуск показывает repo id, primary и provenance, затем позволяет изменить primary и
CSV index list. Primary обязан входить в index; parsing и validation переиспользуют инварианты
`RepoBranches`. `--yes` и `--dry-run` принимают candidates без prompt.

Текущая feature branch не используется как primary fallback: это часто случайная ветка задачи.

### Repo config plan

Новый модуль `reviewer/config/onboarding.py` отделяет вычисление от side effects:

```python
@dataclass(frozen=True)
class RepositoryDetection:
    root: Path
    repo: str
    repo_source: str
    primary: str
    primary_source: str

@dataclass(frozen=True)
class RepositoryConfigPlan:
    path: Path
    repo: str
    primary: str
    index: tuple[str, ...]
    repo_source: str
    primary_source: str
    action: Literal["create", "append", "noop"]
    preview: str

def detect_repository(path: str, repo_override: str | None, *, settings: Settings) \
        -> RepositoryDetection | None: ...
def plan_repository_config(detection: RepositoryDetection, *, settings: Settings,
                           primary: str | None = None,
                           index: tuple[str, ...] | None = None,
                           config_root: Path | None = None) \
        -> RepositoryConfigPlan: ...
def apply_repository_config(plan: RepositoryConfigPlan) -> None: ...
```

`config_root` допускается как keyword-only test seam у plan, аналогично
`resolve_repo_branches`; apply получает уже вычисленный target path, а публичный CLI test seam
не выставляет.

Plan не создаёт директорий и не пишет файлы. `preview` показывает только будущий
`repository.primary_branch` / `repository.index_branches`; секретов в этом блоке быть не может.

### Non-destructive write

Repo destination всегда вычисляется через `home_repo_path(repo)`.

- отсутствующий файл создаётся exclusive;
- файл без `repository` получает YAML block append-ом, сохраняя существующие keys/comments;
- существующий `repository` даёт noop, без merge или overwrite;
- unknown top-level и `repository` subkeys не удаляются;
- повторный запуск byte-for-byte не меняет файл.

Общую create/append/noop механику следует извлечь из `migrate_repo_branches`, чтобы migration и
onboarding не расходились по quoting, race handling и comment preservation. Renderer пишет
`primary_branch` явно и использует `yaml.safe_dump`, чтобы имена вроде `2.0`, `on` и
`feature{x}` оставались строками.

## Command Flow

`reviewer init` работает в пяти фазах:

1. Resolve scope и собрать current `.env` без записи.
2. Собрать global plan: values и redacted content строятся во всех режимах, а интерактивный
   provider setup запускается только без `--yes`/`--dry-run` и может использовать сеть.
3. Выполнить offline repo detection, интерактивно скорректировать candidates и построить repo plan.
4. Показать единый preview всех выбранных targets и provenance; в interactive mode запросить одно
   финальное подтверждение. До него filesystem не меняется.
5. Применить global/repo plans, затем вывести результаты и effective branch report.

`--dry-run` выполняет фазы 1-4 в noninteractive режиме и завершает работу. Он не создаёт parent
directories, не запускает provider setup, `reviewer check`, client install или policy VCS.

`--yes` также не вызывает prompt, provider setup, browser, `reviewer check` или полный
`config show`; он показывает preview и сразу применяет план.

Для `scope=all` два файла не образуют транзакцию. Ошибка записи второго файла не откатывает первый:
rollback конфигурации опаснее явного частичного результата. Команда сообщает каждый успешный
target и завершается non-zero на ошибке. Ключевой инвариант — ни один target не меняется до preview
и подтверждения.

## Effective Output

После успешной repo write/noop команда повторно вызывает `resolve_repo_branches` и выводит ту же
структуру:

```text
branches:
  primary: dev  (home:repos/mimfort/rag_for_git.yml)
  index:   dev, main  (home:repos/mimfort/rag_for_git.yml)
```

Renderer/data builder извлекается из `config show` и переиспользуется `init`, чтобы не появилось
второго формата provenance. Полный `reviewer config show` не вызывается автоматически: он читает
committed policy через VCS и нарушил бы no-network контракт `--yes`. Команда печатает точный
follow-up `reviewer config show --repo <repo>` для полного отчёта.

## Error Handling

- invalid `--repo` → Click error до preview;
- no git root / no repo id → skip только в `scope=all`, hard error в `scope=repo`;
- missing `origin/HEAD` → effective branch fallback, не network lookup;
- invalid interactive branch CSV → повторный prompt; в noninteractive candidates уже валидны;
- malformed existing home YAML / invalid `repository` → sanitized `HomeConfigError`, без fallback и
  без записи;
- existing `repository` → noop, не конфликт и не rewrite;
- `click.Abort` до final confirmation → ни один target не изменён;
- write error → non-zero с path и типом ошибки, без вывода secret values.

## Testing

Все тесты unit, без Postgres, Neo4j, localhost и внешней сети.

`tests/test_gitutil.py`:

- git root success/failure;
- local `origin/HEAD` → branch без prefix;
- missing symbolic ref → None;
- helper никогда не вызывает сетевые git subcommands.

`tests/config/test_onboarding.py`:

- `--repo` выше remote; remote выше отсутствия repo id;
- `origin/HEAD` выше effective branch fallback;
- compatible effective index сохраняется, incompatible сокращается до detected primary;
- existing per-repo block → noop;
- create и append сохраняют unrelated YAML/comments;
- ambiguous YAML branch names round-trip как строки;
- repeat apply byte-for-byte noop;
- два repo получают разные paths и не меняют конфиг друг друга.

`tests/install/test_install_wizard.py` и `tests/test_install_wizard.py`:

- fresh wizard keys не содержат удалённые поля;
- `_ENV_TEMPLATE_BASE` / `.env.example` всё ещё содержат runtime compatibility keys;
- existing удалённые keys сохраняются через `extra`;
- board common fields больше не содержат `TASK_BOARD_MCP`.

CLI tests:

- `--scope global` не вызывает git/repo write;
- `--scope repo` не читает и не меняет global `.env`;
- default `all` показывает оба targets до первой записи;
- interactive reject оставляет оба targets неизменными;
- `--dry-run` не prompt-ит, не вызывает сеть и ничего не создаёт;
- `--yes` не prompt-ит и не вызывает provider/check/config-show network paths;
- no git/remote поведение различается для `all` и `repo` как описано выше;
- preview redacts current, legacy и unknown secret-like env;
- successful repo stage выводит effective branch source и follow-up command;
- второй repo через `--scope repo` не меняет `.env` и первый repo file.

## Риски

- `origin/HEAD` не создан в некоторых старых клонах. Offline fallback намеренно использует
  effective config, а interactive mode позволяет исправить значение; скрытый network fallback
  запрещён.
- Существующий `repository` block не редактируется мастером. Это сознательная защита
  comments/unknown settings; изменение готового блока остаётся за частью D (`configure-review`)
  или ручной правкой.
- Global и repo writes не атомарны вместе. Preview-first и per-target result делают частичный
  результат явным, а `--scope` позволяет безопасно повторить только незавершённую стадию.
- Удалённые wizard fields остаются в `.env.example`, поэтому документация должна чётко отличать
  standard onboarding от advanced/runtime fallback; это будет завершено частью D.
