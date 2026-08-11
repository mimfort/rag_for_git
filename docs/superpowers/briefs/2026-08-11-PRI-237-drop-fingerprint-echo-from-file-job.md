# Brief — PRI-237 summarize-subsystems: убрать эхо fingerprint из промпта file-job'а
https://ru.yougile.com/team/686c049c8af8/#PRI-237

## Task
- Скилл `summarize-subsystems` (шаг 5.2) требует передавать fingerprint файла в промпт субагента и получать его обратно в `{path, fingerprint, summary, provenance}`.
- Субагент не может вычислить это значение (skeleton-fingerprint считает сервер и отдаёт в `get_subsystem_summary_work`) → для LLM это генерация 64 hex-символов, а не копирование. Прогон 11.08.2026 (Haiku, dev, depth=2, 9 кластеров, 82 файла): ≥8 неверных fingerprint, один — неверной длины.
- Авторитетные пары `path → fingerprint` уже у оркестратора (ответ `get_subsystem_summary_work`); значение делает петлю в ненадёжное место и возвращается хуже. Job'у оно не нужно.
- Риск: выдуманный fingerprint уходит в `index_subsystem_summary` → в лучшем случае бандл отвергается и проход теряется, в худшем — фиксируется неверная свежесть и сводка файла перестаёт обновляться.
- Критерии: (1) шаг 5.2 не упоминает fingerprint ни во входе, ни в результате file-job'а; (2) SKILL.md явно предписывает оркестратору брать fingerprint только из `get_subsystem_summary_work`; (3) guard-тест краснеет при возврате старой формулировки; (4) `update_codex_plugin_manifest.py --check` → exit 0 и `pytest -q` зелёный; (5) серверный контракт `index_subsystem_summary` не меняется — правка чисто промптовая, без миграций.

## Related work
- ID-287 — «перевести summarize-subsystems на сжатое перечисление, проверить цикл на крупном репо»: предыдущая правка того же SKILL.md + его guard-тестов, задаёт образец, как менять скилл вместе с контрактными тестами.
- ID-280 — «сделать subsystem summaries инкрементальными на уровне файлов»: ввела сам протокол fragments/fingerprint, который сейчас правится (источник формулировки шага 5.2).
- ID-165 — «freshness по структуре, а не по содержимому»: определила skeleton-fingerprint как серверную величину — прямое обоснование, почему job не может её вычислить.
(dropped 5: ID-291 — сама задача; ID-166/173/283/167 — про depth, поле `stale`, пагинацию listing и векторизацию сводок: соседняя область, другой механизм, промпта file-job'а не касаются.)

## Subsystems
- `reviewer/index` — `chunker.symbol_skeleton_hash` (скелет-хэш = «сигнатура + docstring») и `summary_store` (идемпотентный upsert, атомарный `commit_summary_bundle`): подтверждает, что fingerprint вычисляется и валидируется серверно.
- `plugin/hooks` — хуки brief (fail-open, изоляция ошибок): соседняя plugin-подсистема, правкой не затрагивается.
- `reviewer/entrypoints` — `mcp_server.py` регистрирует `list_subsystem_clusters` / `index_subsystem_summary`; контракт этих тулов по задаче остаётся неизменным.
(остальные вернувшиеся сводки — про ревью PR/поиск, к задаче не относятся)

## Relevant code
- `plugin/skills/summarize-subsystems/SKILL.md:86-87` — «Each file prompt must name only its own path (plus that entry's fingerprint) … require one Russian result: `{path, fingerprint, summary, provenance}`» — правится: убрать скобку и `fingerprint` из требуемого результата, добавить явный запрет job'у выдумывать/возвращать fingerprint.
- `plugin/skills/summarize-subsystems/SKILL.md:88-96` (шаг 5.3, «ordered reused/moved/new fragment texts») — место, где оркестратор мержит `reused_fragments` + `moved_files` + новые результаты; здесь закрепить подстановку authoritative `path → fingerprint` из ответа `get_subsystem_summary_work`.
- `plugin/skills/summarize-subsystems/SKILL.md:97-103` (шаг 5.4, `index_subsystem_summary(..., fragments=[new file results])`) — потребитель fingerprint'ов; формулировка о персисте должна остаться совместимой с существующим guard-тестом (точное совпадение строки вызова).
- `plugin/skills/summarize-subsystems/SKILL.md:45` и `:144` — прочие упоминания fingerprint (сжатый listing без fingerprint'ов; «fingerprint granularity» в описании инвариантов): менять не требуется, но проверить, что новый guard-грep не ловит их ложно.
- `tests/skills/test_summarize_subsystems.py:108-126` — `test_skill_uses_incremental_file_summary_protocol` и `test_skill_composes_only_from_ordered_fragment_texts` (последний уже ассертит `"file prompt must name only its own path"`): ближайшее место для нового guard-теста; грепов по `fingerprint` в файле сейчас нет — контракт не закреплён.
- `scripts/update_codex_plugin_manifest.py` — обязательный прогон после правки контента под `plugin/` (payload-digest), затем `--check` (exit 0).
- Греп `fingerprint|source_hash|layout_token` по `plugin/skills/**/*.md` вне summarize-subsystems: единственный хит — `sync-tasks/SKILL.md:42` (`filter_fingerprint` в списке полей отчёта, не эхо через субагента). Прямых аналогов анти-паттерна в `review-pr`/`pr-walkthrough` по этим ключам нет.
(dropped 0)

## Test exemplars
- `tests/skills/test_summarize_subsystems.py:108` `test_skill_uses_incremental_file_summary_protocol` — паттерн guard-теста: `_assembled_skill()` (сборка SKILL.md с развёрнутыми `_common`-инклюдами) + набор `assert "<точная фраза>" in text`.
- `tests/skills/test_summarize_subsystems.py:119` `test_skill_composes_only_from_ordered_fragment_texts` — уже ассертит фразу, которую задача правит («file prompt must name only its own path»); при переформулировке шага 5.2 фразу надо либо сохранить дословно, либо обновить тест синхронно.
- `tests/skills/test_summarize_subsystems.py:127` `test_skill_persists_new_fragments_and_defers_races` — пример ассерта по нормализованному тексту (`" ".join(text.split())`) для многострочной сигнатуры вызова.
(dropped 0)

## Constraints / open questions
- Правка **только промптовая**: серверный контракт `index_subsystem_summary`/`get_subsystem_summary_work` не меняется, миграций и изменений схемы нет.
- Открытый вопрос (шаг 4 задачи): полная проверка остальных скиллов на анти-паттерн «эхо server-side идентификатора через субагента» — греп по `fingerprint|source_hash|layout_token` чист, но другие идентификаторы (например, id/хэши в `review-pr`) не проверялись; решить в брейншторме — чинить здесь или выносить отдельной задачей.
- Открытый вопрос: нужен ли негативный guard (грeп, что в шаге 5.2 fingerprint отсутствует) в дополнение к позитивному, и как не поймать легитимные упоминания на строках 45/144.
- Не забыть: `python scripts/update_codex_plugin_manifest.py` + `--check`, иначе install-тесты краснеют.
- Индекс свеж (drift 0 на `dev`, 6474 чанка, 40 сводок); корпус задач прогрет (`sync_board`: 94 задачи, 1 обновлена).
- Родственная ловушка того же класса (из контекста задачи): дешёвые модели не вызывают deferred MCP-тул `index_subsystem_summary`, а печатают вызов текстом — персист обязан делать оркестратор; скилл это уже требует, отдельной правки не нужно.

Собран на: session-модель (Opus 5), режим: inline

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 35 · out 9.5K · cache-write 165.9K · cache-read 1.2M
Всего: 1.4M токенов
