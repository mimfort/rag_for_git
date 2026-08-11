# Brief — PRI-239 Репорт багов самого reviewer: анонимизированный issue в mimfort/rag_for_git

https://ru.yougile.com/team/686c049c8af8/#PRI-239

## Task
Встроенный канал обратной связи: заметив дефект **плагина/reviewer**, агент сообщает пользователю,
собирает **анонимизированный** отчёт и предлагает опубликовать issue в `mimfort/rag_for_git`.
Публикация — только после явного апрува; автономных публикаций нет ни в одном режиме
(headless/cron/фон — канал молчит). Плагин ставят на коммерческий код → в issue не должно попасть
ничего из репозитория пользователя.
Ключевые части: (1) фильтр «наш баг / не наш», (2) severity + порог шума и дедуп по сигнатуре,
(3) блок «Окружение» (форма без содержимого, урезаемый пользователем), (4) публикация через
gh-токен + фолбэк на ручную, (5) выключатель на уровне репо/деплоя.

## Related work
- PRI-238 — исходный триггер идеи (`finish_task` вернул `task_link_added: false` без warning);
  образец «три различимых исхода вместо одного bool» — тот же приём применим к статусу репорта.
- PRI-237 — пример модель-специфичного дефекта (эхо fingerprint ломалось только на дешёвой модели)
  → обоснование блока «Окружение» с моделью/тиром/режимом.
(dropped 0)

## Subsystems
- `reviewer/mcp` — сервисный слой MCP; сюда садится `report_bug`, здесь же `_safe_board_payload`
  и уже принятая практика вымарывания на границе.
- `reviewer/entrypoints` — `mcp_server.py`, регистрация тулов FastMCP; инвариант: логи только в stderr.
- `reviewer/policy` — `ReviewPolicy` из `.review.yml`; сюда добавляется выключатель канала.

## Relevant code
- `reviewer/tasks/boards/errors.py:19` — `sanitize_provider_text` / `sanitize_provider_payload`:
  готовое литеральное вымарывание секретов, переиспользуется санитайзером как последний проход.
- `reviewer/config/fetch_errors.py:24` — `classify_fetch_error`: канон бессекретной классификации
  (решение по форме исключения, не по тексту) — тот же принцип для триажа.
- `reviewer/mcp/service.py:1117` — `finish_task`: образец server-side lifecycle (resolve → write →
  write-through → безопасный payload).
- `reviewer/mcp/service.py:819` — `_backlink_pr`: образец fail-soft записи в VCS с тремя исходами.
- `reviewer/entrypoints/mcp_server.py:136` — регистрация тула + докстринг-контракт для LLM.
- `reviewer/vcs/github.py:10` — `GitHubProvider` (httpx + `_RetryTransport`); issue-API здесь нет,
  нужен отдельный минимальный клиент (VCSProvider — контракт про PR, расширять его не следует).
- `reviewer/policy/policy.py:18` — поля `ReviewPolicy` + `from_yaml`: точка для `bug_reports`.
- `reviewer/config/settings.py:38` — `Settings`: env-дефолт + `github_token`.
- `plugin/skills/finish-task/SKILL.md:1` — образец тонкого скилла над server-side lifecycle.
(dropped 0)

## Test exemplars
- `tests/skills/test_finish_task_skill.py:1` — guard-тесты промпта: позитивные ассерты формулировок
  контракта + негативные на provider-specific лексику.
- `tests/skills/test_common_blocks.py:11` — реестр `_common`-блоков (новый блок надо туда внести).
(dropped 0)

## Constraints / open questions
- Публикация под личным gh-токеном раскрывает ник пользователя в публичном репо → предупреждать
  при апруве и предлагать ручную альтернативу.
- Неинтерактивность должен гарантировать сервер, а не только промпт (скилл — не `try/finally`).
- `.venv/bin/pytest -q` + `scripts/update_codex_plugin_manifest.py --check` обязательны;
  README.md и README.ru.md — синхронно.

Собран на: inline (Opus, сессионная модель), режим: inline

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 99 · out 16.7K · cache-write 204.6K · cache-read 3.9M
Всего: 4.1M токенов
