# Калибровка `confidence` в промптах ревью — дизайн

- **Задача:** PRI-144 (canonical ID-144) — «Калибровка confidence в промптах ревью (привязка к grounding и min_confidence-гейту)».
- **Дата:** 2026-06-21
- **Оценка:** S
- **Ссылка:** https://ru.yougile.com/team/686c049c8af8/#PRI-144

## Контекст и проблема

Поле `confidence` (`0.0..1.0`) есть в JSON-контракте находок и кормит publish-гейт
(`min_confidence` в `ReviewPolicy.gate`), но **нет правил калибровки**: не определено, что
значит 0.5 против 0.9 и как привязать оценку к grounding. Из-за этого:

1. **В промптах** субагенты выставляют `confidence` произвольно — только `blast-radius-prompt.md`
   имеет явную шкалу; в `analyze`/`performance`/`maintainability` слово `confidence` не упомянуто
   вовсе, в `requirements` — без шкалы.
2. **В коде** парсер LLM-вывода коэрцит отсутствующий/мусорный `confidence` ровно в `0.5`
   (`reviewer/mcp/service.py:76-79`), а порог гейта по умолчанию тоже `0.5`
   (`review_min_confidence`, `reviewer/config/settings.py:45`). `0.5 >= 0.5` → находка **без
   честной оценки проходит гейт**. Это и есть «гейт работает по случайным значениям».
3. Дефолт порога рассинхронен: dataclass `ReviewPolicy.min_confidence = 0.0`
   (`reviewer/policy/policy.py:18`) и `from_yaml` (`policy.py:35`) против env `0.5`
   (`from_settings`). Рабочий путь — `ReviewPolicy.load → from_settings` (`0.5`), поэтому
   рассинхрон — «спящая ловушка» при прямом конструировании `ReviewPolicy(...)`
   (`session_serde`, тесты), а не активный баг.

## Цель и критерий приёмки

- Находки получают **осмысленный** `confidence`, привязанный к grounding и воспроизводимости.
- Гейт по `min_confidence` отсекает **предсказуемо** (проверка на наборе примеров).

## Граница задачи

В scope: **промпты + согласование кода**. Из scope исключены: guard-тесты на сборку промптов,
изменение самого `gate()`, изменение env-дефолта `review_min_confidence`.

## Дизайн

### 1. Единая шкала калибровки (`_common/findings-schema.md`)

Заменить в `plugin/skills/_common/findings-schema.md` одну строку про `confidence` (строка 29)
на блок с 3-уровневой шкалой, привязанной к **grounding** (есть ли точный `code_quote` из
реального кода) и **воспроизводимости** (подтверждена ли проблема инструментами):

```
- `confidence` — float `0.0..1.0`; it feeds the publish gate (`min_confidence`),
  so be honest. Calibrate against grounding + reproducibility:
  - 0.8–1.0 — grounded AND verified: an exact `code_quote` from the new file AND
    the problem is confirmed via tools (read_file/search_code showed the handling
    is truly absent / the call graph confirms the impact). An unambiguous, real defect.
  - 0.5–0.7 — grounded but context-dependent: a valid `code_quote`, the issue is
    plausible, but reproducibility depends on runtime data / unchecked branches /
    caller context. Phrase as "verify that…", not a categorical claim.
  - ≤ 0.4 — speculative: no solid grounding, not verified with tools, or a guess about
    intent. Below the 0.5 gate → it will be dropped. Prefer dropping it yourself
    (an empty findings list is valid).
```

Правила привязки:
- **Grounding задаёт потолок.** Нет валидного `code_quote` ⇒ `confidence ≤ 0.4` (отсекается).
  Смыкается с `_common/anti-hallucination.md:20-22` («каждая находка обязана нести точный
  `code_quote`»).
- **Воспроизводимость задаёт уровень** внутри grounded-зоны: подтверждено инструментами → `0.8+`,
  контекстно-зависимо → `0.5–0.7`.
- **Порог `0.5`** ложится на границу «спекулятивное / grounded»: догадки выпадают, обоснованное
  публикуется — гейт предсказуем.

Пример в `findings-schema.md:16` (`"confidence": 0.0`) **оставляем без изменений**: это
format-плейсхолдер JSON-шаблона (показывает тип поля), и на него опираются guard-тесты
`tests/skills/test_assembled_prompts.py:34,62,68` (`'"confidence": 0.0' in ...`). Подмена на
«рекомендованное» число ввела бы ложный дефолт и сломала бы guard. Калибровка живёт в семантике
поля (блок выше), а не в примере. *(Отклонение от первоначального дизайна: ранее планировалась
правка примера `0.0 → 0.7`; отменено после обнаружения связи guard-тестов с текстом.)*

### 2. `blast-radius-prompt.md` — согласование

`plugin/skills/review-pr/references/blast-radius-prompt.md:31-44` уже содержит verification-шкалу
(`0.8–0.9 / 0.5–0.6 / ≤0.4`) — это более узкое **подмножество** общей шкалы (`0.8–1.0 / 0.5–0.7 /
≤0.4`): те же зоны и тот же порог `0.5`, но уже верхние границы под cross-file-специфику. Зоны
непротиворечивы — оставляем специфику blast-radius (read caller + `get_changed_file_diff`), но
помечаем её как **частный случай** общей шкалы (короткая ссылка на findings-schema). Содержательно
шкала не меняется.

### 3. Dimension-надстройки (по 1 строке)

Поверх общей шкалы, в соответствующих промптах/скилах:
- `requirements-prompt.md` — привязка к тому, насколько явно нарушен критерий приёмки.
- `performance-review/SKILL.md`, `maintainability-review/SKILL.md` — нет измеримого/
  воспроизводимого эффекта ⇒ `≤0.4`.
- `analyze-prompt.md` — общей шкалы из включённого `findings-schema.md` достаточно; отдельная
  надстройка не требуется (надстройку добавлять только если потребуется по факту).

### 4. Согласование кода

**4.1 Коэрция `confidence` — `reviewer/mcp/service.py:76-79` (главный фикс).**

```python
try:
    confidence = float(d.get("confidence"))
except (TypeError, ValueError):
    confidence = 0.1          # не оценено = спекулятивно (ниже честного потолка 0.4) → отсекается
confidence = max(0.0, min(1.0, confidence))   # clamp в [0,1]
```

- Fallback `0.5 → 0.1`: неоценённая/мусорная находка больше не протискивается через гейт. `0.1`
  (а не `0.4`) сознательно ниже честного потолка спекулятивной зоны — это **отличает** «модель
  промолчала» (`0.1`) от «модель честно оценила слабую находку» (`0.4`). Неоценённое всплывёт
  только при осознанно низком пороге.
- Clamp `[0,1]`: `1.5 → 1.0`, `-0.2 → 0.0`, `95 → 1.0` — `confidence` всегда в диапазоне.
- Обновить докстринг `service.py:61` (`None/мусор → 0.5` → `→ 0.1; значения клампятся в [0,1]`).

**4.2 Дефолты порога — `reviewer/policy/policy.py:18,35` (выравнивание).**

dataclass-дефолт `ReviewPolicy.min_confidence` и дефолт в `from_yaml` привести к `0.5` —
чтобы «честный» порог был единым независимо от пути конструирования. Рабочий путь
(`load → from_settings`) уже `0.5`; сохранённые сессии десериализуются с явным значением и
не затрагиваются.

**4.3 Что НЕ трогаем.** `gate()` (`policy.py:91`) уже корректно сравнивает
`confidence < min_confidence`; env `review_min_confidence` остаётся `0.5`.

### Инвариант

Связка fallback `0.1` + порог `0.5`: неоценённая находка строго ниже порога → отсекается.
Граница grounded-зоны (`0.5`) совпадает с порогом → grounded-находки проходят.

## Файлы к изменению

| Файл | Изменение |
|---|---|
| `plugin/skills/_common/findings-schema.md` | шкала калибровки (стр. 29 → блок); пример стр. 16 НЕ трогаем (guard) |
| `plugin/skills/review-pr/references/blast-radius-prompt.md` | пометить шкалу как частный случай общей |
| `plugin/skills/review-pr/references/requirements-prompt.md` | надстройка (1 строка) |
| `plugin/skills/performance-review/SKILL.md` | надстройка (1 строка) |
| `plugin/skills/maintainability-review/SKILL.md` | надстройка (1 строка) |
| `reviewer/mcp/service.py` | fallback `0.5→0.1` + clamp `[0,1]` + докстринг |
| `reviewer/policy/policy.py` | dataclass- и `from_yaml`-дефолт `min_confidence` → `0.5` |
| тесты `mcp/service` (коэрция) | юнит на набор примеров коэрции `confidence` |
| `tests/policy/test_policy.py` | юнит на `gate()` по набору примеров `confidence` |

## Проверка приёмки (юниты на изменённый код)

Не guard-тесты на промпты — юниты на тот код, что меняется (гоняются обычным `pytest -q`,
без Postgres/Neo4j/Voyage):

1. **Коэрция `confidence`** (тесты `mcp/service`): `0.9→0.9`, `0.5→0.5`, отсутствует/`None`/
   `"abc"`→`0.1`, `1.5→1.0`, `-0.2→0.0`.
2. **`gate()` по набору примеров** (`tests/policy/test_policy.py`): при `min_confidence=0.5` —
   `confidence` `0.4 / 0.49 / 0.5 / 0.8` → отсек/отсек/проход/проход.

## Совместимость с guard-тестами

`tests/skills/` опираются на конкретный текст собранных промптов:
- `test_assembled_prompts.py:34,62,68` — `'"confidence": 0.0'` в analyze/performance/maintainability
  ⇒ пример в `findings-schema.md:16` сохраняем дословно.
- `test_assembled_prompts.py:50` — `"0.8"` в blast-radius ⇒ числа его шкалы сохраняем.
- `test_common_blocks.py:46-57` — токен `confidence` присутствует в `findings-schema.md` ⇒ новый
  блок шкалы это требование выполняет.

Новые guard-тесты на промпты не добавляем (вне scope). Существующие должны остаться зелёными —
после правок промптов обязателен прогон `.venv/bin/pytest tests/skills -q`.

## Связанные задачи

- **ID-142** «Общие reference-блоки» — шкала кладётся в `_common/findings-schema.md`, в духе
  уже внедрённой инфраструктуры include-маркеров.
- **ID-143** «Унификация performance/maintainability-скилов» — оба `SKILL.md` уже на
  `_common`-блоках; надстройки встраиваются тем же include-механизмом.
