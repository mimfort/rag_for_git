# План улучшений агента ревью: качество локаций + стоимость

> **For agentic workers:** REQUIRED SUB-SKILL: используй `superpowers:subagent-driven-development` (рекомендуется) или `superpowers:executing-plans` для пошаговой реализации. Шаги помечены чекбоксами (`- [ ]`).

**Goal:** Убрать ложные `file:line` в находках в источнике, сделать verify-проверку основным путём (а не ослабляемым на множестве находок), срезать стоимость analyze за счёт графово-смежного PR-bundle и управляемой длины tool-loop.

**Architecture:** Четыре независимых рычага. №1 — грунтовка строки находки по реальному коду через цитату (`code_quote`) + резолв номера строки. №3 — verify по умолчанию агентный (поштучный), oneshot только как fallback по бюджету/жёсткому потолку. №2 — в PR-bundle класть диффы только графово-смежных файлов. №4 — прицельный tool-loop (промпт + кап + замер). Каждый рычаг — отдельная рабочая, протестированная единица; реализовывать можно в любом порядке, но рекомендуется A→B→C→D.

**Tech Stack:** Python 3.11–3.13, pydantic, LangChain/LangGraph, pytest, ruff (line-length 100). Внешние сервисы за интерфейсами, в unit-тестах мокаются.

---

## Background (контекст для реализующего — он не видел этот код)

### Что произошло
Прогнали реальное ревью PR на модели `minimax/minimax-m3` (`reviewer review … --dry-run`). Трейс прогона:

| Стадия | Вызовов LLM | Input токенов (из них кэш) | Стоимость |
|---|---:|---:|---:|
| analyze | 423 | 26.5M (24.3M кэш) | $2.46 (98%) |
| verify | 2 | 3.4K | $0.0008 |
| synthesize | 7 | 288K | $0.047 |
| **Итого** | 432 | 26.8M | **$2.50** |

Выводы, которые лечит этот план:
1. **Ложные координаты.** Находки про класс `LLMVerifier` (живёт в `reviewer/agent/analyzer.py`) были помечены `reviewer/agent/state.py:616`; про `ReviewService` — `state.py:110`; про `SnapshotProvider` — `app.py:140`. Все эти `file:line` несуществующие — модель галлюцинирует номера строк и иногда файл. В assemble уже есть посмертная валидация (выкидывает несуществующую строку), но **грунтовать надо в источнике**. (рычаг №1, №3)
2. **Стоимость = повторная пересылка контекста.** 423 tool-вызова на 39 файлов (~11 на файл) → 24.3M кэш-ридов ($1.46). PR-bundle (диффы всех изменённых файлов) вшивается в промпт каждого файла. (рычаг №2, №4)
3. **verify почти ничего не сделал** (2 вызова) и не поймал мислокации. При >`verify_oneshot_threshold` (10) находок verify принудительно уходит в дешёвый oneshot — т.е. чем больше шума, тем слабее проверка (инверсия). (рычаг №3)

### Ключевые инварианты проекта (не выводятся из задачи)
- **`node_id = "path#fqn"`** — единый ключ связи RAG↔граф. Чанк в Postgres и узел в Neo4j используют его. `node_id.split("#", 1)[0]` = путь файла. Граф-инструменты (`get_definition`, `find_callers`) уже возвращают реальные `path:start-end`.
- **Модель-агностичный парсинг JSON.** `analyzer.py` НЕ использует langchain `with_structured_output` — находки/вердикты достаются из обычного текста (`_extract_json`) + pydantic. **Не переписывать на structured output.** Добавлять поля в схему можно (это просто текст промпта + поле pydantic-модели).
- **verify fail-open**: при неразборе вердикта находка остаётся.
- **assemble**: inline ставится только если строка реально в диффе (`commentable_lines`); остальное — в сводку. Уже есть `_sane_line` (валидация координат) — это финальная страховка, грунтовка из этого плана работает ДО неё.
- Язык проекта — **русский** (комментарии, докстринги, промпты). Коммиты — Conventional Commits на русском, **без self-attribution** (никаких Co-Authored-By).

### Команды окружения
```bash
.venv/bin/pytest -q                       # unit (integration исключены по умолчанию)
.venv/bin/pytest tests/agent/test_analyzer.py -q
.venv/bin/ruff check reviewer/ tests/     # line-length 100, target py311
```

### Карта затрагиваемых файлов
| Файл | Роль | Что меняем |
|---|---|---|
| `reviewer/agent/analyzer.py` | LLM-фазы, схемы находок (строковые константы), `_to_findings`, `_pr_bundle`, `LLMVerifier` | A: `code_quote`+`_resolve_line`; C: граф-смежный bundle; B: `_use_oneshot` |
| `reviewer/agent/prompts.py` | системные промпты `ANALYZE_SYSTEM`/`VERIFY_SYSTEM`/`SYNTHESIZE_SYSTEM` | A: требовать `code_quote`; D: правило прицельного поиска |
| `reviewer/config/settings.py` | дефолты ревью | C: `review_bundle_graph_adjacent`; B: дефолт `verify_oneshot_threshold` |
| `tests/agent/test_analyzer.py` | unit-тесты фаз | новые тесты A, B, C |
| `tests/agent/test_nodes_anchoring.py` | существующий (создан ранее) | без изменений (страховка остаётся) |

---

## Part A — Рычаг №1: грунтовка строки находки по цитате (`code_quote`)

**Идея:** модель цитирует точную строку проблемного кода в поле `code_quote`; инструмент находит её настоящий номер в реальном исходнике (`deps.sources`) и переписывает `line`. Модель часто путает номер строки (особенно для чужого файла) — цитата надёжнее числа. Резолв консервативный: переписываем `line` только при ЕДИНСТВЕННОМ совпадении.

### Task A1: резолвер `_resolve_line`

**Files:**
- Modify: `reviewer/agent/analyzer.py` (добавить функцию рядом с `_window`, ~строка 95)
- Test: `tests/agent/test_analyzer.py`

- [ ] **Step 1: Написать падающий тест**

Добавить в конец `tests/agent/test_analyzer.py`:
```python
def test_resolve_line_unique_exact_match():
    from reviewer.agent.analyzer import _resolve_line
    source = "def a():\n    x = 1\n    return compute(x)\n"
    assert _resolve_line("return compute(x)", source) == 3
    assert _resolve_line("    return compute(x)", source) == 3   # ведущие пробелы игнорируются


def test_resolve_line_ambiguous_returns_none():
    from reviewer.agent.analyzer import _resolve_line
    source = "x = 1\nx = 1\n"
    assert _resolve_line("x = 1", source) is None   # 2 совпадения -> не угадываем


def test_resolve_line_substring_fallback_unique():
    from reviewer.agent.analyzer import _resolve_line
    source = "alpha\n    result = compute(x) + 1\nbeta\n"
    assert _resolve_line("compute(x)", source) == 2   # уникальная подстрока


def test_resolve_line_empty_inputs():
    from reviewer.agent.analyzer import _resolve_line
    assert _resolve_line(None, "x = 1") is None
    assert _resolve_line("x = 1", None) is None
    assert _resolve_line("   ", "x = 1") is None
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py::test_resolve_line_unique_exact_match -q`
Expected: FAIL — `ImportError: cannot import name '_resolve_line'`

- [ ] **Step 3: Реализовать `_resolve_line`**

В `reviewer/agent/analyzer.py` после функции `_window` (заканчивается на ~строке 94) добавить:
```python
def _resolve_line(quote: str | None, source: str | None) -> int | None:
    """Настоящий 1-based номер строки, текст которой совпадает с quote.

    Модель часто путает номер строки (особенно для символа из другого модуля),
    но цитирует код верно. Сопоставляем по содержимому, игнорируя ведущие/хвостовые
    пробелы. Возвращаем номер ТОЛЬКО при единственном совпадении (иначе None —
    не привязываем к чужой строке). Фолбэк — уникальная подстрока."""
    if not quote or not source:
        return None
    needle = quote.strip()
    if not needle:
        return None
    lines = source.splitlines()
    exact = [i for i, ln in enumerate(lines, 1) if ln.strip() == needle]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        sub = [i for i, ln in enumerate(lines, 1) if needle in ln]
        if len(sub) == 1:
            return sub[0]
    return None
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -k resolve_line -q`
Expected: PASS (4 теста)

- [ ] **Step 5: Коммит**
```bash
git add reviewer/agent/analyzer.py tests/agent/test_analyzer.py
git commit -m "feat(agent): резолвер _resolve_line — настоящий номер строки по цитате кода"
```

### Task A2: поле `code_quote` в модели находки и грунтовка в `_to_findings`

**Files:**
- Modify: `reviewer/agent/analyzer.py` (`_FindingModel` ~строка 406; `_to_findings` ~строка 423 — уже отредактирована ранее, сигнатура `def _to_findings(models, default_file: str | None) -> list[Finding]`)
- Test: `tests/agent/test_analyzer.py`

- [ ] **Step 1: Написать падающий тест**

Добавить в конец `tests/agent/test_analyzer.py`:
```python
def test_to_findings_grounds_line_from_code_quote():
    from reviewer.agent.analyzer import _to_findings, _FindingModel
    source = "def a():\n    x = 1\n    return compute(x)\n"
    models = [_FindingModel(category="correctness", severity="high", message="m",
                            file="a.py", line=999, code_quote="return compute(x)")]
    out = _to_findings(models, default_file="a.py", sources={"a.py": source})
    assert out[0].line == 3   # реальный номер вместо выдуманного 999


def test_to_findings_keeps_model_line_when_quote_absent():
    from reviewer.agent.analyzer import _to_findings, _FindingModel
    models = [_FindingModel(category="correctness", severity="high", message="m",
                            file="a.py", line=7)]
    out = _to_findings(models, default_file="a.py", sources={"a.py": "x\ny\n"})
    assert out[0].line == 7   # нет code_quote -> номер модели не трогаем


def test_to_findings_grounding_is_optional_without_sources():
    from reviewer.agent.analyzer import _to_findings, _FindingModel
    models = [_FindingModel(category="correctness", severity="high", message="m",
                            file="a.py", line=42, code_quote="whatever")]
    out = _to_findings(models, default_file="a.py")   # sources не передан
    assert out[0].line == 42   # обратная совместимость
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py::test_to_findings_grounds_line_from_code_quote -q`
Expected: FAIL — `_FindingModel` не принимает `code_quote` (или `_to_findings` не принимает `sources`)

- [ ] **Step 3: Добавить поле и грунтовку**

(а) В `_FindingModel` (ищи `class _FindingModel(BaseModel):`) добавить поле после `line`:
```python
class _FindingModel(BaseModel):
    category: str
    file: str | None = None
    severity: str = Field(description="low|medium|high|critical")
    line: int | None = None
    code_quote: str | None = None
    message: str
    suggestion: str | None = None
    fix: _Fix | None = None
    confidence: float = 0.7
```

(б) Изменить сигнатуру и тело `_to_findings` (она уже начинается с `def _to_findings(models, default_file: str | None) -> list[Finding]:`):
```python
def _to_findings(models, default_file: str | None,
                 sources: dict[str, str] | None = None) -> list[Finding]:
    """Преобразовать распарсенные модели в Finding. file берётся из модели либо default.

    Если default_file=None (синтез) и модель не указала file — находку пропускаем.
    Если передан sources и модель дала code_quote — номер строки грунтуем по реальному
    коду (модель часто путает номера); иначе оставляем line как есть."""
    out: list[Finding] = []
    for f in models:
        file = f.file or default_file
        if not file:
            continue
        line = f.line
        if sources is not None:
            resolved = _resolve_line(getattr(f, "code_quote", None), sources.get(file))
            if resolved is not None:
                line = resolved
        fs = f.fix.start_line if f.fix else None
        fe = f.fix.end_line if f.fix else None
        rp = f.fix.replacement if f.fix else None
        if rp is not None and (fs is None or fe is None):
            rp = None
        out.append(Finding(
            category=f.category,
            severity=(f.severity if f.severity in _VALID_SEVERITY else "medium"),
            file=file, line=line, side="RIGHT", message=f.message,
            suggestion=f.suggestion, confidence=f.confidence,
            fix_start=fs, fix_end=fe, replacement=rp))
    return out
```

- [ ] **Step 4: Запустить — убедиться, что проходит (и старые тесты живы)**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -k "to_findings or resolve_line" -q`
Expected: PASS. Проверь, что `test_to_findings_respects_model_file_then_default` (существующий) тоже зелёный.

- [ ] **Step 5: Коммит**
```bash
git add reviewer/agent/analyzer.py tests/agent/test_analyzer.py
git commit -m "feat(agent): поле code_quote в находке + грунтовка номера строки в _to_findings"
```

### Task A3: прокинуть `sources` в вызовы `_to_findings` (analyze + synthesize)

**Files:**
- Modify: `reviewer/agent/analyzer.py` — `LLMAnalyzer.analyze` (2 вызова `_to_findings(..., default_file=unit.path)`: inline-путь и fallback) и `LLMSynthesizer.synthesize` (2 вызова `_to_findings(decision.add, default_file=None)`)
- Test: `tests/agent/test_analyzer.py`

- [ ] **Step 1: Написать падающий интеграционный тест (analyze грунтует строку)**

Добавить в конец `tests/agent/test_analyzer.py`. Использует существующие хелперы `FakeProvider`, `_deps`, `ReviewUnit`:
```python
def test_analyze_grounds_line_via_code_quote(monkeypatch):
    from reviewer.agent.analyzer import LLMAnalyzer
    src = "def a():\n    x = 1\n    return compute(x)\n"
    unit = ReviewUnit("a.py", ["a.py#a"], "@@ -1,3 +1,3 @@", new_source=src)
    out_json = ('{"findings": [{"category":"correctness","severity":"high",'
                '"line":999,"code_quote":"return compute(x)","message":"bug"}]}')
    prov = FakeProvider([AIMessage(content=out_json)], "SHOULD NOT BE CALLED")
    deps = _deps()
    deps.sources = {"a.py": src}
    a = LLMAnalyzer(prov, max_iterations=2)
    out = a.analyze(unit, deps)
    assert len(out) == 1
    assert out[0].line == 3   # грунтовка сработала, а не 999
```
> Примечание: проверь сигнатуру `_deps(**kwargs)` и `FakeProvider` в начале файла. Если `_deps()` не выставляет `sources`, присвой как в примере (`deps.sources = {...}`). `ReviewUnit(path, node_ids, changed_text, new_source=...)` — см. `reviewer/agent/state.py`.

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py::test_analyze_grounds_line_via_code_quote -q`
Expected: FAIL — `out[0].line == 999` (sources ещё не прокинут)

- [ ] **Step 3: Прокинуть `sources` в analyze и synthesize**

В `LLMAnalyzer.analyze` оба вызова `_to_findings(parsed.findings, default_file=unit.path)` заменить на:
```python
                return _to_findings(parsed.findings, default_file=unit.path,
                                    sources=deps.sources)
```
и (в fallback в конце метода):
```python
        return _to_findings(parsed.findings, default_file=unit.path,
                            sources=deps.sources)
```

В `LLMSynthesizer.synthesize` оба вызова `_to_findings(decision.add, default_file=None)` заменить на:
```python
                added = _to_findings(decision.add, default_file=None,
                                     sources=deps.sources)
```
(и аналогично в fallback-ветке метода).

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -q`
Expected: PASS (весь файл)

- [ ] **Step 5: Коммит**
```bash
git add reviewer/agent/analyzer.py tests/agent/test_analyzer.py
git commit -m "feat(agent): прокинуть sources в _to_findings (analyze/synthesize) для грунтовки строк"
```

### Task A4: попросить модель давать `code_quote` (промпты + схемы)

**Files:**
- Modify: `reviewer/agent/analyzer.py` — строковые константы `_FINDINGS_SCHEMA` (~строка 37) и `_SYNTH_SCHEMA` (~строка 25)
- Modify: `reviewer/agent/prompts.py` — `ANALYZE_SYSTEM` (последний абзац перед «## Антишумовые правила»)
- Test: `tests/agent/test_analyzer.py` (presence-проверка, чтобы поле не выпало из схемы)

- [ ] **Step 1: Написать тест присутствия поля в схеме**
```python
def test_findings_schema_mentions_code_quote():
    from reviewer.agent.analyzer import _FINDINGS_SCHEMA, _SYNTH_SCHEMA
    assert "code_quote" in _FINDINGS_SCHEMA
    assert "code_quote" in _SYNTH_SCHEMA
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py::test_findings_schema_mentions_code_quote -q`
Expected: FAIL

- [ ] **Step 3: Дополнить схемы и системный промпт**

(а) В `_FINDINGS_SCHEMA` добавить поле `code_quote` в JSON и инструкцию. Заменить строку
```python
    '"line": <номер строки в НОВОЙ версии файла или null>, '
```
на
```python
    '"line": <номер строки в НОВОЙ версии файла или null>, '
    '"code_quote": "<точная строка кода, к которой относится проблема — '
    'скопируй её ДОСЛОВНО из показанной новой версии файла>", '
```

(б) В `_SYNTH_SCHEMA` аналогично — заменить
```python
    '"severity": "low|medium|high|critical", "line": <int|null>, "message": "...", '
```
на
```python
    '"severity": "low|medium|high|critical", "line": <int|null>, '
    '"code_quote": "<дословная строка кода проблемы из указанного файла>", "message": "...", '
```

(в) В `ANALYZE_SYSTEM` (`reviewer/agent/prompts.py`) в абзац про формат вывода (заканчивается на «…и, по возможности, suggestion.») добавить предложение:
```
Всегда прикладывай code_quote — дословную строку кода, к которой относится \
проблема (по ней проверяется реальный номер строки; неточная цитата — хуже, чем её отсутствие).
```

- [ ] **Step 4: Запустить — проходит**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -q`
Expected: PASS

- [ ] **Step 5: Коммит**
```bash
git add reviewer/agent/analyzer.py reviewer/agent/prompts.py tests/agent/test_analyzer.py
git commit -m "feat(agent): требовать code_quote в схемах находок и системном промпте analyze"
```

---

## Part B — Рычаг №3: verify по умолчанию агентный (фикс инверсии oneshot)

**Идея:** сейчас `len(findings) > verify_oneshot_threshold` (дефолт 10) принудительно включает дешёвый oneshot — т.е. на типичных 11–20 находках проверка слабее всего. Делаем агентный (поштучный) путь основным; oneshot — только fallback при жёстком потолке числа находок или нехватке `max_verify_tokens`. Выносим решение в чистый метод `_use_oneshot` (тестируемо без LLM).

> Зависит от уже сделанного ранее фикса `_estimate_verify_tokens` (branch-aware). Если в репо его нет — сверься с историей: метод должен возвращать agentic-оценку (Σ по проверяемым находкам), а не oneshot.

### Task B1: чистый метод решения `_use_oneshot`

**Files:**
- Modify: `reviewer/agent/analyzer.py` — `LLMVerifier.verify` (~строка 584) и рядом
- Test: `tests/agent/test_analyzer.py`

- [ ] **Step 1: Написать падающие тесты решения**
```python
def test_verify_decision_agentic_for_normal_count():
    """11 находок (> старого порога 10) при agentic -> поштучный путь, не oneshot."""
    v = LLMVerifier(object(), agentic=True, oneshot_threshold=30, min_severity="high")
    findings = [_F(i) for i in range(11)]
    assert v._use_oneshot(findings) is False


def test_verify_decision_oneshot_above_hard_cap():
    """Выше жёсткого потолка -> oneshot (защита от десятков LLM-вызовов)."""
    v = LLMVerifier(object(), agentic=True, oneshot_threshold=5, min_severity="low")
    findings = [_F(i) for i in range(11)]
    assert v._use_oneshot(findings) is True


def test_verify_decision_oneshot_when_not_agentic():
    v = LLMVerifier(object(), agentic=False, oneshot_threshold=30)
    assert v._use_oneshot([_F(1)]) is True
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py::test_verify_decision_agentic_for_normal_count -q`
Expected: FAIL — нет метода `_use_oneshot`

- [ ] **Step 3: Добавить `_use_oneshot` и использовать его в `verify`**

Добавить метод в `LLMVerifier` (рядом с `_budget_exceeded`):
```python
    def _use_oneshot(self, findings: list[Finding]) -> bool:
        """Решение: oneshot (один общий промпт) vs agentic (поштучно).

        Агентный путь — основной. Oneshot выбираем только как fallback:
        - не-agentic режим;
        - находок больше жёсткого потолка oneshot_threshold (защита от десятков
          LLM-вызовов на патологически больших PR);
        - задан max_verify_tokens и agentic-оценка в него не влезает."""
        if not self.agentic:
            return True
        if len(findings) > self.oneshot_threshold:
            return True
        if self.max_verify_tokens > 0:
            usage = getattr(self, "_last_usage", None)
            current = usage.total_tokens if usage is not None else 0
            if current + self._estimate_verify_tokens(findings) > self.max_verify_tokens:
                return True
        return False
```
> `_last_usage` опционально; в `verify()` бюджет уже проверяется через `_budget_exceeded(findings, deps)` (использует `deps.usage`). Чтобы не дублировать, оставь бюджет-логику как есть и в `_use_oneshot` ветку `max_verify_tokens` можно опустить (тогда метод проще). Минимальный безопасный вариант — без бюджет-ветки:
```python
    def _use_oneshot(self, findings: list[Finding]) -> bool:
        if not self.agentic:
            return True
        return len(findings) > self.oneshot_threshold
```
Используй минимальный вариант (бюджет уже закрыт `_budget_exceeded`).

Заменить в `verify` блок выбора ветки:
```python
        if len(findings) > self.oneshot_threshold:
            return self._verify_oneshot(findings, deps)
        if not self.agentic:
            return self._verify_oneshot(findings, deps)
        return [f for f in findings if self._verify_one(f, deps)]
```
на
```python
        if self._use_oneshot(findings):
            return self._verify_oneshot(findings, deps)
        return [f for f in findings if self._verify_one(f, deps)]
```

- [ ] **Step 4: Запустить — проходит (и старые verify-тесты живы)**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -k "verify or oneshot or agentic" -q`
Expected: PASS

- [ ] **Step 5: Коммит**
```bash
git add reviewer/agent/analyzer.py tests/agent/test_analyzer.py
git commit -m "fix(agent): verify агентный по умолчанию — oneshot только fallback (_use_oneshot)"
```

### Task B2: поднять дефолт `verify_oneshot_threshold` (порог → жёсткий потолок)

**Files:**
- Modify: `reviewer/config/settings.py` (строка `verify_oneshot_threshold: int = 10`)
- Test: `tests/config/test_settings_flags.py` (см. существующий стиль)

- [ ] **Step 1: Написать тест дефолта**

Добавить в `tests/config/test_settings_flags.py`:
```python
def test_verify_oneshot_threshold_default_is_hard_cap():
    from reviewer.config.settings import Settings
    s = Settings()
    assert s.verify_oneshot_threshold >= 30   # порог стал жёстким потолком, не «10»
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/pytest tests/config/test_settings_flags.py::test_verify_oneshot_threshold_default_is_hard_cap -q`
Expected: FAIL (дефолт 10)

- [ ] **Step 3: Изменить дефолт и комментарий**

В `reviewer/config/settings.py`:
```python
    verify_oneshot_threshold: int = 30            # жёсткий потолок: выше — verify уходит в oneshot (защита от десятков вызовов)
```
Обновить `.env.example` строку про `VERIFY_ONESHOT_THRESHOLD`, если присутствует (комментарий — «жёсткий потолок»).

- [ ] **Step 4: Запустить — проходит**

Run: `.venv/bin/pytest tests/config/test_settings_flags.py -q`
Expected: PASS

- [ ] **Step 5: Коммит**
```bash
git add reviewer/config/settings.py tests/config/test_settings_flags.py .env.example
git commit -m "feat(config): verify_oneshot_threshold=30 (жёсткий потолок вместо инверсии строгости)"
```

---

## Part C — Рычаг №2: графово-смежный PR-bundle (срез стоимости)

**Идея:** сейчас в промпт каждого файла вшиваются диффы ВСЕХ изменённых файлов PR → при 39 файлах вход раздувается. Класть диффы только графово-смежных файлов (callers/callees текущего файла через Neo4j `graph.expand`), плюс файлы с изменёнными сигнатурами (impact). Фолбэк (нет графа / нет node_ids / синтез) — прежнее поведение (все файлы).

### Task C1: настройка `review_bundle_graph_adjacent`

**Files:**
- Modify: `reviewer/config/settings.py`
- Test: `tests/config/test_settings_flags.py`

- [ ] **Step 1: Тест дефолта**
```python
def test_bundle_graph_adjacent_default_true():
    from reviewer.config.settings import Settings
    assert Settings().review_bundle_graph_adjacent is True
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/pytest tests/config/test_settings_flags.py::test_bundle_graph_adjacent_default_true -q`
Expected: FAIL

- [ ] **Step 3: Добавить поле**

В `reviewer/config/settings.py` после `review_bundle_max_lines`:
```python
    review_bundle_graph_adjacent: bool = True     # в PR-bundle класть диффы только графово-смежных файлов (+ изменённые сигнатуры)
```

- [ ] **Step 4: Запустить — проходит**

Run: `.venv/bin/pytest tests/config/test_settings_flags.py -q`
Expected: PASS

- [ ] **Step 5: Коммит**
```bash
git add reviewer/config/settings.py tests/config/test_settings_flags.py
git commit -m "feat(config): флаг review_bundle_graph_adjacent"
```

### Task C2: ограничить `_pr_bundle` графово-смежными файлами

**Files:**
- Modify: `reviewer/agent/analyzer.py` — `_pr_bundle` (~строка 277), вызов в `LLMAnalyzer.analyze` (`bundle = _pr_bundle(deps, deps.changed_paths, current_path=unit.path)`)
- Test: `tests/agent/test_analyzer.py`

- [ ] **Step 1: Написать падающие тесты**
```python
def test_pr_bundle_graph_adjacent_filters_diffs():
    from types import SimpleNamespace
    from reviewer.agent.analyzer import _pr_bundle

    class _G:
        def expand(self, ids, hops=2):
            return {"b.py#f"}   # current связан только с b.py

    deps = SimpleNamespace(
        patches={"a.py": "@@ -1 +1 @@\n-x\n+y", "b.py": "@@ -1 +1 @@\n-p\n+q",
                 "c.py": "@@ -1 +1 @@\n-m\n+n"},
        sources={}, tool_cache={}, graph=_G(),
        settings=SimpleNamespace(review_bundle_max_files=50, review_bundle_max_lines=1500,
                                 review_bundle_graph_adjacent=True),
    )
    bundle = _pr_bundle(deps, ["a.py", "b.py", "c.py"],
                        current_path="a.py", current_node_ids=["a.py#f"])
    assert "--- b.py ---" in bundle
    assert "--- c.py ---" not in bundle


def test_pr_bundle_fallback_includes_all_without_graph():
    from types import SimpleNamespace
    from reviewer.agent.analyzer import _pr_bundle
    deps = SimpleNamespace(
        patches={"a.py": "@@ -1 +1 @@\n-x\n+y", "b.py": "@@ -1 +1 @@\n-p\n+q",
                 "c.py": "@@ -1 +1 @@\n-m\n+n"},
        sources={}, tool_cache={}, graph=None,
        settings=SimpleNamespace(review_bundle_max_files=50, review_bundle_max_lines=1500,
                                 review_bundle_graph_adjacent=True),
    )
    bundle = _pr_bundle(deps, ["a.py", "b.py", "c.py"],
                        current_path="a.py", current_node_ids=["a.py#f"])
    assert "--- b.py ---" in bundle and "--- c.py ---" in bundle
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py::test_pr_bundle_graph_adjacent_filters_diffs -q`
Expected: FAIL — `_pr_bundle` не принимает `current_node_ids` или не фильтрует

- [ ] **Step 3: Реализовать фильтрацию**

В `_pr_bundle` изменить сигнатуру и вставить фильтр сразу после построения `ordered`:
```python
def _pr_bundle(deps, changed_paths: list[str], current_path: str | None = None,
               current_node_ids: list[str] | None = None) -> str:
    ...
    sig_change_paths = _paths_with_signature_changes(patches)
    ordered = sorted(changed_paths, key=lambda p: (p not in sig_change_paths, p))

    # Графово-смежный bundle: диффы только связанных с текущим файлом модулей
    # (+ файлы с изменёнными сигнатурами). Срезает раздувание входа на больших PR.
    graph = getattr(deps, "graph", None)
    adjacent_only = (getattr(settings, "review_bundle_graph_adjacent", True)
                     if settings else True)
    if adjacent_only and graph is not None and current_node_ids and current_path:
        try:
            related = graph.expand(list(current_node_ids), hops=2)
            adj = {nid.split("#", 1)[0] for nid in related}
        except Exception:
            adj = None
        if adj is not None:
            keep = adj | sig_change_paths | {current_path}
            ordered = [p for p in ordered if p in keep]

    parts: list[str] = []
    ...
```
> `settings = getattr(deps, "settings", None)` уже определён выше в функции. Остальное тело без изменений (цикл по `ordered` строит `diff_blocks`, пропуская `current_path`).

В `LLMAnalyzer.analyze` обновить вызов:
```python
        bundle = _pr_bundle(deps, deps.changed_paths, current_path=unit.path,
                            current_node_ids=unit.node_ids)
```

- [ ] **Step 4: Запустить — проходит (весь файл)**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -q`
Expected: PASS
> Внимание: существующие тесты `_pr_bundle`/synthesize не передают `current_node_ids` → попадают в фолбэк (все файлы) → поведение сохранено.

- [ ] **Step 5: Коммит**
```bash
git add reviewer/agent/analyzer.py tests/agent/test_analyzer.py
git commit -m "feat(agent): графово-смежный PR-bundle — диффы только связанных файлов (срез стоимости analyze)"
```

---

## Part D — Рычаг №4: прицельный tool-loop (промпт + кап + замер)

**Идея:** в прогоне было ~11 tool-вызовов/файл при капе `.env` = 40 (дефолт кода — 12). Добавляем в промпт правило «искать прицельно, не повторять», возвращаем кап к разумному и замеряем стоимость/находки на фиксированном PR.

### Task D1: правило прицельного поиска в `ANALYZE_SYSTEM`

**Files:**
- Modify: `reviewer/agent/prompts.py` (`ANALYZE_SYSTEM`, блок «## Антишумовые правила»)
- Test: `tests/agent/test_analyzer.py`

- [ ] **Step 1: Тест присутствия правила**
```python
def test_analyze_system_has_search_budget_rule():
    from reviewer.agent.prompts import ANALYZE_SYSTEM
    assert "прицельно" in ANALYZE_SYSTEM
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py::test_analyze_system_has_search_budget_rule -q`
Expected: FAIL

- [ ] **Step 3: Добавить правило**

В `ANALYZE_SYSTEM` в список «## Антишумовые правила» добавить пункт 6:
```
6. Ищи прицельно. Не делай повторных поисков по одному и тому же — результаты \
инструментов кэшируются, повтор вернёт заглушку. Достаточно подтвердить факт (вызовы, \
определение, контекст) минимально необходимым числом запросов; не «исследуй» файл целиком.
```

- [ ] **Step 4: Запустить — проходит**

Run: `.venv/bin/pytest tests/agent/test_analyzer.py -q`
Expected: PASS

- [ ] **Step 5: Коммит**
```bash
git add reviewer/agent/prompts.py tests/agent/test_analyzer.py
git commit -m "feat(agent): правило прицельного поиска в analyze (короче tool-loop)"
```

### Task D2: вернуть кап tool-loop к разумному + замер

**Files:**
- Modify: `.env` (локально, не коммитится — gitignored) — `REVIEW_MAX_TOOL_ITERATIONS`
- (Опц.) Modify: `.env.example` — комментарий

- [ ] **Step 1: Привести кап**

В локальном `.env` установить:
```
REVIEW_MAX_TOOL_ITERATIONS=15
```
(дефолт кода — 12; 15 — компромисс. `.env` в гите не отслеживается.)

- [ ] **Step 2: Замер «до/после» на фиксированном PR**

Прогнать один и тот же PR в `--dry-run` и сравнить хвост вывода (стоимость + число вызовов analyze):
```bash
.venv/bin/reviewer review <owner>/<repo> <N> --dry-run 2>&1 | tail -8
```
Зафиксировать: `analyze: <calls> вызовов … $<cost>`, число inline/summary находок. Цель: меньше вызовов и стоимости при не-упавшем числе валидных находок. Если находки просели — поднять кап обратно (15→20) и перемерить.

- [ ] **Step 3: Записать результат замера в этот план**

Дописать в раздел «Результаты замеров» ниже строку: дата, модель, PR, calls/cost до и после.

> Тулоп — тюнинг, а не алгоритм: критерий — findings-per-dollar на реальном PR, а не unit-тест.

---

## Сквозная верификация (после любого из Part A–D)

- [ ] **Полный unit-прогон**

Run: `.venv/bin/pytest -q`
Expected: всё зелёное (на момент написания базлайн — `360 passed, 23 deselected`; после плана тестов станет больше).

- [ ] **Линт**

Run: `.venv/bin/ruff check reviewer/ tests/`
Expected: на изменённых файлах — без ошибок (в `tests/test_gitutil.py` есть предсуществующие E702 — не трогать, не мои).

- [ ] **(Опц.) Реальный прогон-сверка**

Один PR в `--dry-run` на `minimax/minimax-m3` — убедиться, что в сводке больше нет несуществующих `file:line`, а стоимость/число вызовов не выросли.

---

## Результаты замеров (заполняется при выполнении Part D)

| Дата | Модель | PR | analyze calls до→после | $ до→после | находок до→после |
|---|---|---|---|---|---|
| _(заполнить)_ | | | | | |

---

## Риски и откат

- **A (грунтовка):** консервативна — переписывает `line` только при единственном совпадении цитаты; при пустом/неоднозначном — поведение прежнее. Откат: revert коммитов Part A; `_sane_line` в assemble остаётся страховкой.
- **B (verify агентный):** агентный verify дороже oneshot, но `_needs_check` (min_severity=high) фильтрует — поштучно проверяются только high/неуверенные. Если стоимость verify подскочит — поднять `review_verify_min_severity` или снизить `verify_oneshot_threshold`. `max_verify_tokens>0` остаётся жёстким бюджет-стопом.
- **C (граф-bundle):** при сбое графа — `try/except` → фолбэк на все файлы (поведение прежнее). Риск: пропустить кросс-файловую проблему в несмежном файле — её ловит узел synthesize (он bundle не сужает). Флаг `review_bundle_graph_adjacent=false` полностью откатывает.
- **D (tool-loop):** только промпт + кап в `.env`; откат — поднять кап.

## Self-review (выполнено при написании плана)

- **Покрытие:** каждый из 4 рычагов имеет TDD-задачи с реальным кодом и командами. №1 → A1–A4; №3 → B1–B2; №2 → C1–C2; №4 → D1–D2.
- **Плейсхолдеры:** код приведён целиком в каждом шаге; «заполнить» — только в таблице замеров (она и есть результат выполнения D).
- **Согласованность имён:** `_resolve_line`, `_use_oneshot`, `review_bundle_graph_adjacent`, `current_node_ids`, `code_quote` — используются одинаково во всех ссылках. Сигнатура `_to_findings(models, default_file, sources=None)` едина для A2/A3.

## Вне области (отдельные планы при необходимости)

- Привязка находки напрямую к `node_id` чанка из выдачи ретривера (сильнее цитаты, но требует менять контракт инструментов).
- Маршрутизация модели по сложности файла (дешёвая на тривиальных, сильная на сложных).
- Перенос записи истории/трейса на батч-запись.
