# Spec — Персист отклонённых находок (verify/gate) для наблюдаемости precision

> Seed: `docs/superpowers/briefs/2026-07-19-persist-rejected-findings-observability.md`

## Проблема

`review_findings` хранит только находки, дошедшие до публикации (survived verify → прошедшие gate →
deduped). Отклонённые верификацией (`verify_rejected`) и гейтом (`gate_dropped`) сохраняются лишь как
агрегатные счётчики в `review_runs` — без файла, строки, категории, сообщения и причины. Подтверждено
запросом к БД: `unpublished находок в review_findings: 0`.

Из ретроспективы логов (актуальная plugin-эра, `model=claude-code`, 14 прогонов): воронка
`analyzed=40 → kept=20 → verify_rejected=12` (≈30% режется вслепую). В plugin-режиме серверного
трейса/usage/cost нет (LLM на клиенте) → персист отклонённых находок = **единственный** доступный
серверу сигнал о работе верификации/гейта.

Цель: персистить каждый кандидат с меткой терминального исхода и причиной отклонения, чтобы можно было
считать precision, находить систематический шум генерации и отличать «verify режет галлюцинацию»
(хорошо) от «verify режет реальный баг» (false negative — плохо).

## Скоуп

**В скоупе:** запись всех кандидатов в `review_findings` с полем исхода и причиной; причина reject от
верификатора (`VerdictIn.reason`) и от гейта (серверно-выведенная); миграция схемы; тесты.

**Вне скоупа (YAGNI):** панель в веб-дашборде и precision-агрегаты в `stats()`; ретенция/GC (объём
крошечный); usage/cost (plugin-архитектура — сервер их не видит).

## Ключевые решения (из brainstorming)

1. **Причина reject — client+server.** Расширяем `VerdictIn.reason` + verify-промпт, чтобы верификатор
   объяснял `is_real=false`. Богаче факта, ценой большего блит-радиуса (schemas + плагин-промпты).
2. **Таксономия исхода — полная воронка (6 состояний).** Сумма по исходам всегда сходится с числом
   кандидатов; ничего не теряется.
3. **Учёт исходов — отдельный юнит** `account_outcomes` в `reviewer/agent/` (не в `assemble`, не в
   `publish_review`): одна ответственность — сопоставить кандидату терминальный исход + строку.
4. **Данные — полностью.** Храним message/code_quote/category/severity/confidence отклонённых находок
   (это и есть то, что нужно разглядывать). Спец-ретенция не нужна.

## Архитектура

### Схема БД (`reviewer/web/schema.sql`)

Идемпотентная миграция:

```sql
ALTER TABLE review_findings ADD COLUMN IF NOT EXISTS outcome       TEXT;
ALTER TABLE review_findings ADD COLUMN IF NOT EXISTS reject_reason TEXT;
```

- **`outcome`** — авторитетное поле воронки, одно из:
  `published_inline | published_summary | verify_rejected | gate_dropped | deduped | already_posted`.
- **`reject_reason`** (nullable) — заполняется для:
  - `verify_rejected` → текст от верификатора (`VerdictIn.reason`);
  - `gate_dropped` → серверно-выведенная причина (сработавшее правило политики, напр.
    `confidence 0.30 < min 0.50`, `category 'style' disabled`, `path ignored`);
  - остальные исходы → `NULL`.
- **Обратная совместимость:** колонки `is_real`/`published`/`inline` продолжают заполняться
  консистентно (web API/фронт читают их). `outcome` — новое поле-истина, не заменяет старые.
- **Бэкфилл истории — best-effort** (в `schema.sql` или отдельным `UPDATE ... WHERE outcome IS NULL`):
  `published AND inline → published_inline`; `published AND NOT inline → published_summary`;
  исторические `NOT published` (неоднозначны: и `already_posted`, и находки error-прогонов) → оставить
  `NULL`/`legacy`. Точность гарантируется только для новых прогонов.

### Учётный юнит (`reviewer/agent/outcomes.py` — новый)

```
account_outcomes(candidates, verdicts, verdict_reasons, parsed, kept, deduped, asm, policy) -> list[dict]
```

Терминальный исход каждого кандидата из состояния сессии на момент publish:

| Исход | Правило |
|---|---|
| `verify_rejected` | `verdicts.get(fid) is False`; `reject_reason = verdict_reasons.get(fid)` |
| `gate_dropped` | из survived (`parsed`): `not policy.gate(f)`; `reject_reason = policy.gate_reason(f)` |
| `deduped` | из прошедших gate: убранные `dedup_findings` (`kept ∖ deduped` по fingerprint) |
| `already_posted` | из `asm.findings_rows`: `skipped_existing` (`published=False`) |
| `published_inline` / `published_summary` | из `asm.findings_rows` по флагу `inline` |

Юнит переиспользует готовые строки `asm.findings_rows` для опубликованных/already_posted и достраивает
строки для verify_rejected/gate_dropped/deduped (поля как в `_row`: file, line, category, severity,
confidence, is_real, published, inline, fingerprint, message, + `outcome`, `reject_reason`).

**Инвариант (unit-тест):** `len(rows) == len(candidates)`; сумма по 6 исходам сходится с числом
кандидатов; `published_inline + published_summary == len(asm.inline)+summary`.

Замечание про строки: `ground_line` применяется только к survived; у verify_rejected кандидатов строка
исходная (не грунтованная) — это допустимо (запись, не публикация).

### Причина reject

**Verify (client+server):**
- `reviewer/mcp/schemas.py`: `VerdictIn += reason: str | None = None`.
- `reviewer/mcp/service.py::submit_verdicts`: хранить причину в **параллельном** `s.verdict_reasons:
  dict[str, str]` (не меняя тип `s.verdicts: dict[str, bool]`, чтобы `verdicts.get(fid) is not False`
  в `publish_review:977` продолжал работать без правок логики отсева).
- `reviewer/mcp/state.py` (`_Session`): добавить поле `verdict_reasons`.
- `plugin/skills/review-pr/` (verify-промпт/reference): инструкция «при `is_real=false` дай одну строку
  причины в `reason`». Guard-тест в `tests/skills/`.

**Gate (server-only):**
- `reviewer/policy/policy.py`: новый `gate_reason(finding) -> str | None` возвращает сработавшее правило
  (детерминированно); существующий `gate(finding) -> bool` = `gate_reason(finding) is None` (рефактор
  без смены поведения).

### Интеграция в publish (`reviewer/mcp/service.py`)

- В `publish_review` заменить сбор `rows` (строки 1196–1199) на вызов `account_outcomes(...)`, передав
  candidates/verdicts/verdict_reasons/parsed/kept/deduped/asm/policy.
- `_record_history` получает уже готовый полный список строк (все исходы). Остальная логика
  (`findings_analyzed`, `findings_kept`, `verify_rejected`, статус, история fail-soft) не меняется.
- При `status='error'` published-строки по-прежнему помечаются `published=False`, но их `outcome`
  сохраняется (напр. `published_inline` → фактически не опубликовано; поле `published` отражает факт,
  `outcome` — намеченный исход). Уточнить в плане: для error-прогона выставлять `outcome` намеченный,
  `published=False` — так воронка «что хотели» отделена от «что реально ушло».

### Чтение (`reviewer/web/history.py`)

- `record_run` `finding_sql`: добавить колонки `outcome`, `reject_reason` в INSERT.
- `get_run` `finding_sql`: добавить их в SELECT (аддитивно; фронт получает новые поля, старые не ломаются).
- `list_runs`/`stats` — не трогаем (агрегаты вне скоупа).

## Обработка ошибок / инварианты

- История fail-soft (гейт `REVIEW_HISTORY`): любая ошибка записи не валит `publish_review`.
- Миграция идемпотентна (повторный `init_schema` безопасен).
- `account_outcomes` чист (без внешних вызовов), детерминирован, тестируется без БД/сети.
- Обратная совместимость: старые строки без `outcome` читаются (NULL); новые API-поля аддитивны.

## Тестирование

- `tests/agent/test_outcomes.py` (новый): воронка сходится (сумма = кандидаты); каждая стадия помечена
  верным `outcome`; `reject_reason` маршрутизируется (verify → verdict_reasons, gate → gate_reason,
  остальные → None); граничные случаи (0 кандидатов, всё published, всё rejected).
- `tests/policy/test_policy.py`: `gate_reason` возвращает верное сработавшее правило по каждому типу
  (category/severity/confidence/path); `gate(f) == (gate_reason(f) is None)`.
- `tests/web/`: `ReviewHistory` пишет/читает `outcome`+`reject_reason`; схема идемпотентна; бэкфилл
  проставляет исходы опубликованным старым строкам.
- `tests/mcp/`: `publish_review` персистит verify_rejected/gate_dropped кандидатов с исходами и
  причинами; `VerdictIn.reason` принимается `submit_verdicts` и долетает до строки.
- `tests/skills/`: guard — verify-промпт содержит инструкцию про `reason` при `is_real=false`.

## Затронутые файлы

| Файл | Изменение |
|---|---|
| `reviewer/web/schema.sql` | +2 колонки, бэкфилл |
| `reviewer/agent/outcomes.py` | новый юнит `account_outcomes` |
| `reviewer/mcp/service.py` | `publish_review` → `account_outcomes`; `submit_verdicts` → `verdict_reasons` |
| `reviewer/mcp/state.py` | `_Session.verdict_reasons` |
| `reviewer/mcp/schemas.py` | `VerdictIn.reason` |
| `reviewer/policy/policy.py` | `gate_reason` (+рефактор `gate`) |
| `reviewer/web/history.py` | `outcome`/`reject_reason` в INSERT/SELECT |
| `plugin/skills/review-pr/` | verify-промпт: reason при is_real=false |
| `tests/{agent,policy,web,mcp,skills}/` | покрытие |

## Открытые вопросы для плана

- `deduped` dropped-набор: расширить `dedup_findings` возвратом dropped ИЛИ вычислять `kept ∖ deduped` по
  fingerprint снаружи. Решить в writing-plans (предпочтение — внешний diff, не менять сигнатуру dedup).
- Точное имя reference-файла verify-промпта в `plugin/skills/review-pr/` (уточнить при реализации).
- Формулировка `gate_reason` строк (стабильные, гриппаемые префиксы vs человекочитаемые).
