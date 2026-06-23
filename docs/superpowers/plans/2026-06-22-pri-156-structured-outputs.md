# PRI-156 Structured Outputs (schema-enforced findings/verdicts) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перевести вывод субагентов analyze/dimension/verify с free-text JSON на schema-enforced через MCP-тулы `submit_findings`/`submit_verdicts`, с единым Pydantic-источником схемы и сессионным накоплением находок.

**Architecture:** Субагенты вызывают MCP-тулы (FastMCP/Pydantic валидирует аргументы по схеме → энфорс на тул-границе), находки копятся в `_Session` с server-assigned id; `publish_review` читает их из сессии (параметр `findings` убран); отсутствие verdict = keep (recall-safe структурно).

**Tech Stack:** Python 3.11+, Pydantic v2, FastMCP (`mcp.server.fastmcp`), pytest.

## Global Constraints

- Python 3.11–3.13; ruff line-length 100, target py311 (`.venv/bin/ruff check .`).
- Проект на русском: комментарии, докстринги, сообщения CLI.
- Коммиты: Conventional Commits на русском (`feat(mcp): …`), **без self-attribution** (никаких Co-Authored-By/Claude).
- `pytest` по умолчанию исключает integration (`-m 'not integration'`); все unit-тесты на фейках.
- Guard-тесты `tests/skills/` держать зелёными.
- Pydantic v2 (проект уже на pydantic-settings → pydantic v2).
- Единый источник LLM-facing схемы — `FindingIn`/`VerdictIn`; коэрция **мягкая** = дословный порт `_finding_from_dict` (сохраняет PRI-144).
- Ветка: `feat/structured-outputs-pri-156`.

---

### Task 1: Pydantic-схемы findings/verdicts (`reviewer/mcp/schemas.py`)

Новый модуль — канонический источник LLM-facing схемы. Валидаторы — дословный порт `_finding_from_dict` (service.py:50-107): мягкая коэрция, только `file` обязателен.

**Files:**
- Create: `reviewer/mcp/schemas.py`
- Test: `tests/mcp/test_schemas.py`

**Interfaces:**
- Produces: `FixIn`, `FindingIn`, `VerdictIn` (Pydantic BaseModel).
  - `FindingIn` поля: `file: str` (required), `category: str="correctness"`, `severity: str="medium"`, `side: str="RIGHT"`, `line: int|None=None`, `code_quote: str|None=None`, `message: str=""`, `suggestion: str|None=None`, `confidence: float=0.1`, `fix: FixIn|None=None`.
  - `FixIn` поля: `start_line: int|None`, `end_line: int|None`, `replacement: str|None`.
  - `VerdictIn` поля: `id: str`, `is_real: bool`.

- [ ] **Step 1: Написать падающий тест коэрции (паритет с `_finding_from_dict`)**

Create `tests/mcp/test_schemas.py`:

```python
"""Тесты FindingIn/VerdictIn — мягкая коэрция (порт _finding_from_dict, PRI-144)."""
from __future__ import annotations

import pytest

from reviewer.mcp.schemas import FindingIn, FixIn, VerdictIn

BASE = {"file": "a.py", "severity": "high", "message": "m", "line": 1, "code_quote": "x = 1"}


def test_confidence_coercion_and_clamp():
    # валидные проходят как есть
    assert FindingIn.model_validate({**BASE, "confidence": 0.9}).confidence == 0.9
    assert FindingIn.model_validate({**BASE, "confidence": 0.5}).confidence == 0.5
    # не оценено / None / мусор → 0.1
    assert FindingIn.model_validate(BASE).confidence == 0.1
    assert FindingIn.model_validate({**BASE, "confidence": None}).confidence == 0.1
    assert FindingIn.model_validate({**BASE, "confidence": "abc"}).confidence == 0.1
    # clamp в [0,1]
    assert FindingIn.model_validate({**BASE, "confidence": 1.5}).confidence == 1.0
    assert FindingIn.model_validate({**BASE, "confidence": -0.2}).confidence == 0.0


def test_severity_side_defaults():
    assert FindingIn.model_validate({**BASE, "severity": "urgent"}).severity == "medium"
    assert FindingIn.model_validate({**BASE, "severity": ["high"]}).severity == "medium"
    assert FindingIn.model_validate({**BASE, "side": "MIDDLE"}).side == "RIGHT"
    assert FindingIn.model_validate({**BASE, "side": "LEFT"}).side == "LEFT"


def test_line_coercion():
    assert FindingIn.model_validate({**BASE, "line": "42"}).line == 42
    assert FindingIn.model_validate({**BASE, "line": "abc"}).line is None
    assert FindingIn.model_validate({**BASE, "line": None}).line is None


def test_suggestion_and_codequote_nonstring_to_none():
    assert FindingIn.model_validate({**BASE, "suggestion": 42}).suggestion is None
    assert FindingIn.model_validate({**BASE, "suggestion": "do x"}).suggestion == "do x"
    assert FindingIn.model_validate({**BASE, "code_quote": 7}).code_quote is None


def test_fix_dropped_when_incomplete():
    ok = FindingIn.model_validate({**BASE, "fix": {"start_line": 1, "end_line": 2, "replacement": "z"}})
    assert ok.fix == FixIn(start_line=1, end_line=2, replacement="z")
    # нестроковый replacement → вся fix отбрасывается
    assert FindingIn.model_validate({**BASE, "fix": {"start_line": 1, "end_line": 2, "replacement": 9}}).fix is None
    # мусорный start_line → вся fix отбрасывается
    assert FindingIn.model_validate({**BASE, "fix": {"start_line": "x", "end_line": 2, "replacement": "z"}}).fix is None
    assert FindingIn.model_validate({**BASE, "fix": None}).fix is None


def test_file_required():
    with pytest.raises(Exception):
        FindingIn.model_validate({"severity": "high", "message": "no file"})


def test_category_default():
    assert FindingIn.model_validate(BASE).category == "correctness"
    assert FindingIn.model_validate({**BASE, "category": "security"}).category == "security"


def test_verdict_in():
    v = VerdictIn.model_validate({"id": "f3", "is_real": False})
    assert v.id == "f3" and v.is_real is False
    with pytest.raises(Exception):
        VerdictIn.model_validate({"id": "f3"})   # is_real required
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_schemas.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.mcp.schemas'`.

- [ ] **Step 3: Реализовать `reviewer/mcp/schemas.py`**

```python
"""Pydantic-схемы LLM-facing вывода ревью — единый источник (PRI-156).

``FindingIn``/``VerdictIn`` — канон схемы findings/verdicts: из них FastMCP
выводит схему тулов ``submit_findings``/``submit_verdicts`` (энфорс на тул-границе),
из них же строится внутренний ``Finding`` (``Finding.from_in``). Валидаторы —
дословный порт ``_finding_from_dict`` (мягкая коэрция, сохраняет PRI-144):
кривые значения коэрцируются с дефолтами, обязателен только ``file``.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

_VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})


def _coerce_int(value) -> int | None:
    """int-коэрция LLM-значения: int("42") → 42, None/мусор → None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class FixIn(BaseModel):
    """Applyable-правка: точная замена диапазона строк НОВОЙ версии (RIGHT)."""

    model_config = ConfigDict(extra="ignore")

    start_line: int | None = None
    end_line: int | None = None
    replacement: str | None = None


class FindingIn(BaseModel):
    """LLM-facing находка. Мягкая коэрция = порт ``_finding_from_dict``."""

    model_config = ConfigDict(extra="ignore")

    file: str
    category: str = "correctness"
    severity: str = "medium"
    side: str = "RIGHT"
    line: int | None = None
    code_quote: str | None = None
    message: str = ""
    suggestion: str | None = None
    confidence: float = 0.1
    fix: FixIn | None = None

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v):
        return str(v) if v else "correctness"

    @field_validator("severity", mode="before")
    @classmethod
    def _severity(cls, v):
        return v if isinstance(v, str) and v in _VALID_SEVERITIES else "medium"

    @field_validator("side", mode="before")
    @classmethod
    def _side(cls, v):
        return v if v in ("RIGHT", "LEFT") else "RIGHT"

    @field_validator("line", mode="before")
    @classmethod
    def _line(cls, v):
        return _coerce_int(v)

    @field_validator("code_quote", mode="before")
    @classmethod
    def _code_quote(cls, v):
        return v if isinstance(v, str) else None

    @field_validator("message", mode="before")
    @classmethod
    def _message(cls, v):
        return str(v) if v else ""

    @field_validator("suggestion", mode="before")
    @classmethod
    def _suggestion(cls, v):
        return v if isinstance(v, str) else None

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, v):
        try:
            c = float(v)
        except (TypeError, ValueError):
            c = 0.1   # не оценено = спекулятивно (ниже честного потолка 0.4) → отсекается гейтом
        return max(0.0, min(1.0, c))

    @field_validator("fix", mode="before")
    @classmethod
    def _fix(cls, v):
        # Кривая/неполная fix отбрасывается целиком (как в _finding_from_dict).
        if not isinstance(v, dict):
            return None
        start = _coerce_int(v.get("start_line"))
        end = _coerce_int(v.get("end_line"))
        replacement = v.get("replacement")
        if start is None or end is None or not isinstance(replacement, str):
            return None
        return {"start_line": start, "end_line": end, "replacement": replacement}


class VerdictIn(BaseModel):
    """Вердикт verify по находке с server-assigned id."""

    model_config = ConfigDict(extra="ignore")

    id: str
    is_real: bool
```

- [ ] **Step 4: Запустить тест — убедиться, что проходит + ruff**

Run: `.venv/bin/pytest tests/mcp/test_schemas.py -q && .venv/bin/ruff check reviewer/mcp/schemas.py tests/mcp/test_schemas.py`
Expected: PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add reviewer/mcp/schemas.py tests/mcp/test_schemas.py
git commit -m "feat(mcp): Pydantic-схемы FindingIn/VerdictIn — канон LLM-facing вывода (PRI-156)"
```

---

### Task 2: `Finding.from_in` — конструктор из FindingIn (`reviewer/vcs/base.py`)

Внутренний `Finding` строится из валидированного `FindingIn` (+`centrality=0.0`). Дакт-тайпинг (без runtime-импорта `FindingIn`, чтобы не плодить обратную зависимость vcs→mcp).

**Files:**
- Modify: `reviewer/vcs/base.py:41-61` (dataclass `Finding`)
- Test: `tests/vcs/test_finding_from_in.py`

**Interfaces:**
- Consumes: `FindingIn` (Task 1) — читается по атрибутам.
- Produces: `Finding.from_in(fi) -> Finding`.

- [ ] **Step 1: Написать падающий тест**

Create `tests/vcs/test_finding_from_in.py`:

```python
"""Finding.from_in: построение внутреннего Finding из валидированного FindingIn."""
from reviewer.mcp.schemas import FindingIn
from reviewer.vcs.base import Finding


def test_from_in_maps_fields_and_fix():
    fi = FindingIn.model_validate({
        "file": "a.py", "severity": "high", "line": 2, "code_quote": "x = 1",
        "message": "bug", "suggestion": "fix it", "confidence": 0.9,
        "fix": {"start_line": 2, "end_line": 2, "replacement": "x = 2"},
    })
    f = Finding.from_in(fi)
    assert isinstance(f, Finding)
    assert (f.file, f.line, f.side, f.severity) == ("a.py", 2, "RIGHT", "high")
    assert f.message == "bug" and f.suggestion == "fix it" and f.confidence == 0.9
    assert (f.fix_start, f.fix_end, f.replacement) == (2, 2, "x = 2")
    assert f.code_quote == "x = 1"
    assert f.centrality == 0.0


def test_from_in_without_fix():
    fi = FindingIn.model_validate({"file": "b.py", "message": "m"})
    f = Finding.from_in(fi)
    assert (f.fix_start, f.fix_end, f.replacement) == (None, None, None)
    assert f.category == "correctness" and f.confidence == 0.1
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/vcs/test_finding_from_in.py -q`
Expected: FAIL — `AttributeError: type object 'Finding' has no attribute 'from_in'`.

- [ ] **Step 3: Добавить `from_in` в `Finding`**

В `reviewer/vcs/base.py` добавить вверху файла (после существующих импортов, строка 3 — `from typing import Literal, Protocol`):

```python
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from reviewer.mcp.schemas import FindingIn
```

В классе `Finding` (после метода `fingerprint`, строка 61) добавить classmethod:

```python
    @classmethod
    def from_in(cls, fi: "FindingIn") -> "Finding":
        """Построить внутренний Finding из валидированного FindingIn (PRI-156).

        Дакт-тайпинг по атрибутам: без runtime-импорта FindingIn (vcs не зависит
        от mcp). centrality стартует с 0.0 (проставляется графом в publish_review).
        """
        fix = fi.fix
        return cls(
            category=fi.category,
            severity=fi.severity,
            file=fi.file,
            line=fi.line,
            side=fi.side,
            message=fi.message,
            suggestion=fi.suggestion,
            confidence=fi.confidence,
            fix_start=fix.start_line if fix else None,
            fix_end=fix.end_line if fix else None,
            replacement=fix.replacement if fix else None,
            code_quote=fi.code_quote,
        )
```

- [ ] **Step 4: Запустить — убедиться, что проходит + ruff**

Run: `.venv/bin/pytest tests/vcs/test_finding_from_in.py -q && .venv/bin/ruff check reviewer/vcs/base.py`
Expected: PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add reviewer/vcs/base.py tests/vcs/test_finding_from_in.py
git commit -m "feat(vcs): Finding.from_in — конструктор из валидированного FindingIn (PRI-156)"
```

---

### Task 3: Сессионное накопление + submit-методы (`reviewer/mcp/service.py`)

Расширить `_Session` (candidates/verdicts/_seq) и добавить методы `submit_findings`/`get_candidate_findings`/`submit_verdicts`.

**Files:**
- Modify: `reviewer/mcp/service.py:110-117` (`_Session`), импорт схем; добавить методы.
- Test: `tests/mcp/test_submit_tools.py`

**Interfaces:**
- Consumes: `FindingIn`, `VerdictIn` (Task 1), `Finding.from_in` (Task 2).
- Produces (методы `MCPReviewService`):
  - `submit_findings(repo: str, pr: int, findings: list[dict]) -> dict` → `{"accepted": int, "ids": list[str]}`. id вида `"f{n}"`.
  - `get_candidate_findings(repo: str, pr: int) -> str` → JSON-строка `{"candidates": [{id,file,line,category,severity,message,code_quote}]}`.
  - `submit_verdicts(repo: str, pr: int, verdicts: list[dict]) -> dict` → `{"recorded": int, "unknown_ids": list[str]}`.
- `_Session` получает поля `candidates: dict[str, Finding]`, `verdicts: dict[str, bool]`, `_seq: int`.

- [ ] **Step 1: Написать падающий тест**

Create `tests/mcp/test_submit_tools.py`:

```python
"""submit_findings/get_candidate_findings/submit_verdicts — сессионное накопление."""
from __future__ import annotations

import json
from unittest.mock import patch

from tests.mcp.test_publish import (
    RAW, _make_mcp_service_with_publish, _fake_chunk,
)


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_submit_findings_accumulates_with_ids(_ov, _ch):
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    r1 = svc.submit_findings("o/r", 7, [RAW])
    r2 = svc.submit_findings("o/r", 7, [dict(RAW, message="bug B")])
    assert r1 == {"accepted": 1, "ids": ["f1"]}
    assert r2 == {"accepted": 1, "ids": ["f2"]}
    sess = svc._sessions[("o/r", 7)]
    assert set(sess.candidates) == {"f1", "f2"}
    assert sess.candidates["f1"].message == "bug here"


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_get_candidate_findings_returns_ids(_ov, _ch):
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    svc.submit_findings("o/r", 7, [RAW])
    payload = json.loads(svc.get_candidate_findings("o/r", 7))
    assert payload["candidates"][0]["id"] == "f1"
    assert payload["candidates"][0]["file"] == "a.py"
    assert payload["candidates"][0]["code_quote"] == "x = 1"


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_submit_verdicts_records_and_flags_unknown(_ov, _ch):
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    svc.submit_findings("o/r", 7, [RAW])              # → f1
    r = svc.submit_verdicts("o/r", 7, [{"id": "f1", "is_real": False},
                                       {"id": "f9", "is_real": True}])
    assert r == {"recorded": 1, "unknown_ids": ["f9"]}
    assert svc._sessions[("o/r", 7)].verdicts == {"f1": False}
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_submit_tools.py -q`
Expected: FAIL — `AttributeError: 'MCPReviewService' object has no attribute 'submit_findings'`.

- [ ] **Step 3a: Добавить импорт схем и расширить `_Session`**

В `reviewer/mcp/service.py` к импортам (после строки 33 `from reviewer.vcs.base import …`) добавить:

```python
from reviewer.mcp.schemas import FindingIn, VerdictIn
```

Заменить `dataclass`-импорт (строка 8) на:

```python
from dataclasses import dataclass, field
```

Заменить класс `_Session` (строки 110-117) на:

```python
@dataclass
class _Session:
    prepared: PreparedReview
    # Храним ctx, а не готовые tools: make_tools(ctx) пересоздаётся на каждый
    # _invoke_tool-вызов, чтобы seen-дедуп (set внутри make_tools) сбрасывался
    # пер-вызов. Повторный одинаковый вызов отдаёт реальный результат из
    # ctx.cache (пер-сессия), а не заглушку «повтор: результат уже показан выше».
    ctx: ToolContext
    # PRI-156: schema-enforced находки/вердикты копятся в сессии между submit_*
    # и publish_review. id вида "f{n}" присваивает submit_findings. Состояние
    # in-memory (регидрированная из стора сессия стартует пустой — допустимо:
    # перезапуск процесса посреди ревью теряет прогресс, как и раньше).
    candidates: dict[str, Finding] = field(default_factory=dict)
    verdicts: dict[str, bool] = field(default_factory=dict)
    _seq: int = 0
```

- [ ] **Step 3b: Добавить submit-методы**

В `MCPReviewService` (например, сразу перед `def publish_review`, строка ~512) добавить:

```python
    def submit_findings(self, repo: str, pr: int, findings: list[dict]) -> dict:
        """Принять находки субагента в сессию (PRI-156): валидация по FindingIn,
        присвоение server-assigned id, накопление в _Session.candidates.

        Энфорс схемы — на тул-границе FastMCP (тип list[FindingIn]); здесь
        повторная model_validate коэрцирует/валидирует dict при прямом вызове
        (тесты). Невалидный элемент (нет file) → ValidationError → ретрай тула.
        """
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        s = self._session(repo, pr)
        ids: list[str] = []
        for d in findings:
            fi = FindingIn.model_validate(d)
            s._seq += 1
            fid = f"f{s._seq}"
            s.candidates[fid] = Finding.from_in(fi)
            ids.append(fid)
        return {"accepted": len(ids), "ids": ids}

    def get_candidate_findings(self, repo: str, pr: int) -> str:
        """Вернуть накопленных кандидатов с id для verify (JSON-строка)."""
        import json
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        s = self._session(repo, pr)
        items = [
            {"id": fid, "file": f.file, "line": f.line, "category": f.category,
             "severity": f.severity, "message": f.message, "code_quote": f.code_quote}
            for fid, f in s.candidates.items()
        ]
        return json.dumps({"candidates": items}, ensure_ascii=False, indent=2)

    def submit_verdicts(self, repo: str, pr: int, verdicts: list[dict]) -> dict:
        """Принять вердикты verify в сессию (PRI-156). id вне candidates →
        игнор + warning. Отсутствие вердикта по находке = keep (см. publish_review)."""
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        s = self._session(repo, pr)
        recorded, unknown = 0, []
        for d in verdicts:
            v = VerdictIn.model_validate(d)
            if v.id not in s.candidates:
                unknown.append(v.id)
                log.warning("submit_verdicts: неизвестный id %s (%s#%s)", v.id, repo, pr)
                continue
            s.verdicts[v.id] = v.is_real
            recorded += 1
        return {"recorded": recorded, "unknown_ids": unknown}
```

- [ ] **Step 4: Запустить — убедиться, что проходит + ruff**

Run: `.venv/bin/pytest tests/mcp/test_submit_tools.py -q && .venv/bin/ruff check reviewer/mcp/service.py`
Expected: PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add reviewer/mcp/service.py tests/mcp/test_submit_tools.py
git commit -m "feat(mcp): submit_findings/submit_verdicts/get_candidate_findings — сессионное накопление (PRI-156)"
```

---

### Task 4: `publish_review` читает из сессии (`reviewer/mcp/service.py`)

Убрать параметр `findings`; читать candidates+verdicts из сессии; отсев только по явному `is_real=false`; `verify_rejected` = число `False`. Удалить `_finding_from_dict`. Мигрировать тесты `test_publish.py`, `test_session_persist.py`, `test_service.py`.

**Files:**
- Modify: `reviewer/mcp/service.py` — `publish_review` (512-662), `_record_history` (664-733), удалить `_finding_from_dict` (50-107) и его использование.
- Modify: `tests/mcp/test_publish.py`, `tests/mcp/test_session_persist.py`, `tests/mcp/test_service.py`.

**Interfaces:**
- Consumes: `_Session.candidates`/`verdicts` (Task 3).
- Produces: `publish_review(repo, pr, summary, dry_run=False, task_key=None) -> dict`. Отчёт включает `verify_rejected`; `invalid` остаётся ключом со значением 0 (back-compat).

- [ ] **Step 1: Мигрировать `test_publish.py` под новый поток (тесты сначала)**

В `tests/mcp/test_publish.py` добавить хелпер (после фабрики `_make_mcp_service_with_publish`, строка ~170):

```python
def _submit_then_publish(svc, repo, pr, findings, *, summary="s", dry_run=False,
                         verdicts=None, task_key=None):
    """PRI-156: вместо publish_review(findings=...) — submit + publish из сессии."""
    if findings:
        svc.submit_findings(repo, pr, findings)
    if verdicts:
        svc.submit_verdicts(repo, pr, verdicts)
    return svc.publish_review(repo, pr, summary=summary, dry_run=dry_run, task_key=task_key)
```

Заменить во всех тестах файла вызовы вида
`svc.publish_review("o/r", 7, summary=S, findings=F[, dry_run=D])`
на
`_submit_then_publish(svc, "o/r", 7, F, summary=S[, dry_run=D])`.

Затронутые места (строки на момент написания плана): 185, 197, 217-218, 228, 246, 265, 281, 301, 319, 330, 343-344, 366-368, 390, 410.

Удалить тест `test_publish_coerces_malformed_llm_findings` (строки 286-306): кейс «без file → invalid» теперь невозможен — FindingIn отвергает на тул-границе, до publish такие не доходят. Коэрция confidence/severity покрыта `tests/mcp/test_schemas.py`. Вместо него добавить тест счётчика `invalid=0`:

```python
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_invalid_always_zero_with_enforced_schema(_ov, _ch) -> None:
    """Все candidates валидны (validated на submit) → invalid всегда 0."""
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True)
    assert report["invalid"] == 0
```

Аналогично заменить тест `test_publish_coerces_unhashable_severity_and_nonstringsuggestion` (строки 396-419): теперь коэрция на submit. Оставить проверку публикации (severity список → medium проходит гейт), но через `_submit_then_publish`:

```python
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_coerced_findings_publish(_ov, _ch) -> None:
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    pack = [dict(RAW, severity=["high"], message="unhashable severity"),
            dict(RAW, suggestion=42, message="non-string suggestion")]
    report = _submit_then_publish(svc, "o/r", 7, pack, dry_run=True)
    assert report["dropped_by_gate"] == 0
    assert len(report["inline"]) == 2
    inline_bodies = [c["body"] for c in report["inline"]]
    assert not any("42" in b and "suggestion" in b.lower() for b in inline_bodies)
```

Добавить новые тесты verify-фолбэка:

```python
@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_drops_findings_with_is_real_false(_ov, _ch) -> None:
    """Явный is_real=false → находка отсеяна; verify_rejected=1."""
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True,
                                  verdicts=[{"id": "f1", "is_real": False}])
    assert report["verify_rejected"] == 1
    assert report["inline"] == []


@patch("reviewer.services.review_service.chunk_python", side_effect=_fake_chunk)
@patch("reviewer.services.review_service.build_overlay")
def test_publish_keeps_finding_without_verdict(_ov, _ch) -> None:
    """Нет вердикта (verify умер/частичный) → находка остаётся (recall-safe)."""
    svc, _, _ = _make_mcp_service_with_publish()
    svc.prepare_review("o/r", 7)
    report = _submit_then_publish(svc, "o/r", 7, [RAW], dry_run=True)  # без verdicts
    assert report["verify_rejected"] == 0
    assert report["inline"][0]["line"] == 2
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_publish.py -q`
Expected: FAIL — `submit_findings`/новой сигнатуры ещё нет в publish; вызовы `publish_review(..., findings=...)` уже убраны, новые тесты падают на `verify_rejected`/`invalid` или на старой сигнатуре `publish_review` (TypeError: unexpected keyword не возникнет, т.к. findings убран из вызовов; упадут assert по verify_rejected — ключа ещё нет).

- [ ] **Step 3a: Переписать `publish_review`**

В `reviewer/mcp/service.py` заменить сигнатуру и блок 1 (коэрция входа). Новая сигнатура и докстринг:

```python
    def publish_review(
        self,
        repo: str,
        pr: int,
        summary: str,
        dry_run: bool = False,
        task_key: str | None = None,
    ) -> dict:
        """Детерминированный хвост ревью: verify-фильтр → grounding → gate →
        dedup → assemble → публикация → история → очистка overlay/сессии.

        PRI-156: находки и вердикты читаются ИЗ СЕССИИ (submit_findings/
        submit_verdicts), параметр findings убран. Отсев verify — только по
        явному is_real=false; отсутствие вердикта = keep (recall-safe).
        Сессия (repo, pr) должна быть подготовлена prepare_review. Overlay и
        сессия очищаются ВСЕГДА (даже при сбое VCS-публикации) — см. _cleanup.

        При указании task_key и успешной публикации (не dry_run) линкует
        PR↔задача↔код в граф задач (рёбра IMPLEMENTED_BY + TOUCHES).

        Returns:
            Отчёт со счётчиками (posted/invalid/verify_rejected/dropped_by_gate/
            deduped/already_posted/moved_to_summary/capped) и inline.
        """
        from reviewer.services.repo_id import normalize_repo
        repo = normalize_repo(repo)
        s = self._session(repo, pr)
        p = s.prepared

        # 1) Verify-фильтр из сессии: keep, кроме явного is_real=false (recall-safe).
        # Затем грунтовка строки по дословной цитате (анти-галлюцинация).
        _commentable_cache: dict[str, dict[str, set[int]]] = {
            path: commentable_lines(patch)
            for path, patch in p.patches.items()
            if patch is not None
        }
        survived = [f for fid, f in s.candidates.items() if s.verdicts.get(fid) is not False]
        verify_rejected = len(s.candidates) - len(survived)
        parsed: list[Finding] = []
        for f in survived:
            f.line = ground_line(p.sources.get(f.file), f.code_quote, f.line)
            if f.line is not None and f.side == "RIGHT" and f.file in _commentable_cache:
                f.line = snap_to_commentable(
                    f.line, f.side, f.code_quote, _commentable_cache[f.file], p.sources.get(f.file, ""),
                    max_distance=p.policy.grounding_max_distance,
                )
            parsed.append(f)
```

Остальные блоки 2–5 (gate/dedup/centrality/existing-fps/assemble/publish/link) — без изменений (с `parsed`). В блоке 6 (история+cleanup) пробросить `verify_rejected`. Заменить строки 638-644:

```python
        # 6) История (fail-soft) и очистка overlay/сессии (ВСЕГДА).
        dropped_by_gate = len(parsed) - len(kept)
        run_id = self._record_history(
            repo, pr, p, list(s.candidates.values()), deduped, asm,
            dropped_by_gate=dropped_by_gate, verify_rejected=verify_rejected,
            dry_run=dry_run, posted=posted, error=error,
        )
        self._cleanup(repo, pr)
```

В возвращаемом отчёте (строки 646-662) заменить `"invalid": invalid,` на `"invalid": 0,` и добавить `"verify_rejected": verify_rejected,`:

```python
        return {
            "posted": posted,
            "dry_run": dry_run,
            "error": error,
            "run_id": run_id,
            "summary": full_summary,
            "inline": [
                {"path": c.path, "line": c.line, "side": c.side, "body": c.body}
                for c in asm.inline_comments
            ],
            "invalid": 0,                       # PRI-156: вход валиден по схеме (submit)
            "verify_rejected": verify_rejected,
            "dropped_by_gate": dropped_by_gate,
            "deduped": len(kept) - len(deduped),
            "already_posted": asm.skipped_existing,
            "moved_to_summary": asm.moved_to_summary,
            "capped": asm.capped,
        }
```

- [ ] **Step 3b: Обновить `_record_history` (analyzed = candidates, verify_rejected проброшен)**

Заменить сигнатуру `_record_history` (664-677): параметр `parsed` → `analyzed`, добавить `verify_rejected`:

```python
    def _record_history(
        self,
        repo: str,
        pr: int,
        p: PreparedReview,
        analyzed: list[Finding],
        deduped: list[Finding],
        asm: AssembledReview,
        *,
        dropped_by_gate: int,
        verify_rejected: int,
        dry_run: bool,
        posted: bool,
        error: str,
    ) -> int | None:
```

В теле заменить строку 695 и блок verify_rejected (713-715):

```python
            findings_analyzed = len({f.fingerprint() for f in analyzed})
```
```python
                "findings_analyzed": findings_analyzed,
                "findings_kept": len(deduped),
                # PRI-156: verify_rejected = число is_real=false (verify живёт в
                # сессии submit_verdicts); gate-отсев отдельно в отчёте publish.
                "verify_rejected": verify_rejected,
```

Удалить ставшую неиспользуемой переменную `dropped_by_gate` из тела `_record_history`, если она там фигурирует только в комментарии (она приходит параметром — оставить параметр, он не используется внутри? Проверить: в новом теле `dropped_by_gate` не используется. Убрать его из сигнатуры `_record_history` ради чистоты — он считается в publish_review и идёт только в отчёт). Итог: у `_record_history` параметра `dropped_by_gate` нет; вызов из publish_review передаёт только `verify_rejected`. Скорректировать вызов в Step 3a соответственно:

```python
        run_id = self._record_history(
            repo, pr, p, list(s.candidates.values()), deduped, asm,
            verify_rejected=verify_rejected,
            dry_run=dry_run, posted=posted, error=error,
        )
```

- [ ] **Step 3c: Удалить `_finding_from_dict` и `_coerce_int`/`_VALID_SEVERITIES` из service.py**

Удалить функцию `_finding_from_dict` (строки 50-107). `_VALID_SEVERITIES` (39) и `_coerce_int` (42-47) больше не используются в service.py (живут в schemas.py) — удалить их тоже. Проверить отсутствие других ссылок:

Run: `grep -n "_finding_from_dict\|_coerce_int\|_VALID_SEVERITIES" reviewer/mcp/service.py`
Expected: пусто.

- [ ] **Step 3d: Мигрировать `test_service.py` и `test_session_persist.py`**

В `tests/mcp/test_service.py`: убрать `_finding_from_dict` из импорта (строка 14 → `from reviewer.mcp.service import MCPReviewService`); удалить тест `test_finding_confidence_coercion_and_clamp` (683-697) — покрыт `tests/mcp/test_schemas.py`.

В `tests/mcp/test_session_persist.py`: заменить три вызова `svc.publish_review("o/r", 7, summary="s", findings=[RAW], dry_run=True)` (строки 128, 151, 167) на пару:
```python
        svc.submit_findings("o/r", 7, [RAW])
        report = svc.publish_review("o/r", 7, summary="s", dry_run=True)
```
(для строки 128, где результат используется как `report`; для 151/167 — по месту, сохранив присваивание если есть). Проверить наличие `RAW` в файле; если импортируется из test_publish — оставить.

- [ ] **Step 4: Запустить весь mcp-набор — убедиться, что проходит + ruff**

Run: `.venv/bin/pytest tests/mcp/ -q && .venv/bin/ruff check reviewer/mcp/service.py tests/mcp/`
Expected: PASS (кроме test_server*.py — их сигнатуры чиним в Task 5; если упадут именно они — это ожидаемо, продолжаем). Точечно: `.venv/bin/pytest tests/mcp/test_publish.py tests/mcp/test_service.py tests/mcp/test_session_persist.py tests/mcp/test_submit_tools.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add reviewer/mcp/service.py tests/mcp/test_publish.py tests/mcp/test_service.py tests/mcp/test_session_persist.py
git commit -m "feat(mcp): publish_review читает findings/verdicts из сессии, убран free-text вход (PRI-156)"
```

---

### Task 5: Регистрация MCP-тулов + смена сигнатуры publish_review (`reviewer/entrypoints/mcp_server.py`)

Добавить 3 тула (типизированы Pydantic для энфорса FastMCP); поправить обёртку `publish_review` (без findings). Мигрировать `test_server.py`/`test_server_tools.py`.

**Files:**
- Modify: `reviewer/entrypoints/mcp_server.py` (docstring «22 тула» → 25; обёртка publish_review; +3 обёртки).
- Modify: `tests/mcp/test_server.py`, `tests/mcp/test_server_tools.py`.

**Interfaces:**
- Consumes: `service.submit_findings/get_candidate_findings/submit_verdicts/publish_review` (Tasks 3-4), `FindingIn`/`VerdictIn` (Task 1).
- Produces: MCP-тулы `submit_findings`, `submit_verdicts`, `get_candidate_findings`; обновлённый `publish_review`.

- [ ] **Step 1: Обновить тесты регистрации/форвардинга (сначала тесты)**

В `tests/mcp/test_server.py`:
- строка 91 docstring «ровно 22» → «ровно 25»;
- в множество ожидаемых имён (около строк 96-120) добавить `"submit_findings"`, `"submit_verdicts"`, `"get_candidate_findings"`;
- тест `test_publish_review_dry_run_callable_via_mcp` (147-): вызвать через MCP сначала submit, потом publish без findings:
```python
    asyncio.run(server.call_tool("prepare_review", {"repo": "o/r", "pr": 7}))
    asyncio.run(server.call_tool(
        "submit_findings",
        {"repo": "o/r", "pr": 7, "findings": [RAW]},
    ))
    result = asyncio.run(server.call_tool(
        "publish_review",
        {"repo": "o/r", "pr": 7, "summary": "s", "dry_run": True},
    ))
```
(где `RAW` — тот же словарь, что в test_publish; импортировать при необходимости.)

В `tests/mcp/test_server_tools.py` тест `test_publish_review_tool_forwards_task_key` (24-37): убрать findings из args и из ожидаемого вызова:
```python
    asyncio.run(server.call_tool(
        "publish_review",
        {"repo": "o/r", "pr": 7, "summary": "s", "task_key": "ID-1"},
    ))
    svc.publish_review.assert_called_once_with("o/r", 7, "s", False, "ID-1")
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_server.py tests/mcp/test_server_tools.py -q`
Expected: FAIL — тулов нет / число тулов 22 / publish форвардит лишний `[]`.

- [ ] **Step 3a: Обновить обёртку `publish_review` в `mcp_server.py`**

Заменить существующую обёртку publish_review (строки 185-200 — сейчас `findings: list[dict]`, передаёт `findings` 4-м позиционным аргументом) на сигнатуру без findings:

```python
    @mcp.tool()
    def publish_review(
        repo: str, pr: int, summary: str,
        dry_run: bool = False, task_key: str | None = None,
    ) -> dict:
        """Опубликовать ревью: verify-фильтр/gate/dedup/assemble из сессии (PRI-156)."""
        return service.publish_review(repo, pr, summary, dry_run, task_key)
```

- [ ] **Step 3b: Добавить 3 обёртки (рядом с publish_review)**

```python
    @mcp.tool()
    def submit_findings(repo: str, pr: int, findings: list[FindingIn]) -> dict:
        """Сдать находки в сессию (schema-enforced). FastMCP валидирует по FindingIn."""
        return service.submit_findings(repo, pr, [f.model_dump() for f in findings])

    @mcp.tool()
    def submit_verdicts(repo: str, pr: int, verdicts: list[VerdictIn]) -> dict:
        """Сдать вердикты verify в сессию (schema-enforced)."""
        return service.submit_verdicts(repo, pr, [v.model_dump() for v in verdicts])

    @mcp.tool()
    def get_candidate_findings(repo: str, pr: int) -> str:
        """Прочитать накопленных кандидатов с id (для verify)."""
        return service.get_candidate_findings(repo, pr)
```

Добавить импорт в начало `mcp_server.py`:

```python
from reviewer.mcp.schemas import FindingIn, VerdictIn
```

Обновить docstring `create_server` (строка 18): «с 22 тулами» → «с 25 тулами».

- [ ] **Step 4: Запустить — убедиться, что проходит + ruff + полный прогон**

Run: `.venv/bin/pytest tests/mcp/ -q && .venv/bin/ruff check reviewer/entrypoints/mcp_server.py tests/mcp/`
Expected: PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add reviewer/entrypoints/mcp_server.py tests/mcp/test_server.py tests/mcp/test_server_tools.py
git commit -m "feat(mcp): тулы submit_findings/submit_verdicts/get_candidate_findings + publish без findings (PRI-156)"
```

---

### Task 6: Контракт промптов и скилла под submit-тулы (`plugin/skills/`)

Перевести findings-schema/dimension-tail/verify/analyze/SKILL на submit-тулы; обновить guard-тесты.

**Files:**
- Modify: `plugin/skills/_common/findings-schema.md`, `plugin/skills/_common/dimension-output-tail.md`, `plugin/skills/_common/tool-usage.md`, `plugin/skills/review-pr/references/verify-prompt.md`, `plugin/skills/review-pr/references/analyze-prompt.md`, `plugin/skills/review-pr/SKILL.md`.
- Modify: `tests/skills/test_common_blocks.py`.

**Interfaces:** документы; контракт сверяется guard-тестами `tests/skills/`.

- [ ] **Step 1: Обновить guard-тесты (сначала тест)**

В `tests/skills/test_common_blocks.py`:

Добавить тест соответствия схемы Pydantic-канону `FindingIn`:

```python
def test_findings_schema_matches_finding_in_model():
    # PRI-156: findings-schema.md — проекция канона FindingIn.
    from reviewer.mcp.schemas import FindingIn

    schema = _read("findings-schema.md")
    for name in FindingIn.model_fields:
        token = "fix" if name == "fix" else name
        assert token in schema, f"поле FindingIn.{name} отсутствует в findings-schema.md"
```

В `test_tool_usage_has_both_tool_families` добавить проверку submit-тулов:

```python
    # PRI-156: submit-тулы schema-enforced вывода
    assert "submit_findings" in text and "submit_verdicts" in text
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_common_blocks.py -q`
Expected: FAIL — `submit_findings` ещё нет в tool-usage.md; FindingIn-guard может пройти (поля совпадают) или упасть, если token не найден.

- [ ] **Step 3a: `findings-schema.md` — submit вместо free-text**

Заменить строки 1-3:

```markdown
Findings output schema (shared). The calling skill sets `category`.

Submit findings by calling `submit_findings(repo, pr, findings=[…])` — one call,
each item matching this per-finding schema (the server validates against it and
assigns a stable id). Do NOT return findings as prose/JSON text.
```

(Блок полей `{…}` и «Field semantics» — без изменений.)

- [ ] **Step 3b: `dimension-output-tail.md` — submit**

Заменить весь файл на:

```markdown
Submit the findings by calling `submit_findings(repo, pr, findings=[…])` (one call).
Each item matches the shared findings schema; the server validates and assigns ids.

If a finding cannot be tied to a specific line, use the closest changed line and
explain the scope in `message`.

If there are no meaningful findings, call `submit_findings` with an empty list and say so.

Write `message` and `suggestion` in the output language given by the orchestrator
(standalone: the user's language).
```

- [ ] **Step 3c: `tool-usage.md` — добавить submit-тулы**

В разделе «PR-session tools» (после строки 19 `get_impact`) добавить:

```markdown
- `submit_findings` — submit findings (schema-enforced; server assigns ids);
- `get_candidate_findings` — read accumulated candidate findings (verify only);
- `submit_verdicts` — submit verify verdicts by candidate id (verify only).
```

- [ ] **Step 3d: `verify-prompt.md` — get_candidate_findings + submit_verdicts**

Заменить строку 42:

```markdown
Read the candidate findings via `get_candidate_findings(repo, pr)` (each has a stable
`id`). For each, submit your decision via `submit_verdicts(repo, pr, verdicts=[{"id": "<id>", "is_real": true|false}])`.
Submit a verdict only for findings you decide to kill or explicitly keep; a finding
with no verdict is kept (recall-safe). Do NOT return verdicts as text.
```

- [ ] **Step 3e: `analyze-prompt.md` — submit**

Заменить строку 60 (хвост после findings-schema include):

```markdown
Set "category" to one of: correctness|security|performance|maintainability|style.
Submit via `submit_findings(repo, pr, findings=[…])` — do not return JSON text.
```

- [ ] **Step 3f: `review-pr/SKILL.md` — submit-поток + убрать «KEEP all on malformed»**

- Строка 75: `Each subagent returns a JSON object {"findings": [...]}` → `Each subagent submits findings via submit_findings(repo, pr, findings=[...]) (schema-enforced; the server assigns ids).`
- Строки 85-86, 90-91, 94: «returns the same findings JSON schema …» → «submits findings via submit_findings with category … ».
- Шаг 5 Verify (96-102) заменить на:
```markdown
5. **Verify.** Dispatch one subagent with `references/verify-prompt.md` and the
   repo/pr identifiers. It reads candidates via `get_candidate_findings(repo, pr)`
   and submits verdicts via `submit_verdicts(repo, pr, verdicts=[{id, is_real}])`.
   A finding with `is_real=false` is dropped at publish; a finding with no verdict
   is kept (recall-safe — no orchestrator action needed if verify fails).
```
- Шаг 6 Publish (109): `Call publish_review(repo, pr, summary, findings, dry_run, task_key)` → `Call publish_review(repo, pr, summary, dry_run, task_key)` (findings приходят из сессии). Счётчики отчёта (112-113): добавить `verify_rejected` в перечисление.

- [ ] **Step 4: Запустить guard + полный прогон skills**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (включая `test_assembled_prompts.py` — include-сборка цела).

- [ ] **Step 5: Commit**

```bash
git add plugin/skills tests/skills/test_common_blocks.py
git commit -m "feat(skills): контракт промптов review-pr на submit_findings/submit_verdicts (PRI-156)"
```

---

### Финальная проверка (после всех задач)

- [ ] **Полный прогон + линт**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: все unit-тесты PASS; ruff без новых ошибок (на изменённых файлах). Note: ruff может быть не идеально чист на репо в целом — ориентир на изменённые файлы (см. практику проекта).

- [ ] **Проверить, что free-text findings нигде не остался**

Run: `grep -rn "findings=\[" tests/ ; grep -rn "_finding_from_dict" reviewer/ tests/`
Expected: пусто (или только хелпер `_submit_then_publish`).

---

## Известные ограничения (вне скоупа, YAGNI)

- **Персистентность candidates/verdicts.** Накопленные находки/вердикты — in-memory в `_Session`; `SessionStore` сериализует только `prepared`. Перезапуск процесса reviewer-mcp между `submit_*` и `publish_review` теряет их (регидрированная сессия стартует с пустыми candidates → пустое ревью). Это тот же профиль риска, что и прерывание ревью сегодня. Персистентность candidates можно добавить позже (расширение `session_serde`), но в PRI-156 не входит.
- **Прямой Anthropic `response_format`** недоступен (архитектура на LLM-субагентах). Энфорс — только на тул-границе FastMCP.

## Self-Review (выполнено при написании)

- **Spec coverage:** механизм submit-тулов → Tasks 3,5; server-assigned id + session boundary → Tasks 3,4; Pydantic-канон → Task 1; Finding.from_in → Task 2; мягкая коэрция = порт _finding_from_dict → Task 1 (валидаторы) + Task 4 (удаление старой функции); фолбэк «нет verdict = keep» → Task 4; промпты/SKILL/guard → Task 6. Все секции спеки покрыты.
- **Type consistency:** `submit_findings(repo,pr,findings)->{accepted,ids}`, `submit_verdicts(...)->{recorded,unknown_ids}`, `get_candidate_findings(repo,pr)->str`, `publish_review(repo,pr,summary,dry_run,task_key)`, `Finding.from_in(fi)`, `FindingIn`/`VerdictIn`/`FixIn` — имена согласованы между задачами.
- **Placeholder scan:** код приведён полностью в каждом шаге; миграции тестов перечислены по строкам.
