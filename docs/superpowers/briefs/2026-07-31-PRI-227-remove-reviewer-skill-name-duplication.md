# Brief — PRI-227 Убрать дублирование reviewer из имён skills
https://ru.yougile.com/team/686c049c8af8/#PRI-227

## Task

- Источник: reviewer store после sync; канонический ключ `ID-281`, alias `PRI-227`, статус «Бэклог».
- Сохранить namespace `rag-reviewer`, но атомарно заменить каждый frontmatter `name: reviewer_*` на basename каталога (`ask`, `review-pr`, `solve-task`, включая будущий `decompose-task`).
- Обновить cross-skill references, EN/RU README, CLAUDE.md, AGENTS.md, plugin README, hook attribution и test fixtures без смешения старой и новой схемы.
- Добавить единый guard: имя frontmatter равно имени каталога, уникально и не начинается с `reviewer_`.
- Проверить Claude/Codex payload и установку; описать breaking migration, cache/plugin update, New Chat/new CLI session и Reload Window для IDE.
- Критерии в описании задачи, а не отдельном `criteria[]`; решение об alias допустимо только если нет двойного отображения.

## Related work

- PRI-143 — использовать прецедент унификации dimension-skills для атомарного удаления общего boilerplate во всех skill-поверхностях.
- PRI-210 / PR mimfort/rag_for_git#104 — сохранить data-driven inventory `plugin/skills/*/SKILL.md` и проверку общего Claude/Codex payload при миграции имён.

(dropped 5: PRI-142/164/273/161/114 близки по prompt/docs/task-контексту, но не задают migration skill invocation names.)

## Subsystems

- tests/skills — статические проверки содержимого skill prompts и общих reference-блоков; место для одного structure guard.
- tests/install — фейковые Claude/Codex CLI и payload/install проверки без внешней инфраструктуры.
- plugin/hooks — attribution для `rag-reviewer:solve-task`; требуется обновить строку при новом invocation name.

## Relevant code

- reviewer/install_codex.py:591 — snapshot validation перечисляет директории с `SKILL.md`; сохранить динамический discovery, добавив/покрыв новый инвариант имён.
- reviewer/install_codex.py:637 — Codex payload обязан указывать `./skills/`, включать зарегистрированные skill directories и исключать `_common`.
- reviewer/install_codex.py:774 — legacy-skill migration сопоставляет каталоги payload и установленного набора; проверить влияние переименованных frontmatter на upgrade/cache path.
- reviewer/install.py:910 — tarball extraction возвращает имена каталогов, поэтому short invocation name должен следовать каталожному inventory, не отдельному registry.
- reviewer/install.py:986 — skill payload hash строится по каталогам и файлам; массовое переименование frontmatter требует регенерации/cachebuster и проверки обновления.
- reviewer/install_codex.py:1024 — Codex install верифицирует snapshot и установленный plugin до legacy migration; это blast radius для payload/manifest regression tests.

(dropped 9: низкоуровневые JSON/symlink/config helpers и generic MCP lifecycle не меняют naming contract напрямую.)

## Test exemplars

- tests/install/test_codex_plugin_payload.py:268 — динамически собирает каталоги `plugin/skills/*/SKILL.md` и проверяет README markers; заменить ожидания `reviewer_{name}` на short names и добавить общий guard.
- tests/skills/test_configure_review_skill.py:11 — точечная проверка старого `name: reviewer_configure-review`; заменить на общий test всех skills, а не новые per-skill assertions.
- tests/install/test_install.py:629 — tarball fixture подтверждает, что installer возвращает basename каталога (`review-pr`); использовать для consistency между payload и invocation.
- tests/install/test_skills_stamp.py:64 — fixture для stamp нескольких directory names; проверить обновление/установку mixed skill set после migration.

(dropped 11: tests snapshot hardening, CLI stamps и unrelated installer cases не проверяют display/invocation name непосредственно.)

## Constraints / open questions

- `get_task_context(PRI-227)` не вернул linked tasks или touched code; related work дополнено семантическим search и PR-104 diff.
- Поиск задачи вернул canonical `ID-281`; filename и heading сохраняют board key `PRI-227`.
- Нужно подтвердить реальный platform contract для alias: добавлять его только если Claude/Codex не показывают две команды; иначе документировать breaking rename.
- До изменения выполнить repo-wide поиск старых invocation strings, включая non-Python docs, hook metadata и fixtures: retrieval их покрывает неполно.

Собран на: mid/gpt-5.6-terra, режим: subagent
