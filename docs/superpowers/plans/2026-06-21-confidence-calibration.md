# Confidence Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать находкам ревью осмысленный `confidence` (явная шкала, привязанная к grounding) и сделать `min_confidence`-гейт предсказуемым.

**Architecture:** Две стороны. (1) Промпты: единая 3-уровневая шкала калибровки в общем `_common/findings-schema.md` (включается в 4 dimension-промпта), плюс короткие dimension-надстройки; blast-radius помечается как частный случай. (2) Код: парсер LLM-вывода больше не коэрцит «нет оценки» в проходное `0.5`, а клампит и опускает в `0.1`; дефолты порога выравниваются на `0.5`.

**Tech Stack:** Python 3.11–3.13, pytest, dataclasses; промпты — Markdown с include-маркерами `<!-- include: _common/<file>.md -->`.

## Global Constraints

- Язык проекта — **русский**: комментарии, докстринги, сообщения. Текст промптов — английский (как в существующих файлах), но правила остаются на английском в `.md`.
- Коммиты — **Conventional Commits на русском**, без self-attribution (никаких `Co-Authored-By`/упоминаний Claude).
- Тесты — `.venv/bin/pytest -q` (unit, integration исключены по умолчанию). Unit не трогают Postgres/Neo4j/Voyage.
- `line-length 100`, `ruff check .` (target py311).
- НЕ менять: `ReviewPolicy.gate()` логику сравнения (`policy.py:91`), env-дефолт `review_min_confidence` (`settings.py:45` остаётся `0.5`).
- Guard-инвариант: `findings-schema.md:16` (`"confidence": 0.0`) и числа шкалы blast-radius (`0.8`) сохраняются дословно — на них опираются `tests/skills/test_assembled_prompts.py`.

---

### Task 1: Коэрция `confidence` — fallback `0.1` + clamp `[0,1]`

**Files:**
- Modify: `reviewer/mcp/service.py:61` (докстринг), `reviewer/mcp/service.py:76-79` (коэрция)
- Test: `tests/mcp/test_service.py`

**Interfaces:**
- Consumes: `_finding_from_dict(d: dict) -> Finding | None` (`reviewer/mcp/service.py:49`), `Finding` (`reviewer/vcs/base.py:30`).
- Produces: после изменения `_finding_from_dict` возвращает `Finding.confidence`, гарантированно в `[0.0, 1.0]`; отсутствующий/`None`/нечисловой `confidence` → `0.1`.

- [ ] **Step 1: Write the failing test**

В конец `tests/mcp/test_service.py` добавить (импорт `_finding_from_dict` — в блок импортов файла: `from reviewer.mcp.service import MCPReviewService, _finding_from_dict`):

```python
def test_finding_confidence_coercion_and_clamp():
    base = {"file": "a.py", "severity": "high", "message": "m", "line": 1, "code_quote": "x = 1"}
    # валидные значения проходят как есть
    assert _finding_from_dict({**base, "confidence": 0.9}).confidence == 0.9
    assert _finding_from_dict({**base, "confidence": 0.5}).confidence == 0.5
    # не оценено / None / мусор → 0.1 (ниже честного потолка спекулятивной зоны 0.4)
    assert _finding_from_dict(base).confidence == 0.1
    assert _finding_from_dict({**base, "confidence": None}).confidence == 0.1
    assert _finding_from_dict({**base, "confidence": "abc"}).confidence == 0.1
    # clamp в [0,1]
    assert _finding_from_dict({**base, "confidence": 1.5}).confidence == 1.0
    assert _finding_from_dict({**base, "confidence": -0.2}).confidence == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/mcp/test_service.py::test_finding_confidence_coercion_and_clamp -q`
Expected: FAIL — текущий код даёт `0.5` для отсутствующего/`None`/`"abc"` (ожидается `0.1`) и `1.5` для `1.5` (ожидается `1.0`, clamp отсутствует).

- [ ] **Step 3: Write minimal implementation**

В `reviewer/mcp/service.py` заменить блок коэрции (строки 76-79):

```python
    try:
        confidence = float(d.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
```

на:

```python
    try:
        confidence = float(d.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.1   # не оценено = спекулятивно (ниже честного потолка 0.4) → отсекается гейтом
    confidence = max(0.0, min(1.0, confidence))   # clamp в [0,1]
```

И обновить строку докстринга `service.py:61`:

```
    - ``confidence``: float-коэрция, None/мусор → 0.5;
```

на:

```
    - ``confidence``: float-коэрция, None/мусор → 0.1; значение клампится в [0.0, 1.0];
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/mcp/test_service.py::test_finding_confidence_coercion_and_clamp -q`
Expected: PASS

- [ ] **Step 5: Run the full service test file (regression)**

Run: `.venv/bin/pytest tests/mcp/test_service.py tests/mcp/test_publish.py -q`
Expected: PASS (ни один существующий тест не полагался на fallback `0.5`).

- [ ] **Step 6: Commit**

```bash
git add reviewer/mcp/service.py tests/mcp/test_service.py
git commit -m "fix(mcp): коэрция confidence — fallback 0.1 и clamp в [0,1] (PRI-144)"
```

---

### Task 2: Выравнивание дефолтов `min_confidence` на `0.5` + предсказуемый гейт

**Files:**
- Modify: `reviewer/policy/policy.py:18` (dataclass-дефолт), `reviewer/policy/policy.py:35` (`from_yaml`-дефолт)
- Test: `tests/policy/test_policy.py`

**Interfaces:**
- Consumes: `ReviewPolicy` (`reviewer/policy/policy.py:11`), хелпер `F(cat, sev, file="a.py", confidence=0.9)` (`tests/policy/test_policy.py:5`), `Settings` (`reviewer/config/settings.py`).
- Produces: `ReviewPolicy().min_confidence == 0.5`, `ReviewPolicy.from_yaml(None).min_confidence == 0.5`; `gate()` логика не меняется.

- [ ] **Step 1: Write the failing test**

В конец `tests/policy/test_policy.py` добавить:

```python
def test_min_confidence_default_aligned_to_0_5():
    # dataclass-дефолт и from_yaml-дефолт согласованы с env (0.5)
    assert ReviewPolicy().min_confidence == 0.5
    assert ReviewPolicy.from_yaml(None).min_confidence == 0.5


def test_min_confidence_gate_predictable_set():
    # набор примеров приёмки: порог 0.5 отсекает предсказуемо
    p = ReviewPolicy(min_confidence=0.5)
    assert p.gate(F("correctness", "high", confidence=0.4)) is False
    assert p.gate(F("correctness", "high", confidence=0.49)) is False
    assert p.gate(F("correctness", "high", confidence=0.5)) is True
    assert p.gate(F("correctness", "high", confidence=0.8)) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/policy/test_policy.py::test_min_confidence_default_aligned_to_0_5 -q`
Expected: FAIL — текущий dataclass-дефолт `0.0` (assert ждёт `0.5`). (Второй тест `test_min_confidence_gate_predictable_set` уже проходит — `ReviewPolicy(min_confidence=0.5)` задаёт порог явно; он фиксирует критерий приёмки.)

- [ ] **Step 3: Write minimal implementation**

В `reviewer/policy/policy.py` строка 18:

```python
    min_confidence: float = 0.0
```

→

```python
    min_confidence: float = 0.5
```

И в `from_yaml` строка 35:

```python
            min_confidence=data.get("min_confidence", 0.0),
```

→

```python
            min_confidence=data.get("min_confidence", 0.5),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/policy/test_policy.py::test_min_confidence_default_aligned_to_0_5 tests/policy/test_policy.py::test_min_confidence_gate_predictable_set -q`
Expected: PASS

- [ ] **Step 5: Run the full policy test file (regression)**

Run: `.venv/bin/pytest tests/policy/test_policy.py -q`
Expected: PASS — существующие тесты используют `F(..., confidence=0.9)` (проходит при `0.5`) либо задают `min_confidence` явно; подъём дефолта `0.0 → 0.5` их не ломает.

- [ ] **Step 6: Commit**

```bash
git add reviewer/policy/policy.py tests/policy/test_policy.py
git commit -m "fix(policy): выровнять дефолт min_confidence на 0.5 (PRI-144)"
```

---

### Task 3: Шкала калибровки в промптах

**Files:**
- Modify: `plugin/skills/_common/findings-schema.md:29` (строка `confidence` → блок шкалы)
- Modify: `plugin/skills/review-pr/references/blast-radius-prompt.md:31` (пометить как частный случай общей шкалы)
- Modify: `plugin/skills/review-pr/references/requirements-prompt.md` (надстройка после include findings-schema, строка 40)
- Modify: `plugin/skills/performance-review/SKILL.md` (надстройка после include findings-schema, строка 53)
- Modify: `plugin/skills/maintainability-review/SKILL.md` (надстройка после include findings-schema, строка 105)
- Test: `tests/skills/` (существующие guard — должны остаться зелёными; новых не добавляем)

**Interfaces:**
- Consumes: include-механизм `<!-- include: _common/findings-schema.md -->` (разворачивается оркестратором).
- Produces: собранные промпты содержат шкалу калибровки; `'"confidence": 0.0'` (пример) и `0.8` (blast-radius) сохранены для guard.

- [ ] **Step 1: Заменить семантику `confidence` в общей схеме**

В `plugin/skills/_common/findings-schema.md` строку 29:

```
- `confidence` — float `0.0..1.0`; it feeds the publish gate, so be honest.
```

заменить на блок (пример на строке 16 — `"confidence": 0.0` — НЕ трогать):

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

- [ ] **Step 2: Пометить шкалу blast-radius как частный случай**

В `plugin/skills/review-pr/references/blast-radius-prompt.md` строку 31:

```
- Set `confidence` (a float; it feeds the publish gate — be honest) by this scale:
```

заменить на (подпункты `0.8–0.9 / 0.5–0.6 / ≤ 0.4` ниже — НЕ трогать, число `0.8` обязано сохраниться для guard):

```
- Set `confidence` by the shared scale in findings-schema (grounding + reproducibility);
  for blast radius that scale concretely means:
```

- [ ] **Step 3: Добавить dimension-надстройки (по одной строке)**

В `plugin/skills/review-pr/references/requirements-prompt.md` сразу после строки 40
(`<!-- include: _common/findings-schema.md -->`) добавить:

```
- Calibrate `confidence` by how explicitly the acceptance criterion is broken: an exact,
  quoted criterion clearly violated → 0.8+; an inferred/implicit requirement → 0.5–0.7;
  a guess about intent → ≤ 0.4 (drop).
```

В `plugin/skills/performance-review/SKILL.md` сразу после строки 53
(`<!-- include: _common/findings-schema.md -->`) добавить:

```
- Calibrate `confidence` against a measurable, reproducible effect: a hot path you can point
  to (loop bound, query inside a loop) → 0.8+; a plausible but data-dependent cost → 0.5–0.7;
  no measurable/reproducible effect → ≤ 0.4 (drop).
```

В `plugin/skills/maintainability-review/SKILL.md` сразу после строки 105
(`<!-- include: _common/findings-schema.md -->`) добавить:

```
- Calibrate `confidence` against concrete, grounded evidence: a duplicated block you can quote
  or a real complexity hotspot → 0.8+; a subjective readability concern → 0.5–0.7; pure taste
  → ≤ 0.4 (drop).
```

(`analyze-prompt.md` отдельной надстройки не получает — общей шкалы из включённого
`findings-schema.md` достаточно.)

- [ ] **Step 4: Run skills guard tests**

Run: `.venv/bin/pytest tests/skills -q`
Expected: PASS — пример `"confidence": 0.0` и blast-radius `0.8` сохранены; токен `confidence` присутствует в схеме.

Если падает `test_assembled_prompts.py` на `'"confidence": 0.0'` или `"0.8"` — значит был случайно изменён пример/шкала; вернуть их дословно (надстройки добавляются, а не заменяют пример/числа).

- [ ] **Step 5: Commit**

```bash
git add plugin/skills/_common/findings-schema.md \
        plugin/skills/review-pr/references/blast-radius-prompt.md \
        plugin/skills/review-pr/references/requirements-prompt.md \
        plugin/skills/performance-review/SKILL.md \
        plugin/skills/maintainability-review/SKILL.md
git commit -m "feat(skills): шкала калибровки confidence в dimension-промптах (PRI-144)"
```

---

### Task 4: Финальная проверка (вся сюита + линт)

**Files:** —

- [ ] **Step 1: Run full unit suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены по умолчанию).

- [ ] **Step 2: Lint**

Run: `.venv/bin/ruff check reviewer/mcp/service.py reviewer/policy/policy.py tests/mcp/test_service.py tests/policy/test_policy.py`
Expected: чисто на изменённых файлах. (Repo-wide `ruff check .` может быть не чист на main — не гнаться за этим, проверять только тронутые файлы.)

- [ ] **Step 3: Проверка критерия приёмки вручную (sanity)**

Убедиться, что инвариант выполнен: fallback `0.1 < 0.5` (порог) → неоценённая находка отсекается; grounded `≥ 0.5` → проходит. Подтверждено тестами Task 1 (коэрция) и Task 2 (gate).

## Self-Review

**Spec coverage:**
- Шкала калибровки (spec §1) → Task 3 Step 1. ✔
- blast-radius как частный случай (spec §2) → Task 3 Step 2. ✔
- Dimension-надстройки (spec §3) → Task 3 Step 3. ✔
- Коэрция fallback `0.1` + clamp (spec §4.1) → Task 1. ✔
- Дефолты порога `0.5` (spec §4.2) → Task 2. ✔
- `gate()`/env не трогаем (spec §4.3) → Global Constraints + Task 2 Step 3 (только дефолты). ✔
- Тесты приёмки (spec «Проверка приёмки») → Task 1 Step 1, Task 2 Step 1. ✔
- Guard-совместимость (spec «Совместимость с guard-тестами») → Global Constraints + Task 3 Step 4. ✔

**Placeholder scan:** нет TBD/«add error handling»; весь код приведён дословно. ✔

**Type/name consistency:** `_finding_from_dict`, `ReviewPolicy`, `F`, `Finding.confidence` — имена сверены с кодом (`service.py:49`, `policy.py:11`, `test_policy.py:5`, `base.py:39`). ✔
