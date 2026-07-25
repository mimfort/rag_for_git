# PRI-218 — глобальный интерактивный launcher reviewer

Исходный brief:
`docs/superpowers/briefs/2026-07-25-PRI-218-global-interactive-launcher.md`.

## Контекст

PyPI-пакет `rag-reviewer` уже устанавливает два глобальных entry point:
`reviewer` с Click CLI и `reviewer-mcp` с MCP-сервером. Прямые команды являются публичным
машинным контрактом: скрипты ожидают текущие stdout, stderr и exit code, а
`reviewer status --json` обязан печатать только JSON.

Новый launcher добавляет human-facing слой только для вызова `reviewer` без аргументов в
настоящем интерактивном терминале. Он не заменяет Click-команды, не меняет MCP entry point и
не требует checkout репозитория. Пользователь устанавливает и запускает его так же, как пакет
сейчас:

```bash
uv tool install rag-reviewer
reviewer
```

Временный запуск также является полноценным сценарием:

```bash
uvx --from rag-reviewer@latest reviewer
```

## Цели

- Открывать компактную command palette только для no-args TTY-вызова `reviewer`.
- Показывать все видимые Click-команды, fuzzy search, описание, последствия и типичные сценарии.
- Давать редактор arguments/options с defaults, валидацией, basic/advanced секциями и
  маскированием чувствительных значений.
- Перед любой командой показывать preview и требовать отдельное подтверждение.
- После подтверждения закрывать TUI и передавать argv существующему Click CLI in-process.
- Сохранить byte-compatible прямые маршруты для аргументов, `--help`, JSON, CI, pipe/redirect,
  `TERM=dumb`, неизвестных команд и ошибок Click.
- Поставлять launcher в основном wheel с обычной публикацией на PyPI; не требовать extra,
  git clone, `cd` в проект или локальные assets.
- Проверять platform contract на Windows, macOS и Linux.

## Не входит

- Переписывание callback'ов существующих Click-команд.
- Замена линейного wizard внутри `reviewer init`.
- Изменение `reviewer-mcp`, MCP transport или plugin lifecycle.
- Фоновая проверка обновлений, автообновление при старте или telemetry.
- Shell execution, генерация shell-скриптов или хранение command history.
- Mouse-first UI, полноэкранный dashboard и сохранение введённых значений между сессиями.
- Требование Docker, git-репозитория или настроенного `.env` для самого открытия palette.

## Принятые решения

1. Backend — `prompt_toolkit`; UX — компактная command palette, а не полноэкранный dashboard.
2. `prompt_toolkit>=3.0,<4` входит в основные dependencies. Поэтому `uv tool install` и `uvx`
   работают без `[tui]` extra, но импорт библиотеки остаётся ленивым.
3. Click command tree является источником имён, параметров, типов, defaults и validation.
   Отдельный presentation metadata слой добавляет только human-facing сведения.
4. Все видимые команды автоматически попадают в каталог. Отсутствие rich metadata не скрывает
   новую команду: runtime использует Click help как fallback, а CI требует metadata для текущего
   набора команд.
5. Первый `Enter` никогда не выполняет команду. Даже команда без параметров проходит через
   details и masked preview; выполнение требует отдельного подтверждения.
6. Существующий Click `cli` остаётся единственным исполнителем команд и authority для финального
   парсинга argv.
7. Наличие обновления проверяется только после явного выбора действия. Само обновление требует
   второго явного действия.

## Distribution и entry points

`pyproject.toml` меняет только entry point `reviewer` и добавляет core dependency:

```toml
dependencies = [
  # existing dependencies
  "prompt_toolkit>=3.0,<4",
]

[project.scripts]
reviewer = "reviewer.entrypoints.launcher:main"
reviewer-mcp = "reviewer.entrypoints.mcp_server:main"
```

Новые Python-модули находятся под `reviewer/`, поэтому существующий
`[tool.setuptools.packages.find] include = ["reviewer*", "eval*"]` включает их в wheel без
package-data или генерации assets. UI строится Python-кодом; runtime не читает файлы из checkout.

`reviewer-mcp` продолжает импортировать только `reviewer.entrypoints.mcp_server:main`.
Launcher, catalog и `prompt_toolkit` не входят в его import path. Обычная публикация wheel на
PyPI выпускает интерфейс одновременно для постоянной user-scope установки и `uvx @latest`.

## Архитектура

### `reviewer/entrypoints/launcher.py`

Минимальный публичный dispatcher без импорта `prompt_toolkit`.

```text
main(argv=None)
  ├─ direct route → existing cli.main(args=argv, prog_name="reviewer")
  └─ interactive route → lazy import reviewer.launcher.app
                         → run palette
                         → cancel | selected argv
                         → existing cli.main(args=selected, prog_name="reviewer")
```

Direct route выбирается, если выполняется хотя бы одно условие:

- в argv есть любой токен, включая `--help` или неизвестную команду;
- `stdin.isatty()` или `stdout.isatty()` возвращает false либо бросает исключение;
- `TERM`, после `strip().casefold()`, равен `dumb`;
- `CI` имеет значение, отличное от `""`, `0`, `false`, `no` и `off`, либо присутствует
  vendor marker `GITHUB_ACTIONS`, `GITLAB_CI`, `TF_BUILD`, `BUILDKITE`, `CIRCLECI`,
  `JENKINS_URL` или `TEAMCITY_VERSION`.

Проверка fail-closed: сомнение в интерактивности ведёт в существующий Click CLI, а не в TUI.
Dispatcher ничего не печатает перед делегированием и не перехватывает Click usage/errors.

### `reviewer/launcher/models.py`

Независимые от UI immutable-модели:

- `CommandSpec` — Click command path, summary, details, effects, scenarios, keywords и params;
- `ParameterSpec` — kind, required, default, choices, multiplicity, basic/advanced и sensitive;
- `CommandDraft` — выбранная команда и только явно изменённые значения;
- `PreparedCommand` — реальный `tuple[str, ...]` argv и отдельный masked preview;
- `LauncherResult` — cancel либо prepared argv.

Сырые secret values никогда не входят в `repr`, exception text или preview.

### `reviewer/launcher/catalog.py`

Read-only адаптер рекурсивно обходит видимое Click command tree. Hidden-команды не показываются;
deprecated-команды остаются видимыми с badge. Из Click берутся:

- command name, `short_help`/`help`;
- arguments/options, `required`, `nargs`, `multiple`, flags и secondary flags;
- ParamType, Choice values, metavar и статический default.

Callable defaults не вызываются при построении каталога: они показываются как вычисляемые во
время исполнения и не попадают в argv без явного редактирования. Option callbacks также не
вызываются до финального Click parse.

Presentation metadata keyed by command path и parameter name содержит:

- понятное назначение, последствия и сценарии;
- search keywords;
- `basic`/`advanced`;
- `sensitive`;
- уровень эффекта `read`, `write`, `network` или `destructive`.

При отсутствии metadata каталог использует Click help и безопасные defaults. Отдельный guard-тест
требует полного metadata coverage для всех текущих видимых команд и запрещает orphan keys.

### `reviewer/launcher/command.py`

Чистая сборка argv без shell:

- required positional arguments сериализуются в Click-порядке;
- optional arguments и options добавляются только после явного изменения;
- неизменённые defaults не материализуются, поэтому Click остаётся authority для dynamic/default
  semantics;
- flags, secondary flags, `multiple`, `count` и fixed `nargs` сохраняют Click-форму;
- unsupported custom parameter shape получает raw-text fallback, а не исчезает из UI.

`PreparedCommand.argv` исполняется как список токенов. Preview форматирует те же токены для
текущей ОС только для чтения; shell не запускается, а copy-to-clipboard не входит в v1.
Sensitive values заменяются на `••••••`.

### `reviewer/launcher/app.py`

`prompt_toolkit` импортируется только внутри interactive route. UI является тонким отображением
тестируемого state/controller:

```text
PALETTE → DETAILS/PARAMS → PREVIEW → CONFIRM → EXIT_AND_EXECUTE
            ↑      Esc       |
            └───────────────┘
```

Palette использует fuzzy search по name, help и keywords. Стрелки меняют выбранную команду.
Details показывают назначение, последствия, required/basic fields и свёрнутые advanced fields.
Интерфейс адаптируется к узкому терминалу одной колонкой.

Управление:

- `Enter` — открыть команду, применить поле или перейти к preview;
- явная кнопка/shortcut «Выполнить» — единственный переход к исполнению;
- `Esc` — назад; из palette закрывает launcher с exit code 0;
- `Ctrl+C` — восстанавливает терминал и завершает процесс с exit code 130.

## Валидация и исполнение

До preview launcher проверяет required/arity. Для встроенных Click-типов он вызывает
`click.ParamType.convert`; custom type не исполняется заранее и проверяется только финальным
Click parse. Command/option callbacks не запускаются. Ошибка остаётся рядом с полем.

После подтверждения порядок строгий:

1. Зафиксировать immutable argv.
2. Закрыть prompt_toolkit Application и восстановить terminal modes/screen.
3. Удалить UI-state и references на чувствительные buffers.
4. Вызвать существующий `cli.main(args=argv, prog_name="reviewer", standalone_mode=True)`.
5. Не оборачивать stdout, stderr, usage errors или exit code команды.

Click повторно парсит argv и остаётся финальным authority. Launcher не вызывает callback дважды
и не пытается возвращаться в TUI после начала команды.

Команды с собственными prompts, например интерактивный `init`, начинают prompt-flow только после
закрытия launcher. Их secret input продолжает принадлежать существующему Click wizard.

## Проверка и установка обновлений

`reviewer update` сейчас одновременно определяет install mode, запрашивает PyPI и при постоянной
`uv tool`-установке выполняет upgrade. Его прямой output и поведение без аргументов сохраняются.

Read-only часть выносится из вложенных функций CLI в `reviewer/versioning.py`:

- определить current version и режим `editable` / `uv tool` / `uvx`;
- запросить latest PyPI version с timeout;
- сравнить нормализованные версии;
- вернуть typed result без печати и mutation.

Launcher не вызывает этот service при старте. Только явный выбор «Проверить обновления» запускает
запрос с progress state и показывает current → latest. Ошибка сети остаётся внутри palette и не
закрывает её.

Если доступно обновление постоянной `uv tool`-установки, отдельное подтверждение «Обновить»
закрывает TUI и передаёт существующему Click CLI argv `("update",)`. Возможный повторный read-only
запрос внутри совместимого CLI flow допустим; сама mutation по-прежнему выполняется единственным
существующим command callback. В `uvx` и editable режимах palette показывает соответствующую
инструкцию без кнопки mutation.

## Ошибки и безопасность

- Ошибка импорта/инициализации TUI относится только к новому interactive route: terminal state
  восстанавливается, в stderr печатается одна строка с `reviewer --help`, exit code равен 1.
- Ошибка построения каталога не запускает частичный UI и не вызывает команду.
- Исключение `isatty()` ведёт в direct Click route.
- Ошибка PyPI-check показывается в palette; установка не меняется.
- Ошибка после начала Click-команды форматируется самим Click/командой без launcher-префикса.
- Отмена до command confirm не пишет файлы и не запускает subprocess. Обычный command flow не
  обращается к сети; только отдельно подтверждённое read-only действие «Проверить обновления»
  может уже выполнить запрос к PyPI, но оно не меняет установку.
- Prompt buffers не используют persistent history. Secret values не логируются и не попадают в
  preview, repr или traceback context.
- `TERM=dumb`, pipe, redirect, subprocess и CI никогда не инициализируют terminal application.

## Тестирование

### Routing contract

`tests/entrypoints/test_launcher.py` параметризует:

- args present / no args;
- stdin TTY / non-TTY / `isatty()` error;
- stdout TTY / non-TTY / `isatty()` error;
- `TERM=dumb`, CI truthy и обычный терминал;
- cancel, `Ctrl+C`, TUI import failure и successful selected argv.

Direct cases проверяют exact argv/prog_name, отсутствие launcher output и отсутствие импорта
`prompt_toolkit`.

### Public CLI regression

Контрактные тесты сравнивают launcher direct route с вызовом существующего `cli` для:

- `--help` и help подкоманд;
- `check`;
- `status --json`, включая `json.loads(stdout)` и отсутствие ANSI;
- `init --yes` и `init --dry-run`;
- `install --dry-run`;
- неизвестной команды;
- no-args non-TTY.

Внешние сервисы остаются замоканными. Проверяются stdout, stderr и exit code; намеренно новый
no-args TTY-сценарий тестируется отдельно.

### Catalog, forms и execution

Чистые unit-тесты покрывают:

- recursive discovery и metadata coverage;
- positional/options, defaults, Choice, flags/secondary flags, multiple/count/nargs;
- callable default и custom-type fallback без раннего callback;
- basic/advanced partition;
- sensitive input, masked preview и отсутствие secret в repr;
- platform preview при одном и том же реальном argv;
- state transitions, back/cancel и обязательный confirm.

Один prompt_toolkit smoke-test использует in-memory pipe input и dummy output, без реального
терминала и snapshot'ов control sequences.

### Wheel, uv и platform matrix

Отдельный launcher job в GitHub Actions запускается на:

```text
ubuntu-latest
macos-latest
windows-latest
```

На каждой ОС Python 3.11 выполняет:

1. build wheel;
2. `uv tool install` из локального wheel;
3. direct contract smoke для `reviewer --help`;
4. `uvx --from <local-wheel> reviewer --help`;
5. запуск из временного каталога вне checkout;
6. проверку, что `reviewer-mcp` import path не загружает launcher/prompt_toolkit.

No-args TTY проверяется детерминированными unit/smoke tests с fake terminal, а не нестабильным
PTY GitHub runner. Полный существующий suite остаётся обязательным Linux-гейтом.

## Документация

README сохраняет quick setup `uv tool install rag-reviewer` и добавляет:

- `reviewer` без аргументов открывает palette только в TTY;
- все прежние команды и automation routes работают напрямую;
- `uvx --from rag-reviewer@latest reviewer` открывает тот же интерфейс;
- launcher работает из любого каталога и не требует clone;
- update check выполняется только вручную;
- keyboard shortcuts, cancel semantics и пример preview.

CLI reference остаётся каноническим для прямых команд. Документация не предлагает отдельный
TUI extra или второй installer.

## Совместимость и выпуск

Миграций данных и конфигурации нет. Выпуск требует обычного version bump и публикации wheel на
PyPI. Existing `uv tool` users получают launcher после `uv tool upgrade rag-reviewer`; `uvx
@latest` получает его при следующем запуске.

Compatibility invariant:

```text
argv != [] OR non-interactive environment
    ⇒ prompt_toolkit не импортирован
    ⇒ существующий Click CLI получает исходный argv
    ⇒ перед его stdout/stderr нет launcher output
```

Rollback — вернуть `project.scripts.reviewer` к `reviewer.entrypoints.cli:cli`; остальные Click и
MCP-компоненты не изменены.

## Критерии готовности

- Wheel, установленный через `uv tool install`, открывает palette из каталога вне checkout.
- Локальный wheel запускается через `uvx --from` без предварительной установки.
- Полный каталог текущих Click-команд доступен через search.
- Каждая команда проходит details, validation, masked preview и explicit confirm.
- Cancel не имеет side effects; update не делает фоновых запросов.
- Direct CLI regression и три platform job проходят.
- `reviewer-mcp` не импортирует launcher или `prompt_toolkit`.
- Все исходные критерии PRI-218 покрыты spec и автоматизированными тестами.
