# PRI-206 — blast-radius общих интерфейсов в измерении review-pr Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Зашить в blast-radius-измерение `/reviewer_review-pr` обязательную проверку конформности общих интерфейсов — при правке `Protocol`/ABC перечислять все реализации через reviewer-тулы и подтверждать их покрытие, а не «на глаз».

**Architecture:** Расширяем **существующий** blast-radius-субагента (`blast-radius-prompt.md`) второй заботой «interface expansion», параллельной уже имеющемуся `get_impact`-проходу. Новый субагент/измерение НЕ заводим. Дополнительно синхронно обновляем оба README и guard-тест сборки промпта. Движок (`impact.py`/`get_impact`) не трогаем.

**Tech Stack:** Markdown-промпты плагина (`plugin/skills/`), pytest guard-тесты (`tests/skills/`), ruff. Без изменений Python-движка.

## Global Constraints

- **Ветка:** работать на feature-ветке off `dev` (напр. `feat/pri-206-interface-blast-radius`), НЕ коммитить в `dev` напрямую (`dev` защищена, PR-required).
- **Модель субагентов:** Sonnet (стоячее предпочтение «код через superpowers → Sonnet»); Fable не применять.
- **Коммиты:** Conventional Commits на русском, БЕЗ self-attribution (никаких `Co-Authored-By`/упоминаний Claude).
- **Язык:** тело промпта / `SKILL.md` / `README.md` — английский (как в репо); `README.ru.md` — русский; текст находок субагент пишет на `policy.output_language`.
- **Движок нетронут:** никаких правок `reviewer/tools/impact.py` / `get_impact` (граница ID-155). Новых MCP-тулов и новых полей payload не добавляем — субагенту уже доступны `get_related_symbols`/`search_code`/`read_file`/`get_changed_file_diff`.
- **Include-инвариант:** новый текст добавляем в сам `blast-radius-prompt.md`; вложенных `<!-- include: -->` не плодим (нерекурсивный резолвер — guard `assemble()` роняет на неразрешённом маркере).
- **README-синхрон:** правки `README.md` (EN) и `README.ru.md` (RU) держать содержательно идентичными.
- **Прогон проверок:** `.venv/bin/pytest tests/skills/ -q`; линт `.venv/bin/ruff check .`.

---

### Task 1: Секция «Interface expansion» в blast-radius-prompt.md + диспетч SKILL.md + guard-тест

**Files:**
- Modify: `tests/skills/test_assembled_prompts.py:47-50` (расширить `test_blast_radius_assembled_has_tooling_and_confidence_tail`)
- Modify: `plugin/skills/review-pr/references/blast-radius-prompt.md` (интро, новая секция, confidence-бюллетень, anchoring)
- Modify: `plugin/skills/review-pr/SKILL.md:98-102` (описание диспетча blast-radius)

**Interfaces:**
- Consumes: helper `assemble(rel_path)` из `tests/skills/test_assembled_prompts.py:8` (подставляет include-маркеры, роняет на неразрешённом).
- Produces: собранный `blast-radius-prompt.md` содержит строки `Interface expansion`, `Protocol`, `abstract` (в дополнение к прежним `get_impact`, `0.8`).

- [ ] **Step 1: Расширить guard-тест новыми ассертами (падающими)**

В `tests/skills/test_assembled_prompts.py` заменить существующий тест (строки 47-50):

```python
def test_blast_radius_assembled_has_tooling_and_confidence_tail():
    b = assemble("review-pr/references/blast-radius-prompt.md")
    assert "get_impact" in b
    assert "0.8" in b                              # confidence-scale хвост остался
```

на:

```python
def test_blast_radius_assembled_has_tooling_and_confidence_tail():
    b = assemble("review-pr/references/blast-radius-prompt.md")
    assert "get_impact" in b
    assert "0.8" in b                              # confidence-scale хвост остался
    # interface expansion (PRI-206): триггер + секция + lower-bound фрейминг
    assert "Interface expansion" in b             # новая секция измерения
    assert "Protocol" in b                         # триггер интерфейс-правки
    assert "abstract" in b.lower()                 # ABC / abstractmethod триггер
```

Все три новых ассерта падают на текущем промпте: слов `Interface expansion`, `Protocol`, `abstract` в нём (и в подключаемом `_common/tool-usage.md`) нет.

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_assembled_prompts.py::test_blast_radius_assembled_has_tooling_and_confidence_tail -q`
Expected: FAIL на `assert "Interface expansion" in b` (секция ещё не добавлена).

- [ ] **Step 3: Правка интро blast-radius-prompt.md — назвать ДВЕ проверки**

В `plugin/skills/review-pr/references/blast-radius-prompt.md` заменить строки 2-4:

```
You are a senior reviewer measuring the BLAST RADIUS of a pull request: cross-file
contract breaks that per-file review misses. A changed function signature can break
its callers in OTHER files that the diff never touched.
```

на:

```
You are a senior reviewer measuring the BLAST RADIUS of a pull request: cross-file
contract breaks that per-file review misses. You run TWO checks: (A) a changed function
signature can break its callers in OTHER files that the diff never touched; (B) interface
expansion — a changed `Protocol` / abstract base class whose implementations in OTHER
files may not all be updated.
```

- [ ] **Step 4: Вставить секцию «Interface expansion»**

В том же файле, сразу ПОСЛЕ строки 20 (`  empty findings list.`, конец `get_impact`-блока Method) и ПЕРЕД пустой строкой перед `Confidence & graph completeness:`, вставить:

```

Interface expansion (also mandatory):
- This is a SECOND kind of blast radius that `get_impact` does NOT catch: when the diff
  ADDS a method to a shared interface, or CHANGES the signature of an abstract method,
  every implementation in OTHER files must be updated too. A missing implementation is
  not a caller break, so `get_impact` will not report it — check it separately.
- Trigger: a changed file defines an interface — signals: `class X(Protocol)`,
  `class X(ABC)` / `abc.ABC`, `@abstractmethod`, or a method body that is just `...` or
  `raise NotImplementedError` — AND the diff adds a method to it or changes an abstract
  method's signature. No such change in the diff → skip this check entirely.
- Enumerate the implementations (layered, best-effort):
  1. `get_related_symbols(<interface node_id>)` — IMPLEMENTS neighbours / subclasses,
     when the graph has them.
  2. `search_code(<interface name>)` — subclass declarations AND the construction or
     dispatch site (a factory such as `make_board_provider`) that names the concrete
     types. Python `Protocol` conformance is STRUCTURAL: an implementation need NOT
     inherit the interface, so searching subclasses alone misses duck-typed conformers —
     the factory / dispatch site and type annotations are where they surface.
  3. For each candidate: `read_file(path, start, end)` to check whether it has the
     new/changed method, and `get_changed_file_diff(path)` to confirm whether this PR
     already updated it.
- Report an implementation that LACKS the new/changed method AND was NOT updated in this
  PR. One finding per interface method (do not split per implementation); in `message`
  enumerate the implementations you found and name the ones that look uncovered.
```

- [ ] **Step 5: Дополнить секцию «Confidence & graph completeness»**

В том же файле, ПОСЛЕ строки 30 (`  - Do NOT lower \`severity\` to benign because the caller list is empty or short.`) вставить новый вложенный буллет:

```
  - The IMPLEMENTATION list from interface expansion is LIKEWISE a lower bound: Python
    `Protocol` conformance is structural (no complete list is guaranteed), and the
    live-review graph is tree-sitter CALLS-only, so IMPLEMENTS edges may be absent.
    NEVER claim "all implementations are covered" as proven; frame it as a list to verify.
```

И в той же секции заменить строку 32:

```
  for blast radius that scale concretely means:
```

на:

```
  for blast radius that scale concretely means (a "caller" below reads as an
  "implementation" for interface-expansion findings):
```

- [ ] **Step 6: Обобщить секцию «Anchoring» на обе проверки**

В том же файле заменить строки 47-48:

```
Anchoring (important): the stale callers live OUTSIDE the diff, where GitHub forbids
inline comments. So anchor each finding on the CHANGED SIGNATURE line:
```

на:

```
Anchoring (important): both the stale callers and the missing implementations live
OUTSIDE the diff, where GitHub forbids inline comments. So anchor each finding on the
CHANGED line (the signature for check A, the interface method for check B):
```

Затем заменить строки 52-54:

```
- `message` = describe the contract change and ENUMERATE the callers to verify
  (`path:line`), applying the Framing rule above per caller;
- one finding per changed signature (do not split per caller).
```

на:

```
- `message` = describe the contract change and ENUMERATE the callers (check A) or the
  implementations (check B) to verify (`path:line`), applying the Framing rule per item;
- one finding per changed signature or interface method (do not split per caller/implementation).
```

- [ ] **Step 7: Правка диспетча blast-radius в SKILL.md**

В `plugin/skills/review-pr/SKILL.md` заменить строки 98-102:

```
   - blast-radius: dispatch one subagent with `references/blast-radius-prompt.md`, the diffs of
     all units (path + patch), each unit's `commentable_right`/`commentable_left` (the line numbers
     where inline comments are allowed), the PR `title`/`body`, the repo/pr identifiers (so it can
     call the reviewer MCP tools, including `get_impact`), and the target output language. It
     submits findings via `submit_findings` with category `correctness`.
```

на:

```
   - blast-radius: dispatch one subagent with `references/blast-radius-prompt.md`, the diffs of
     all units (path + patch), each unit's `commentable_right`/`commentable_left` (the line numbers
     where inline comments are allowed), the PR `title`/`body`, the repo/pr identifiers, and the
     target output language. It runs two checks — changed signatures breaking callers (via
     `get_impact`) and interface expansion (a changed `Protocol`/ABC whose implementations must all
     be updated, via `get_related_symbols`/`search_code`) — and submits findings via
     `submit_findings` with category `correctness`.
```

- [ ] **Step 8: Прогнать тесты и линт — убедиться, что зелено**

Run: `.venv/bin/pytest tests/skills/ -q && .venv/bin/ruff check .`
Expected: PASS — новые ассерты (`Interface expansion`, `Protocol`, `abstract`) и все прежние (`get_impact`, `0.8`, отсутствие неразрешённых include в `assemble()`), ruff чист по изменённым файлам.

- [ ] **Step 9: Commit**

```bash
git add plugin/skills/review-pr/references/blast-radius-prompt.md plugin/skills/review-pr/SKILL.md tests/skills/test_assembled_prompts.py
git commit -m "feat(skills): blast-radius измерение ловит конформность общих интерфейсов (PRI-206)"
```

---

### Task 2: Синхронное обновление README (EN + RU)

**Files:**
- Modify: `README.md:625` (строка описания измерений review-pr)
- Modify: `README.ru.md:553` (та же строка, RU)

**Interfaces:**
- Consumes: ничего (документация).
- Produces: обе строки описывают, что blast-radius покрывает и конформность общих интерфейсов.

- [ ] **Step 1: Правка README.md**

В `README.md` заменить строку 625:

```
  exists) + **blast-radius** (impact analysis via `get_impact`) → **verify** pass (drops `is_real=false` findings) → publish (gate/grounding/dedup/assemble).
```

на:

```
  exists) + **blast-radius** (impact analysis via `get_impact`, plus shared-interface conformance: a changed `Protocol`/ABC → enumerate implementations and confirm all are updated) → **verify** pass (drops `is_real=false` findings) → publish (gate/grounding/dedup/assemble).
```

- [ ] **Step 2: Правка README.ru.md (синхронно)**

В `README.ru.md` заменить строку 553:

```
  `TaskBrief`) + **blast-radius** (impact-анализ через `get_impact`) → **verify** (отсев находок с `is_real=false`) → publish (gate/grounding/dedup/assemble).
```

на:

```
  `TaskBrief`) + **blast-radius** (impact-анализ через `get_impact`, плюс конформность общих интерфейсов: правка `Protocol`/ABC → перечислить реализации и подтвердить, что все обновлены) → **verify** (отсев находок с `is_real=false`) → publish (gate/grounding/dedup/assemble).
```

- [ ] **Step 3: Проверить обе правки**

Run: `grep -n "shared-interface conformance" README.md && grep -n "конформность общих интерфейсов" README.ru.md`
Expected: по одному совпадению в каждом файле.

- [ ] **Step 4: Commit**

```bash
git add README.md README.ru.md
git commit -m "docs: README (EN+RU) — blast-radius покрывает конформность общих интерфейсов (PRI-206)"
```

---

## Self-Review

**1. Spec coverage** (проверка против `docs/superpowers/specs/2026-07-03-pri-206-blast-radius-shared-interfaces-design.md`):
- Компонент 1 (interface-expansion секция: триггер / enumeration / finding / anchoring / confidence & fail-open) → Task 1, Steps 3-6. ✓
- Компонент 2 (диспетч SKILL.md) → Task 1, Step 7. ✓
- Компонент 3 (README EN+RU) → Task 2. ✓
- Компонент 4 (guard-тест) → Task 1, Steps 1-2, 8. ✓
- Границы (движок нетронут, нет нового `_common`-блока, узкий триггер) → Global Constraints + текст секции. ✓

**2. Placeholder scan:** плейсхолдеров нет — весь текст промпта, диспетча, README и тестов приведён дословно.

**3. Type consistency:** guard-ассерты (`Interface expansion`, `Protocol`, `abstract`) соответствуют дословному тексту, добавляемому в Steps 3-4; helper `assemble()` используется с той же сигнатурой, что в файле. Тул-имена (`get_related_symbols`/`search_code`/`read_file`/`get_changed_file_diff`) совпадают между промптом и диспетчем SKILL.md.

**Ограничение приёмки:** guard-тест проверяет наличие инструкции в собранном промпте, не рантайм-поведение LLM. Полная приёмка — ручной прогон `/reviewer_review-pr` на PR, добавляющем метод в `TaskBoardProvider` (кейс PRI-205/`fetch_one`), где ревьюер должен перечислить конформеров Yougile/YouTrack.
