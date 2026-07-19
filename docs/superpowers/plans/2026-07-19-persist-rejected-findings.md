# Персист отклонённых находок (verify/gate) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Персистить каждый кандидат-находку в `review_findings` с меткой терминального исхода (`outcome`) и причиной отклонения (`reject_reason`), чтобы наблюдаемость видела ЧТО и ПОЧЕМУ режут verify и gate (сейчас ~30% находок отклоняется вслепую).

**Architecture:** Новый чистый юнит `reviewer/agent/outcomes.py::account_outcomes` сопоставляет каждому кандидату из состояния MCP-сессии терминальный исход и строку для БД. Причина reject для verify приходит от клиента (`VerdictIn.reason` → `_Session.verdict_reasons`), для gate выводится сервером (`ReviewPolicy.gate_reason`). `publish_review` вызывает `account_outcomes` вместо прямого сбора `asm.findings_rows`; схема `review_findings` расширяется двумя nullable-колонками идемпотентной миграцией.

**Tech Stack:** Python 3.11–3.13, pydantic (schemas), psycopg3 + ParadeDB/Postgres (:5433), pytest (unit `-m 'not integration'` по умолчанию, integration за `-m integration` + docker compose test-профиль), FastMCP.

## Global Constraints

- Язык проекта — **русский**: комментарии, докстринги, сообщения. Сохранять стиль в новом коде.
- Коммиты: **без self-attribution** (никаких `Co-Authored-By`/упоминаний Claude). Conventional Commits на русском (`feat(agent): …`, `fix(mcp): …`).
- Ruff: line-length 100, target py311. Прогонять `.venv/bin/ruff check <изменённые файлы>` (не гнаться за repo-wide clean — на `main` он уже не чист).
- Unit-тестам запрещены внешние и localhost-сокеты. Любой тест с реальной БД/сетью обязан иметь `@pytest.mark.integration`.
- Обратная совместимость: колонки `is_real`/`published`/`inline` продолжают заполняться консистентно; `outcome` — новое поле-истина, не заменяет старые. Новые API-поля аддитивны.
- История прогонов fail-soft (гейт `settings.review_history`): любая ошибка записи не валит `publish_review`.
- Инвариант учёта: `len(account_outcomes(...)) == len(candidates)`; сумма по 6 исходам = числу кандидатов.
- Не трогать: панель веб-дашборда, `stats()`-агрегаты, ретенцию/GC, usage/cost (вне скоупа, YAGNI).

**Порядок задач:** 1 (gate_reason) → 2 (VerdictIn.reason) → 3 (account_outcomes) → 4 (схема + history) → 5 (интеграция в publish) → 6 (verify-промпт). Задача 3 использует `gate_reason` из Задачи 1; Задача 5 — капстоун, связывает 1–4.

---

### Task 1: `ReviewPolicy.gate_reason` (+ рефактор `gate`)

**Files:**
- Modify: `reviewer/policy/policy.py:111-122` (метод `gate`)
- Test: `tests/policy/test_policy.py`

**Interfaces:**
- Consumes: `reviewer.vcs.base.Finding` (атрибуты `category`, `severity`, `confidence`, `file`); `reviewer.index.pathfilter.is_ignored` (уже импортирован); модульный `_SEV` (уже определён в policy.py).
- Produces: `ReviewPolicy.gate_reason(self, finding) -> str | None` — возвращает строку сработавшего правила политики или `None`, если находка проходит гейт. `ReviewPolicy.gate(self, finding) -> bool` теперь возвращает `self.gate_reason(finding) is None` (поведение неизменно). Строки причин имеют стабильные префиксы: `category …` / `severity …` / `confidence …` / `path …`.

- [ ] **Step 1: Написать падающие тесты**

Добавить в конец `tests/policy/test_policy.py`:

```python
def test_gate_reason_none_when_passes():
    p = ReviewPolicy(min_confidence=0.5, severity_threshold="medium")
    assert p.gate_reason(F("correctness", "high", confidence=0.9)) is None


def test_gate_reason_category_disabled():
    p = ReviewPolicy.from_yaml("categories: {style: false}")
    r = p.gate_reason(F("style", "high"))
    assert r is not None and r.startswith("category")


def test_gate_reason_low_severity():
    p = ReviewPolicy(severity_threshold="high")
    r = p.gate_reason(F("correctness", "low"))
    assert r is not None and r.startswith("severity")


def test_gate_reason_low_confidence():
    p = ReviewPolicy(min_confidence=0.7)
    r = p.gate_reason(F("correctness", "high", confidence=0.4))
    assert r is not None and r.startswith("confidence")


def test_gate_reason_ignored_path():
    p = ReviewPolicy.from_yaml("paths: {ignore: ['vendor/**']}")
    r = p.gate_reason(F("correctness", "high", file="vendor/x.py"))
    assert r is not None and r.startswith("path")


def test_gate_equivalent_to_gate_reason_is_none():
    p = ReviewPolicy.from_yaml(
        "categories: {correctness: true, style: false}\n"
        "severity_threshold: medium\n"
        "min_confidence: 0.5\n"
        "paths: {ignore: ['vendor/**']}\n"
    )
    cases = [
        F("correctness", "high", confidence=0.9),
        F("style", "high"),
        F("correctness", "low"),
        F("correctness", "high", confidence=0.3),
        F("correctness", "high", file="vendor/x.py"),
    ]
    for f in cases:
        assert p.gate(f) == (p.gate_reason(f) is None)
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/policy/test_policy.py -q -k gate_reason`
Expected: FAIL — `AttributeError: 'ReviewPolicy' object has no attribute 'gate_reason'`

- [ ] **Step 3: Реализовать `gate_reason` и переписать `gate`**

Заменить метод `gate` (`reviewer/policy/policy.py:111-122`) на:

```python
    def gate_reason(self, finding) -> str | None:
        """Причина отсева находки гейтом или None, если находка проходит.

        Детерминированно возвращает первое сработавшее правило политики
        (категория/severity/confidence/путь). Строки — со стабильными
        префиксами (`category`/`severity`/`confidence`/`path`), пригодны
        для группировки в наблюдаемости.
        """
        if not self.category_enabled(finding.category):
            return f"category '{finding.category}' disabled"
        sev_f = _SEV.get(finding.severity)
        sev_t = _SEV.get(self.severity_threshold)
        if sev_f is None or sev_t is None or sev_f < sev_t:
            return f"severity '{finding.severity}' below threshold '{self.severity_threshold}'"
        if finding.confidence < self.min_confidence:
            return f"confidence {finding.confidence:.2f} below min {self.min_confidence:.2f}"
        if is_ignored(finding.file, self.ignore):
            return f"path '{finding.file}' ignored"
        return None

    def gate(self, finding) -> bool:
        return self.gate_reason(finding) is None
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят (и старые gate-тесты не сломались)**

Run: `.venv/bin/pytest tests/policy/test_policy.py -q`
Expected: PASS (все тесты, включая существующие `test_gate_*`/`test_min_confidence_*`)

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/policy/policy.py tests/policy/test_policy.py
git add reviewer/policy/policy.py tests/policy/test_policy.py
git commit -m "feat(policy): gate_reason — причина отсева находки гейтом"
```

---

### Task 2: `VerdictIn.reason` + `_Session.verdict_reasons`

**Files:**
- Modify: `reviewer/mcp/schemas.py:108-114` (класс `VerdictIn`)
- Modify: `reviewer/mcp/service.py:52-70` (dataclass `_Session`), `reviewer/mcp/service.py:908-923` (`submit_verdicts`)
- Test: `tests/mcp/test_schemas.py`, `tests/mcp/test_submit_tools.py`

**Interfaces:**
- Consumes: FastMCP выводит схему тула `submit_verdicts` из `VerdictIn`.
- Produces: `VerdictIn.reason: str | None = None` (новое опциональное поле). `_Session.verdict_reasons: dict[str, str]` — параллельный `s.verdicts` словарь id→причина (заполняется только непустыми причинами). `submit_verdicts` сохраняет `v.reason` в `s.verdict_reasons[v.id]` при непустом reason. Тип `s.verdicts: dict[str, bool]` НЕ меняется — отсев verify в `publish_review:977` работает без правок.

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/mcp/test_schemas.py` (тест валидации схемы; проверить существующие импорты — там уже импортируется `VerdictIn`, если нет — добавить `from reviewer.mcp.schemas import VerdictIn`):

```python
def test_verdict_in_reason_optional_defaults_none():
    from reviewer.mcp.schemas import VerdictIn
    v = VerdictIn.model_validate({"id": "f1", "is_real": False})
    assert v.reason is None


def test_verdict_in_reason_accepted():
    from reviewer.mcp.schemas import VerdictIn
    v = VerdictIn.model_validate(
        {"id": "f1", "is_real": False, "reason": "line does not exist"})
    assert v.reason == "line does not exist"
```

Добавить в `tests/mcp/test_submit_tools.py`:

```python
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_submit_verdicts_records_reason(_ov, _ch):
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    svc.submit_findings("o/r", 7, [RAW])                     # → f1
    svc.submit_verdicts("o/r", 7,
                        [{"id": "f1", "is_real": False, "reason": "pre-existing"}])
    sess = svc._sessions[("o/r", 7)]
    assert sess.verdicts == {"f1": False}
    assert sess.verdict_reasons == {"f1": "pre-existing"}


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_submit_verdicts_no_reason_leaves_reasons_empty(_ov, _ch):
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    svc.submit_findings("o/r", 7, [RAW])                     # → f1
    svc.submit_verdicts("o/r", 7, [{"id": "f1", "is_real": True}])
    sess = svc._sessions[("o/r", 7)]
    assert sess.verdict_reasons == {}
```

(`_make_mcp_service_with_publish`, `RAW`, `_fake_chunk` уже импортируются в `test_submit_tools.py` из `tests.mcp.test_publish`.)

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_schemas.py tests/mcp/test_submit_tools.py -q -k "reason"`
Expected: FAIL — `AttributeError: '_Session' object has no attribute 'verdict_reasons'` и/или `VerdictIn` без `reason`.

- [ ] **Step 3: Добавить поле в `VerdictIn`**

В `reviewer/mcp/schemas.py` заменить тело класса `VerdictIn` (строки 108-114):

```python
class VerdictIn(BaseModel):
    """Вердикт verify по находке с server-assigned id."""

    model_config = ConfigDict(extra="ignore")

    id: str
    is_real: bool
    # PRI: причина reject (при is_real=false) для наблюдаемости; None = не указана.
    reason: str | None = None

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, v):
        return v if isinstance(v, str) and v.strip() else None
```

- [ ] **Step 4: Добавить поле в `_Session` и сохранение в `submit_verdicts`**

В `reviewer/mcp/service.py` в dataclass `_Session` после строки `verdicts: dict[str, bool] = field(default_factory=dict)` (строка 65) добавить:

```python
    # PRI: причины reject от верификатора (id → строка), параллельно verdicts.
    # In-memory, как candidates/verdicts (регидрированная сессия стартует пустой).
    verdict_reasons: dict[str, str] = field(default_factory=dict)
```

В методе `submit_verdicts` (строки 915-922) после `s.verdicts[v.id] = v.is_real` добавить сохранение причины:

```python
        for d in verdicts:
            v = VerdictIn.model_validate(d)
            if v.id not in s.candidates:
                unknown.append(v.id)
                log.warning("submit_verdicts: неизвестный id %s (%s#%s)", v.id, repo, pr)
                continue
            s.verdicts[v.id] = v.is_real
            if v.reason:
                s.verdict_reasons[v.id] = v.reason
            recorded += 1
```

- [ ] **Step 5: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/test_schemas.py tests/mcp/test_submit_tools.py -q`
Expected: PASS

- [ ] **Step 6: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/mcp/schemas.py reviewer/mcp/service.py tests/mcp/test_schemas.py tests/mcp/test_submit_tools.py
git add reviewer/mcp/schemas.py reviewer/mcp/service.py tests/mcp/test_schemas.py tests/mcp/test_submit_tools.py
git commit -m "feat(mcp): VerdictIn.reason — причина reject от верификатора в сессии"
```

---

### Task 3: Учётный юнит `account_outcomes`

**Files:**
- Create: `reviewer/agent/outcomes.py`
- Test: `tests/agent/test_outcomes.py`

**Interfaces:**
- Consumes: `reviewer.vcs.base.Finding` (атрибуты + `fingerprint()`); `ReviewPolicy.gate_reason` (Task 1); объект `asm` с атрибутом `findings_rows: list[dict]` (строки формата `assemble._row`: ключи `file, line, category, severity, confidence, fingerprint, message, is_real, published, inline`).
- Produces: `account_outcomes(candidates, verdicts, verdict_reasons, parsed, kept, deduped, asm, policy) -> list[dict]`. Каждая строка — dict с ключами `assemble._row` + `outcome` (одно из `published_inline|published_summary|verify_rejected|gate_dropped|deduped|already_posted`) + `reject_reason` (`str | None`). Инвариант: `len(result) == len(candidates)`.
  - `candidates: dict[str, Finding]`, `verdicts: dict[str, bool]`, `verdict_reasons: dict[str, str]`, `parsed: list[Finding]` (survived, grounded), `kept: list[Finding]` (прошедшие gate), `deduped: list[Finding]` (выжившие dedup), `policy` (с методом `gate_reason`).
  - Детекция dropped-наборов — по identity (`id()`): `deduped`/`kept`/`parsed` содержат те же объекты, поэтому identity-разность корректна (fingerprint-разность недосчитала бы точные дубли — у них одинаковый fingerprint с выжившим).

- [ ] **Step 1: Написать падающий тест**

Создать `tests/agent/test_outcomes.py`:

```python
"""account_outcomes — учёт терминального исхода каждого кандидата."""
from __future__ import annotations

from types import SimpleNamespace

from reviewer.agent.outcomes import account_outcomes
from reviewer.policy.policy import ReviewPolicy
from reviewer.vcs.base import Finding


def F(msg, *, cat="correctness", sev="high", file="a.py", line=1, conf=0.9):
    return Finding(cat, sev, file, line, "RIGHT", msg, None, conf)


def _asm_row(f: Finding, *, published: bool, inline: bool) -> dict:
    """Строка как её строит assemble._row."""
    return {
        "file": f.file, "line": f.line, "category": f.category,
        "severity": f.severity, "confidence": f.confidence,
        "fingerprint": f.fingerprint(), "message": (f.message or "")[:500],
        "is_real": True, "published": published, "inline": inline,
    }


def test_all_published_inline():
    f = F("bug")
    candidates = {"f1": f}
    asm = SimpleNamespace(findings_rows=[_asm_row(f, published=True, inline=True)])
    rows = account_outcomes(
        candidates, {}, {}, parsed=[f], kept=[f], deduped=[f],
        asm=asm, policy=ReviewPolicy(),
    )
    assert len(rows) == len(candidates)
    assert rows[0]["outcome"] == "published_inline"
    assert rows[0]["reject_reason"] is None


def test_verify_rejected_carries_reason():
    f = F("hallucinated")
    candidates = {"f1": f}
    asm = SimpleNamespace(findings_rows=[])
    rows = account_outcomes(
        candidates, {"f1": False}, {"f1": "line does not exist"},
        parsed=[], kept=[], deduped=[], asm=asm, policy=ReviewPolicy(),
    )
    assert len(rows) == 1
    assert rows[0]["outcome"] == "verify_rejected"
    assert rows[0]["reject_reason"] == "line does not exist"
    assert rows[0]["is_real"] is False
    assert rows[0]["published"] is False


def test_gate_dropped_carries_gate_reason():
    f = F("style nit", sev="low")                    # ниже medium-порога
    candidates = {"f1": f}
    asm = SimpleNamespace(findings_rows=[])
    policy = ReviewPolicy(severity_threshold="medium")
    rows = account_outcomes(
        candidates, {}, {}, parsed=[f], kept=[], deduped=[],
        asm=asm, policy=policy,
    )
    assert rows[0]["outcome"] == "gate_dropped"
    assert rows[0]["reject_reason"].startswith("severity")
    assert rows[0]["is_real"] is True


def test_deduped_dropped_no_reason():
    winner = F("same bug")
    dup = F("same bug")                              # точный дубль → тот же fingerprint
    candidates = {"f1": winner, "f2": dup}
    asm = SimpleNamespace(findings_rows=[_asm_row(winner, published=True, inline=True)])
    rows = account_outcomes(
        candidates, {}, {}, parsed=[winner, dup], kept=[winner, dup],
        deduped=[winner], asm=asm, policy=ReviewPolicy(),
    )
    outcomes = sorted(r["outcome"] for r in rows)
    assert outcomes == ["deduped", "published_inline"]
    deduped_row = next(r for r in rows if r["outcome"] == "deduped")
    assert deduped_row["reject_reason"] is None


def test_already_posted_from_unpublished_asm_row():
    f = F("dup of prior run")
    candidates = {"f1": f}
    asm = SimpleNamespace(findings_rows=[_asm_row(f, published=False, inline=False)])
    rows = account_outcomes(
        candidates, {}, {}, parsed=[f], kept=[f], deduped=[f],
        asm=asm, policy=ReviewPolicy(),
    )
    assert rows[0]["outcome"] == "already_posted"


def test_published_summary():
    f = F("out of diff", line=None)
    candidates = {"f1": f}
    asm = SimpleNamespace(findings_rows=[_asm_row(f, published=True, inline=False)])
    rows = account_outcomes(
        candidates, {}, {}, parsed=[f], kept=[f], deduped=[f],
        asm=asm, policy=ReviewPolicy(),
    )
    assert rows[0]["outcome"] == "published_summary"


def test_zero_candidates():
    asm = SimpleNamespace(findings_rows=[])
    rows = account_outcomes({}, {}, {}, parsed=[], kept=[], deduped=[],
                            asm=asm, policy=ReviewPolicy())
    assert rows == []


def test_full_funnel_sums_to_candidates():
    """Все 6 исходов присутствуют, сумма = числу кандидатов."""
    inline = F("real inline bug")
    summary = F("real out-of-diff bug", line=None)
    already = F("seen before")
    rejected = F("false positive")
    gated = F("low sev", sev="low")
    d_winner = F("dupe")
    d_loser = F("dupe")
    candidates = {
        "f1": inline, "f2": summary, "f3": already,
        "f4": rejected, "f5": gated, "f6": d_winner, "f7": d_loser,
    }
    parsed = [inline, summary, already, gated, d_winner, d_loser]   # rejected исключён
    kept = [inline, summary, already, d_winner, d_loser]            # gated отсеян
    deduped = [inline, summary, already, d_winner]                  # d_loser схлопнут
    asm = SimpleNamespace(findings_rows=[
        _asm_row(inline, published=True, inline=True),
        _asm_row(summary, published=True, inline=False),
        _asm_row(already, published=False, inline=False),
        _asm_row(d_winner, published=True, inline=True),
    ])
    policy = ReviewPolicy(severity_threshold="medium")
    rows = account_outcomes(
        candidates, {"f4": False}, {"f4": "not a bug"},
        parsed, kept, deduped, asm, policy,
    )
    assert len(rows) == len(candidates)
    counts = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    assert counts == {
        "published_inline": 2, "published_summary": 1, "already_posted": 1,
        "verify_rejected": 1, "gate_dropped": 1, "deduped": 1,
    }
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/agent/test_outcomes.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.agent.outcomes'`

- [ ] **Step 3: Реализовать `account_outcomes`**

Создать `reviewer/agent/outcomes.py`:

```python
"""Учёт терминального исхода каждого кандидата-находки для наблюдаемости (PRI).

Одна ответственность: сопоставить кандидату из состояния MCP-сессии на момент
publish его терминальный исход воронки и построить строку для review_findings.
Чистая функция — без сети/БД, детерминирована, тестируется изолированно.

Воронка (6 исходов, сумма = числу кандидатов):
  verify_rejected  — verdicts[fid] is False; reject_reason = verdict_reasons[fid]
  gate_dropped     — survived (parsed), но not policy.gate(f); reject_reason = gate_reason(f)
  deduped          — прошли gate, но схлопнуты dedup_findings (kept ∖ deduped по identity)
  already_posted   — из asm.findings_rows со скрытым fingerprint прошлого прогона (published=False)
  published_inline / published_summary — из asm.findings_rows по флагу inline

Замечание про строки: grounding применяется только к survived; у verify_rejected
кандидатов строка исходная (не грунтованная) — допустимо (запись, не публикация).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reviewer.vcs.base import Finding


def _row(f: "Finding", outcome: str, reject_reason: str | None, *, is_real: bool) -> dict:
    """Строка review_findings для отклонённого кандидата (поля как в assemble._row)."""
    return {
        "file": f.file,
        "line": f.line,
        "category": f.category,
        "severity": f.severity,
        "confidence": f.confidence,
        "fingerprint": f.fingerprint(),
        "message": (f.message or "")[:500],
        "is_real": is_real,
        "published": False,
        "inline": False,
        "outcome": outcome,
        "reject_reason": reject_reason,
    }


def _asm_outcome(row: dict) -> str:
    """Исход строки assemble.findings_rows: published=False ⟺ already_posted."""
    if not row["published"]:
        return "already_posted"
    return "published_inline" if row["inline"] else "published_summary"


def account_outcomes(
    candidates: dict,
    verdicts: dict,
    verdict_reasons: dict,
    parsed: list,
    kept: list,
    deduped: list,
    asm,
    policy,
) -> list[dict]:
    """Построить полный список строк review_findings по терминальным исходам.

    См. модульный докстринг. Инвариант: ``len(result) == len(candidates)``.
    """
    rows: list[dict] = []

    # 1) verify_rejected — кандидаты с явным is_real=false.
    for fid, f in candidates.items():
        if verdicts.get(fid) is False:
            rows.append(_row(f, "verify_rejected", verdict_reasons.get(fid), is_real=False))

    # 2) gate_dropped — survived (parsed), не прошедшие gate. Identity-разность:
    #    kept ⊆ parsed по тем же объектам (kept = [f for f in parsed if gate(f)]).
    kept_ids = {id(f) for f in kept}
    for f in parsed:
        if id(f) not in kept_ids:
            rows.append(_row(f, "gate_dropped", policy.gate_reason(f), is_real=True))

    # 3) deduped — прошли gate, но схлопнуты dedup. deduped ⊆ kept по identity
    #    (dedup_findings возвращает те же объекты). Fingerprint-разность здесь
    #    неверна: точный дубль имеет ТОТ ЖЕ fingerprint, что и выживший.
    deduped_ids = {id(f) for f in deduped}
    for f in kept:
        if id(f) not in deduped_ids:
            rows.append(_row(f, "deduped", None, is_real=True))

    # 4) published_* / already_posted — готовые строки assemble (по одной на deduped).
    for row in asm.findings_rows:
        rows.append({**row, "outcome": _asm_outcome(row), "reject_reason": None})

    return rows
```

- [ ] **Step 4: Прогнать тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/agent/test_outcomes.py -q`
Expected: PASS (8 тестов)

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/agent/outcomes.py tests/agent/test_outcomes.py
git add reviewer/agent/outcomes.py tests/agent/test_outcomes.py
git commit -m "feat(agent): account_outcomes — учёт терминального исхода находок"
```

---

### Task 4: Миграция схемы + чтение/запись `outcome`/`reject_reason`

**Files:**
- Modify: `reviewer/web/schema.sql:36-52` (таблица `review_findings` + миграция + бэкфилл)
- Modify: `reviewer/web/history.py:107-115` (`finding_sql` INSERT), `reviewer/web/history.py:132-135` (нормализация дефолтов), `reviewer/web/history.py:224-228` (`get_run` `finding_sql` SELECT)
- Test: `tests/web/test_history.py`

**Interfaces:**
- Consumes: `record_run(run, findings, steps)` получает строки, часть которых несёт ключи `outcome`/`reject_reason` (из `account_outcomes`, Task 5), а часть (старые вызовы/тестовые фикстуры) — нет.
- Produces: `review_findings` имеет nullable-колонки `outcome TEXT` и `reject_reason TEXT`. `record_run` подставляет `None` для отсутствующих ключей (back-compat). `get_run` возвращает эти колонки в каждой находке. Идемпотентная миграция + best-effort бэкфилл опубликованных исторических строк.

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/web/test_history.py` — сначала unit (fail-soft round-trip дефолтов через мок не нужен; проверяем нормализацию ключей напрямую) и integration:

```python
def test_record_run_defaults_missing_outcome_keys():
    """record_run не падает на находках без outcome/reject_reason (back-compat)."""
    history = ReviewHistory("postgresql://bad:bad@localhost:1/nonexistent")
    captured = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def executemany(self, sql, rows): captured["rows"] = rows

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, row=None):
            class _R:
                def fetchone(self_inner): return (1,)
            return _R()
        def cursor(self): return _Cur()
        def commit(self): pass

    with patch.object(history, "_connect", return_value=_Conn()):
        rid = history.record_run(_sample_run(), _sample_findings())
    assert rid == 1
    # _sample_findings() без outcome → в строках дефолт None по обоим ключам.
    assert all(r["outcome"] is None and r["reject_reason"] is None
               for r in captured["rows"])


@pytest.mark.integration
def test_record_and_get_run_persists_outcome():
    """outcome/reject_reason пишутся и читаются обратно."""
    from reviewer.config.settings import Settings
    history = ReviewHistory(Settings().pg_dsn)
    history.init_schema()

    findings = [
        {**_sample_findings()[0], "outcome": "published_inline", "reject_reason": None},
        {"file": "reviewer/x.py", "line": 3, "category": "correctness",
         "severity": "high", "confidence": 0.9, "is_real": False,
         "published": False, "inline": False, "fingerprint": "rej1",
         "message": "false positive", "outcome": "verify_rejected",
         "reject_reason": "line does not exist"},
    ]
    run_id = history.record_run(_sample_run(), findings)
    result = history.get_run(run_id)
    by_outcome = {f["outcome"]: f for f in result["findings"]}
    assert by_outcome["published_inline"]["reject_reason"] is None
    assert by_outcome["verify_rejected"]["reject_reason"] == "line does not exist"


@pytest.mark.integration
def test_schema_idempotent_and_backfill():
    """Повторный init_schema безопасен; бэкфилл проставляет исход старым published."""
    from reviewer.config.settings import Settings
    history = ReviewHistory(Settings().pg_dsn)
    history.init_schema()
    history.init_schema()   # второй раз не падает (ADD COLUMN IF NOT EXISTS)

    # Строка без outcome (эмулируем legacy): published+inline → бэкфилл published_inline.
    findings = [{**_sample_findings()[0]}]   # без ключа outcome → NULL при вставке
    run_id = history.record_run(_sample_run(), findings)
    # Бэкфилл проставляет исход опубликованным строкам при следующем init_schema.
    history.init_schema()
    result = history.get_run(run_id)
    assert result["findings"][0]["outcome"] == "published_inline"
```

- [ ] **Step 2: Прогнать unit-тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/web/test_history.py -q -k defaults_missing_outcome`
Expected: FAIL — `KeyError: 'outcome'` (INSERT-параметр `%(outcome)s` без ключа в dict).

- [ ] **Step 3: Расширить схему (миграция + бэкфилл)**

В `reviewer/web/schema.sql` заменить блок `CREATE TABLE ... review_findings (...)` и добавить после него миграцию. Заменить строки 36-52 на:

```sql
CREATE TABLE IF NOT EXISTS review_findings (
    id          BIGSERIAL    PRIMARY KEY,
    run_id      BIGINT       NOT NULL
                    REFERENCES review_runs (id) ON DELETE CASCADE,
    file        TEXT         NOT NULL,
    line        INT,
    category    TEXT         NOT NULL,
    severity    TEXT         NOT NULL,
    confidence  REAL         NOT NULL DEFAULT 0,
    is_real     BOOL         NOT NULL DEFAULT true,
    published   BOOL         NOT NULL DEFAULT false,
    inline      BOOL         NOT NULL DEFAULT false,
    fingerprint TEXT,
    message     TEXT,
    outcome     TEXT,        -- терминальный исход воронки (PRI); NULL = legacy
    reject_reason TEXT       -- причина reject (verify/gate); NULL = не отклонена/legacy
);

-- Идемпотентная миграция для БД, где таблица уже существовала без колонок исхода.
ALTER TABLE review_findings ADD COLUMN IF NOT EXISTS outcome       TEXT;
ALTER TABLE review_findings ADD COLUMN IF NOT EXISTS reject_reason TEXT;

-- Best-effort бэкфилл: опубликованным историческим строкам ставим исход по флагам.
-- NOT published (already_posted и находки error-прогонов) неоднозначны → NULL.
UPDATE review_findings SET outcome = 'published_inline'
    WHERE outcome IS NULL AND published AND inline;
UPDATE review_findings SET outcome = 'published_summary'
    WHERE outcome IS NULL AND published AND NOT inline;

CREATE INDEX IF NOT EXISTS review_findings_run_id ON review_findings (run_id);
```

- [ ] **Step 4: Расширить `record_run` (нормализация дефолтов + INSERT)**

В `reviewer/web/history.py` заменить `finding_sql` (строки 107-115) на:

```python
            finding_sql = """
            INSERT INTO review_findings (
                run_id, file, line, category, severity, confidence,
                is_real, published, inline, fingerprint, message,
                outcome, reject_reason
            ) VALUES (
                %(run_id)s, %(file)s, %(line)s, %(category)s, %(severity)s, %(confidence)s,
                %(is_real)s, %(published)s, %(inline)s, %(fingerprint)s, %(message)s,
                %(outcome)s, %(reject_reason)s
            )
            """
```

В том же методе заменить сборку `rows` (строка 133) на нормализацию дефолтов — было:

```python
                if findings:
                    rows = [{**f, "run_id": run_id} for f in findings]
```

стало:

```python
                if findings:
                    # Дефолты outcome/reject_reason для строк без этих ключей
                    # (старые вызовы / тестовые фикстуры) — back-compat.
                    rows = [
                        {"outcome": None, "reject_reason": None, **f, "run_id": run_id}
                        for f in findings
                    ]
```

- [ ] **Step 5: Расширить `get_run` SELECT**

В `reviewer/web/history.py` заменить `finding_sql` в `get_run` (строки 224-228) на:

```python
        finding_sql = """
        SELECT id, file, line, category, severity, confidence,
               is_real, published, inline, fingerprint, message,
               outcome, reject_reason
        FROM review_findings WHERE run_id = %(run_id)s ORDER BY id
        """
```

- [ ] **Step 6: Прогнать unit-тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/web/test_history.py -q -k defaults_missing_outcome`
Expected: PASS

- [ ] **Step 7: Прогнать integration-тесты истории (нужен test-профиль)**

```bash
docker compose --profile test up -d --wait paradedb-test
.venv/bin/pytest tests/web/test_history.py -q -m integration
```
Expected: PASS (round-trip outcome, идемпотентность+бэкфилл, существующие integration-тесты).

Если ParadeDB-test недоступен — зафиксировать в отчёте и прогнать позже; unit-часть (Step 6) достаточна для коммита.

- [ ] **Step 8: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/web/history.py tests/web/test_history.py
git add reviewer/web/schema.sql reviewer/web/history.py tests/web/test_history.py
git commit -m "feat(web): outcome/reject_reason в review_findings — схема, запись, чтение"
```

---

### Task 5: Интеграция `account_outcomes` в `publish_review`

**Files:**
- Modify: `reviewer/mcp/service.py` — импорт (верх файла), вызов `_record_history` (строки 1062-1068), сигнатура и тело `_record_history` (строки 1107-1200)
- Test: `tests/mcp/test_publish.py`

**Interfaces:**
- Consumes: `account_outcomes` (Task 3); `_Session.verdict_reasons` (Task 2); `ReviewPolicy.gate_reason` (Task 1, транзитивно через account_outcomes); колонки БД (Task 4).
- Produces: `publish_review` персистит ВСЕ кандидаты (verify_rejected/gate_dropped/deduped/already_posted/published) с `outcome`+`reject_reason`. `_record_history` получает новые параметры `parsed: list[Finding]`, `kept: list[Finding]` и строит строки через `account_outcomes` внутри fail-soft try. При `status=='error'` все строки помечаются `published=False`, но `outcome` (намеченный исход) сохраняется. Отчёт `publish_review` (счётчики) не меняется.

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/mcp/test_publish.py`:

```python
def _outcomes(history):
    """Список outcome по строкам первого прогона фейковой истории."""
    return [r["outcome"] for r in history.findings[0]]


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_persists_verify_rejected_with_reason(_ov, _ch) -> None:
    svc, _, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(
        svc, "o/r", 7, [RAW], dry_run=True,
        verdicts=[{"id": "f1", "is_real": False, "reason": "line does not exist"}],
    )
    assert report["verify_rejected"] == 1
    rows = history.findings[0]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "verify_rejected"
    assert rows[0]["reject_reason"] == "line does not exist"
    assert rows[0]["published"] is False


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_persists_gate_dropped_with_reason(_ov, _ch) -> None:
    svc, _, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    low = dict(RAW, severity="low")                  # ниже medium-порога
    report = _submit_then_publish(svc, "o/r", 7, [low], dry_run=True)
    assert report["dropped_by_gate"] == 1
    rows = history.findings[0]
    gate_rows = [r for r in rows if r["outcome"] == "gate_dropped"]
    assert len(gate_rows) == 1
    assert gate_rows[0]["reject_reason"].startswith("severity")


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_outcomes_sum_to_candidates(_ov, _ch) -> None:
    """len(findings_rows) == число кандидатов (инвариант воронки)."""
    svc, _, history = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    # 3 кандидата: 1 published_inline, 1 verify_rejected, 1 gate_dropped(low).
    pack = [RAW,
            dict(RAW, message="reject me"),
            dict(RAW, severity="low", message="low sev")]
    _submit_then_publish(svc, "o/r", 7, pack, dry_run=True,
                         verdicts=[{"id": "f2", "is_real": False}])
    rows = history.findings[0]
    assert len(rows) == 3
    assert sorted(_outcomes(history)) == [
        "gate_dropped", "published_inline", "verify_rejected"]


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_error_keeps_outcome_but_unpublishes(_ov, _ch) -> None:
    """При status=error published=False у всех строк, но outcome сохранён."""
    svc, _, history = _make_mcp_service_with_publish(vcs_fails=True)
    svc.prepare_review("o/r", 7)
    _submit_then_publish(svc, "o/r", 7, [RAW])       # реальная публикация → VCS падает
    rows = history.findings[0]
    assert history.runs[0]["status"] == "error"
    assert all(r["published"] is False for r in rows)
    # Намеченный исход не затёрт: инлайновая находка осталась published_inline.
    assert rows[0]["outcome"] == "published_inline"
```

Скорректировать существующий `test_publish_cleans_overlay_even_on_vcs_error` — он уже проверяет `all(row["published"] is False ...)`, что остаётся истинным; править не нужно.

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/mcp/test_publish.py -q -k "outcome or verify_rejected_with_reason or gate_dropped_with_reason or sum_to_candidates"`
Expected: FAIL — `KeyError: 'outcome'` (строки истории пока без ключа `outcome`).

- [ ] **Step 3: Импортировать `account_outcomes` в service.py**

Найти блок импортов из `reviewer.agent` в `reviewer/mcp/service.py` (там уже импортируются `assemble_review`, `annotate_centrality`, `dedup_findings`). Добавить к ним:

```python
from reviewer.agent.outcomes import account_outcomes
```

(Если отдельной строки под `reviewer.agent` нет — добавить импорт рядом с `from reviewer.agent.assemble import ...`.)

- [ ] **Step 4: Прокинуть `parsed`/`kept` в вызов `_record_history`**

В `publish_review` заменить вызов (`reviewer/mcp/service.py:1062-1068`):

```python
        run_id = self._record_history(
            repo, pr, p, list(s.candidates.values()), deduped, asm,
            verify_rejected=verify_rejected,
            dry_run=dry_run, posted=posted, error=error,
            session=s,
            metadata=metadata,
            parsed=parsed,
            kept=kept,
        )
```

- [ ] **Step 5: Расширить сигнатуру и тело `_record_history`**

В `reviewer/mcp/service.py` в сигнатуру `_record_history` (после `metadata: _RunMetadata | None = None`, строка 1121) добавить два keyword-параметра:

```python
        metadata: _RunMetadata | None = None,
        parsed: list[Finding] | None = None,
        kept: list[Finding] | None = None,
    ) -> int | None:
```

Заменить блок сбора `rows` (`reviewer/mcp/service.py:1196-1199`) на построение через `account_outcomes` с error-override:

```python
            finding_rows = account_outcomes(
                session.candidates, session.verdicts, session.verdict_reasons,
                parsed or [], kept or [], deduped, asm, p.policy,
            )
            # При сбое публикации (status=error) фактически ничего не ушло:
            # снимаем published, но намеченный outcome сохраняем (что хотели).
            rows = (
                [dict(r, published=False) for r in finding_rows]
                if status == "error" else finding_rows
            )
            return history.record_run(run, rows, steps=all_steps or None)
```

- [ ] **Step 6: Прогнать тесты publish — убедиться, что проходят (и старые не сломались)**

Run: `.venv/bin/pytest tests/mcp/test_publish.py tests/mcp/test_submit_tools.py -q`
Expected: PASS (новые + все существующие, включая `test_publish_cleans_overlay_even_on_vcs_error`, `test_publish_dedups_near_identical_findings`).

- [ ] **Step 7: Прогнать полный unit-набор mcp/agent/policy/web — регрессия**

Run: `.venv/bin/pytest tests/mcp tests/agent tests/policy tests/web -q`
Expected: PASS (integration исключены по умолчанию через `addopts = -m 'not integration'`).

- [ ] **Step 8: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/mcp/service.py tests/mcp/test_publish.py
git add reviewer/mcp/service.py tests/mcp/test_publish.py
git commit -m "feat(mcp): publish_review персистит все исходы находок через account_outcomes"
```

---

### Task 6: Инструкция про `reason` в verify-промпте + guard-тест

**Files:**
- Modify: `plugin/skills/review-pr/references/verify-prompt.md`
- Test: `tests/skills/test_assembled_prompts.py`

**Interfaces:**
- Consumes: guard-тест собирает промпт функцией `assemble("review-pr/references/verify-prompt.md")` (уже есть в файле).
- Produces: verify-промпт инструктирует верификатора при `is_real=false` передавать одну строку причины в поле `reason` тула `submit_verdicts`. Guard-тест закрепляет наличие инструкции (регрессия на «сборка промпта потеряла reason»).

- [ ] **Step 1: Написать падающий guard-тест**

Добавить в `tests/skills/test_assembled_prompts.py` в тест `test_verify_keeps_verdicts_schema_and_tools` дополнительную проверку (или отдельный тест):

```python
def test_verify_prompt_instructs_reject_reason():
    v = assemble("review-pr/references/verify-prompt.md")
    # PRI: при is_real=false верификатор обязан дать краткую причину в reason.
    assert "reason" in v
    assert "is_real=false" in v or 'is_real": false' in v.lower()
    # инструкция связывает reason именно с отклонением
    lowered = v.lower()
    assert "reason" in lowered and "kill" in lowered or "reject" in lowered
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_assembled_prompts.py -q -k reject_reason`
Expected: FAIL — `assert "reason" in v` (в текущем промпте слова `reason` нет).

- [ ] **Step 3: Дополнить verify-промпт инструкцией про `reason`**

В `plugin/skills/review-pr/references/verify-prompt.md` заменить финальный блок (строки 42-45, начинающийся «Read the candidate findings…») на:

```markdown
Read the candidate findings via `get_candidate_findings(repo, pr)` (each has a stable
`id`). For each, submit your decision via `submit_verdicts(repo, pr, verdicts=[{"id": "<id>", "is_real": true|false, "reason": "<one line>"}])`.

When you set `is_real=false` (you kill/reject a finding), you MUST include a short
one-line `reason` naming which rule fired — e.g. "quoted line not in new version",
"pre-existing, outside the diff", "already handled in shown context", "pure style, no
behaviour change". The `reason` is persisted for observability (why the finding was
rejected); keep it terse and factual. For `is_real=true` the `reason` is optional.

Submit a verdict only for findings you decide to kill or explicitly keep; a finding
with no verdict is kept (recall-safe). Do NOT return verdicts as text.
```

- [ ] **Step 4: Прогнать guard-тесты verify — убедиться, что проходят**

Run: `.venv/bin/pytest tests/skills/test_assembled_prompts.py -q -k verify`
Expected: PASS (новый `test_verify_prompt_instructs_reject_reason` + существующий `test_verify_keeps_verdicts_schema_and_tools`).

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check tests/skills/test_assembled_prompts.py
git add plugin/skills/review-pr/references/verify-prompt.md tests/skills/test_assembled_prompts.py
git commit -m "feat(plugin): verify-промпт требует reason при is_real=false"
```

---

## Финальная проверка (после всех задач)

- [ ] **Полный unit-прогон**

Run: `.venv/bin/pytest -q`
Expected: PASS (весь набор без integration).

- [ ] **Integration-прогон истории (если ещё не сделан в Task 4 Step 7)**

```bash
docker compose --profile test up -d --wait paradedb-test neo4j-test
.venv/bin/pytest -q -m integration -k history
docker compose --profile test rm -sfv paradedb-test neo4j-test
```
Expected: PASS. (Никогда не использовать `--profile test down -v` — снесёт dev-тома.)

- [ ] **Линт затронутых файлов**

Run: `.venv/bin/ruff check reviewer/policy/policy.py reviewer/mcp/schemas.py reviewer/mcp/service.py reviewer/agent/outcomes.py reviewer/web/history.py`
Expected: чисто по изменённым файлам.

- [ ] **Обновить документацию** (memory `update-readme-en-ru-on-changes`): добавить в `README.md` (EN) и `README.ru.md` (RU) короткую заметку про персист исходов находок (`review_findings.outcome`/`reject_reason`) в разделе про наблюдаемость `reviewer/web/`; при желании — строку в CLAUDE.md «Неочевидные факты». Коммит: `docs: персист исходов находок в наблюдаемости (README EN/RU)`.

---

## Self-Review (проведён при написании плана)

**1. Покрытие спеки:**
- Схема БД (+2 колонки, бэкфилл, идемпотентность) → Task 4. ✓
- `account_outcomes` (6 исходов, инвариант) → Task 3. ✓
- Причина reject client+server (`VerdictIn.reason`, `verdict_reasons`, `gate_reason`) → Tasks 1, 2. ✓
- Интеграция в publish + error-обработка (outcome сохранён, published=False) → Task 5. ✓
- Чтение (`history.py` INSERT/SELECT) → Task 4. ✓
- verify-промпт + guard-тест → Task 6. ✓
- Тесты по `tests/{agent,policy,web,mcp,skills}` → распределены по задачам. ✓

**2. Разрешение открытых вопросов спеки:**
- *deduped dropped-набор*: выбран identity-diff (`id()`), НЕ fingerprint и НЕ расширение `dedup_findings`. Обоснование зафиксировано в коде и плане: точные дубли имеют одинаковый fingerprint с выжившим → fingerprint-разность недосчитала бы их; `dedup_findings` возвращает те же объекты, поэтому identity корректна. Сигнатура `dedup_findings` не тронута.
- *outcome при status=error*: `account_outcomes` возвращает намеченные исходы (published отражает интент assemble); `_record_history` применяет error-override `published=False`, сохраняя `outcome`. Тест `test_publish_error_keeps_outcome_but_unpublishes`.
- *имя reference-файла verify-промпта*: подтверждено — `plugin/skills/review-pr/references/verify-prompt.md`.
- *формулировка gate_reason*: человекочитаемые строки со стабильными префиксами (`category`/`severity`/`confidence`/`path`); тесты — на `.startswith`, не на полное совпадение (устойчивость к float-форматированию).

**3. Уточнение расположения относительно спеки:** спека называла `reviewer/mcp/state.py` для `_Session` — фактически `_Session` в `reviewer/mcp/service.py`. План правит service.py (Task 2). Serde сессии (`session_serde.py`) сериализует только `PreparedReview`, поэтому новое in-memory поле `verdict_reasons` его не затрагивает — правок serde не требуется.

**4. Согласованность типов:** `account_outcomes(candidates, verdicts, verdict_reasons, parsed, kept, deduped, asm, policy)` — сигнатура идентична в Task 3 (определение), Task 5 (вызов) и спеке. Строки исхода единообразны во всех задачах: `published_inline|published_summary|verify_rejected|gate_dropped|deduped|already_posted`. Ключи строки review_findings совпадают с `assemble._row` + `outcome`/`reject_reason`.
