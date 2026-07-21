# PRI-172: Path-level guard для solve-task brief

## Цель

Добавить дешёвую детерминированную проверку, которая после записи solve-task brief помечает
`path:line` ссылки на файлы, отсутствующие в code-retrieval выдаче текущего solve-task окна.
Guard снижает риск передачи выдуманного пути в planning, но намеренно не валидирует номер строки
и не заменяет общую проверку структуры brief из PRI-187.

## Scope

- Новый stdlib-only guard `plugin/hooks/brief_guard.py` и same-process wrapper
  `plugin/hooks/brief_post_write.py`.
- Один handler в существующем matcher `PostToolUse/Write`: wrapper последовательно вызывает
  `brief_cost.run(payload)`, затем `brief_guard.run(payload)`.
- Проверяются только секции `## Relevant code` и `## Test exemplars`.
- Неподтверждённая цитата сохраняется и получает inline-маркер
  `⚠️ [файл не в результатах поиска]`.
- Механический guard доступен только в Claude Code plugin hooks. В клиентах без
  `PostToolUse/Write` остаётся prompt-контракт solve-task.

Не входят в задачу: line-level validation, schema/caps validation всего brief, новый config-флаг,
рефакторинг `brief_cost.py`, изменение solve-task skill и cross-CLI hook emulation.

## Архитектура

Claude Code запускает matching hook handlers параллельно. Поэтому `hooks.json` сохраняет один
`Write` matcher и одну команду `brief_post_write.py`; это не две последовательно
зарегистрированные команды. Wrapper один раз читает payload и в том же процессе вызывает cost,
затем guard. Каждый вызов окружён собственным `try/except`, поэтому сбой cost не пропускает guard,
а сбой guard не меняет успешный результат cost; wrapper всегда возвращает 0.

`brief_guard.py` остаётся отдельно тестируемым executable stdlib-модулем с pure-функциями
парсинга и тонкой fail-open orchestration `run(payload)`. Он не требует `.review.yml`; отсутствие
доверенного контекста, schema mismatch или исключение означает no-op.

Есть два transcript path. Path B (main) допускается только после последнего user marker с
`skills/solve-task` и `Base directory for this skill:`. В Path A transcript сабагента может не
быть marker, а все rows имеют `isSidechain=true`; такой markerless запуск допускается только при
точном provenance текущего `Write`: совпадают payload `agent_id`, `tool_use_id`, `tool_name` и
`tool_input.file_path`, а найденная assistant row имеет тот же `agentId`,
`attributionSkill=rag-reviewer:solve-task` и соответствующий `Write` tool block. Поля
`agentId`/`attributionSkill` наблюдались в Claude Code 2.1.209 и не считаются стабильным публичным
контрактом, поэтому при schema drift markerless path закрывается fail-closed.

## Поток данных

1. Из `payload.tool_input.file_path` берётся путь записанного файла. Путь вне
   `docs/superpowers/briefs/` немедленно отбрасывается.
2. JSONL из `payload.transcript_path` читается tolerant-образом максимум три раза, пока не
   появится assistant `tool_use` с exact `payload.tool_use_id`; между попытками пауза 50 ms.
   Пустой id или timeout дают no-op. Текущий user `tool_result` для `Write` не требуется.
3. Для Path B выбирается последнее solve-task окно. Если marker отсутствует, применяется
   fail-closed provenance-проверка Path A; при успехе рассматривается весь markerless transcript.
   Sidechain rows не фильтруются ни в одном path.
4. Evidence принимается только из user `tool_result`, чей `tool_use_id` связан с предшествующим
   assistant `tool_use` из allowlist. Разрешены direct/plugin prefixes `mcp__reviewer__` и
   `mcp__plugin_rag-reviewer_reviewer__` для `search_codebase`, `related_symbols`, `callers`,
   `implementations`, `definition`. Bash, task/обычный text, orphan results, near-prefix и другие
   tools отвергаются.
5. `tool_result.content` поддерживается как строка и как список text-блоков. Если строка является
   FastMCP JSON envelope с string-полем `result`, evidence берётся из этого поля.
6. Evidence path извлекается только из code/graph headers вида
   `// path#fqn (path:start-end)` или `path#fqn (path:line)`. Произвольный `path:line` внутри
   task/search text не подтверждает цитату.
7. Brief обрабатывается построчно. Состояние проверки включается после точного заголовка
   `## Relevant code` или `## Test exemplars` и выключается на следующем заголовке `## `.
8. В проверяемых строках извлекаются цитаты формата `path.ext:line`. Для каждой цитаты
   выполняется нормализация и matching. Неподтверждённая цитата получает inline-маркер.
9. Файл атомарно заменяется через временный файл только если текст реально изменился.

## Matching путей

Нормализация заменяет `\\` на `/`, удаляет начальный `./` и повторные `/`. Основное правило —
точное совпадение repository-relative путей.

Suffix-match нужен только для observed path с внешним абсолютным/worktree-префиксом:
`/tmp/worktree/reviewer/index/store.py` подтверждает `reviewer/index/store.py`. Он разрешён лишь
когда цитата содержит `/`. Поэтому голая цитата `utils.py` не подтверждается найденным
`a/utils.py`; root-level `utils.py` подтверждается только точным observed path `utils.py`.

Номер строки извлекается для определения цитаты, но не участвует в matching.

## Идемпотентность

Маркер добавляется непосредственно после конкретной цитаты. Перед вставкой guard проверяет текст
между концом match и концом строки; если там уже начинается тот же маркер, повторной вставки нет.
Это поддерживает строки с несколькими цитатами и повторный PostToolUse запуск без накопления
warning-текста.

## Fail-open и диагностика

Guard возвращает exit code 0 при любом исходе. Файл не меняется, если:

- путь не относится к brief-каталогу;
- нет transcript, текущего `Write` tool use или читаемого brief;
- нет последнего solve-task marker и exact markerless Path A provenance;
- после фильтрации нет ни одного evidence path;
- любой доверенный tool result содержит hard-truncation sentinel `[truncated]`,
  `[...truncated]` или `[…truncated]`;
- чтение, парсинг или атомарная запись завершились исключением.

Сообщения `(граф недоступен)` и `(ничего не найдено)` не считаются ошибкой и просто не добавляют
evidence. Adaptive cliff/rails notes и похожие не-sentinel сообщения не отключают guard.

При `BRIEF_GUARD_DEBUG=1` hook печатает в stderr нормализованные observed, cited и missing paths.
Без флага stdout/stderr остаются пустыми.

## Тестирование

Новый `tests/hooks/test_brief_guard.py` загружает скрипт через `importlib` без создания bytecode и
проверяет:

- exact linkage user result к allowlisted direct/plugin tool use, FastMCP unwrap и отказ от
  arbitrary/Bash/orphan/near-prefix evidence;
- Path B по последнему marker и markerless Path A по exact current-Write provenance, включая
  transcript, состоящий из sidechain rows;
- bounded freshness polling, no-op без текущего tool use и отсутствие требования current
  `tool_result`;
- извлечение evidence headers;
- exact match и suffix-match абсолютного префикса;
- отказ принимать basename вложенного файла и корректность для двух `utils.py`;
- проверку только `Relevant code`/`Test exemplars`;
- inline marking отсутствующего пути, несколько цитат и идемпотентность;
- no-op вне briefs, без trusted context/evidence и при трёх hard-truncation sentinel;
- продолжение guard при cliff/rails notes и игнорирование truncation из недоверенного tool;
- tolerant parsing битого JSONL и fail-open orchestration;
- debug output в stderr;
- регистрацию единственного wrapper в `hooks.json`.

`tests/hooks/test_brief_post_write.py` отдельно фиксирует same-payload порядок cost → guard,
независимый fail-open обоих вызовов и malformed stdin. Регрессия в
`tests/hooks/test_brief_cost.py` также требует ровно один registered wrapper. Проверки выполняются
unit-командами без сети и инфраструктуры, затем запускается Ruff для изменённых Python-файлов.

## File scope

- Созданы `plugin/hooks/brief_guard.py`, `plugin/hooks/brief_post_write.py`,
  `tests/hooks/test_brief_guard.py` и `tests/hooks/test_brief_post_write.py`.
- Изменены `plugin/hooks/hooks.json` и registration regression в
  `tests/hooks/test_brief_cost.py`.
- Обновления `.codex-plugin/plugin.json` и `plugin/.codex-plugin/plugin.json` являются
  packaging/generated consequence нового plugin content, а не дополнительной runtime-логикой
  guard.

## Критерии приёмки

- Подтверждённые code/test citations остаются без изменений.
- Неподтверждённые citations получают ровно один inline-маркер.
- Ссылки вне двух code/test секций не меняются.
- Неоднозначный basename не подтверждается suffix-match вложенного файла.
- Недостаток или усечение evidence не портит brief.
- Существующий `brief_cost` продолжает работать без изменений.
- Все новые и существующие unit-тесты и Ruff проходят.
