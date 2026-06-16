# Grounding fix (PRI-97) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Снизить долю находок, «улетающих в сводку» вместо inline-комментариев, на ≥30% через три независимых рычага: логирование метрики, явная передача доступных строк агенту, server-side fuzzy snap на ближайшую строку диффа.

**Architecture:** (1) `Finding.code_quote` — новое поле для хранения цитаты; (2) `snap_to_commentable()` в `assemble.py` — чистая функция, снапает line на ближайшую commentable строку с верификацией по цитате; (3) обновлённый промпт субагента — запрещает использовать строки вне `commentable_right`.

**Tech Stack:** Python 3.11+, dataclasses, pytest, FastMCP (reviewer MCP server).

---

## File Map

| Файл | Изменение |
|---|---|
| `reviewer/vcs/base.py` | + поле `code_quote: str \| None = None` в `Finding` |
| `reviewer/agent/assemble.py` | + функция `snap_to_commentable(...)` |
| `reviewer/mcp/service.py` | populate `code_quote` в `_finding_from_dict`; упростить цикл + добавить snap + log.info в `publish_review`; расширить импорт |
| `plugin/skills/review-pr/SKILL.md` | step 3: передавать `commentable_right`/`left` субагенту |
| `plugin/skills/review-pr/references/analyze-prompt.md` | + правило выбора `line` из `commentable_right` |
| `tests/agent/test_assemble.py` | + unit-тесты для `snap_to_commentable` |

---

## Task 1: Добавить `code_quote` в `Finding` dataclass

**Files:**
- Modify: `reviewer/vcs/base.py:43`

- [ ] **Step 1: Добавить поле `code_quote` в конец `Finding`**

  В `reviewer/vcs/base.py` после строки `replacement: str | None = None` добавить:

  ```python
  code_quote: str | None = None   # дословная цитата строки (для fuzzy snap)
  ```

  Полный датакласс после изменения (`Finding`, строки 30–48):
  ```python
  @dataclass
  class Finding:
      category: str
      severity: Literal["low", "medium", "high", "critical"]
      file: str
      line: int | None
      side: Literal["RIGHT", "LEFT"]
      message: str
      suggestion: str | None
      confidence: float
      fix_start: int | None = None
      fix_end: int | None = None
      replacement: str | None = None
      code_quote: str | None = None   # дословная цитата строки (для fuzzy snap)

      def fingerprint(self) -> str:
          import hashlib
          key = f"{self.file}|{self.line}|{self.side}|{self.category}|{self.message}"
          return hashlib.sha256(key.encode()).hexdigest()[:16]
  ```

- [ ] **Step 2: Убедиться, что существующие тесты не сломались**

  ```bash
  .venv/bin/pytest tests/agent/test_assemble.py -q
  ```

  Ожидание: все тесты **PASS** (поле с дефолтом `None` — обратно совместимо).

- [ ] **Step 3: Закоммитить**

  ```bash
  git add reviewer/vcs/base.py
  git commit -m "feat(vcs): добавить code_quote в Finding для fuzzy snap"
  ```

---

## Task 2: TDD — `snap_to_commentable` в `assemble.py`

**Files:**
- Modify: `tests/agent/test_assemble.py`
- Modify: `reviewer/agent/assemble.py`

- [ ] **Step 1: Написать падающие тесты**

  Добавить в конец `tests/agent/test_assemble.py`:

  ```python
  from reviewer.agent.assemble import snap_to_commentable

  _SNAP_COMMENTABLE = {"RIGHT": {10, 11, 12, 20}, "LEFT": {8, 9, 10}}
  # Строки 1..24 вида "line 1", "line 2", ...
  _SNAP_SOURCE = "\n".join(f"line {i}" for i in range(1, 25))


  def test_snap_line_already_commentable():
      """Строка уже в commentable — без изменений."""
      assert snap_to_commentable(10, "RIGHT", None, _SNAP_COMMENTABLE, _SNAP_SOURCE) == 10


  def test_snap_with_matching_code_quote():
      """code_quote совпадает с кандидатом — снапаем."""
      src = "\n".join([""] * 11 + ["match_me"] + [""] * 5)  # line 12 = "match_me"
      commentable = {"RIGHT": {12}, "LEFT": set()}
      assert snap_to_commentable(13, "RIGHT", "match_me", commentable, src) == 12


  def test_snap_code_quote_no_match_returns_original():
      """code_quote не совпадает ни с одним кандидатом — возвращаем оригинал."""
      src = "\n".join([""] * 11 + ["other_content"] + [""] * 5)
      commentable = {"RIGHT": {12}, "LEFT": set()}
      assert snap_to_commentable(13, "RIGHT", "no_match", commentable, src) == 13


  def test_snap_without_code_quote_snaps_nearest():
      """Без code_quote снапаем на ближайшего кандидата в пределах max_distance."""
      commentable = {"RIGHT": {10, 12}, "LEFT": set()}
      # line=14, ближайший в RIGHT — 12 (расстояние 2 ≤ 5)
      assert snap_to_commentable(14, "RIGHT", None, commentable, _SNAP_SOURCE) == 12


  def test_snap_too_far_returns_original():
      """Ближайший кандидат дальше max_distance=5 — без изменений."""
      # line=1, ближайший в RIGHT — 10 (расстояние 9 > 5)
      assert snap_to_commentable(1, "RIGHT", None, _SNAP_COMMENTABLE, _SNAP_SOURCE) == 1


  def test_snap_empty_commentable_returns_original():
      """Нет commentable строк — без изменений."""
      assert snap_to_commentable(5, "RIGHT", "x", {}, _SNAP_SOURCE) == 5


  def test_snap_wrong_side_no_candidates():
      """Кандидаты есть в RIGHT, но сторона LEFT пустая — без изменений."""
      commentable = {"RIGHT": {10}, "LEFT": set()}
      assert snap_to_commentable(11, "LEFT", None, commentable, _SNAP_SOURCE) == 11
  ```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

  ```bash
  .venv/bin/pytest tests/agent/test_assemble.py::test_snap_line_already_commentable -v
  ```

  Ожидание: **FAIL** с `ImportError: cannot import name 'snap_to_commentable'`.

- [ ] **Step 3: Реализовать `snap_to_commentable` в `assemble.py`**

  Добавить после функции `ground_line` (после строки 59) в `reviewer/agent/assemble.py`:

  ```python
  def snap_to_commentable(
      line: int,
      side: str,
      code_quote: str | None,
      commentable: dict[str, set[int]],
      source: str,
      max_distance: int = 5,
  ) -> int:
      """Снапнуть line к ближайшей commentable строке если текущая вне хунка.

      Верификация по code_quote: целевая строка должна содержать цитату.
      Без цитаты — принимаем ближайшего кандидата в пределах max_distance.
      Возвращает исходный line если подходящего кандидата нет.
      """
      target_set = commentable.get(side, set())
      if not target_set or line in target_set:
          return line
      src_lines = source.splitlines() if source else []
      candidates = sorted(target_set, key=lambda ln: abs(ln - line))
      for cand in candidates:
          if abs(cand - line) > max_distance:
              break
          if code_quote:
              cand_text = src_lines[cand - 1].strip() if 0 < cand <= len(src_lines) else ""
              if code_quote.strip() in cand_text:
                  return cand
          else:
              return cand
      return line
  ```

- [ ] **Step 4: Запустить тесты и убедиться, что все проходят**

  ```bash
  .venv/bin/pytest tests/agent/test_assemble.py -q
  ```

  Ожидание: все тесты **PASS**, в том числе 7 новых `test_snap_*`.

- [ ] **Step 5: Закоммитить**

  ```bash
  git add tests/agent/test_assemble.py reviewer/agent/assemble.py
  git commit -m "feat(agent): snap_to_commentable — fuzzy snap на ближайшую строку диффа"
  ```

---

## Task 3: Подключить snap + logging в `service.py`

**Files:**
- Modify: `reviewer/mcp/service.py`

- [ ] **Step 1: Обновить импорт из `assemble`**

  В `reviewer/mcp/service.py` строка 11:

  Было:
  ```python
  from reviewer.agent.assemble import AssembledReview, assemble_review, ground_line
  ```

  Стало:
  ```python
  from reviewer.agent.assemble import AssembledReview, assemble_review, ground_line, snap_to_commentable
  ```

- [ ] **Step 2: Populate `code_quote` в `_finding_from_dict`**

  В `reviewer/mcp/service.py`, в функции `_finding_from_dict` (строка ~82), в вызов `Finding(...)` добавить последним параметром:

  Было (строки 82–94):
  ```python
  return Finding(
      category=str(d.get("category") or "correctness"),
      severity=severity,
      file=str(d["file"]),
      line=_coerce_int(d.get("line")),
      side=side,
      message=str(d.get("message") or ""),
      suggestion=suggestion,
      confidence=confidence,
      fix_start=fix_start,
      fix_end=fix_end,
      replacement=replacement,
  )
  ```

  Стало:
  ```python
  return Finding(
      category=str(d.get("category") or "correctness"),
      severity=severity,
      file=str(d["file"]),
      line=_coerce_int(d.get("line")),
      side=side,
      message=str(d.get("message") or ""),
      suggestion=suggestion,
      confidence=confidence,
      fix_start=fix_start,
      fix_end=fix_end,
      replacement=replacement,
      code_quote=d.get("code_quote") if isinstance(d.get("code_quote"), str) else None,
  )
  ```

  Также обновить строку в докстринге `_finding_from_dict` (строка ~46), чтобы убрать устаревший комментарий:

  Было:
  ```
  ``code_quote`` тут не используется (нужен только для грунтовки строки).
  ```

  Стало:
  ```
  ``code_quote`` хранится в Finding для fuzzy snap в publish_review.
  ```

- [ ] **Step 3: Упростить цикл грунтовки и добавить snap в `publish_review`**

  В `reviewer/mcp/service.py`, в методе `publish_review`, в цикл `for d in findings:` (строки ~327–336):

  Было:
  ```python
  for d in findings:
      f = _finding_from_dict(d)
      if f is None:
          invalid += 1
          log.warning("publish_review: пропущена некорректная находка: %r", d)
          continue
      quote = d.get("code_quote")
      if not isinstance(quote, str):
          quote = None
      f.line = ground_line(p.sources.get(f.file), quote, f.line)
      parsed.append(f)
  ```

  Стало:
  ```python
  for d in findings:
      f = _finding_from_dict(d)
      if f is None:
          invalid += 1
          log.warning("publish_review: пропущена некорректная находка: %r", d)
          continue
      f.line = ground_line(p.sources.get(f.file), f.code_quote, f.line)
      patch = p.patches.get(f.file)
      if patch and f.line is not None:
          _commentable = commentable_lines(patch)
          f.line = snap_to_commentable(
              f.line, f.side, f.code_quote, _commentable, p.sources.get(f.file, ""),
          )
      parsed.append(f)
  ```

- [ ] **Step 4: Добавить `log.info` после `assemble_review()`**

  В `reviewer/mcp/service.py`, после строки `asm = assemble_review(...)` (строки ~352–359), перед `full_summary = ...`:

  ```python
  log.info(
      "publish_review %s pr:%s — grounding: inline=%d moved_to_summary=%d capped=%d",
      repo, pr, len(asm.inline_comments), asm.moved_to_summary, asm.capped,
  )
  ```

- [ ] **Step 5: Запустить тесты**

  ```bash
  .venv/bin/pytest -q
  ```

  Ожидание: все тесты **PASS**.

- [ ] **Step 6: Закоммитить**

  ```bash
  git add reviewer/mcp/service.py
  git commit -m "feat(mcp): wire snap_to_commentable + grounding log в publish_review"
  ```

---

## Task 4: Обновить промпт субагента (Track 2)

**Files:**
- Modify: `plugin/skills/review-pr/SKILL.md:59`
- Modify: `plugin/skills/review-pr/references/analyze-prompt.md:36`

- [ ] **Step 1: Обновить SKILL.md step 3**

  В `plugin/skills/review-pr/SKILL.md` строка 59:

  Было:
  ```
     - the unit's `path` and `patch`, the PR `title`/`body`;
  ```

  Стало:
  ```
     - the unit's `path`, `patch`, `commentable_right` (sorted list of new-file line numbers
       available for inline), `commentable_left` (sorted list of old-file line numbers available
       for inline), and the PR `title`/`body`;
  ```

- [ ] **Step 2: Добавить правило в `analyze-prompt.md`**

  В `plugin/skills/review-pr/references/analyze-prompt.md` после строки 36 (после bullet про `code_quote`):

  Было (строки 34–38):
  ```
  - Every finding MUST carry an exact `code_quote` — one line copied verbatim from
    the NEW version of the file. It is used to ground the line number; an
    inaccurate quote is worse than no quote.
  - `fix` block only when you are sure of the exact replacement for a line range
    in the new file; otherwise use `suggestion` text or null.
  ```

  Стало:
  ```
  - Every finding MUST carry an exact `code_quote` — one line copied verbatim from
    the NEW version of the file. It is used to ground the line number; an
    inaccurate quote is worse than no quote.
  - Your `line` MUST be a number from `commentable_right` for `side: RIGHT`,
    or from `commentable_left` for `side: LEFT`. These are the only line numbers
    where GitHub allows inline comments. If the problem is at a non-commentable
    line, pick the nearest number from the list. If no list entry is within 5
    lines, set `line: null` — the finding will appear in the summary.
  - `fix` block only when you are sure of the exact replacement for a line range
    in the new file; otherwise use `suggestion` text or null.
  ```

- [ ] **Step 3: Запустить тесты (регрессия)**

  ```bash
  .venv/bin/pytest -q
  ```

  Ожидание: **PASS** (промпты не влияют на unit-тесты).

- [ ] **Step 4: Закоммитить**

  ```bash
  git add plugin/skills/review-pr/SKILL.md \
          plugin/skills/review-pr/references/analyze-prompt.md
  git commit -m "feat(skill): передавать commentable_right/left субагенту + правило выбора line"
  ```
