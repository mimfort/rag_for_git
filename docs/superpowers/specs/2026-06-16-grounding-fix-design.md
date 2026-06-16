# Grounding fix: снизить долю «улетающих в сводку» находок (PRI-97)

**Дата:** 2026-06-16  
**Задача:** [PRI-97](https://ru.yougile.com/team/686c049c8af8/#PRI-97)  
**Цель:** снизить `moved_to_summary` на ≥30% на тестовом PR без регрессий.

---

## Проблема

Агент ревью галлюцинирует `line` в finding'е. Сервер не может поставить inline-комментарий
на строку диффа и отправляет находку в `summary` PR description. Пользователь видит
«текстовый совет» вместо точного inline-комментария — ценность ревью снижается.

### Root Cause

`commentable_right`/`commentable_left` вычисляются в `_prepared_payload` (service.py:500–510)
и присутствуют в payload units. Но `SKILL.md` step 3 передаёт субагенту только
`path + patch` — без этих списков. Субагент читает файл через `read_file` (абсолютные
номера строк), выбирает `line` оттуда. `ground_line()` уточняет позицию по `code_quote`
в исходнике, но source-позиция может не совпасть с хунком диффа →
`f.line in commentable == False` → `moved_to_summary`.

---

## Решение: три трека

### Track 1 — Measure (1 строка)

**Файл:** `reviewer/mcp/service.py`, функция `publish_review`, после `assemble_review()`.

```python
log.info(
    "publish_review %s pr:%s — grounding: inline=%d moved_to_summary=%d capped=%d",
    repo, pr, len(asm.inline_comments), asm.moved_to_summary, asm.capped,
)
```

`moved_to_summary` = `grounding_failed`, `inline` = `grounding_ok`. Уже возвращается
в dict-ответе, просто не логировался явно.

---

### Track 2 — Prompt fix (основной рычаг)

#### `plugin/skills/review-pr/SKILL.md` — step 3

Текущий текст (≈строка 58):
> "the unit's `path` and `patch`, the PR `title`/`body`"

Новый текст:
> "the unit's `path`, `patch`, `commentable_right` (sorted list of new-file line numbers
> available for inline), `commentable_left` (old-file line numbers available for inline);
> the PR `title`/`body`"

Данные уже есть в units из `_prepared_payload` — просто начинаем передавать субагенту.

#### `plugin/skills/review-pr/references/analyze-prompt.md`

Добавить правило после блока про `code_quote` (≈строка 36):

```
- Your `line` MUST be a number from `commentable_right` for `side: RIGHT`,
  or from `commentable_left` for `side: LEFT`. These are the only line numbers
  where GitHub allows inline comments. If the problem is at a non-commentable
  line, pick the nearest number from the list. If no list entry is within 5
  lines, set `line: null` — the finding will appear in the summary.
```

**Обоснование:** числа из `commentable_right` однозначны и не требуют парсинга patch.
Правило «выбери ближайшую или null» сохраняет ценность находки даже если inline
невозможен.

---

### Track 3 — Server-side fuzzy snap

#### `reviewer/vcs/base.py` — `Finding` dataclass

Добавить поле (после `replacement: str | None = None`):

```python
code_quote: str | None = None   # дословная цитата строки (для fuzzy snap)
```

Дефолт `None` — обратно совместимо, все существующие `Finding(...)` вызовы не ломаются.

#### `reviewer/agent/assemble.py` — новая функция `snap_to_commentable`

Разместить рядом с `ground_line()`:

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

#### `reviewer/mcp/service.py` — два изменения

**В `_finding_from_dict`** — populate `code_quote`:
```python
code_quote=d.get("code_quote") if isinstance(d.get("code_quote"), str) else None,
```

**В `publish_review`**, после вызова `ground_line()` (≈строка 335):
```python
patch = p.patches.get(f.file)
if patch and f.line is not None:
    _commentable = commentable_lines(patch)  # уже импортирован на строке 23
    f.line = snap_to_commentable(
        f.line, f.side, f.code_quote, _commentable, p.sources.get(f.file, ""),
    )
```

**Логика верификации snap'а:** если есть `code_quote`, снапаем только на строку,
содержащую цитату — защита от «соседней строки с другим смыслом». Без цитаты —
снапаем на ближайшую в ±5 строках.

---

## Тестирование

### Unit-тесты (новые, без внешних зависимостей)

**`tests/agent/test_assemble.py`** — новые кейсы для `snap_to_commentable`:

| Сценарий | Ожидание |
|---|---|
| `line` уже в commentable | возвращается без изменений |
| line + 2 в commentable, code_quote совпадает | снап на line + 2 |
| code_quote не совпадает ни с одним кандидатом | возвращается оригинал |
| нет кандидата в ±5 | возвращается оригинал |
| пустой source, нет code_quote | снап на ближайшего кандидата |
| пустой commentable | возвращается оригинал |

### Регрессия

```bash
.venv/bin/pytest -q
```

---

## Файлы, затронутые реализацией

| Файл | Изменение |
|---|---|
| `reviewer/vcs/base.py` | + поле `code_quote` в `Finding` |
| `reviewer/agent/assemble.py` | + функция `snap_to_commentable` |
| `reviewer/mcp/service.py` | populate `code_quote`, вызов snap, log.info; добавить `snap_to_commentable` в импорт из `assemble` |
| `plugin/skills/review-pr/SKILL.md` | передавать `commentable_right`/`left` субагенту |
| `plugin/skills/review-pr/references/analyze-prompt.md` | правило выбора `line` из списка |
| `tests/agent/test_assemble.py` | новые unit-тесты snap |

---

## Критерии готовности

- `moved_to_summary` снизился на ≥30% на тестовом PR.
- `pytest -q` зелёный.
- `log.info` виден в выводе `reviewer-mcp` при прогоне ревью.
