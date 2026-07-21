# Brief — PRI-172 solve-task: path-level anti-hallucination guard для brief (hook)
https://ru.yougile.com/team/686c049c8af8/#PRI-172

## Task
- Задача реальна: prompt-only правило не мешает LLM сослаться в брифе на файл, которого не было в retrieval-выдаче.
- Добавить stdlib-only `brief_guard.py`: после записи solve-task brief сверять его `path:line` цитаты с путями из tool results и помечать неподтверждённые.
- Guard должен быть fail-open, идемпотентным, ограниченным `docs/superpowers/briefs/` и иметь `BRIEF_GUARD_DEBUG=1`.
- Уточнение исходной постановки: это path-level warning, не доказательство корректности строки и не общий schema-validator брифа.
- Уточнение парсинга: извлекать текст из структурированных `tool_result` блоков в solve-task окне, а не считать доказательством любое совпадение в transcript.

## Related work
- PRI-209 — переиспользовать проверенный паттерн standalone PostToolUse-хука: окно solve-task, JSONL, sidechain-семантика, fail-open и идемпотентная правка brief.
- PRI-187 — не дублировать широкую верификацию структуры/caps брифа; PRI-172 отвечает только за grounding пути.
- (dropped 11: остальные найденные solve-task/anti-hallucination задачи не задают механизм path-level guard либо относятся к общей hygiene/наблюдаемости.)

## Subsystems
- `plugin/hooks` — существующий `brief_cost` задаёт архитектуру standalone PostToolUse-хука над solve-task transcript и brief-файлом.
- `tests/hooks` — готовые fixtures и проверки загрузки, fail-open, идемпотентности и регистрации hook-команд.

## Relevant code
- `plugin/hooks/brief_cost.py:98` — `_message_text` показывает поддерживаемые формы `message.content`; для guard нужен отдельный структурный обход `tool_result` блоков.
- `plugin/hooks/brief_cost.py:112` — `find_window_start` находит последнее solve-task окно по двум маркерам; семантику следует сохранить.
- `plugin/hooks/brief_cost.py:196` — `_under_briefs` ограничивает любые in-place изменения каталогом brief-артефактов.
- `plugin/hooks/brief_cost.py:221` — `_read_jsonl` задаёт tolerant JSONL parsing с пропуском битых строк.
- `plugin/hooks/brief_cost.py:253` — `run` показывает fail-open orchestration и получение `file_path`/`transcript_path` из hook payload.
- (dropped 3: MCP service, install verification и config-флаг token cost не участвуют в grounding путей.)

## Test exemplars
- `tests/hooks/test_brief_cost.py:11` — динамическая загрузка standalone hook без записи `__pycache__`.
- `tests/hooks/test_brief_cost.py:128` — fixture формата main/sidechain transcript; sidechain rows нельзя исключать, потому что Path A целиком записывается как sidechain.
- `tests/hooks/test_brief_cost.py:185` — минимальный JSONL transcript fixture для unit/e2e сценариев.
- `tests/hooks/test_brief_cost.py:196` — end-to-end паттерн `payload → run → изменённый brief`.
- `tests/hooks/test_brief_cost.py:222` — no-op для файла вне `docs/superpowers/briefs/`.
- `tests/hooks/test_brief_cost.py:245` — повторный запуск проверяет идемпотентность.
- `tests/hooks/test_brief_cost.py:263` — guard-тест регистрации PostToolUse `Write`; итоговый config должен содержать одну команду-wrapper, а не два параллельных handler.
- (dropped 5: token formatting/config и Codex payload tests не определяют поведение path guard.)

## Constraints / open questions
- [refined_scope] Guard предупреждает только об отсутствующем пути; line-level grounding намеренно остаётся вне задачи из-за несовместимых форматов диапазонов.
- [refined_scope] Источником истины должны быть tool results текущего solve-task окна; user/assistant prompt text нельзя принимать за retrieval evidence.
- [refined_scope] Сверять нормализованный repo-relative путь с точным совпадением либо suffix относительно абсолютного префикса; голый basename не должен подтверждать неоднозначные `utils.py`.
- [refined_scope] Проверять только `## Relevant code` и `## Test exemplars`; Path B требует последний solve-task marker, а markerless Path A разрешён лишь при exact provenance текущего `Write` по payload и assistant tool block с `attributionSkill=rag-reviewer:solve-task`.
- [refined_scope] Sidechain rows участвуют в evidence. Принимать только связанный user `tool_result` разрешённого reviewer retrieval tool; FastMCP JSON string `result` разворачивать, а Bash, task text и произвольные results отвергать.
- [refined_scope] До трёх раз ждать появления текущего assistant `Write` tool_use в transcript; timeout означает no-op, связанный tool_result самого `Write` не требуется.
- [refined_scope] Любой hard-truncation sentinel `[truncated]`, `[...truncated]` или `[…truncated]` в доверенном evidence означает no-op; cliff/rails notes не отключают guard.
- [refined_scope] Claude запускает matching hook handlers параллельно, поэтому `hooks.json` регистрирует один `brief_post_write.py`, который в одном процессе последовательно и независимо fail-open вызывает cost, затем guard.
- [platform_limit] `hooks.json` защищает Claude Code `PostToolUse/Write`; у Codex/OpenCode такой hook-контракт отсутствует, это нужно явно задокументировать, а не обещать cross-CLI enforcement.
- [overlap] PRI-187 остаётся отдельной задачей про полноту/форму brief; не расширять PRI-172 до schema gate.
- Task отсутствовал в reviewer store после sync, прочитан через YouGile fallback и проиндексирован; связанных PR у PRI-172 нет.
- Base-index `dev` обновлён перед сбором брифа: drift 0, SCIP-граф доступен; сводки подсистем и task corpus доступны.

Собран на: premium (gpt-5.6-sol), режим: inline
