# Brief — PRI-218 Глобальный интерактивный launcher reviewer без изменения CLI-контрактов
https://ru.yougile.com/team/686c049c8af8/#PRI-218

## Task

- Источник: reviewer store после sync; канонический ключ `ID-270`, alias `PRI-218`, статус «Движок (reviewer CLI/MCP)».
- Добавить в PyPI-пакет launcher: TUI открывается только без аргументов при интерактивных stdin и stdout; при любых аргументах dispatcher немедленно отдаёт управление существующему Click CLI.
- Контракт: `--help`, JSON, pipe/redirect, CI, `TERM=dumb`, неизвестная команда и `reviewer-mcp` не получают баннеров, ANSI, ожидания ввода или другого stdout/stderr/exit code.
- TUI даёт стрелочную навигацию, поиск, описание команды, редактор параметров с defaults/validation/secret masking и preview; после выбора закрывается и запускает существующую Click-команду.
- Приёмка из описания задачи: глобальные `uv tool install` и `uvx`, lazy imports, явное обновление, документация и cross-platform contract/regression tests.

## Related work

- PRI-210 / PR mimfort/rag_for_git#104 — переиспользовать паттерн global user-scope distribution, idempotent lifecycle и документацию без клона репозитория.
- PRI-122 / PR mimfort/rag_for_git#21 — сохранить тонкую CLI-оркестрацию и существующий onboarding `init` как отдельную Click-команду.
- PRI-136 — терминальный интерактивный триаж является ближайшим UX-прецедентом; проверить возможность общего backend/паттерна без смешивания с CLI dispatcher.

(dropped 9: PRI-98/208/116/124/157/147/188/206 и собственный ID-270 либо про другой механизм, либо не дают реализуемого паттерна launcher.)

## Subsystems

- reviewer/entrypoints — публичные Click CLI и FastMCP entry points; здесь критичен разделённый human/machine output contract.
- reviewer — composition и кроссплатформенная установка; содержит client registry и installer lifecycle.
- tests/entrypoints — `CliRunner`-контракты команд без реальной сети/хранилищ.
- tests/install — безопасные non-interactive/dry-run сценарии, временные home/config и plugin lifecycle.

## Relevant code

- reviewer/entrypoints/cli.py:47 — существующий объект Click `cli`; dispatcher обязан делегировать его без изменения маршрутов.
- reviewer/entrypoints/cli.py:378 — graph показывает, что `check` — одна из прямых команд группы; её exit/output контракт входит в blast radius dispatcher.
- reviewer/entrypoints/cli.py:502 — `status` — прямая команда той же группы; JSON-путь нельзя предварять TUI-выводом.
- reviewer/entrypoints/cli.py:547 — `install` уже использует Click arguments/options и lazy import `reviewer.install`; каталог/launcher не должен дублировать эту логику.
- reviewer/entrypoints/cli.py:828 — `init` имеет собственные prompts и `--yes`/`--dry-run`; launcher должен передавать их существующей команде, а не встраивать wizard.

(dropped 35: остальные retrieval-хиты — отдельные команды/внутренние installer details, не задающие entry dispatcher или TUI-каталог напрямую.)

## Test exemplars

- tests/services/test_status.py:125 — `CliRunner` проверяет `status --json` через `json.loads(res.output)`; добавить гарантию отсутствия TUI/ANSI до JSON.
- tests/services/test_status.py:85 — smoke-test обычного `status` задаёт существующий human-readable output contract.
- tests/install/test_install_wizard.py:200 — параметризованный `init --dry-run/--yes` утверждает отсутствие prompts, сети и provider setup; сохранить при dispatcher.
- tests/install/test_install_wizard.py:152 — `init --dry-run` проверяет redaction секрета и отсутствие записи; подходит для contract test прямого вызова.
- tests/install/test_claude_cli.py:168 — `install claude-code --dry-run` проверяет отсутствие config-write; образец для неинтерактивного install path.

(dropped 10: тесты миграции, allowlist и fake external CLI не покрывают launcher-dispatch или его совместимость напрямую.)

## Constraints / open questions

- Base index свеж: `drift=0` на `dev`; task corpus уже синхронизирован (107 unchanged).
- Store вернул пустой `criteria`, но описание содержит раздел «Критерии приёмки»; использовать его как источник acceptance requirements.
- `get_task_context(PRI-218)` не вернул linked tasks/PR/touched code; related work получен из semantic search, а PR #104 просмотрен только как distribution precedent.
- Поиск кода не обнаружил текущий console-script dispatcher/packaging entry module с line-numbered snippet; определить его source of truth и кроссплатформенный способ проверки TTY на этапе design.
- Выбор Textual versus prompt_toolkit остаётся решением дизайна: оценить wheel size, startup и Windows before adding dependency; TUI import обязан оставаться вне hot path `reviewer-mcp` и прямых subcommands.

Собран на: mid/gpt-5.6-terra, режим: subagent
