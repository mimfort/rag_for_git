# PRI-210 — автоустановка и обновление Codex plugin

## Контекст

Сейчас `reviewer install codex` управляет только `[mcp_servers.reviewer]` в
`~/.codex/config.toml`. Workflow-скиллы вне клона приходится копировать вручную в
`~/.codex/skills`, где они теряют namespace плагина и не имеют безопасного lifecycle.
Корневой `.codex-plugin/plugin.json` ссылается на весь репозиторий и бандлит `.mcp.json`
с `/bin/bash`; существующая Codex MCP-секция не обновляется, а inventory скиллов и версия
manifest устарели.

Нужен единый кроссплатформенный сценарий:

```bash
uvx --from rag-reviewer@latest reviewer install codex
```

Команда должна установить один глобальный reviewer MCP и namespaced Codex plugin через
публичный `codex plugin` CLI. Исходный brief:
`docs/superpowers/briefs/2026-07-10-PRI-210-codex-plugin-auto-install.md`.

## Цели

- Поддержать чистые Windows, macOS и Linux без shell-обёрток, symlink и машинно-зависимых
  путей.
- Сделать Git marketplace и `plugin/` каноническим переносимым источником Codex plugin.
- Обеспечить fresh install, repeat/update, dry-run, verification и безопасный rollback.
- Оставить ровно одного владельца MCP: глобальную запись из `reviewer install codex`.
- Поддержать `install-skills codex` как plugin-only entrypoint и `--no-skills` как MCP-only.
- Мигрировать только положительно идентифицированные standalone reviewer skills после
  успешной проверки plugin.
- Исключить stale cache детерминированной release identity и CI guard.
- Завершать установку явным требованием New Chat/new CLI session; для IDE также Reload
  Window.

## Не входит

- Переписывание installer flow остальных клиентов.
- Изменение reviewer MCP, review pipeline или бизнес-логики скиллов.
- Прямое редактирование внутреннего Codex plugin cache.
- Новый Git tag/release pipeline: marketplace отслеживает `main`.
- Автоматическая замена marketplace с тем же именем, но другим source.
- Миграция изменённых, частичных или неоднозначных legacy skill-каталогов.

## Принятые решения

1. Codex lifecycle изолируется в новом `reviewer/install_codex.py`; существующий
   `reviewer/install.py` остаётся владельцем MCP plans, backup и standalone skills.
2. `plugin/` — общий компактный payload Claude Code и Codex. Codex manifest не объявляет
   MCP; Claude wiring сохраняется.
3. Marketplace использует переносимый Git source `mimfort/rag_for_git`, ref `main` и sparse
   paths `.agents/plugins` и `plugin`.
4. Release version имеет вид `<package-version>+codex.<payload-hash>`; hash вычисляется
   детерминированно, а не увеличивается вручную.
5. `reviewer init` предлагает Codex install только интерактивно. `init --yes` не запускает
   внешние installer-команды.
6. Legacy-копия считается reviewer-owned только при валидном installer stamp либо полном
   побайтовом совпадении с уже проверенным plugin payload.

## Структура репозитория и manifests

Добавляется Codex marketplace:

```text
.agents/plugins/marketplace.json
plugin/
  .codex-plugin/plugin.json
  .claude-plugin/plugin.json
  .mcp.json
  assets/icon.svg
  hooks/
  skills/
```

`.agents/plugins/marketplace.json` содержит marketplace `rag-reviewer` и одну запись:

```json
{
  "name": "rag-reviewer",
  "interface": {"displayName": "RAG Reviewer"},
  "plugins": [
    {
      "name": "rag-reviewer",
      "source": {"source": "local", "path": "./plugin"},
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
        "products": ["CODEX"]
      },
      "category": "Productivity"
    }
  ]
}
```

Путь `./plugin` относителен к корню marketplace snapshot. В marketplace и manifest нет
абсолютных путей, symlink и ссылок на checkout разработчика.

`plugin/.codex-plugin/plugin.json` является каноническим distributable manifest:

- `skills` указывает на `./skills/`;
- `composerIcon` указывает на `./assets/icon.svg`;
- поле `mcpServers` отсутствует;
- `version` соответствует release identity;
- inventory не перечисляет имена скиллов вручную.

Корневой `.codex-plugin/plugin.json` остаётся для project-level режима. Он также не объявляет
MCP и семантически совпадает с distributable manifest, кроме относительных путей
`./plugin/skills/` и `./plugin/assets/icon.svg`. Генератор/guard строит корневую форму из
канонической, поэтому два manifest не расходятся вручную.

`plugin/.mcp.json` сохраняется для Claude Code, но Codex его не загружает. Root `.mcp.json` и
Claude marketplace остаются совместимыми. `assets/icon.svg` копируется в compact payload из
корневого canonical asset и проверяется на равенство.

## Компоненты

### `reviewer/install_codex.py`

Модуль не зависит от Click и предоставляет четыре группы операций.

**State discovery**

- `find_codex_executable() -> Path` — абсолютный executable через `shutil.which`;
- `detect_codex_capabilities(...) -> CodexCapabilities` — feature detection команд
  `plugin`, `marketplace`, `add`, `upgrade` и `--json` без жёсткой проверки версии;
- `read_codex_state(...) -> CodexPluginState` — локальные `marketplace list --json` и
  `plugin list --json`.

**Planning**

- `build_codex_plugin_plan(state, options) -> CodexPluginPlan`;
- план содержит marketplace action, argv операций, ожидаемую версию, MCP change,
  verification checks и legacy scan;
- построение плана не пишет на диск и не выполняет сетевые/mutating команды.

**Execution and verification**

- `install_codex_plugin(plan, runner, paths) -> CodexInstallResult`;
- `verify_marketplace_snapshot(root, expected) -> SnapshotVerification`;
- `verify_installed_plugin(state, expected) -> PluginVerification`.

**Legacy migration**

- `find_owned_legacy_skills(...) -> list[LegacySkillCandidate]`;
- `migrate_legacy_skills(...) -> LegacyMigrationResult`.

Subprocess runner и filesystem roots инъектируются. Production runner использует
`subprocess.run(argv, capture_output=True, text=True, check=False)` без `shell=True`; тесты
подставляют stateful fake.

### `reviewer/install.py`

Существующие `launch_command`, `Client`, `InstallPlan`, `build_plan` и `apply_plan` остаются
общими для MCP. Codex-ветка `build_plan` меняется с append-only на безопасную замену
reviewer-owned TOML tables.

Перед изменением используется `tomllib` для валидации всего документа. Line-aware rewriter:

- находит `[mcp_servers.reviewer]` и дочерние reviewer tables;
- удаляет только эти table blocks;
- вставляет одну актуальную секцию в позицию первой найденной либо в конец файла;
- сохраняет весь остальной текст байт-в-байт;
- отклоняет inline representation или структуру, которую нельзя безопасно переписать.

Команда/аргументы продолжают сериализоваться TOML-совместимыми quoted strings, поэтому пути с
пробелами и Windows separators не проходят через shell.

### `reviewer/entrypoints/cli.py`

CLI выполняет только dispatch, форматирование плана/результата и преобразование ошибок в
`click.ClickException`. Lifecycle-решения не дублируются между командами.

## Публичное поведение CLI

### `reviewer install codex`

Устанавливает или обновляет MCP и plugin. Порядок:

1. Выполнить local preflight и построить полный план.
2. Сохранить transaction backup `$CODEX_HOME/config.toml` до первой mutation.
3. Добавить marketplace, если он отсутствует, либо обновить совпадающий Git source.
4. Проверить marketplace snapshot до `plugin add`.
5. Применить актуальный MCP plan.
6. Выполнить `codex plugin add rag-reviewer@rag-reviewer --json`.
7. Проверить installed/enabled/version через `plugin list --json` и payload inventory.
8. Мигрировать положительно идентифицированные legacy skills.
9. Напечатать backup/rollback paths и New Chat/new session instructions.

Fresh marketplace использует argv:

```text
codex plugin marketplace add mimfort/rag_for_git
  --ref main
  --sparse .agents/plugins
  --sparse plugin
  --json
```

Повторный запуск использует `codex plugin marketplace upgrade rag-reviewer --json`, затем
идемпотентный `plugin add`.

### Варианты

- `reviewer install codex --no-skills` — только MCP plan; Codex plugin команды не вызываются.
- `reviewer install codex --dry-run` — local read-only discovery и печать MCP/marketplace/plugin
  plan; add/upgrade/install и сетевые вызовы запрещены.
- `reviewer install-skills codex` — тот же plugin lifecycle без MCP mutation.
- `reviewer install-skills codex --dry-run` — plugin-only read-only plan.
- `reviewer install-skills codex --path` отклоняется: Codex plugin не является standalone
  skills directory.
- `reviewer install codex --path ...` с plugin lifecycle отклоняется как неоднозначный;
  custom MCP path допустим вместе с `--no-skills`.
- Ошибка Codex target в `reviewer install --all` делает общий exit non-zero. Успехи других
  targets показываются, но не маскируют ошибку.

### `reviewer init`

После успешной записи `.env` интерактивный wizard спрашивает, установить ли Codex. При
согласии вызывает тот же orchestration helper, что `reviewer install codex`. Никакой второй
реализации lifecycle нет. `init --yes` сохраняет текущий неинтерактивный контракт и только
пишет `.env`.

## Marketplace state и конфликты

Marketplace name — `rag-reviewer`. Если marketplace отсутствует, выполняется add. Если имя и
source совпадают, выполняется upgrade. Если имя уже связано с другим local/Git source,
installer останавливается до MCP/plugin mutation и печатает найденный source и команды для
ручного устранения конфликта. Автоматически удалять или заменять чужой marketplace нельзя.

JSON output парсится по обязательным полям, но неизвестные дополнительные поля допускаются.
Malformed JSON, отсутствующий marketplace root, несовпадающий plugin name или версия считаются
ошибкой preflight/verification.

## Snapshot verification

До `plugin add` installer читает только root, возвращённый публичным
`codex plugin marketplace list --json`, и проверяет:

- `./plugin/.codex-plugin/plugin.json` существует и валиден;
- name равен `rag-reviewer`, version соответствует release identity;
- Codex manifest не содержит `mcpServers`;
- каждый непосредственный каталог `plugin/skills/*` с `SKILL.md` попадает в inventory;
- `_common` не регистрируется как skill, но его файлы входят в payload;
- вложенные references, hooks и указанные assets существуют;
- source не выходит за marketplace root;
- payload hash совпадает с token manifest version.

Проверка не читает и не меняет внутренний plugin cache Codex.

## Transaction и rollback

До первой mutating-команды сохраняется точная копия `config.toml` в отдельный backup с
timestamp. Если исходного файла не было, transaction запоминает это и удаляет созданный файл
при rollback.

При ошибке после начала mutation:

1. Восстановить исходный `config.toml` либо удалить вновь созданный.
2. Повторно вызвать локальный `plugin list --json` и проверить прежнюю selection, если она была.
3. Не запускать legacy migration.
4. Вернуть non-zero и напечатать причину, backup path и manual recovery command.

`codex plugin add` рассматривается как атомарная публичная операция: при его non-zero предыдущая
installed selection должна оставаться рабочей. Marketplace snapshot после upgrade может остаться
обновлённым, но он не активирует plugin сам по себе. Если `plugin add` вернул success, а
post-verification не прошла, восстановление `config.toml` возвращает прежнюю selection; inert
cache/snapshot не удаляется напрямую.

Legacy migration выполняется последней. Поэтому rollback основного lifecycle не должен
восстанавливать перемещённые skills.

## Legacy migration

Сканируется только `$CODEX_HOME/skills`. Кандидаты ограничены именами из проверенного
`plugin/skills` payload, включая reviewer-owned `_common`.

Каталог считается положительно идентифицированным, если выполняется одно условие:

1. Корневой `.reviewer-skills.json` валиден, содержит ожидаемый source и hash каталога.
2. Все файлы каталога и их относительные пути побайтово совпадают с verified payload.

Лишний, отсутствующий или изменённый файл делает каталог неоднозначным. Такой каталог остаётся
на месте с предупреждением.

Идентифицированные каталоги атомарно перемещаются в:

```text
$CODEX_HOME/reviewer-legacy-backups/<UTC-timestamp>/skills/<name>/
```

Перемещение начинается только после полной plugin/MCP verification. Финальный вывод содержит
backup root и команду/инструкцию возврата. Никакие каталоги не удаляются.

## Release identity и cachebuster

Codex version имеет форму:

```text
<pyproject-version>+codex.<12-hex-payload-hash>
```

Пример: `0.2.27+codex.a1b2c3d4e5f6`.

Hash считается по всем regular files компактного `plugin/` в сортированном порядке как
`relative-path + NUL + bytes`. Поле `version` Codex manifest перед вычислением заменяется на
фиксированный sentinel, чтобы исключить self-reference. Временные файлы, VCS metadata и build
artifacts в payload не допускаются.

CI guard проверяет:

- base version обоих Codex manifests равна `project.version` из `pyproject.toml`;
- version содержит ровно один `+codex.` token;
- token равен вычисленному payload hash;
- root manifest является допустимым path-transformed представлением canonical manifest;
- icon copy и declared assets совпадают/существуют;
- marketplace source равен relative `./plugin`.

Любое изменение payload автоматически требует обновления version token и не может незаметно
переиспользовать stale cache.

## Ошибки и сообщения

Все ошибки содержат failed phase, argv без секретов, краткий stderr/JSON parse detail и следующий
шаг пользователя. Отдельно различаются:

- Codex не найден;
- `codex plugin` или нужный `--json` не поддерживается;
- marketplace source conflict;
- offline/fetch failure;
- invalid marketplace snapshot;
- MCP TOML нельзя безопасно обновить;
- plugin add failure;
- post-install verification failure;
- config rollback failure;
- ambiguous legacy skills.

Первые девять состояний, кроме ambiguous legacy skills, дают non-zero. Неоднозначные legacy
копии являются предупреждением: новый plugin уже установлен и проверен, а пользовательские файлы
сохранены.

## Тестирование

### Pure unit

- fresh/repeat/update/conflict/dry-run plans;
- точные argv, отсутствие shell, Windows paths и пути с пробелами;
- tolerant JSON parsing и обязательные поля;
- TOML add/update, nested reviewer tables, сохранение чужого текста и unsafe representations;
- payload hash/version и root/canonical manifest transform;
- stamp/hash/ambiguous legacy classification.

### Stateful fake Codex

Fake executable работает с временными `HOME`/`CODEX_HOME`, хранит marketplace/plugin state и
фиксирует каждый argv. Он моделирует list/add/upgrade/plugin add и возвращает реалистичный JSON.
CLI-тесты покрывают:

- `install codex` fresh и repeat/update;
- `--no-skills`, `--dry-run` и custom path rules;
- `install-skills codex` и его dry-run;
- `install --all` с частичным успехом;
- интерактивный `init` и `init --yes`;
- New Chat/new session и Reload Window output.

Dry-run тест отдельно запрещает любые network/mutating argv.

### Failure matrix

Инъекция ошибок выполняется на marketplace add/upgrade, snapshot verification, MCP write,
plugin add, post-verification, config restore и legacy migration. После каждого сценария
проверяются исходный config, прежняя plugin selection, отсутствие преждевременной migration,
non-zero exit и actionable recovery output.

### Packaging/CI

CI запускает unit и fake integration tests на Windows, macOS и Linux. Guard динамически считает
все `plugin/skills/*/SKILL.md`; `_common` и nested references присутствуют, но `_common` не
регистрируется как skill. Реальный сетевой Codex smoke — отдельный opt-in job и не блокирует
обычные PR без credentials/network.

## Документация

Обновляются README EN/RU, `AGENTS.md` и `plugin/README.md`:

- одна каноническая команда установки;
- отличие `install`, `install-skills`, `--no-skills` и `--dry-run`;
- fresh/update lifecycle и диагностика конфликтов;
- один глобальный MCP и отсутствие plugin-bundled MCP для Codex;
- безопасная legacy migration и rollback path;
- проверка `codex plugin list --json` и MCP registration;
- обязательный New Chat/new CLI session, для IDE — Reload Window;
- динамический актуальный inventory скиллов вместо числа, размноженного по документации.

## Критерий готовности

Фича готова, когда на трёх ОС fake integration matrix проходит fresh/repeat/update/failure
сценарии, packaging guard подтверждает compact payload/release identity, dry-run не выполняет
mutation/network, а успешная реальная установка показывает один reviewer MCP и enabled
`rag-reviewer` plugin со всеми namespaced skills в новом thread.
