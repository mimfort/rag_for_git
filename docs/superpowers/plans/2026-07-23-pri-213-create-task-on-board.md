# PRI-213 — создание задач на доске (create_task) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** научить reviewer создавать задачи на доске (YouGile и YouTrack) server-side, с единой структурой описания и чистым markdown в обе стороны.

**Architecture:** board-agnostic ядро (`taskdoc.py`) собирает канонический markdown из структурированных полей; провайдер доски конвертирует его в формат своего транспорта (YouGile — HTML через новый `markup.py`, YouTrack — passthrough) и возвращает markdown обратно при нормализации. Сверху — сервисный метод `MCPReviewService.create_task`, MCP-тул и клиентский скилл `/reviewer_create-task`.

**Tech Stack:** Python 3.11+, httpx, stdlib (`html`, `html.parser`, `re`, `dataclasses`), pytest, ruff.

## Global Constraints

- Язык проекта — русский: комментарии, докстринги, сообщения. Тела SKILL.md — по-английски, но скилл инструктирует отвечать пользователю по-русски.
- Коммиты: Conventional Commits на русском (`feat(tasks): …`), **без** self-attribution (никаких `Co-Authored-By` / упоминаний Claude).
- Юнит-тесты запрещают сеть и localhost-сокеты. Все новые тесты — юнит, на фейковом HTTP-клиенте по образцу `tests/tasks/boards/test_yougile_finish.py`.
- Линт: `.venv/bin/ruff check .`, line-length 100, target py311.
- Прогон юнит-тестов: `.venv/bin/pytest -q` (integration исключены по умолчанию).
- Креды доски живут только в env сервера и никогда не возвращаются клиенту.
- Сервер репо-агностичен: `.review.yml` парсит клиент и передаёт значения аргументами тула.
- Инвариант: `normalize()` / `normalize_meta()` любого провайдера возвращают `description` в markdown.
- Структура описания задаётся ТОЛЬКО в `reviewer/tasks/taskdoc.py`; провайдеры не переписывают секции.
- Никаких эмодзи и декоративных разделителей в генерируемом тексте задач.

---

## File Structure

**Создаются:**
- `reviewer/tasks/taskdoc.py` — `TaskDoc` + `render_markdown` (чистое ядро, без I/O).
- `reviewer/tasks/boards/markup.py` — `md_to_html` / `html_to_md` (stdlib, чистые функции).
- `plugin/skills/create-task/SKILL.md` — клиентский скилл `/reviewer_create-task`.
- `tests/tasks/test_taskdoc.py`, `tests/tasks/boards/test_markup.py`,
  `tests/tasks/boards/test_yougile_create.py`, `tests/tasks/boards/test_youtrack_create.py`,
  `tests/mcp/test_create_task.py`, `tests/skills/test_create_task_skill.py`.

**Изменяются:**
- `reviewer/tasks/boards/base.py` — метод `create` в Protocol + уточнение контракта нормализации.
- `reviewer/tasks/boards/yougile.py` — `normalize_yougile` через `html_to_md`; `_columns_of_project`; `create`.
- `reviewer/tasks/boards/youtrack.py` — `_set_status` (выделен из `finish`); `create`.
- `reviewer/tasks/sync.py` — параметр `force_renormalize`.
- `reviewer/mcp/service.py` — `create_task`; `sync_board(force_renormalize=…)`.
- `reviewer/entrypoints/mcp_server.py` — тул `create_task`; параметр у тула `sync_board`.
- `tests/tasks/boards/test_yougile_normalize.py`, `tests/tasks/boards/test_base.py`, `tests/tasks/test_sync.py` — дополняются.
- `README.md`, `README.ru.md`, `plugin/skills/solve-task/SKILL.md` (перекрёстная ссылка), манифест codex.

---

### Task 1: Ядро — TaskDoc и канонический markdown

**Files:**
- Create: `reviewer/tasks/taskdoc.py`
- Test: `tests/tasks/test_taskdoc.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `TaskDoc(title: str, problem: str = "", steps: list[str] = [], criteria: list[str] = [], context: str | None = None)`; `render_markdown(doc: TaskDoc) -> str`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/tasks/test_taskdoc.py`:

```python
"""Каноническая структура описания задачи (PRI-213)."""
from reviewer.tasks.taskdoc import TaskDoc, render_markdown


def _doc(**kw):
    base = dict(title="Заголовок", problem="Суть проблемы",
                steps=["Первый шаг", "Второй шаг"],
                criteria=["Первый критерий"], context="Ссылка на спеку")
    base.update(kw)
    return TaskDoc(**base)


def test_render_sections_in_canonical_order():
    md = render_markdown(_doc())
    assert md.index("## Проблема") < md.index("## Что сделать")
    assert md.index("## Что сделать") < md.index("## Критерии приёмки")
    assert md.index("## Критерии приёмки") < md.index("## Контекст")


def test_title_is_not_part_of_the_body():
    # заголовок — отдельное поле задачи на доске, в описание он не дублируется
    assert "Заголовок" not in render_markdown(_doc())


def test_steps_and_criteria_are_numbered():
    md = render_markdown(_doc())
    assert "1. Первый шаг" in md
    assert "2. Второй шаг" in md
    assert "1. Первый критерий" in md


def test_empty_sections_are_omitted():
    md = render_markdown(_doc(context=None, criteria=[]))
    assert "## Контекст" not in md
    assert "## Критерии приёмки" not in md
    assert "## Проблема" in md


def test_blank_items_are_dropped_and_whitespace_trimmed():
    md = render_markdown(_doc(steps=["  Шаг с пробелами  ", "", "   "]))
    assert "1. Шаг с пробелами" in md
    assert "2." not in md


def test_fully_empty_doc_renders_empty_string():
    assert render_markdown(TaskDoc(title="Только заголовок")) == ""
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/test_taskdoc.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.tasks.taskdoc'`

- [ ] **Step 3: Реализовать модуль**

Создать `reviewer/tasks/taskdoc.py`:

```python
"""Каноническая структура задачи: TaskDoc → markdown (PRI-213).

Board-agnostic ядро: структура описания задаётся здесь ОДИН раз для всех досок.
Провайдер получает готовый markdown и лишь конвертирует его в формат своего
транспорта (см. reviewer/tasks/boards/markup.py). Модуль чистый: без I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TaskDoc:
    """Поля новой задачи. title в тело описания НЕ входит — это отдельное поле доски."""

    title: str
    problem: str = ""
    steps: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    context: str | None = None


def _numbered(items: list[str] | None) -> str:
    """Нумерованный список; пустые и пробельные элементы отбрасываются."""
    cleaned = [s.strip() for s in (items or []) if s and s.strip()]
    return "\n".join(f"{i}. {s}" for i, s in enumerate(cleaned, 1))


def render_markdown(doc: TaskDoc) -> str:
    """Канонический markdown описания: фиксированный порядок секций, пустые опускаются.

    Секции: Проблема → Что сделать → Критерии приёмки → Контекст. Без эмодзи и
    декоративных разделителей — текст читают и человек в UI доски, и LLM из стора.
    """
    blocks: list[str] = []
    if (doc.problem or "").strip():
        blocks.append("## Проблема\n\n" + doc.problem.strip())
    steps = _numbered(doc.steps)
    if steps:
        blocks.append("## Что сделать\n\n" + steps)
    criteria = _numbered(doc.criteria)
    if criteria:
        blocks.append("## Критерии приёмки\n\n" + criteria)
    if (doc.context or "").strip():
        blocks.append("## Контекст\n\n" + doc.context.strip())
    return "\n\n".join(blocks)
```

- [ ] **Step 4: Прогнать тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tasks/test_taskdoc.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/taskdoc.py tests/tasks/test_taskdoc.py
git commit -m "feat(tasks): каноническая структура описания задачи TaskDoc (PRI-213)"
```

---

### Task 2: Конвертер markdown → HTML

**Files:**
- Create: `reviewer/tasks/boards/markup.py`
- Test: `tests/tasks/boards/test_markup.py`

**Interfaces:**
- Consumes: ничего (чистый stdlib).
- Produces: `md_to_html(md: str) -> str`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/tasks/boards/test_markup.py`:

```python
"""Конвертеры разметки для досок, хранящих описание в HTML (PRI-213)."""
from reviewer.tasks.boards.markup import md_to_html


def test_headings_become_h2_h3():
    assert "<h2>Проблема</h2>" in md_to_html("## Проблема")
    assert "<h3>Деталь</h3>" in md_to_html("### Деталь")


def test_paragraphs_wrapped_in_p():
    html = md_to_html("Первый абзац\n\nВторой абзац")
    assert "<p>Первый абзац</p>" in html
    assert "<p>Второй абзац</p>" in html


def test_numbered_and_bullet_lists():
    ol = md_to_html("1. Раз\n2. Два")
    assert "<ol>" in ol and "<li>Раз</li>" in ol and "<li>Два</li>" in ol
    ul = md_to_html("- Пункт\n- Ещё")
    assert "<ul>" in ul and "<li>Пункт</li>" in ul


def test_inline_code_content_is_escaped():
    # HTML внутри инлайн-кода должен доехать до доски как ВИДИМЫЙ текст, не как тег
    html = md_to_html("перенос строки это `<br />`")
    assert "<code>&lt;br /&gt;</code>" in html
    assert "<br />" not in html.replace("&lt;br /&gt;", "")


def test_fenced_code_block():
    html = md_to_html("```\nprint(1)\n```")
    assert "<pre><code>" in html and "print(1)" in html


def test_link_and_bold():
    html = md_to_html("см. [спеку](https://e/x) и **важное**")
    assert '<a href="https://e/x">спеку</a>' in html
    assert "<strong>важное</strong>" in html


def test_raw_html_in_plain_text_is_escaped():
    html = md_to_html("текст с <script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_empty_input_returns_empty():
    assert md_to_html("") == ""
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_markup.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.tasks.boards.markup'`

- [ ] **Step 3: Реализовать `md_to_html`**

Создать `reviewer/tasks/boards/markup.py`:

```python
"""Конвертеры разметки описания задачи (PRI-213).

Общая валюта между ядром (reviewer/tasks/taskdoc.py) и досками — канонический
markdown. Доски, хранящие описание в HTML (YouGile), конвертируют его на запись
(md_to_html) и обратно на чтение (html_to_md). Доски с нативным markdown
(YouTrack) этот модуль не используют.

Подмножество узкое и намеренно неполное: заголовки, абзацы, списки, код,
ссылки, жирный. Всё остальное деградирует в текст, а не ломает конвертацию.
Только stdlib — в ядре нет и не заводится markdown-зависимостей.
"""
from __future__ import annotations

import html
import logging
import re

log = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\d+\.\s+(.*)$")
_FENCE_RE = re.compile(r"^```")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_STASH = "\x00%d\x00"


def _inline(text: str) -> str:
    """Экранировать текст и разложить инлайн-разметку в HTML.

    Инлайн-код вынимается ПЕРВЫМ в плейсхолдеры, поэтому его содержимое
    экранируется целиком и не участвует в остальных заменах.
    """
    stash: list[str] = []

    def _keep(m: re.Match) -> str:
        stash.append(f"<code>{html.escape(m.group(1))}</code>")
        return _STASH % (len(stash) - 1)

    out = html.escape(_INLINE_CODE_RE.sub(_keep, text))
    out = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', out)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    for i, chunk in enumerate(stash):
        out = out.replace(_STASH % i, chunk)
    return out


def md_to_html(md: str) -> str:
    """Канонический markdown → узкий HTML для доски, хранящей описание в HTML."""
    if not md:
        return ""
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    list_tag = ""
    code: list[str] | None = None

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if items:
            body = "".join(f"<li>{_inline(i)}</li>" for i in items)
            out.append(f"<{list_tag}>{body}</{list_tag}>")
            items.clear()
            list_tag = ""

    for line in md.splitlines():
        if _FENCE_RE.match(line.strip()):
            if code is None:
                flush_para()
                flush_list()
                code = []
            else:
                out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        if not line.strip():
            flush_para()
            flush_list()
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush_para()
            flush_list()
            level = 2 if len(heading.group(1)) <= 2 else 3
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            continue
        bullet = _BULLET_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if bullet or ordered:
            flush_para()
            tag = "ul" if bullet else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            items.append((bullet or ordered).group(1).strip())
            continue
        flush_list()
        para.append(line.strip())

    if code is not None:                      # незакрытый fence — не теряем текст
        out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
    flush_para()
    flush_list()
    return "".join(out)
```

- [ ] **Step 4: Прогнать тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tasks/boards/test_markup.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/boards/markup.py tests/tasks/boards/test_markup.py
git commit -m "feat(tasks): конвертер markdown → HTML для досок с HTML-описанием (PRI-213)"
```

---

### Task 3: Конвертер HTML → markdown

**Files:**
- Modify: `reviewer/tasks/boards/markup.py`
- Test: `tests/tasks/boards/test_markup.py` (дополняется)

**Interfaces:**
- Consumes: `md_to_html` из Task 2 (для round-trip тестов).
- Produces: `html_to_md(html_text: str) -> str`.

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `tests/tasks/boards/test_markup.py`:

```python
from reviewer.tasks.boards.markup import html_to_md

PRI_213 = """## Проблема

Reviewer читает доски по REST и пишет узко.

## Что сделать

1. Добавить тул `create_task`
2. Реализовать в провайдерах

## Критерии приёмки

1. Тул создаёт задачу и возвращает ключ"""


def test_round_trip_preserves_canonical_markdown():
    assert html_to_md(md_to_html(PRI_213)) == PRI_213


def test_plain_markdown_passes_through_unchanged():
    # почти все задачи на доске — markdown, лежащий в HTML-поле как обычный текст
    assert html_to_md(PRI_213) == PRI_213


def test_entities_are_unescaped_in_plain_text():
    assert html_to_md("created_at &gt; now()") == "created_at > now()"


def test_finish_task_pr_block_becomes_plain_line():
    src = 'тело<div>PR: <a href="https://g/p/1">https://g/p/1</a></div>'
    md = html_to_md(src)
    assert "<div>" not in md and "<a" not in md
    assert "PR: https://g/p/1" in md
    assert "тело" in md


def test_br_becomes_newline_and_unknown_tags_are_transparent():
    md = html_to_md("первая<br />вторая<span>третья</span>")
    assert "<br" not in md and "<span>" not in md
    assert md.splitlines()[0] == "первая"
    assert "третья" in md


def test_named_link_becomes_markdown_link():
    assert html_to_md('<a href="https://e/x">спека</a>') == "[спека](https://e/x)"


def test_lists_become_markdown_lists():
    md = html_to_md("<ul><li>раз</li><li>два</li></ul>")
    assert md == "- раз\n- два"
    md = html_to_md("<ol><li>раз</li><li>два</li></ol>")
    assert md == "1. раз\n2. два"


def test_broken_html_never_raises():
    assert html_to_md("<div><p>текст") .strip() == "текст"


def test_empty_input_returns_empty_md():
    assert html_to_md("") == ""
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_markup.py -q`
Expected: FAIL — `ImportError: cannot import name 'html_to_md'`

- [ ] **Step 3: Реализовать `html_to_md`**

Добавить импорт в шапку `reviewer/tasks/boards/markup.py` (рядом с `import html`):

```python
from html.parser import HTMLParser
```

Дописать в конец `reviewer/tasks/boards/markup.py` (после `md_to_html`):

```python
_BLOCK_TAGS = {"p", "div", "section", "article", "blockquote", "tr"}
_HEADINGS = {"h1": 2, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class _MarkdownWriter(HTMLParser):
    """HTML → markdown: узкое подмножество; неизвестные теги прозрачны (текст цел).

    Пробелы и переносы ВНУТРИ текста сохраняются как есть: почти все описания на
    досках — markdown, лежащий в HTML-поле обычным текстом, и схлопывание пробелов
    склеило бы его в один абзац.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._lists: list[dict] = []
        self._pre = False
        self._href: str | None = None
        self._link_at: int | None = None

    def _newblock(self) -> None:
        text = "".join(self._parts)
        if not text:
            return
        if text.endswith("\n\n"):
            return
        self._parts.append("\n" if text.endswith("\n") else "\n\n")

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _HEADINGS:
            self._newblock()
            self._parts.append("#" * _HEADINGS[tag] + " ")
        elif tag == "br":
            self._parts.append("\n")
        elif tag in ("ul", "ol"):
            self._newblock()
            self._lists.append({"ordered": tag == "ol", "n": 0})
        elif tag == "li":
            lst = self._lists[-1] if self._lists else {"ordered": False, "n": 0}
            lst["n"] += 1
            text = "".join(self._parts)
            if text and not text.endswith("\n"):
                self._parts.append("\n")
            self._parts.append(f"{lst['n']}. " if lst["ordered"] else "- ")
        elif tag == "pre":
            self._newblock()
            self._parts.append("```\n")
            self._pre = True
        elif tag == "code" and not self._pre:
            self._parts.append("`")
        elif tag in ("strong", "b"):
            self._parts.append("**")
        elif tag in ("em", "i"):
            self._parts.append("*")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._link_at = len(self._parts)
        elif tag in _BLOCK_TAGS:
            self._newblock()

    def handle_endtag(self, tag: str) -> None:
        if tag in _HEADINGS or tag in _BLOCK_TAGS:
            self._newblock()
        elif tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            self._newblock()
        elif tag == "pre":
            self._pre = False
            if not "".join(self._parts).endswith("\n"):
                self._parts.append("\n")
            self._parts.append("```")
            self._newblock()
        elif tag == "code" and not self._pre:
            self._parts.append("`")
        elif tag in ("strong", "b"):
            self._parts.append("**")
        elif tag in ("em", "i"):
            self._parts.append("*")
        elif tag == "a":
            at = self._link_at if self._link_at is not None else len(self._parts)
            text = "".join(self._parts[at:]).strip()
            del self._parts[at:]
            href = self._href
            if href and text and text != href:
                self._parts.append(f"[{text}]({href})")
            else:
                self._parts.append(href or text)
            self._href = None
            self._link_at = None

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def result(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_md(html_text: str) -> str:
    """HTML-описание доски → markdown. Терпима к чужому дереву; НИКОГДА не бросает.

    Вход без тегов (markdown, лежащий в HTML-поле как текст) возвращается как есть,
    только с разэкранированными сущностями. Ограничение: HTML-теги, написанные
    человеком внутри инлайн-кода прямо в UI доски, неотличимы от настоящей разметки
    и будут съедены. Для текста, записанного через create_task, этого не случается —
    md_to_html экранирует содержимое кода.
    """
    if not html_text:
        return ""
    if "<" not in html_text:
        return html.unescape(html_text)
    writer = _MarkdownWriter()
    try:
        writer.feed(html_text)
        writer.close()
        return writer.result()
    except Exception:
        log.warning("markup: HTML не разобран — отдаём исходный текст", exc_info=True)
        return html_text
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/tasks/boards/test_markup.py -q`
Expected: PASS (17 passed)

Если round-trip падает на списках или пустых строках — правь `result()`/`_newblock()`, а не тест: канонический markdown из Task 1 обязан переживать круг.

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/tasks/boards/markup.py tests/tasks/boards/test_markup.py
git add reviewer/tasks/boards/markup.py tests/tasks/boards/test_markup.py
git commit -m "feat(tasks): конвертер HTML → markdown с round-trip гарантией (PRI-213)"
```

---

### Task 4: YouGile отдаёт описание в markdown

**Files:**
- Modify: `reviewer/tasks/boards/yougile.py:63-109` (`normalize_yougile`)
- Test: `tests/tasks/boards/test_yougile_normalize.py` (дополняется)

**Interfaces:**
- Consumes: `html_to_md` (Task 3).
- Produces: инвариант «`normalize()` YouGile возвращает `description` в markdown» — на него опираются Task 7 (write-through) и потребители стора.

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `tests/tasks/boards/test_yougile_normalize.py`:

```python
def test_normalize_converts_html_description_to_markdown():
    # YouGile хранит описание в HTML; в стор reviewer оно обязано попадать markdown-ом
    raw = RawTask(key="ID-1", project_code="PRI-1", title="t",
                  description='тело<div>PR: <a href="https://g/p/1">https://g/p/1</a></div>',
                  status="Бэклог", subtask_ids=[], timestamp=1)
    out = normalize_yougile(raw, r"PRI-\d+", "https://b/#{code}")
    assert "<div>" not in out["description"]
    assert "PR: https://g/p/1" in out["description"]


def test_normalize_keeps_plain_markdown_intact():
    raw = RawTask(key="ID-2", project_code="PRI-2", title="t",
                  description="## Проблема\n\nтекст", status=None,
                  subtask_ids=[], timestamp=1)
    out = normalize_yougile(raw, r"PRI-\d+", "https://b/#{code}")
    assert out["description"] == "## Проблема\n\nтекст"
```

Если в файле ещё нет импортов `RawTask` / `normalize_yougile` — они там уже есть (файл целиком про эту функцию); при расхождении сигнатуры `RawTask` смотри `reviewer/tasks/boards/base.py:24`.

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_normalize.py -q`
Expected: FAIL — `assert '<div>' not in ...` (описание отдаётся как есть)

- [ ] **Step 3: Провести описание через конвертер**

В `reviewer/tasks/boards/yougile.py` добавить импорт рядом с существующими импортами из `reviewer.tasks.boards`:

```python
from reviewer.tasks.boards.markup import html_to_md
```

В `normalize_yougile` заменить строку возврата описания:

```python
        "description": html_to_md(raw.description),
```

И дополнить докстроку функции:

```python
    """RawTask → TaskBrief dict. Чистая: без I/O (titles подзадач инжектятся).

    description конвертируется из HTML доски в markdown (PRI-213): стор и LLM
    видят чистый текст, а не <br />, &gt; и <div> транспорта YouGile.
    """
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/tasks/boards/ -q`
Expected: PASS — все тесты провайдеров, включая существующие normalize/attachments (вложения парсятся из **сырого** описания в `normalize`, до конвертации, поэтому не задеты).

- [ ] **Step 5: Коммит**

```bash
git add reviewer/tasks/boards/yougile.py tests/tasks/boards/test_yougile_normalize.py
git commit -m "feat(tasks): YouGile отдаёт описание задачи в markdown (PRI-213)"
```

---

### Task 5: YouGile — создание задачи

**Files:**
- Modify: `reviewer/tasks/boards/base.py:43-96` (Protocol), `reviewer/tasks/boards/yougile.py:361-403` (`list_done_targets` → хелпер) и далее (`create`)
- Test: `tests/tasks/boards/test_yougile_create.py`

**Interfaces:**
- Consumes: `md_to_html` (Task 2).
- Produces: `YougileBoard.create(doc_md: str, *, title: str, target: str | None, project: str | None) -> dict` с ключами `{key, url, board_id, target_resolved, warnings}`; приватный `_columns_of_project(project) -> tuple[list[dict], list[str]]`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/tasks/boards/test_yougile_create.py`:

```python
"""Создание задачи в YouGile (PRI-213)."""
import pytest

from reviewer.tasks.boards.yougile import YougileBoard

MD = "## Проблема\n\nтекст"


class _Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _Client:
    def __init__(self, get_routes, post_resp=None):
        self._get = get_routes
        self._post = post_resp or _Resp(200, {"id": "u-new"})
        self.calls = []  # (method, path, json)

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path not in self._get:
            raise RuntimeError(f"нет маршрута {path}")
        return self._get[path]

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        return self._post

    def close(self):
        pass


def _board(get_routes, post_resp=None):
    b = YougileBoard.__new__(YougileBoard)  # обойти httpx.Client в __init__
    b._client = _Client(get_routes, post_resp)
    b._key_pattern = r"PRI-\d+"
    b._url_template = "https://b/#{code}"
    return b


def _routes(columns):
    return {
        "/projects": _Resp(200, {"content": [{"id": "p1"}]}),
        "/boards": _Resp(200, {"content": [{"id": "b1", "title": "доска"}]}),
        "/columns": _Resp(200, {"content": columns}),
        "/tasks": _Resp(200, {"content": [{"idTaskProject": "PRI-1"}]}),
        "/tasks/u-new": _Resp(200, {"idTaskProject": "PRI-42", "id": "u-new"}),
    }


def test_create_puts_task_into_requested_column():
    b = _board(_routes([{"id": "c1", "title": "Бэклог"},
                        {"id": "c2", "title": "Движок"}]))
    res = b.create(MD, title="Заголовок", target="Движок", project="PRI")
    post = next(c for c in b._client.calls if c[0] == "POST")
    assert post[1] == "/tasks"
    assert post[2]["columnId"] == "c2"
    assert post[2]["title"] == "Заголовок"
    assert res["target_resolved"] == "Движок"
    assert not res["warnings"]


def test_create_sends_html_description():
    b = _board(_routes([{"id": "c1", "title": "Бэклог"}]))
    b.create(MD, title="t", target=None, project="PRI")
    post = next(c for c in b._client.calls if c[0] == "POST")
    assert "<h2>Проблема</h2>" in post[2]["description"]


def test_create_resolves_project_key_with_second_get():
    # POST /tasks отдаёт только uuid; проектный код (PRI-N) присваивает доска
    b = _board(_routes([{"id": "c1", "title": "Бэклог"}]))
    res = b.create(MD, title="t", target=None, project="PRI")
    assert res["key"] == "PRI-42"
    assert res["board_id"] == "u-new"
    assert res["url"] == "https://b/#PRI-42"


def test_create_falls_back_to_first_column_with_warning():
    b = _board(_routes([{"id": "c1", "title": "Бэклог"}]))
    res = b.create(MD, title="t", target="Нет такой", project="PRI")
    post = next(c for c in b._client.calls if c[0] == "POST")
    assert post[2]["columnId"] == "c1"
    assert res["target_resolved"] == "Бэклог"
    assert res["warnings"]


def test_create_failsoft_when_key_lookup_fails():
    routes = _routes([{"id": "c1", "title": "Бэклог"}])
    routes.pop("/tasks/u-new")            # второй GET недоступен
    b = _board(routes)
    res = b.create(MD, title="t", target=None, project="PRI")
    assert res["key"] == "u-new"          # деградация до внутреннего id
    assert res["warnings"]


def test_create_raises_when_no_columns():
    b = _board({"/projects": _Resp(200, {"content": []})})
    with pytest.raises(RuntimeError):
        b.create(MD, title="t", target=None, project="PRI")
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_create.py -q`
Expected: FAIL — `AttributeError: 'YougileBoard' object has no attribute 'create'`

- [ ] **Step 3: Реализовать хелпер и `create`**

3.1. В `reviewer/tasks/boards/base.py` в `TaskBoardProvider` добавить метод (после `finish`) и уточнить контракт нормализации в докстроке `normalize`:

```python
    def create(self, doc_md: str, *, title: str, target: str | None,
               project: str | None) -> dict:
        """Создать задачу из канонического markdown (см. reviewer/tasks/taskdoc.py).

        target — доска-специфичная цель размещения (YouGile: title колонки;
        YouTrack: значение поля статуса); не найдена → создать в дефолтном месте
        и вернуть причину в warnings, не падать. Возвращает
        {key, url, board_id, target_resolved, warnings}. Бросает только если
        задачу не удалось создать вовсе.
        """
        ...
```

В докстроку `normalize` дописать строку:

```python
        """RawTask → TaskBrief dict {key, aliases, title, description,
        criteria, status, url, links, attachments}.

        ИНВАРИАНТ: description возвращается в markdown. Если транспорт доски
        хранит другой формат (YouGile — HTML), конвертация делается внутри
        провайдера (reviewer/tasks/boards/markup.py).
        """
```

3.2. В `reviewer/tasks/boards/yougile.py` добавить импорт:

```python
from reviewer.tasks.boards.markup import html_to_md, md_to_html
```

3.3. Выделить обход колонок из `list_done_targets` в хелпер и переписать сам метод на него:

```python
    def _columns_of_project(self, project: str | None) -> tuple[list[dict], list[str]]:
        """Колонки досок проекта: ([{title, id, board_id, board_title}], warnings).

        project — код-префикс задач (напр. PRI): доска включается, если на ней есть
        хоть одна задача проекта. Пустой project → все доски. fail-soft: ошибка →
        ([], warnings). Общая основа для discovery done-цели и для create.
        """
        warnings: list[str] = []
        boards: list[dict] = []
        hosts: set[str] = set()
        scanned = 0
        _CAP = 500
        try:
            for proj in self._get_all("/projects"):
                for brd in self._get_all("/boards", {"projectId": proj["id"]}):
                    bid = brd["id"]
                    cols = [{"id": c["id"], "title": c.get("title", "")}
                            for c in self._get_all("/columns", {"boardId": bid})]
                    boards.append({"board_id": bid, "board_title": brd.get("title", ""),
                                   "columns": cols})
                    if not project:
                        continue
                    for c in cols:
                        if scanned >= _CAP:
                            break
                        hit = False
                        for t in self._get_all("/tasks", {"columnId": c["id"]}):
                            scanned += 1
                            if project_prefix(t.get("idTaskProject", "")) == project:
                                hit = True
                                break
                            if scanned >= _CAP:
                                break
                        if hit:
                            hosts.add(bid)
                            break
        except Exception:
            log.warning("yougile: обход колонок не удался", exc_info=True)
            warnings.append("не удалось перечислить колонки доски")
        kept = boards if not project else [b for b in boards if b["board_id"] in hosts]
        columns = [{"title": col["title"], "id": col["id"],
                    "board_id": b["board_id"], "board_title": b["board_title"]}
                   for b in kept for col in b["columns"]]
        if project and not hosts and not warnings:
            warnings.append(f"колонки для проекта {project!r} не найдены")
        return columns, warnings

    def list_done_targets(self, project: str | None) -> dict:
        """Колонки досок проекта (read-only, fail-soft). НИКОГДА не бросает."""
        columns, warnings = self._columns_of_project(project)
        return {"columns": columns, "warnings": warnings}
```

3.4. Добавить `create` (рядом с `finish`):

```python
    def create(self, doc_md: str, *, title: str, target: str | None,
               project: str | None) -> dict:
        """Создать задачу YouGile: POST /tasks + резолв проектного кода вторым GET.

        Описание конвертируется в HTML (транспорт YouGile). Колонка ищется по
        точному title среди колонок досок проекта; не найдена → первая колонка
        + warning. POST возвращает только внутренний uuid, поэтому ключ вида
        PRI-N дочитывается GET /tasks/{uuid} (fail-soft: остаётся uuid).
        """
        columns, warnings = self._columns_of_project(project)
        if not columns:
            raise RuntimeError(f"колонки доски для проекта {project!r} не найдены")
        col = next((c for c in columns if c["title"] == target), None) if target else None
        if target and col is None:
            warnings.append(
                f"колонка '{target}' не найдена — задача создана в '{columns[0]['title']}'")
        col = col or columns[0]

        r = self._client.post("/tasks", json={"title": title, "columnId": col["id"],
                                              "description": md_to_html(doc_md)})
        r.raise_for_status()
        uuid = str((r.json() or {}).get("id") or "")
        key = uuid
        try:
            rr = self._client.get(f"/tasks/{quote(uuid, safe='')}")
            rr.raise_for_status()
            key = (rr.json() or {}).get("idTaskProject") or uuid
        except Exception:
            log.warning("yougile: проектный код задачи %s не резолвится", uuid, exc_info=True)
            warnings.append("проектный код задачи не резолвится — вернули внутренний id")
        url = self._url_template.replace("{code}", key) if self._url_template else None
        return {"key": key, "url": url, "board_id": uuid,
                "target_resolved": col["title"], "warnings": warnings}
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/tasks/boards/ -q`
Expected: PASS — новые тесты создания + существующие `test_yougile_targets.py` (рефактор `list_done_targets` поведение не меняет).

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/tasks/boards/ tests/tasks/boards/
git add reviewer/tasks/boards/base.py reviewer/tasks/boards/yougile.py tests/tasks/boards/test_yougile_create.py
git commit -m "feat(tasks): создание задачи в YouGile через REST (PRI-213)"
```

---

### Task 6: YouTrack — создание задачи

**Files:**
- Modify: `reviewer/tasks/boards/youtrack.py:213-285` (выделить `_set_status` из `finish`), далее — `create`
- Test: `tests/tasks/boards/test_youtrack_create.py`, `tests/tasks/boards/test_base.py` (дополняется)

**Interfaces:**
- Consumes: контракт `create` из Task 5 (`base.py`).
- Produces: `YouTrackBoard.create(...) -> dict` теми же ключами; приватный `_set_status(safe_key: str, state: str, custom_fields: list[dict]) -> tuple[bool, list[str]]`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/tasks/boards/test_youtrack_create.py`:

```python
"""Создание задачи в YouTrack (PRI-213)."""
import pytest

from reviewer.tasks.boards.youtrack import YouTrackBoard

MD = "## Проблема\n\nтекст"


class _Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data if json_data is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _Client:
    def __init__(self, get_routes, post_routes=None):
        self._get = get_routes
        self._post = post_routes or {}
        self.calls = []  # (method, path, json)

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path not in self._get:
            raise RuntimeError(f"нет маршрута {path}")
        return self._get[path]

    def post(self, path, json=None, params=None):
        self.calls.append(("POST", path, json))
        return self._post.get(path, _Resp(200, {"idReadable": "PRI-42"}))

    def close(self):
        pass


def _board(get_routes, post_routes=None, status_field="State"):
    b = YouTrackBoard.__new__(YouTrackBoard)  # обойти httpx.Client в __init__
    b._client = _Client(get_routes, post_routes)
    b._key_pattern = r"PRI-\d+"
    b._base = "https://yt.example/api"
    b._status_field = status_field
    return b


def _routes():
    return {
        "/admin/projects": _Resp(200, [{"id": "0-1", "shortName": "PRI"}]),
        "/issues/PRI-42": _Resp(200, {"customFields": [
            {"name": "State", "$type": "StateIssueCustomField",
             "value": {"$type": "StateBundleElement", "name": "Open"}}]}),
    }


def test_create_posts_markdown_as_is():
    b = _board(_routes())
    res = b.create(MD, title="Заголовок", target=None, project="PRI")
    post = next(c for c in b._client.calls if c[0] == "POST" and c[1] == "/issues")
    assert post[2]["description"] == MD          # YouTrack хранит markdown нативно
    assert post[2]["summary"] == "Заголовок"
    assert post[2]["project"] == {"id": "0-1"}
    assert res["key"] == "PRI-42"
    assert res["url"] == "https://yt.example/issue/PRI-42"


def test_create_sets_status_field_when_target_given():
    b = _board(_routes())
    res = b.create(MD, title="t", target="In Progress", project="PRI")
    upd = next(c for c in b._client.calls
               if c[0] == "POST" and c[1] == "/issues/PRI-42")
    field = upd[2]["customFields"][0]
    assert field["name"] == "State"
    assert field["value"]["name"] == "In Progress"
    assert res["target_resolved"] == "In Progress"
    assert not res["warnings"]


def test_create_failsoft_when_status_update_rejected():
    b = _board(_routes(), post_routes={"/issues/PRI-42": _Resp(400, {})})
    res = b.create(MD, title="t", target="Нет такого", project="PRI")
    assert res["key"] == "PRI-42"        # задача создана
    assert res["target_resolved"] is None
    assert res["warnings"]


def test_create_requires_project():
    b = _board(_routes())
    with pytest.raises(ValueError):
        b.create(MD, title="t", target=None, project=None)


def test_create_raises_when_project_unknown():
    b = _board({"/admin/projects": _Resp(200, [])})
    with pytest.raises(ValueError):
        b.create(MD, title="t", target=None, project="NOPE")
```

Дописать в `tests/tasks/boards/test_base.py`:

```python
def test_both_providers_implement_create():
    # контракт Protocol: обе доски умеют создавать задачу с одной сигнатурой
    import inspect

    from reviewer.tasks.boards.yougile import YougileBoard
    from reviewer.tasks.boards.youtrack import YouTrackBoard

    for cls in (YougileBoard, YouTrackBoard):
        sig = inspect.signature(cls.create)
        assert list(sig.parameters) == ["self", "doc_md", "title", "target", "project"]
```

- [ ] **Step 2: Прогнать тесты — убедиться, что падают**

Run: `.venv/bin/pytest tests/tasks/boards/test_youtrack_create.py tests/tasks/boards/test_base.py -q`
Expected: FAIL — `AttributeError: type object 'YouTrackBoard' has no attribute 'create'`

- [ ] **Step 3: Выделить `_set_status` и реализовать `create`**

3.1. В `reviewer/tasks/boards/youtrack.py` добавить приватный хелпер (перед `finish`):

```python
    def _set_status(self, safe_key: str, state: str,
                    custom_fields: list[dict]) -> tuple[bool, list[str]]:
        """Структурно выставить поле статуса задачи (без command-DSL).

        custom_fields — уже прочитанные поля задачи (name/$type/value). Возвращает
        (успех, warnings). Поле не найдено или REST отверг значение → (False, warning).
        """
        warnings: list[str] = []
        field = next((cf for cf in custom_fields if cf.get("name") == self._status_field), None)
        if field is None:
            warnings.append(
                f"поле статуса {self._status_field!r} не найдено на задаче — статус не изменён")
            return False, warnings
        cur = field.get("value")
        value_type = (cur.get("$type") if isinstance(cur, dict) and cur.get("$type")
                      else _FIELD_TO_ELEMENT.get(field.get("$type")))
        value_obj: dict = {"name": state}
        if value_type:
            value_obj["$type"] = value_type
        payload = {"name": self._status_field, "$type": field.get("$type"), "value": value_obj}
        resp = self._client.post(f"/issues/{safe_key}", json={"customFields": [payload]})
        if getattr(resp, "status_code", 200) >= 400:
            warnings.append(
                f"не удалось установить {self._status_field}={state}: HTTP {resp.status_code}")
            return False, warnings
        return True, warnings
```

3.2. В `finish` заменить блок `if mark_done:` (строки ~255-280) на вызов хелпера — поведение и число запросов сохраняются, потому что `custom_fields` уже прочитаны первым GET:

```python
        warnings: list[str] = []
        done_set = False
        if mark_done:
            done_set, w = self._set_status(safe_key, done_state or "Fixed", custom_fields)
            warnings.extend(w)
```

Текст warning'а в хелпере теперь без ключа задачи — это безопасно: существующие тесты
`tests/tasks/boards/test_youtrack_finish.py:119,157` проверяют только непустой список `warnings`,
без сверки подстроки.

3.3. Добавить `create`:

```python
    def create(self, doc_md: str, *, title: str, target: str | None,
               project: str | None) -> dict:
        """Создать задачу YouTrack: POST /issues с markdown как есть.

        project (shortName) обязателен — без него YouTrack не примет задачу; это
        единственный не-fail-soft случай. target, если задан, выставляется как
        значение self._status_field тем же структурным REST, что в finish.
        """
        if not project:
            raise ValueError("project обязателен для создания задачи в YouTrack")
        pr = self._client.get("/admin/projects",
                              params={"fields": "id,shortName", "query": project})
        pr.raise_for_status()
        pid = next((p["id"] for p in (pr.json() or []) if p.get("shortName") == project), None)
        if not pid:
            raise ValueError(f"проект {project!r} не найден в YouTrack")

        r = self._client.post("/issues", params={"fields": "idReadable"},
                              json={"project": {"id": pid}, "summary": title,
                                    "description": doc_md})
        r.raise_for_status()
        key = (r.json() or {}).get("idReadable") or ""
        warnings: list[str] = []
        target_resolved = None
        if target and key:
            safe_key = quote(key, safe="")
            try:
                rr = self._client.get(
                    f"/issues/{safe_key}",
                    params={"fields": "customFields(name,$type,value($type,name))"})
                rr.raise_for_status()
                fields = (rr.json() or {}).get("customFields") or []
            except Exception:
                log.warning("youtrack: поля задачи %s недоступны", key, exc_info=True)
                fields = []
            ok, w = self._set_status(safe_key, target, fields)
            warnings.extend(w)
            target_resolved = target if ok else None
        web = re.sub(r"/api/?$", "", self._base.rstrip("/"))
        return {"key": key, "url": f"{web}/issue/{key}" if key else None,
                "board_id": key, "target_resolved": target_resolved,
                "warnings": warnings}
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/tasks/boards/ -q`
Expected: PASS — новые тесты создания, соответствие Protocol и существующие `test_youtrack_finish.py` (рефактор поведение не меняет).

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/tasks/boards/ tests/tasks/boards/
git add reviewer/tasks/boards/youtrack.py tests/tasks/boards/test_youtrack_create.py tests/tasks/boards/test_base.py
git commit -m "feat(tasks): создание задачи в YouTrack через REST (PRI-213)"
```

---

### Task 7: Сервисный слой и MCP-тул create_task

**Files:**
- Modify: `reviewer/mcp/service.py` (рядом с `finish_task:365`), `reviewer/entrypoints/mcp_server.py` (рядом с `finish_task:120`)
- Test: `tests/mcp/test_create_task.py`

**Interfaces:**
- Consumes: `TaskDoc`/`render_markdown` (Task 1), `provider.create` (Tasks 5-6), существующие `make_board_provider`, `Settings.configured_board_types`, `TaskService.index_task`.
- Produces: `MCPReviewService.create_task(title, problem="", steps=None, criteria=None, context=None, board_type=None, project=None, target=None, status_field=None) -> dict` и одноимённый MCP-тул.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/mcp/test_create_task.py`:

```python
"""Сервисный слой create_task (PRI-213)."""
import pytest

from reviewer.mcp.service import MCPReviewService


class _Settings:
    def __init__(self, types):
        self._types = types

    def configured_board_types(self):
        return list(self._types)


class _Provider:
    def __init__(self):
        self.created = None
        self.closed = False

    def create(self, doc_md, *, title, target, project):
        self.created = {"doc_md": doc_md, "title": title, "target": target,
                        "project": project}
        return {"key": "PRI-42", "url": "https://b/#PRI-42", "board_id": "u1",
                "target_resolved": target, "warnings": []}

    def fetch_one(self, key):
        return {"raw": key}

    def normalize(self, raw):
        return {"key": "PRI-42", "description": "## Проблема\n\nтекст"}

    def close(self):
        self.closed = True


class _TaskService:
    def __init__(self):
        self.indexed = []

    def index_task(self, task):
        self.indexed.append(task)
        return {"key": task.get("key"), "embedded": True}


class _Components:
    def __init__(self, task_service):
        self.task_service = task_service


@pytest.fixture
def service(monkeypatch):
    def _make(types=("yougile",), provider=None):
        provider = provider or _Provider()
        tasks = _TaskService()
        svc = MCPReviewService.__new__(MCPReviewService)
        svc.settings = _Settings(types)
        svc.components = _Components(tasks)
        monkeypatch.setattr("reviewer.mcp.service.make_board_provider",
                            lambda *a, **kw: provider)
        return svc, provider, tasks
    return _make


def test_create_task_renders_canonical_markdown(service):
    svc, provider, _ = service()
    res = svc.create_task(title="Заголовок", problem="Суть",
                          steps=["Шаг"], criteria=["Критерий"], project="PRI",
                          target="Движок")
    assert res["status"] == "ok"
    assert res["key"] == "PRI-42"
    assert res["url"] == "https://b/#PRI-42"
    doc = provider.created["doc_md"]
    assert doc.startswith("## Проблема")
    assert "## Что сделать" in doc and "1. Шаг" in doc
    assert "## Критерии приёмки" in doc
    assert provider.created["title"] == "Заголовок"     # заголовок отдельным полем
    assert "Заголовок" not in doc


def test_create_task_write_through_indexes_task(service):
    svc, _, tasks = service()
    res = svc.create_task(title="t", problem="p", project="PRI")
    assert res["reindexed"] is True
    assert tasks.indexed and tasks.indexed[0]["key"] == "PRI-42"


def test_create_task_closes_provider(service):
    svc, provider, _ = service()
    svc.create_task(title="t", problem="p", project="PRI")
    assert provider.closed is True


def test_create_task_requires_board_type_when_ambiguous(service):
    svc, _, _ = service(types=("yougile", "youtrack"))
    res = svc.create_task(title="t", problem="p", project="PRI")
    assert res["status"] == "error"
    assert "board_type" in res["reason"]


def test_create_task_rejects_unconfigured_board(service):
    svc, _, _ = service(types=("yougile",))
    res = svc.create_task(title="t", problem="p", project="PRI",
                          board_type="youtrack")
    assert res["status"] == "error"


def test_create_task_returns_error_dict_on_provider_failure(service):
    class _Boom(_Provider):
        def create(self, doc_md, *, title, target, project):
            raise ValueError("проект не найден")

    svc, provider, _ = service(provider=_Boom())
    res = svc.create_task(title="t", problem="p", project="NOPE")
    assert res["status"] == "error"
    assert "проект не найден" in res["reason"]
    assert provider.closed is True
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/mcp/test_create_task.py -q`
Expected: FAIL — `AttributeError: 'MCPReviewService' object has no attribute 'create_task'`

- [ ] **Step 3: Реализовать сервисный метод и тул**

3.1. В `reviewer/mcp/service.py` добавить импорт ядра рядом с другими импортами `reviewer.tasks`:

```python
from reviewer.tasks.taskdoc import TaskDoc, render_markdown
```

3.2. Добавить метод сразу после `finish_task`:

```python
    def create_task(self, title: str, problem: str = "",
                    steps: list[str] | None = None, criteria: list[str] | None = None,
                    context: str | None = None, board_type: str | None = None,
                    project: str | None = None, target: str | None = None,
                    status_field: str | None = None) -> dict:
        """Создать задачу на доске (server-side write) из структурированных полей.

        Структура описания собирается сервером (reviewer/tasks/taskdoc.py), поэтому
        одинакова во всех клиентах и моделях. target — колонка (YouGile) или
        значение поля статуса (YouTrack). Креды из env; наружу не отдаются.
        """
        types = self.settings.configured_board_types()
        if board_type is None:
            if len(types) == 1:
                board_type = types[0]
            else:
                return {"status": "error",
                        "reason": f"board_type required (configured: {types or 'none'})"}
        if board_type not in types:
            return {"status": "error",
                    "reason": f"board '{board_type}' not configured (have: {types or 'none'})"}
        provider = make_board_provider(self.settings, board_type, status_field=status_field)
        if provider is None:
            return {"status": "error", "reason": f"board '{board_type}' not configured"}
        doc_md = render_markdown(TaskDoc(title=title, problem=problem,
                                         steps=list(steps or []),
                                         criteria=list(criteria or []), context=context))
        reindexed = False
        try:
            result = provider.create(doc_md, title=title, target=target, project=project)
            # Write-through: созданная задача сразу видна в get_task/search_tasks,
            # не дожидаясь ближайшего sync_board. Best-effort, fail-soft.
            try:
                raw = provider.fetch_one(result.get("key") or "")
                if raw is not None:
                    self.components.task_service.index_task(provider.normalize(raw))
                    reindexed = True
            except Exception:
                log.warning("create_task: write-through реиндекс не удался", exc_info=True)
        except Exception as e:
            log.warning("create_task: сбой создания задачи", exc_info=True)
            return {"status": "error", "reason": f"{type(e).__name__}: {e}"}
        finally:
            try:
                provider.close()
            except Exception:
                pass
        return {"status": "ok", "board_type": board_type, "reindexed": reindexed, **result}
```

3.3. В `reviewer/entrypoints/mcp_server.py` добавить тул сразу после `finish_task`:

```python
    @mcp.tool()
    def create_task(title: str, problem: str = "", steps: list[str] | None = None,
                    criteria: list[str] | None = None, context: str | None = None,
                    board_type: str | None = None, project: str | None = None,
                    target: str | None = None, status_field: str | None = None) -> dict:
        """Create a task on the board (server-side write) from structured fields.

        The canonical markdown body (Проблема / Что сделать / Критерии приёмки /
        Контекст) is assembled by the server, so every client and model produces the
        same structure; the board-specific markup conversion happens in the provider.
        board_type, project, target and status_field come from the repo's .review.yml
        (target = YouGile column title or YouTrack status value; discover valid values
        with get_board_targets). Credentials come from env; fail-soft. Returns
        {status, key, url, target_resolved, reindexed, warnings}."""
        return service.create_task(title, problem, steps, criteria, context,
                                   board_type, project, target, status_field)
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/mcp/ -q`
Expected: PASS — новые тесты + существующие тесты MCP-слоя.

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_create_task.py
git add reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/mcp/test_create_task.py
git commit -m "feat(mcp): тул create_task — создание задачи на доске server-side (PRI-213)"
```

---

### Task 8: force_renormalize — перенормализация задач ниже watermark

**Files:**
- Modify: `reviewer/tasks/sync.py:29-92` (`_sync_provider`), `reviewer/tasks/sync.py:94` (`run`), `reviewer/mcp/service.py:400-423` (`sync_board`), `reviewer/entrypoints/mcp_server.py:105-118` (тул `sync_board`)
- Test: `tests/tasks/test_sync.py` (дополняется)

**Interfaces:**
- Consumes: существующие `TaskService.index_batch` / `refresh_meta_batch`.
- Produces: `SyncService.run(..., force_renormalize: bool = False)`, `MCPReviewService.sync_board(..., force_renormalize: bool = False)`, одноимённый параметр тула.

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `tests/tasks/test_sync.py`:

```python
def test_force_renormalize_reindexes_tasks_below_watermark():
    """Разовая перенормализация: задачи ниже курсора идут через полный normalize,
    а не через дешёвый meta-refresh (иначе смена нормализации не доедет до стора)."""
    from reviewer.tasks.sync import SyncService

    class _Provider:
        board_type = "yougile"

        def iter_raw(self, board, limit):
            yield _raw("ID-1", 10)                    # ниже курсора

        def normalize(self, raw):
            return {"key": raw.key, "description": "## Проблема\n\nчисто"}

        def normalize_meta(self, raw):
            return {"key": raw.key}

    class _Tasks:
        def __init__(self):
            self.indexed = []
            self.meta = []

        def index_batch(self, items):
            self.indexed.extend(items)
            return [{"key": i["key"], "embedded": True} for i in items]

        def refresh_meta_batch(self, items):
            self.meta.extend(items)
            return {"meta_refreshed": len(items), "warnings": []}

    class _Meta:
        def get_index_meta(self, repo, ref):
            return "100"                              # курсор выше timestamp задачи

        def set_index_meta(self, repo, ref, value):
            pass

    tasks = _Tasks()
    svc = SyncService([_Provider()], tasks, _Meta())

    out = svc.run(force_renormalize=True)
    assert out["changed"] == 1
    assert tasks.indexed and "чисто" in tasks.indexed[0]["description"]
    assert not tasks.meta                              # meta-refresh не вызывался

    tasks2 = _Tasks()
    svc2 = SyncService([_Provider()], tasks2, _Meta())
    svc2.run()                                         # обычный прогон — как раньше
    assert not tasks2.indexed
    assert tasks2.meta
```

Хелпер `_raw(key, ts)` уже есть в `tests/tasks/test_sync.py:63` — используй его как есть (позиционные аргументы).

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/test_sync.py -q`
Expected: FAIL — `TypeError: run() got an unexpected keyword argument 'force_renormalize'`

- [ ] **Step 3: Пробросить флаг через три слоя**

3.1. `reviewer/tasks/sync.py` — сигнатуры и условие watermark:

```python
    def _sync_provider(self, provider, board, limit, force_renormalize=False) -> tuple[list[str], dict]:
```

```python
            if raw.timestamp <= cursor and not force_renormalize:
```

```python
    def run(self, board=None, limit=None, purge_orphaned=False,
            keep_with_prs=True, board_type=None, status_field=None,
            force_renormalize=False) -> dict:
```

```python
            active, one = self._sync_provider(provider, board, limit, force_renormalize)
```

И дополнить докстроку модуля (шапка файла) строкой:

```
force_renormalize=True игнорирует watermark: каждая задача проходит полный
normalize → index_batch. Разовая операция после смены правил нормализации
(PRI-213); дедуп по content_hash сам отсечёт задачи с неизменившимся текстом.
```

3.2. `reviewer/mcp/service.py` — `sync_board`:

```python
    def sync_board(self, board: str | None = None, limit: int | None = None,
                   purge_orphaned: bool = False, keep_with_prs: bool = True,
                   board_type: str | None = None,
                   status_field: str | None = None,
                   force_renormalize: bool = False) -> dict:
```

```python
            return sync.run(board=board, board_type=board_type, limit=limit,
                            purge_orphaned=purge_orphaned,
                            keep_with_prs=keep_with_prs, status_field=status_field,
                            force_renormalize=force_renormalize)
```

3.3. `reviewer/entrypoints/mcp_server.py` — тул `sync_board`: добавить параметр в сигнатуру, строку в докстроку и пробросить:

```python
                   status_field: str | None = None,
                   force_renormalize: bool = False) -> dict:
```

```
        force_renormalize=True ignores the watermark and re-normalizes every task —
        a one-off after normalization rules change (PRI-213); content_hash dedup keeps
        the embedding cost to actually-changed descriptions.
```

```python
        return service.sync_board(board, limit, purge_orphaned, keep_with_prs,
                                  board_type, status_field, force_renormalize)
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/tasks/test_sync.py tests/mcp/ -q`
Expected: PASS

- [ ] **Step 5: Линт и коммит**

```bash
.venv/bin/ruff check reviewer/tasks/sync.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py
git add reviewer/tasks/sync.py reviewer/mcp/service.py reviewer/entrypoints/mcp_server.py tests/tasks/test_sync.py
git commit -m "feat(tasks): force_renormalize для разовой перенормализации корпуса задач (PRI-213)"
```

---

### Task 9: Клиентский скилл /reviewer_create-task

**Files:**
- Create: `plugin/skills/create-task/SKILL.md`
- Modify: `plugin/skills/solve-task/SKILL.md` (перекрёстная ссылка в конце шага 5)
- Test: `tests/skills/test_create_task_skill.py`

**Interfaces:**
- Consumes: тул `create_task` (Task 7), существующие `get_board_config`, `get_board_targets`, `sync_board`, `search_codebase`.
- Produces: скилл `/reviewer_create-task`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/skills/test_create_task_skill.py`:

```python
"""Guardrail: скилл create-task — тонкий триггер server-side тула create_task."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "create-task" / "SKILL.md"
SOLVE = ROOT / "plugin" / "skills" / "solve-task" / "SKILL.md"


def test_create_task_name_follows_reviewer_prefix():
    assert "name: reviewer_create-task" in SKILL.read_text(encoding="utf-8")


def test_create_task_calls_write_tool_and_resyncs():
    t = SKILL.read_text(encoding="utf-8")
    assert "create_task(" in t
    assert "sync_board(" in t


def test_create_task_discovers_target_before_writing():
    t = SKILL.read_text(encoding="utf-8")
    assert "get_board_targets(" in t       # колонка/статус — из discovery, не из головы
    assert "get_board_config()" in t       # фолбэк конфига доски


def test_create_task_confirms_and_noops_boardless():
    t = SKILL.read_text(encoding="utf-8").lower()
    assert "confirm" in t                  # никогда не пишет молча
    assert "board-less" in t or "no-op" in t


def test_create_task_grounds_body_in_code():
    t = SKILL.read_text(encoding="utf-8")
    assert "search_codebase(" in t         # «Проблема» ссылается на path:line
    assert "path:line" in t


def test_create_task_forbids_decorative_output():
    t = SKILL.read_text(encoding="utf-8").lower()
    assert "no emoji" in t


def test_create_task_answers_in_russian():
    assert "Russian" in SKILL.read_text(encoding="utf-8")


def test_solve_task_points_to_create_task():
    assert "create-task" in SOLVE.read_text(encoding="utf-8")
```

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/skills/test_create_task_skill.py -q`
Expected: FAIL — `FileNotFoundError: .../plugin/skills/create-task/SKILL.md`

- [ ] **Step 3: Написать скилл**

Создать `plugin/skills/create-task/SKILL.md`:

```markdown
---
name: reviewer_create-task
description: Create a task on the connected board (YouGile / YouTrack) with a canonical, LLM-readable structure — server-side write via the reviewer MCP tool create_task. Use when the user asks to file/create a task ("заведи задачу", "создай тикет", "file a task", "create a task on the board"). Requires the reviewer MCP server + a configured board.
---

# Create Task

File a new task on the board from a structured draft. The body (Проблема / Что сделать /
Критерии приёмки / Контекст) is assembled **server-side**, so every client produces the same
shape and the board-specific markup conversion is not your problem. Reply to the user in Russian.

## Pipeline

1. **Config.** Read the `task_board` block (`type`, `project`, `status_field`) from the repo's
   `.review.yml`; if there is no block, fall back to `get_board_config()`. Nothing anywhere →
   **board-less no-op**: tell the user (in Russian) that no board is configured and stop.

2. **Draft the body.** Turn the user's request into four fields:
   - `problem` — what is broken or missing, grounded in the code: cite `path:line` from
     `search_codebase(...)` (and `callers`/`definition` when the blast radius matters) instead of
     paraphrasing. Never invent a path you have not seen in a tool result.
   - `steps` — concrete actions, one per list item.
   - `criteria` — acceptance criteria, one per item, each checkable.
   - `context` — links, related task keys, the origin of the request.
   Plain technical Russian: **no emoji**, no decorative separators, no marketing tone.

3. **Resolve the target.** Call `get_board_targets(board_type=<type>, project=<project>)` and pick
   the column (YouGile) / status value (YouTrack) that matches the task's topic; on a thematic
   board the right column is a judgment call, so propose one and let the user correct it. Empty
   discovery result → create without a target.

4. **Confirm.** Show the title, the resolved target and the full body text; write **only** after
   explicit confirmation. Never write to the board silently.

5. **Write.** Call `create_task(title=…, problem=…, steps=[…], criteria=[…], context=…,
   board_type=<type>, project=<project>, target=<column or status>,
   status_field=<status_field or null>)`. `status == "error"` → report the reason in Russian,
   fail-open.

6. **Re-index.** Call `sync_board(board=<project or null>, board_type=<type>,
   status_field=<status_field or null>)` so the new task is in the corpus for `search_tasks` /
   `get_task`. Cheap when the corpus is warm (the write-through already indexed it; this keeps
   the watermark honest).

7. **Report.** Give the user (in Russian) the task key, its URL and any `warnings` — in
   particular when the requested column was not found and the task landed elsewhere.

## Failure handling (fail-open)

- No board configured → board-less no-op with a short Russian note; never abort.
- `create_task` error (board unreachable, project unknown, key unresolved) → report the reason
  and stop.
- Read-only intent everywhere except the single confirmed `create_task` write.
```

Дописать в `plugin/skills/solve-task/SKILL.md` в конце раздела **5. Hand off to development** (после абзаца про `/reviewer_finish-task`):

```markdown
   **Board-less mode:** when the user's formulation has no task key and a board IS configured,
   you may offer `/reviewer_create-task` first — it files the task with the canonical structure,
   so the work gets a key, a URL and a place in the task corpus before implementation starts.
```

- [ ] **Step 4: Прогнать тесты — убедиться, что проходят**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS — новый guard + существующие guard-тесты скиллов (включая `test_assembled_prompts.py`).

- [ ] **Step 5: Коммит**

```bash
git add plugin/skills/create-task/SKILL.md plugin/skills/solve-task/SKILL.md tests/skills/test_create_task_skill.py
git commit -m "feat(skills): скилл reviewer_create-task для заведения задач на доске (PRI-213)"
```

---

### Task 10: Документация, манифест и финальная верификация

**Files:**
- Modify: `README.md` (раздел со списком скиллов ~строка 420 и «Skills reference» ~строка 650), `README.ru.md` (~строка 370 и «Скиллы (справочник)» ~строка 570)
- Modify: манифест codex через `scripts/update_codex_plugin_manifest.py`

**Interfaces:**
- Consumes: всё построенное в Tasks 1-9.
- Produces: синхронизированные доки и манифест; зелёный полный прогон.

- [ ] **Step 1: Добавить скилл в оба README**

В `README.md` в список скиллов (рядом с `/rag-reviewer:reviewer_finish-task`) добавить строку:

```markdown
  `/rag-reviewer:reviewer_create-task`
```

В «Skills reference» — новый подраздел после `reviewer_finish-task`:

```markdown
### `reviewer_create-task` — file a task on the board

Creates a task on the connected board (YouGile / YouTrack) with a canonical body assembled
server-side: Проблема / Что сделать / Критерии приёмки / Контекст. The description is stored as
clean markdown in both directions — the board's own markup (YouGile keeps HTML) is converted by
the provider, so `get_task` never returns `<br />` or `&gt;` to a model.

- **Arguments:** free-text description of the task.
- **MCP tools used:** `get_board_config`, `get_board_targets`, `search_codebase`, `create_task`,
  `sync_board`.
- **Flow:** read `.review.yml` task board → draft the four fields grounded in `path:line` →
  discover the target column/status → confirm with the user → `create_task(...)` → `sync_board(...)`
  → report key + URL.
- **Requires:** reviewer MCP server + a board configured in its env.
```

В `README.ru.md` — симметрично: строку `/rag-reviewer:reviewer_create-task — заведение задачи на доске` в список скиллов и подраздел в «Скиллы (справочник с параметрами)»:

```markdown
### `reviewer_create-task` — завести задачу на доске

Создаёт задачу на подключённой доске (YouGile / YouTrack). Тело описания собирает сервер по
канонической структуре: Проблема / Что сделать / Критерии приёмки / Контекст. Описание хранится
чистым markdown в обе стороны — разметку транспорта (YouGile хранит HTML) конвертирует провайдер,
поэтому `get_task` больше не отдаёт модели `<br />` и `&gt;`.

- **Аргументы:** свободное описание задачи.
- **Используемые MCP-тулы:** `get_board_config`, `get_board_targets`, `search_codebase`,
  `create_task`, `sync_board`.
- **Поток:** прочитать `task_board` из `.review.yml` → собрать четыре поля с опорой на `path:line`
  → discovery целевой колонки/статуса → подтверждение у пользователя → `create_task(...)` →
  `sync_board(...)` → ключ и ссылка в ответе.
- **Требует:** reviewer MCP-сервер + настроенную в его env доску.
```

Там же, в разделе про доску задач, добавить абзац про разовую перенормализацию:

```markdown
После обновления, меняющего нормализацию описаний, один раз запусти синк с
`force_renormalize=true` — он игнорирует watermark и пере-нормализует весь корпус
(дедуп по `content_hash` оставит эмбеддинг только реально изменившимся задачам).
```

- [ ] **Step 2: Пересобрать манифест codex**

Run:
```bash
.venv/bin/python scripts/update_codex_plugin_manifest.py
```
Expected: скрипт печатает обновлённый digest / переписывает манифест (правки под `plugin/` меняют payload-digest — без пересборки install-тесты краснеют).

- [ ] **Step 3: Полный прогон unit-тестов**

Run: `.venv/bin/pytest -q`
Expected: PASS, без ошибок и без новых предупреждений. Особое внимание: `tests/install/` (манифест), `tests/skills/` (guard-тесты), `tests/tasks/`, `tests/mcp/`.

- [ ] **Step 4: Линт всего изменённого**

Run: `.venv/bin/ruff check reviewer/ tests/ scripts/`
Expected: no issues в новых/изменённых файлах. Репозиторий исторически не полностью чист — не гоняйся за чужими предупреждениями, но свои файлы обязаны быть зелёными.

- [ ] **Step 5: Коммит**

```bash
git add README.md README.ru.md
git add -A  # манифест codex, если скрипт его переписал
git commit -m "docs: скилл create-task и force_renormalize в README (PRI-213)"
```

---

## Проверка на живой доске (после мержа и деплоя)

Не входит в TDD-цикл, но обязательна для приёмки задачи:

1. Передеплоить reviewer-mcp (`pip install -e .` / релиз), переподключить MCP-сервер.
2. `/reviewer_create-task` на тестовой формулировке → проверить в UI YouGile, что заголовки и
   списки отрисованы, а не слиплись; убедиться, что в тексте нет `<br />`, `&gt;`, `</div>`.
3. `sync_board(..., force_renormalize=true)` один раз → `get_task("PRI-213")` → описание
   markdown-ом, без HTML-хвостов.
4. Повторить создание на YouTrack-доске (если подключена) — markdown уходит нативно.
