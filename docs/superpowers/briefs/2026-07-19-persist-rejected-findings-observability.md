# Brief — Персистить отклонённые находки (verify/gate) для наблюдаемости precision

_Board-less (карточку PRI сознательно не заводим сейчас — только бриф; см. Constraints).
Источник: ретроспектива по логам reviewer в БД (review_runs/findings/steps), 2026-07-19._

## Task
Сейчас `review_findings` хранит **только находки, дошедшие до публикации** (survived verify →
прошедшие gate → deduped). Находки, отклонённые авто-верификацией (`verify_rejected`) и гейтом
(`dropped_by_gate`), сохраняются лишь как **агрегатные счётчики** в `review_runs` — без файла,
строки, категории, сообщения и причины. Подтверждено запросом: `unpublished находок в
review_findings: 0`.

Цель: персистить отклонённые находки (с меткой исхода и, по возможности, причиной), чтобы
наблюдаемость видела **что именно и почему** режется — и можно было считать precision
верификации/гейта, ловить систематический шум генерации и отличать «verify режет галлюцинацию»
(хорошо) от «verify режет реальный баг» (false negative — плохо).

**Данные ретроспективы (актуальная plugin-эра, `model=claude-code`, 14 прогонов, 12.06–09.07):**
- Воронка: `analyzed=40 → kept=20 → verify_rejected=12` → **reject_rate ≈ 30%**.
- В `review_findings` — только опубликованные; **0 отклонённых**.
- Показательны прогоны `rag_for_git#63` (#58/#59/#60): 5 находок суммарно, `kept=0` — всё
  отфильтровано, и **непонятно что и почему**.
- В plugin-режиме серверного трейса/usage/cost нет (`review_steps` пуст у всех 14 plugin-прогонов —
  LLM крутится на клиенте) → persist отклонённых находок = **единственный** доступный серверу
  сигнал о работе верификации.

**Критерии приёмки (черновик — уточнить в brainstorming):**
- Отклонённые verify/gate находки записываются в `review_findings` с явной меткой исхода
  (`published` / `verify_rejected` / `gate_dropped` / `duplicate`).
- Наблюдаемость может показать разбор reject по категории/severity/файлу и агрегат precision.
- Миграция схемы идемпотентна и обратно совместима (старые строки → дефолтный исход `published`).
- `_record_history` остаётся fail-soft (гейт `REVIEW_HISTORY`), publish не падает при сбое истории.
- Юнит/интеграционные тесты на `_record_history` и `ReviewHistory` обновлены и проходят.

## Related work
- **PRI-127** [бэклог] «Петля обратной связи (учёба на resolved/dismissed)» — про исход
  **опубликованных** комментариев (resolved/dismissed человеком, GitHub threads). Другой конец
  воронки; данная задача — предпосылка (без persist отклонённых нельзя измерить, стоит ли PRI-127).
- **PRI-209** [done, PR #99] «Улучшения плагина после анализа трейса в БД» — прокинула метаданные
  прогона (`duration_ms`/`usage`/`total_cost`/`model`) и серверный `review_steps`. Занималась
  **прогоном и трейсом, не находками** — не пересекается; хорошо показывает паттерн доработки
  `_record_history` + миграции схемы.
- **PRI-144** [done] «Калибровка confidence» и **PRI-137** [бэклог] «Авто-извлечение находок в
  правила команды» — потребители этих данных; сейчас слепы.
- (dropped: PRI-131 why-trace комментариев — про UI грундинга, не про persist; PRI-156 structured
  outputs — уже даёт candidates/verdicts в сессии, база для этой задачи, но реализовывать нечего.)

## Subsystems
- `reviewer/mcp` — MCPReviewService: `publish_review` = детерминированный хвост (verify-фильтр →
  gate → dedup → assemble → publish → история). Здесь и теряются отклонённые.
- `reviewer/web` — веб-админка наблюдаемости: `ReviewHistory` (review_runs/findings/steps),
  REST API + SPA, агрегаты (cost, reject_rate, разбивка по категориям/severity). Потребитель схемы.
- `reviewer/agent` — assemble: `findings_rows` строятся только для дошедших до сборки.
- (dropped: policy, graph, index, retrieval — задача не трогает гейтинг-логику и ретрив, только
  учёт исхода после них.)

## Relevant code
- `reviewer/mcp/service.py:977-978` — `survived = [...verdicts.get(fid) is not False]`;
  `verify_rejected = len(candidates) - len(survived)` — отклонённые превращаются в **число**,
  сами объекты дальше не идут.
- `reviewer/mcp/service.py:990-991` — `kept = gate(...)`, `deduped`; `dropped_by_gate` (стр. 1053) —
  тоже только счётчик.
- `reviewer/mcp/service.py:1196-1199` — `rows = asm.findings_rows` (или те же с `published=False`
  при status=error) → в `review_findings` идут **только** survived+assembled. **Точка правки.**
- `reviewer/mcp/service.py:1062-1068` — вызов `_record_history(... list(s.candidates.values()),
  deduped ...)` — `candidates` уже передаются (для `findings_analyzed`), но по ним не строятся rows.
- `reviewer/agent/assemble.py:178-190` (`_row`), `280-341` (сборка `findings_rows`) — источник rows
  с флагами `published`/`inline`; расширить моделью исхода или строить rows отклонённых отдельно.
- `reviewer/web/schema.sql:36-52` — таблица `review_findings` (нет колонки исхода/причины) —
  добавить `outcome` (+опц. `reject_reason`) идемпотентной миграцией.
- `reviewer/web/history.py:107-115` (`finding_sql` INSERT), `224-240` (`get_run` SELECT),
  `298-396` (`stats`) — добавить колонки в запись/чтение; расширить агрегаты precision.
- `reviewer/mcp/schemas.py:108-114` — `VerdictIn.is_real: bool` (нет причины);
  `reviewer/mcp/service.py:908-921` — `submit_verdicts` хранит только булев → чтобы иметь
  **причину** reject, нужно расширить `VerdictIn` полем `reason` (расширяет scope — см. open q.).
- Blast radius: изменение схемы `review_findings` → `history.py` (INSERT/SELECT), `web/api.py`
  (отдаёт findings), фронтенд (`web/frontend`). Все правки аддитивны (новые опциональные колонки).

## Test exemplars
- `tests/web/` — тесты `ReviewHistory` (persist/read runs, findings, traces; идемпотентная схема) —
  паттерн для тестов новых колонок исхода.
- `tests/mcp/` — тесты `publish_review` (dry_run, submit_findings/verdicts, `_record_history`) —
  паттерн проверки, что отклонённые candidate попадают в rows с нужным исходом.
- (проверить точные файлы перед TDD — сверено по subsystem summaries, не по прямому чтению.)

## Constraints / open questions
- **Plugin-архитектура (важно):** reviewer работает как плагин для CLI (Cursor/Claude Code) — LLM на
  клиенте, серверных usage/cost/trace нет (`total_cost=None` у claude-code — норма, не баг). Задача
  **не должна закладываться** на серверный usage/cost; работает с тем, что сервер видит —
  candidates/verdicts из сессии.
- **Данные для валидации:** реальных plugin-прогонов мало (14), OpenRouter-эра (minimax и пр., 15
  прогонов) — устарела и в scope не входит; тестовые фикстуры `owner/repo*` (40) — отделять при
  анализе. minimax-стоимость/латентность — артефакт мёртвой архитектуры, не задача.
- **Open q. — причина reject:** verify-вердикт сейчас булев (`is_real`), причина словами нигде не
  хранится. Решить в brainstorming: (а) персистить только факт исхода (дёшево, без изменения
  протокола) или (б) расширить `VerdictIn.reason` (богаче, но меняет submit-контракт и промпты).
- **Open q. — объём/ретенция:** хранить полный `message`/`code_quote` отклонённой находки или только
  метаданные (риск роста БД на шумных прогонах); нужна ли ретенция/чистка.
- **Идемпотентность прогонов:** повторные ревью одного PR (`rag_for_git#99` — 3 прогона) → отклонённые
  находки не должны плодить дубликаты в истории иначе, чем опубликованные (fingerprint уже есть).
- Ревизия бэклога (отдельный трек этой сессии) показала, что PRI-127/137/144-подобные «улучшения
  качества» невозможно приоритизировать без этих данных — задача разблокирует объективную оценку.

---
Собран на: Opus (session model), режим: inline (одна сессия ретроспективы; Path B).

## Токены (этап solve-task)
Модель: claude-opus-4-8
fresh-in 130 · out 173.2K · cache-write 451K · cache-read 6.4M
Всего: 7.1M токенов
