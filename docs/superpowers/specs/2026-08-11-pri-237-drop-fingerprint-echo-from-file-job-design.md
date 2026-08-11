# PRI-237 — summarize-subsystems: убрать эхо fingerprint из промпта file-job'а

Источник: `docs/superpowers/briefs/2026-08-11-PRI-237-drop-fingerprint-echo-from-file-job.md`
Задача: https://ru.yougile.com/team/686c049c8af8/#PRI-237

## Проблема

Шаг 5.2 скилла `summarize-subsystems` предписывает передавать file-job'у fingerprint файла и требовать
его обратно в результате `{path, fingerprint, summary, provenance}`. Субагент не может вычислить это
значение: skeleton-fingerprint считает сервер и отдаёт в ответе `get_subsystem_summary_work`. Для LLM
дословный перенос 64 hex-символов — не копирование, а генерация: у случайной hex-строки нет семантики,
модель уверенно порождает строку правильной формы вместо исходной. Прогон 11.08.2026 (Haiku, ветка dev,
depth=2, 9 кластеров `reviewer/*`, 82 файла) дал ≥8 неверных fingerprint, один — неверной длины.

Авторитетные пары `path → fingerprint` всё это время лежат у оркестратора — он сам вызывает
`get_subsystem_summary_work`. Значение делает петлю из надёжного места в ненадёжное и возвращается хуже;
job'у для его работы (написать сводку файла) оно не нужно вовсе.

Риск: выдуманный fingerprint уходит в `index_subsystem_summary`. В лучшем случае строгая optimistic-проверка
отвергает бандл и проход теряется; в худшем фиксируется неверная свежесть файла, и его сводка перестаёт
обновляться при последующих правках.

## Границы

Правка **чисто промптовая**. Серверный контракт не меняется: `index_subsystem_summary` по-прежнему
принимает `fragments: list[SummaryFragmentIn]` с полем `fingerprint` и выполняет строгую
optimistic-проверку (`reviewer/entrypoints/mcp_server.py:374-380`), а `get_subsystem_summary_work`
по-прежнему отдаёт авторитетные пары `{path, fingerprint}` (`reviewer/mcp/service.py:1733`). Меняется
только то, **откуда** оркестратор берёт fingerprint при сборке fragments: из ответа сервера, а не из
ответа субагента. Миграций и изменений схемы нет.

## Дизайн

### 1. `plugin/skills/summarize-subsystems/SKILL.md`, шаг 5.2

- Убрать вставку `(plus that entry's fingerprint)`.
- Требуемый от job'а результат: `{path, summary, provenance}`.
- Добавить явный запрет: job никогда не вычисляет, не угадывает и не возвращает `fingerprint` —
  значение серверное, его подставляет оркестратор.
- Рассинхрон `path` (единственное поле, которое job теперь может исказить): результат с `path` вне
  pending-списка отбрасывается, job перевызывается для этой pending-записи один раз; при повторном
  несовпадении кластер считается `deferred` и для него ничего не персистится.
- Фраза `file prompt must name only its own path` сохраняется дословно — её уже ассертит существующий
  `test_skill_composes_only_from_ordered_fragment_texts`.

### 2. Там же, шаг 5.3

Добавить одно предложение: собирая новые file results, оркестратор проставляет каждому фрагменту
`fingerprint` join'ом по `path` с авторитетными записями `added_files` / `changed_files` из ответа
`get_subsystem_summary_work` — и никогда из ответа субагента.

### 3. Шаг 5.4 — не меняется

Строка вызова `index_subsystem_summary(repo, branch, cluster_key, title, summary, source_hash,
fragments=[new file results])` дословно закреплена тестом `test_skill_persists_new_fragments_and_defers_races`;
семантика персиста прежняя.

### 4. `tests/skills/test_summarize_subsystems.py` — guard-тесты

Хелпер вырезает срез шага 5.2 (от `Let pending work be exactly` до начала пункта 5.3) поверх
существующего `_assembled_skill()`.

- `test_skill_file_job_does_not_echo_fingerprint`:
  - позитив — в срезе есть `{path, summary, provenance}` и фраза запрета job'у;
  - негатив — в срезе нет старых конструкций `plus that entry's fingerprint` и `{path, fingerprint`.

  Негатив точечный, по старым формулировкам, а не `"fingerprint" not in срез`: сама фраза запрета
  содержит это слово. Легитимные упоминания fingerprint на строках 45 и 144 (сжатый listing без
  fingerprint'ов; описание гранулярности инвариантов) при этом не затрагиваются.
- `test_skill_orchestrator_supplies_fingerprints`: подстановка приписана оркестратору, источником
  назван `get_subsystem_summary_work`.

### 5. Ревизия прочих скиллов (шаг 4 задачи) — выполнена, кода не порождает

- Греп `fingerprint|source_hash|layout_token` по `plugin/skills/**/*.md` вне summarize-subsystems даёт
  единственный хит `sync-tasks/SKILL.md:42` (`filter_fingerprint` в списке полей отчёта) — не эхо через
  субагента.
- `pr-walkthrough` субагентов не диспатчит вовсе → анти-паттерна нет.
- `review-pr`: единственное эхо server-side идентификатора — id находки в
  `submit_verdicts(repo, pr, verdicts=[{id, is_real}])`. Риск качественно другой: id короткий и
  семантичный (`f1`, `f2`, … — `reviewer/mcp/service.py:2411`), сервер игнорирует неизвестный id с
  warning, а отсутствие вердикта трактуется как keep (`reviewer/mcp/service.py:2429-2446`), то есть
  безопасный дефолт. Правки не требует; отдельная задача не заводится.

### 6. Манифесты и прогон

Правка контента под `plugin/` меняет codex payload-digest: `python scripts/update_codex_plugin_manifest.py`,
затем `--check` (exit 0). Финально `.venv/bin/pytest -q`.

## Критерии приёмки

1. Шаг 5.2 SKILL.md не упоминает fingerprint ни во входе file-job'а, ни в требуемом от него результате.
2. SKILL.md явно предписывает оркестратору брать fingerprint только из `get_subsystem_summary_work`.
3. Новые guard-тесты краснеют при возврате старой формулировки; существующие тесты
   `tests/skills/test_summarize_subsystems.py` остаются зелёными.
4. `python scripts/update_codex_plugin_manifest.py --check` → exit 0; `.venv/bin/pytest -q` зелёный.
5. Серверный контракт `index_subsystem_summary` не меняется — миграций и изменений схемы нет.

## Тестирование

Только unit: guard-тесты по тексту скилла (`tests/skills/`) + существующий набор. Инфраструктура
(Postgres/Neo4j/Voyage) не нужна, integration-тесты не затрагиваются.
