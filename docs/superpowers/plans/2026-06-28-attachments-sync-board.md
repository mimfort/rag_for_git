# Вложения (attachments) в sync_board для YouTrack и YouGile — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Научить `sync_board` скачивать вложения задач (YouTrack и YouGile, включая файлы чата YouGile), парсить `.md`/`.txt`/`.docx`/`.pdf` в текст, складывать в Postgres (`tasks.attachments jsonb`), включать текст в эмбеддинг и отдавать через `get_task`.

**Architecture:** Сквозное поле `attachments` течёт через слои `RawTask → Board.normalize (I/O) → TaskBrief → build_task_text/TaskRow → tasks.attachments jsonb → get_task`. I/O (скачивание/парсинг) живёт в методах классов-провайдеров и инжектится в чистые функции `normalize_youtrack`/`normalize_yougile` (как `subtask_titles` сейчас). Логика «скачать→распарсить» изолирована в новом board-агностичном модуле `reviewer/tasks/boards/attachments.py`.

**Tech Stack:** Python 3.11–3.13, httpx, psycopg3 (jsonb), pgvector, pydantic-settings, python-docx, pypdf, pytest.

## Global Constraints

- Язык проекта — **русский**: комментарии, докстринги, сообщения. Сохранять стиль.
- Коммиты: **Conventional Commits на русском**, **без self-attribution** (никаких Co-Authored-By/упоминаний Claude).
- `ruff check .` — line-length 100, target py311. Прогонять перед коммитом.
- Ветка: `pri-196-attachments-sync-board` (уже создана; спека+бриф закоммичены — `120bf8f`).
- Внешние сервисы изолированы за интерфейсами и **мокаются в unit-тестах**; реальные вызовы — только в `-m integration`.
- `pytest` по умолчанию исключает `integration` (`addopts = -m 'not integration'`); store-round-trip помечать `@pytest.mark.integration`.
- Fail-soft на каждом слое: сбой одного файла → `content_text=None` (метаданные сохранены), не роняет задачу; сбой источника → пропуск, второй источник работает.
- Запуск тестов: `.venv/bin/pytest`. Линт: `.venv/bin/ruff check .`.

---

### Task 1: Зависимости + настройки Settings

**Files:**
- Modify: `pyproject.toml` (блок `dependencies`)
- Modify: `reviewer/config/settings.py:113` (после `youtrack_base_url`)
- Test: `tests/config/test_settings.py` (добавить тест)

**Interfaces:**
- Produces: `Settings.task_attachment_max_bytes: int`, `Settings.task_attachment_timeout: float`, `Settings.task_attachment_embed_chars: int`, `Settings.task_attachment_store_chars: int`. Зависимости `docx` (пакет `python-docx`) и `pypdf` доступны для импорта.

- [ ] **Step 1: Установить deps в venv и добавить в pyproject**

В `pyproject.toml`, в список `dependencies`, после строки `"httpx>=0.27",` добавить:
```toml
    "python-docx>=1.1",
    "pypdf>=4.0",
```
Затем установить в окружение:
```bash
.venv/bin/pip install -e ".[dev]"
```
Expected: установка без ошибок; `python-docx` и `pypdf` появляются в окружении.

- [ ] **Step 2: Написать падающий тест на новые настройки**

В `tests/config/test_settings.py` добавить:
```python
def test_attachment_settings_defaults():
    from reviewer.config.settings import Settings
    s = Settings(_env_file=None)
    assert s.task_attachment_max_bytes == 10 * 1024 * 1024
    assert s.task_attachment_timeout == 10.0
    assert s.task_attachment_embed_chars == 8000
    assert s.task_attachment_store_chars == 200000
```

- [ ] **Step 3: Запустить тест — убедиться, что падает**

Run: `.venv/bin/pytest tests/config/test_settings.py::test_attachment_settings_defaults -v`
Expected: FAIL (`AttributeError`, поля ещё нет).

- [ ] **Step 4: Добавить поля в Settings**

В `reviewer/config/settings.py` после строки `youtrack_base_url: str = ""` (`:113`) добавить:
```python
    # вложения задач (PRI-196): лимиты скачивания/парсинга для server-side синка.
    task_attachment_max_bytes: int = 10 * 1024 * 1024   # пропуск файлов больше (байт)
    task_attachment_timeout: float = 10.0               # таймаут скачивания одного файла (с)
    task_attachment_embed_chars: int = 8000             # потолок текста на файл в эмбеддинге
    task_attachment_store_chars: int = 200000           # санити-кап текста на файл в jsonb
```

- [ ] **Step 5: Запустить тест — убедиться, что проходит**

Run: `.venv/bin/pytest tests/config/test_settings.py::test_attachment_settings_defaults -v`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
.venv/bin/ruff check reviewer/config/settings.py
git add pyproject.toml reviewer/config/settings.py tests/config/test_settings.py
git commit -m "feat(tasks): зависимости и настройки для вложений (PRI-196)"
```

---

### Task 2: `attachments.py` — чистый парсинг `extract_text`

**Files:**
- Create: `reviewer/tasks/boards/attachments.py`
- Test: `tests/tasks/boards/test_attachments.py` (создать)

**Interfaces:**
- Produces: `extract_text(name: str, mime: str | None, data: bytes) -> str | None` — диспатч по расширению (primary) → mime (fallback): `.md/.markdown/.txt` декодирует utf-8; `.docx` через python-docx; `.pdf` через pypdf; иначе/пусто/сбой → `None`.

- [ ] **Step 1: Написать падающие тесты на extract_text**

Создать `tests/tasks/boards/test_attachments.py`:
```python
from io import BytesIO

from reviewer.tasks.boards.attachments import extract_text

# Минимальный валидный PDF с извлекаемым текстом "PDF SPEC CONTENT".
# pypdf логирует безвредный warning про startxref и пересобирает xref сам.
_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 58 >>
stream
BT /F1 18 Tf 20 100 Td (PDF SPEC CONTENT) Tj ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000241 00000 n 
0000000349 00000 n 
trailer
<< /Size 6 /Root 1 0 R >>
startxref
420
%%EOF"""


def _docx_bytes(*paragraphs: str) -> bytes:
    from docx import Document
    buf = BytesIO()
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    doc.save(buf)
    return buf.getvalue()


def test_extract_md_decodes_utf8():
    data = "# Спец\nстрока".encode("utf-8")
    assert extract_text("spec.md", "text/markdown", data) == "# Спец\nстрока"


def test_extract_txt_decodes():
    assert extract_text("notes.txt", None, b"plain text") == "plain text"


def test_extract_docx():
    data = _docx_bytes("Спека ТЗ строка один", "строка два")
    text = extract_text("spec.docx", None, data)
    assert "Спека ТЗ строка один" in text
    assert "строка два" in text


def test_extract_pdf():
    text = extract_text("spec.pdf", None, _MINIMAL_PDF)
    assert "PDF SPEC CONTENT" in text


def test_extract_unknown_returns_none():
    assert extract_text("image.png", "image/png", b"\x89PNG\r\n") is None


def test_extract_corrupt_docx_failsoft():
    assert extract_text("broken.docx", None, b"not a real docx") is None


def test_extract_empty_text_returns_none():
    assert extract_text("empty.md", None, b"   ") is None
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_attachments.py -v`
Expected: FAIL (`ModuleNotFoundError: reviewer.tasks.boards.attachments`).

- [ ] **Step 3: Реализовать `extract_text`**

Создать `reviewer/tasks/boards/attachments.py`:
```python
"""Скачивание и парсинг вложений задач (PRI-196): board-агностичный helper.

Чистая часть (`extract_text`) — без I/O, диспатч по формату; тестируется на байтах.
I/O-часть (`download`/`fetch_attachment`) — скачивание с лимитами и fail-soft.
Текстовые форматы (.md/.txt) декодируются напрямую, .docx через python-docx,
.pdf через pypdf; непарсимое/битое → None (сохраняются только метаданные).
"""
from __future__ import annotations

import logging
from io import BytesIO

log = logging.getLogger(__name__)

_TEXT_EXT = {".md", ".markdown", ".txt", ".text"}


def _ext(name: str) -> str:
    """Расширение файла в нижнем регистре с точкой (``spec.MD`` → ``.md``); ``""`` если нет."""
    i = (name or "").rfind(".")
    return name[i:].lower() if i >= 0 else ""


def _docx_text(data: bytes) -> str | None:
    from docx import Document
    doc = Document(BytesIO(data))
    text = "\n".join(p.text for p in doc.paragraphs)
    return text.strip() or None


def _pdf_text(data: bytes) -> str | None:
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    return text.strip() or None


def extract_text(name: str, mime: str | None, data: bytes) -> str | None:
    """Bytes вложения → текст (или None, если формат не парсится / парсинг упал)."""
    ext = _ext(name)
    try:
        if ext in _TEXT_EXT:
            return data.decode("utf-8", errors="replace").strip() or None
        if ext == ".docx":
            return _docx_text(data)
        if ext == ".pdf":
            return _pdf_text(data)
    except Exception:
        log.warning("attachments: парсинг %s упал (fail-soft)", name, exc_info=True)
        return None
    return None
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tasks/boards/test_attachments.py -v`
Expected: PASS (7 тестов; возможен warning pypdf про startxref — это норма).

- [ ] **Step 5: Коммит**

```bash
.venv/bin/ruff check reviewer/tasks/boards/attachments.py tests/tasks/boards/test_attachments.py
git add reviewer/tasks/boards/attachments.py tests/tasks/boards/test_attachments.py
git commit -m "feat(tasks): парсинг вложений md/txt/docx/pdf (PRI-196)"
```

---

### Task 3: `attachments.py` — I/O: `download` + `fetch_attachment`

**Files:**
- Modify: `reviewer/tasks/boards/attachments.py`
- Test: `tests/tasks/boards/test_attachments.py`

**Interfaces:**
- Consumes: `extract_text` (Task 2).
- Produces:
  - `download(client, url: str, *, timeout: float, max_bytes: int) -> bytes | None` — GET через httpx-совместимый `client.get(url, timeout=...)`; Content-Length > `max_bytes` или фактический размер > `max_bytes` → None; любой сбой → None (fail-soft).
  - `fetch_attachment(client, *, name: str, mime: str | None, size, url: str, timeout: float, max_bytes: int, store_chars: int) -> dict` → `{"name", "mime_type", "size", "content_text"}` (content_text=None при skip/непарсимом/сбое; иначе обрезан до `store_chars`).

- [ ] **Step 1: Написать падающие тесты на download/fetch_attachment**

Добавить в `tests/tasks/boards/test_attachments.py`:
```python
from reviewer.tasks.boards.attachments import download, fetch_attachment


class _FakeResp:
    def __init__(self, content=b"", headers=None, raise_exc=None):
        self.content = content
        self.headers = headers or {}
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc


class _FakeClient:
    """Минимальный httpx-подобный клиент: возвращает заранее заданный ответ."""
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


def test_download_ok():
    client = _FakeClient(_FakeResp(content=b"hello", headers={"Content-Length": "5"}))
    assert download(client, "/files/1", timeout=10.0, max_bytes=1000) == b"hello"


def test_download_skips_by_content_length():
    client = _FakeClient(_FakeResp(content=b"x" * 50, headers={"Content-Length": "9999"}))
    assert download(client, "/files/1", timeout=10.0, max_bytes=10) is None


def test_download_skips_by_actual_size():
    # Content-Length отсутствует, но фактический размер превышает лимит.
    client = _FakeClient(_FakeResp(content=b"x" * 50, headers={}))
    assert download(client, "/files/1", timeout=10.0, max_bytes=10) is None


def test_download_failsoft_on_exception():
    client = _FakeClient(RuntimeError("network down"))
    assert download(client, "/files/1", timeout=10.0, max_bytes=1000) is None


def test_fetch_attachment_parses_and_caps():
    client = _FakeClient(_FakeResp(content=b"hello world", headers={"Content-Length": "11"}))
    att = fetch_attachment(client, name="spec.md", mime="text/markdown", size=11,
                           url="/files/1", timeout=10.0, max_bytes=1000, store_chars=5)
    assert att == {"name": "spec.md", "mime_type": "text/markdown",
                   "size": 11, "content_text": "hello"}   # обрезано до store_chars=5


def test_fetch_attachment_metadata_only_on_skip():
    client = _FakeClient(_FakeResp(content=b"x" * 50, headers={"Content-Length": "9999"}))
    att = fetch_attachment(client, name="big.docx", mime=None, size=9999,
                           url="/files/1", timeout=10.0, max_bytes=10, store_chars=1000)
    assert att == {"name": "big.docx", "mime_type": None, "size": 9999, "content_text": None}
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_attachments.py -k "download or fetch_attachment" -v`
Expected: FAIL (`ImportError: cannot import name 'download'`).

- [ ] **Step 3: Реализовать download + fetch_attachment**

Добавить в `reviewer/tasks/boards/attachments.py` (в конец файла):
```python
def download(client, url: str, *, timeout: float, max_bytes: int) -> bytes | None:
    """Скачать файл по url через httpx-совместимый client. None при skip/сбое (fail-soft)."""
    try:
        resp = client.get(url, timeout=timeout)
        resp.raise_for_status()
        cl = resp.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) > max_bytes:
            log.warning("attachments: %s > max_bytes (%s) — skip", url, cl)
            return None
        data = resp.content
        if len(data) > max_bytes:
            log.warning("attachments: %s превысил max_bytes при чтении — skip", url)
            return None
        return data
    except Exception:
        log.warning("attachments: скачивание %s упало (fail-soft)", url, exc_info=True)
        return None


def fetch_attachment(client, *, name: str, mime: str | None, size, url: str,
                     timeout: float, max_bytes: int, store_chars: int) -> dict:
    """Скачать+распарсить одно вложение → {name, mime_type, size, content_text|None}."""
    content_text: str | None = None
    data = download(client, url, timeout=timeout, max_bytes=max_bytes)
    if data is not None:
        text = extract_text(name, mime, data)
        if text:
            content_text = text[:store_chars]
    return {"name": name, "mime_type": mime, "size": size, "content_text": content_text}
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tasks/boards/test_attachments.py -v`
Expected: PASS (все тесты).

- [ ] **Step 5: Коммит**

```bash
.venv/bin/ruff check reviewer/tasks/boards/attachments.py tests/tasks/boards/test_attachments.py
git add reviewer/tasks/boards/attachments.py tests/tasks/boards/test_attachments.py
git commit -m "feat(tasks): скачивание вложений с лимитами и fail-soft (PRI-196)"
```

---

### Task 4: Модель данных — `RawTask.attachments` + инъекция в чистые нормализаторы

**Files:**
- Modify: `reviewer/tasks/boards/base.py:24-35` (`RawTask`), `:50-53` (докстринг)
- Modify: `reviewer/tasks/boards/youtrack.py:63-89` (`normalize_youtrack`)
- Modify: `reviewer/tasks/boards/yougile.py:25-69` (`normalize_yougile`)
- Test: `tests/tasks/boards/test_youtrack_normalize.py`, `tests/tasks/boards/test_yougile_normalize.py`

**Interfaces:**
- Produces:
  - `RawTask.attachments: list[dict]` (default `[]`) — метаданные вложений из `iter_raw`.
  - `normalize_youtrack(raw, key_pattern, base_url, attachments=None) -> dict` — TaskBrief с ключом `"attachments"` (= `attachments or []`).
  - `normalize_yougile(raw, key_pattern, url_template, subtask_titles=None, attachments=None) -> dict` — то же.

- [ ] **Step 1: Написать падающие тесты на проброс attachments в TaskBrief**

В `tests/tasks/boards/test_youtrack_normalize.py` добавить:
```python
def test_normalize_includes_injected_attachments():
    raw = _issue_to_raw(_issue())
    atts = [{"name": "spec.md", "mime_type": "text/markdown", "size": 4, "content_text": "spec"}]
    b = normalize_youtrack(raw, KP, BASE, attachments=atts)
    assert b["attachments"] == atts


def test_normalize_attachments_default_empty():
    raw = _issue_to_raw(_issue())
    assert normalize_youtrack(raw, KP, BASE)["attachments"] == []
```

В `tests/tasks/boards/test_yougile_normalize.py` добавить:
```python
def test_normalize_includes_injected_attachments():
    atts = [{"name": "tz.docx", "mime_type": None, "size": 9, "content_text": "тз"}]
    b = normalize_yougile(_raw(), KP, URL, attachments=atts)
    assert b["attachments"] == atts


def test_normalize_attachments_default_empty():
    assert normalize_yougile(_raw(), KP, URL)["attachments"] == []
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_youtrack_normalize.py tests/tasks/boards/test_yougile_normalize.py -k attachments -v`
Expected: FAIL (`KeyError: 'attachments'` / `TypeError: unexpected keyword argument`).

- [ ] **Step 3: Добавить поле `RawTask.attachments`**

В `reviewer/tasks/boards/base.py`, в dataclass `RawTask` после строки `links: list[dict] = field(default_factory=list)  # предрезолвленные ссылки` (`:34`) добавить:
```python
    attachments: list[dict] = field(default_factory=list)  # метаданные вложений из iter_raw
    # (youtrack: name/mime/size/url inline из _FIELDS; yougile: пусто, фетчится в normalize)
```
И в докстринге `TaskBoardProvider.normalize` (`:51-52`) дополнить перечень ключей: `..., url, links, attachments}.`

- [ ] **Step 4: Добавить параметр `attachments` в `normalize_youtrack`**

В `reviewer/tasks/boards/youtrack.py` изменить сигнатуру (`:63`) и возвращаемый dict:
```python
def normalize_youtrack(raw: RawTask, key_pattern: str, base_url: str,
                       attachments: list[dict] | None = None) -> dict:
```
В возвращаемом dict (`:79-89`) добавить ключ перед закрытием:
```python
        "project": project_prefix(raw.key),
        "attachments": attachments or [],
    }
```

- [ ] **Step 5: Добавить параметр `attachments` в `normalize_yougile`**

В `reviewer/tasks/boards/yougile.py` изменить сигнатуру (`:25-30`) и dict:
```python
def normalize_yougile(
    raw: RawTask,
    key_pattern: str,
    url_template: str,
    subtask_titles: dict[str, str] | None = None,
    attachments: list[dict] | None = None,
) -> dict:
```
В возвращаемом dict (`:59-69`) добавить:
```python
        "project": project_prefix(raw.project_code or key),
        "attachments": attachments or [],
    }
```

- [ ] **Step 6: Запустить — убедиться, что проходит (и старые тесты целы)**

Run: `.venv/bin/pytest tests/tasks/boards/ -v`
Expected: PASS (новые + все существующие normalize-тесты).

- [ ] **Step 7: Коммит**

```bash
.venv/bin/ruff check reviewer/tasks/boards/base.py reviewer/tasks/boards/youtrack.py reviewer/tasks/boards/yougile.py
git add reviewer/tasks/boards/base.py reviewer/tasks/boards/youtrack.py reviewer/tasks/boards/yougile.py tests/tasks/boards/
git commit -m "feat(tasks): поле attachments в RawTask и инъекция в нормализаторы (PRI-196)"
```

---

### Task 5: Хранение — `build_task_text` fold + `TaskRow` + схема + upsert + get_task SELECT

**Files:**
- Modify: `reviewer/tasks/store.py:25-30` (`build_task_text`), `:39-50` (`TaskRow`), `:107-122` (`get_task`), `:124-144` (`upsert_task`), импорты (`:7-15`)
- Modify: `reviewer/index/schema.sql:56-67` (после блока tasks / ALTER project на `:70`)
- Test: `tests/tasks/test_text.py`, `tests/index/` (store round-trip — integration)

**Interfaces:**
- Consumes: ничего нового.
- Produces:
  - `build_task_text(title, description, criteria, attachments=None, *, embed_chars=8000) -> str` — добавляет `name + усечённый content_text` каждого вложения с непустым текстом.
  - `TaskRow.attachments: list[dict]` (default `[]`).
  - Колонка `tasks.attachments jsonb NOT NULL DEFAULT '[]'`; `upsert_task` пишет её; `TaskStore.get_task` читает её в `TaskRow.attachments`.

- [ ] **Step 1: Написать падающий тест на build_task_text fold**

В `tests/tasks/test_text.py` добавить:
```python
def test_build_task_text_includes_attachment_content():
    from reviewer.tasks.store import build_task_text
    atts = [{"name": "spec.md", "content_text": "детали спецификации"},
            {"name": "img.png", "content_text": None}]   # без текста — пропускается
    text = build_task_text("Заголовок", "Описание", ["крит"], atts)
    assert "детали спецификации" in text
    assert "spec.md" in text
    assert "img.png" not in text


def test_build_task_text_truncates_attachment_to_embed_chars():
    from reviewer.tasks.store import build_task_text
    atts = [{"name": "big.md", "content_text": "X" * 100}]
    text = build_task_text("T", "D", None, atts, embed_chars=10)
    assert "X" * 10 in text
    assert "X" * 11 not in text


def test_build_task_text_backward_compatible_without_attachments():
    from reviewer.tasks.store import build_task_text
    assert build_task_text("T", "D", None) == "T\n\nD"
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/test_text.py -k attachment -v`
Expected: FAIL (`TypeError: build_task_text() takes 3 ... arguments`).

- [ ] **Step 3: Расширить `build_task_text`**

В `reviewer/tasks/store.py` заменить `build_task_text` (`:25-30`):
```python
def build_task_text(title: str | None, description: str | None,
                    criteria: list[str] | None, attachments: list[dict] | None = None,
                    *, embed_chars: int = 8000) -> str:
    """Текст задачи для эмбеддинга и BM25: заголовок + описание + критерии + вложения.

    Текст каждого вложения с непустым ``content_text`` обрезается до ``embed_chars``
    (усечение, не summary — синк не тратит LLM-токены)."""
    parts = [title or "", description or ""]
    if criteria:
        parts.append("\n".join(c for c in criteria if c))
    for att in attachments or []:
        text = (att.get("content_text") or "").strip()
        if text:
            parts.append(f"{att.get('name', '')}\n{text[:embed_chars]}")
    return "\n\n".join(p for p in parts if p).strip()
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tasks/test_text.py -v`
Expected: PASS.

- [ ] **Step 5: Добавить поле в `TaskRow` и `import json`**

В `reviewer/tasks/store.py`:
- В импорты (после `import hashlib` на `:9`) добавить `import json`.
- В dataclass `TaskRow` после `project: str = ""` (`:50`) добавить:
```python
    attachments: list[dict] = field(default_factory=list)
```
- В импорты dataclass изменить `from dataclasses import dataclass` (`:12`) на `from dataclasses import dataclass, field`.

- [ ] **Step 6: Прописать колонку в схему**

В `reviewer/index/schema.sql` после строки `ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project text NOT NULL DEFAULT '';` (`:70`) добавить:
```sql
-- PRI-196: вложения задачи (распарсенный текст + метаданные) как jsonb.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attachments jsonb NOT NULL DEFAULT '[]';
```

- [ ] **Step 7: Прописать колонку в `upsert_task` и `get_task`**

В `upsert_task` (`:124-144`):
- В INSERT-список колонок добавить `attachments`, в VALUES — `%(attachments)s::jsonb`:
```python
        INSERT INTO tasks (key, aliases, title, description, status, url,
                           content_hash, text, embedding, project, attachments)
        VALUES (%(key)s,%(aliases)s,%(title)s,%(description)s,%(status)s,%(url)s,
                %(content_hash)s,%(text)s,%(embedding)s,%(project)s,%(attachments)s::jsonb)
```
- В `ON CONFLICT ... DO UPDATE SET` добавить `, attachments=EXCLUDED.attachments` после `project=EXCLUDED.project`.
- В params dict добавить:
```python
            "attachments": json.dumps(row.attachments, ensure_ascii=False),
```

В `get_task` (`:107-122`):
- В SELECT добавить колонку: `"content_hash, text, project, attachments FROM tasks "`.
- В конструктор `TaskRow` добавить `attachments=list(row[9] or [])` (psycopg3 возвращает jsonb уже распарсенным):
```python
        return TaskRow(
            key=row[0], aliases=list(row[1] or []), title=row[2],
            description=row[3], status=row[4], url=row[5],
            content_hash=row[6], text=row[7], embedding=[], project=row[8],
            attachments=list(row[9] or []))
```

- [ ] **Step 8: Написать integration round-trip тест (Postgres)**

В существующем integration-наборе store-тестов (рядом с другими `@pytest.mark.integration` для ChunkStore/tasks; если файла нет — создать `tests/index/test_tasks_attachments_roundtrip.py`) добавить:
```python
import pytest

from reviewer.tasks.store import TaskRow, TaskStore


@pytest.mark.integration
def test_attachments_roundtrip(pg_dsn):   # pg_dsn — существующая фикстура DSN ParadeDB
    store = TaskStore(pg_dsn)
    atts = [{"name": "spec.md", "mime_type": "text/markdown", "size": 4,
             "content_text": "spec"}]
    store.upsert_task(TaskRow(
        key="ATT-1", aliases=[], title="t", description="d", status=None, url=None,
        content_hash="h1", text="t\n\nd", embedding=[0.0] * 1024, project="ATT",
        attachments=atts))
    row = store.get_task("ATT-1")
    assert row is not None
    assert row.attachments == atts
    store.delete_tasks(["ATT-1"])
    store.close()
```
Примечание: если в проекте нет фикстуры `pg_dsn`, использовать ту же схему получения DSN, что в соседних integration-тестах `tests/index/test_store_hybrid.py` (скопировать их способ инициализации БД/схемы).

- [ ] **Step 9: Запустить unit-тесты (integration — при поднятой БД)**

Run: `.venv/bin/pytest tests/tasks/test_text.py -v`
Expected: PASS.
Run (если поднят ParadeDB): `.venv/bin/pytest -m integration -k attachments_roundtrip -v`
Expected: PASS (или SKIP, если БД не поднята — отметить в отчёте).

- [ ] **Step 10: Коммит**

```bash
.venv/bin/ruff check reviewer/tasks/store.py reviewer/index/schema.sql tests/tasks/test_text.py
git add reviewer/tasks/store.py reviewer/index/schema.sql tests/tasks/test_text.py tests/index/
git commit -m "feat(tasks): хранение вложений в tasks.attachments jsonb и fold в эмбеддинг (PRI-196)"
```

---

### Task 6: `TaskService` — проброс attachments через index_task/index_batch/get_task

**Files:**
- Modify: `reviewer/tasks/service.py:20-24` (`__init__`), `:26-84` (`index_task`), `:86-...` (`index_batch`), `:245-...` (`get_task`)
- Modify: `reviewer/app.py:57-60` (конструктор `TaskService`)
- Test: `tests/tasks/test_service.py`, `tests/tasks/test_service_batch.py`

**Interfaces:**
- Consumes: `build_task_text(..., attachments, embed_chars=...)`, `TaskRow.attachments`, `TaskService.get_task` row (Task 5).
- Produces:
  - `TaskService.__init__(..., attachment_embed_chars: int = 8000)`.
  - `index_task`/`index_batch` берут `task["attachments"]`, передают в `build_task_text` (с `embed_chars=self._attachment_embed_chars`) и в `TaskRow`.
  - `TaskService.get_task(...)` возвращает dict с ключом `"attachments"`.

- [ ] **Step 1: Написать падающие тесты**

В `tests/tasks/test_service.py` (использует существующие фейки `_FakeStore`/`_FakeEmbedder`; свериться с их интерфейсом в файле) добавить:
```python
def test_index_task_passes_attachments_to_row(fake_service_factory):
    # fake_service_factory — существующий хелпер сборки TaskService на фейках.
    # Если такого нет — собрать вручную как в соседних тестах файла.
    svc, store = fake_service_factory()
    task = {"key": "ID-1", "title": "t", "description": "d",
            "attachments": [{"name": "s.md", "mime_type": "text/markdown",
                             "size": 4, "content_text": "spec"}]}
    svc.index_task(task)
    saved = store.last_upserted   # см. как фейк хранит последнюю запись в этом файле
    assert saved.attachments == task["attachments"]


def test_get_task_returns_attachments(fake_service_factory):
    svc, store = fake_service_factory()
    store.seed_task(key="ID-2", attachments=[{"name": "a.txt", "content_text": "x"}])
    out = svc.get_task("ID-2")
    assert out["attachments"] == [{"name": "a.txt", "content_text": "x"}]
```
Примечание: имена фейк-хелперов (`fake_service_factory`, `store.last_upserted`, `store.seed_task`) — заглушки под фактический стиль файла. **Перед написанием прочитать `tests/tasks/test_service.py` и переиспользовать его существующие фейки** (`_FakeStore` с `upsert_task`, `get_task`); добавить в фейк-стор то, что нужно (хранение последней upsert-строки и поле attachments в seeded-строке).

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/test_service.py -k attachments -v`
Expected: FAIL.

- [ ] **Step 3: Прокинуть attachments в `index_task`**

В `reviewer/tasks/service.py`:
- `__init__` (`:20`) — добавить параметр и поле:
```python
    def __init__(self, store, graph, embedder, *, max_chars: int = 8000,
                 attachment_embed_chars: int = 8000) -> None:
        self._store = store
        self._graph = graph
        self._embedder = embedder
        self._max_chars = max_chars
        self._attachment_embed_chars = attachment_embed_chars
```
- В `index_task` после `criteria = task.get("criteria") or []` (`:35`) добавить:
```python
        attachments = task.get("attachments") or []
```
  заменить `text = build_task_text(title, description, criteria)` (`:41`) на:
```python
        text = build_task_text(title, description, criteria, attachments,
                               embed_chars=self._attachment_embed_chars)
```
  и в `TaskRow(...)` (`:52-55`) добавить `attachments=attachments`.

- [ ] **Step 4: Прокинуть attachments в `index_batch`**

В `index_batch` (`:95-115`):
- После `criteria = task.get("criteria") or []` (`:105`) добавить `attachments = task.get("attachments") or []`.
- Заменить `text = build_task_text(title, description, criteria)` (`:110`) на:
```python
            text = build_task_text(title, description, criteria, attachments,
                                   embed_chars=self._attachment_embed_chars)
```
- В словарь `parsed.append({...})` (`:112-115`) добавить ключ `"attachments": attachments`.
- В `TaskRow(...)` внутри upsert-цикла (`:153-157`) добавить `attachments=p["attachments"]`.

- [ ] **Step 5: Вернуть attachments из `get_task`**

В `TaskService.get_task` (`:245`), в возвращаемый dict добавить (после `"url": row.url,` и убрать «criteria=[] несёт description» оставить как есть):
```python
            "criteria": [],
            "status": row.status,
            "url": row.url,
            "attachments": list(row.attachments or []),
        }
```

- [ ] **Step 6: Прокинуть настройку в build_components**

В `reviewer/app.py` изменить конструктор (`:57-60`):
```python
    task_service = TaskService(
        task_store, task_graph, embedder,
        max_chars=settings.max_tool_result_chars,
        attachment_embed_chars=settings.task_attachment_embed_chars,
    )
```

- [ ] **Step 7: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tasks/test_service.py tests/tasks/test_service_batch.py -v`
Expected: PASS (новые + существующие).

- [ ] **Step 8: Коммит**

```bash
.venv/bin/ruff check reviewer/tasks/service.py reviewer/app.py tests/tasks/
git add reviewer/tasks/service.py reviewer/app.py tests/tasks/test_service.py tests/tasks/test_service_batch.py
git commit -m "feat(tasks): проброс вложений через TaskService (index/get_task) (PRI-196)"
```

---

### Task 7: YouTrack (приоритет) — `_FIELDS`, `_issue_to_raw`, `YouTrackBoard.normalize` скачивание

**Files:**
- Modify: `reviewer/tasks/boards/youtrack.py:19-23` (`_FIELDS`), `:48-60` (`_issue_to_raw`), `:97-138` (`YouTrackBoard`)
- Test: `tests/tasks/boards/test_youtrack_normalize.py`

**Interfaces:**
- Consumes: `fetch_attachment` (Task 3), `normalize_youtrack(..., attachments=...)` (Task 4).
- Produces: `YouTrackBoard.normalize(raw)` скачивает вложения из `raw.attachments` по `origin + url` и инжектит распарсенный список в `normalize_youtrack`.

**Контекст (по докам YouTrack REST):** `url` вложения относительный с подписью, напр. `/api/files/7-2?sign=...`; **Authorization не требуется** при скачивании (опознание по `sign=`). Полный URL = `origin (scheme+host из base_url) + url`. `base_url` оканчивается на `/api`, поэтому брать именно origin, не base.

- [ ] **Step 1: Написать падающие тесты (метаданные из issue + скачивание с fake-клиентом)**

В `tests/tasks/boards/test_youtrack_normalize.py` добавить:
```python
from reviewer.tasks.boards.youtrack import YouTrackBoard, _attachments_of, _origin


def test_origin_strips_api_path():
    assert _origin("https://c.youtrack.cloud/api") == "https://c.youtrack.cloud"


def test_attachments_of_extracts_metadata():
    issue = _issue(attachments=[
        {"name": "spec.md", "mimeType": "text/markdown", "size": 4,
         "url": "/api/files/7-2?sign=abc"},
        {"name": "nourl", "mimeType": "text/plain"},   # без url — пропускается
    ])
    atts = _attachments_of(issue)
    assert atts == [{"name": "spec.md", "mime": "text/markdown", "size": 4,
                     "url": "/api/files/7-2?sign=abc"}]


class _FakeHttpResp:
    def __init__(self, content, headers=None):
        self.content = content
        self.headers = headers or {"Content-Length": str(len(content))}

    def raise_for_status(self):
        pass


class _FakeHttpClient:
    def __init__(self, content_by_url):
        self._by_url = content_by_url
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        return _FakeHttpResp(self._by_url[url])

    def close(self):
        pass


def test_youtrack_board_normalize_downloads_attachment():
    board = YouTrackBoard.__new__(YouTrackBoard)   # обойти httpx.Client в __init__
    board._key_pattern = KP
    board._base = BASE
    board._att_max_bytes = 10 * 1024 * 1024
    board._att_timeout = 10.0
    board._att_store_chars = 200000
    board._client = _FakeHttpClient(
        {"https://c.youtrack.cloud/api/files/7-2?sign=abc": b"# Спека\nтекст"})
    raw = _issue_to_raw(_issue(attachments=[
        {"name": "spec.md", "mimeType": "text/markdown", "size": 13,
         "url": "/api/files/7-2?sign=abc"}]))
    brief = board.normalize(raw)
    assert brief["attachments"] == [{"name": "spec.md", "mime_type": "text/markdown",
                                     "size": 13, "content_text": "# Спека\nтекст"}]
    # скачано по полному origin+url (без Bearer-зависимости — sign в url):
    assert board._client.requested == ["https://c.youtrack.cloud/api/files/7-2?sign=abc"]
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_youtrack_normalize.py -k "origin or attachments_of or downloads" -v`
Expected: FAIL (`ImportError` / `AttributeError`).

- [ ] **Step 3: Добавить `attachments` в `_FIELDS`**

В `reviewer/tasks/boards/youtrack.py` заменить `_FIELDS` (`:19-23`):
```python
_FIELDS = (
    "idReadable,summary,description,updated,"
    "customFields(name,value(name)),"
    "links(direction,linkType(name),issues(idReadable)),"
    "attachments(name,size,mimeType,extension,url)"
)
```

- [ ] **Step 4: Добавить `_attachments_of`, `_origin` и проброс в `_issue_to_raw`**

В `reviewer/tasks/boards/youtrack.py` добавить импорт в начало (после `import re`):
```python
from urllib.parse import urlsplit
```
И добавить функции (рядом с `_links_of`):
```python
def _attachments_of(issue: dict) -> list[dict]:
    """Метаданные вложений из issue (url относительный с подписью; без url — пропуск)."""
    out: list[dict] = []
    for a in issue.get("attachments") or []:
        url = a.get("url")
        if not url:
            continue
        out.append({"name": a.get("name") or "", "mime": a.get("mimeType"),
                    "size": a.get("size"), "url": url})
    return out


def _origin(base_url: str) -> str:
    """scheme://host из base_url (отбрасывает путь /api) — для абсолютного URL файла."""
    p = urlsplit(base_url)
    return f"{p.scheme}://{p.netloc}"
```
В `_issue_to_raw` (`:51-60`) добавить в конструктор `RawTask`:
```python
        links=_links_of(issue),
        attachments=_attachments_of(issue),
    )
```

- [ ] **Step 5: Скачивание в `YouTrackBoard`**

В `reviewer/tasks/boards/youtrack.py`:
- Добавить импорт `from reviewer.tasks.boards.attachments import fetch_attachment` (в начало файла).
- В `YouTrackBoard.__init__` (`:97-113`) добавить параметры и поля (с дефолтами, чтобы не ломать существующие вызовы; реальные значения придут из make_board_provider в Task 9):
```python
    def __init__(self, *, token: str, base_url: str, key_pattern: str,
                 attachment_max_bytes: int = 10 * 1024 * 1024,
                 attachment_timeout: float = 10.0,
                 attachment_store_chars: int = 200000) -> None:
```
  и в теле после `self._base = base_url.rstrip("/")`:
```python
        self._att_max_bytes = attachment_max_bytes
        self._att_timeout = attachment_timeout
        self._att_store_chars = attachment_store_chars
```
- Заменить `YouTrackBoard.normalize` (`:137-138`):
```python
    def normalize(self, raw: RawTask) -> dict:
        origin = _origin(self._base)
        contents: list[dict] = []
        for a in raw.attachments:
            contents.append(fetch_attachment(
                self._client, name=a["name"], mime=a.get("mime"), size=a.get("size"),
                url=origin + a["url"], timeout=self._att_timeout,
                max_bytes=self._att_max_bytes, store_chars=self._att_store_chars))
        return normalize_youtrack(raw, self._key_pattern, self._base,
                                  attachments=contents)
```

- [ ] **Step 6: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tasks/boards/test_youtrack_normalize.py -v`
Expected: PASS (новые + существующие).

- [ ] **Step 7: Коммит**

```bash
.venv/bin/ruff check reviewer/tasks/boards/youtrack.py tests/tasks/boards/test_youtrack_normalize.py
git add reviewer/tasks/boards/youtrack.py tests/tasks/boards/test_youtrack_normalize.py
git commit -m "feat(tasks): скачивание вложений YouTrack в normalize (PRI-196)"
```

---

### Task 8: YouGile — файлы задачи и чата (best-effort) + `YougileBoard.normalize`

**Files:**
- Modify: `reviewer/tasks/boards/yougile.py:72-146` (`YougileBoard`)
- Test: `tests/tasks/boards/test_yougile_normalize.py`

**Interfaces:**
- Consumes: `fetch_attachment` (Task 3), `normalize_yougile(..., attachments=...)` (Task 4).
- Produces: `YougileBoard.normalize(raw)` тянет файлы задачи + файлы сообщений чата (best-effort, каждый источник fail-soft), парсит и инжектит в `normalize_yougile`.

> **Discovery (выполнить ПЕРВЫМ, до кода):** точная схема файлового объекта и эндпоинты YouGile API v2 не зафиксированы в публичных доках. Подтвердить эмпирически на живом API (подключённый MCP `mcp__yougile__*` — `get_task`, `get_task_chat`/`get_task_messages` — и/или прямой `GET` с кредами из env reviewer-mcp):
> 1. Есть ли в ответе `GET /tasks/{id}` поле со списком файлов (`files`/`attachments`)? Какова форма элемента (url? имя? id?)?
> 2. Эндпоинт сообщений чата (рабочая гипотеза: `GET /chats/{task_id}/messages`) и форма файлового сообщения — где лежат url/имя файла.
> Зафиксировать найденные форму и пути; код ниже написать под них. Хелперы держать defensive: любой ключ может отсутствовать → пропуск файла (fail-soft).

- [ ] **Step 1: Discovery — подтвердить эндпоинты и форму файлового объекта**

Через MCP `mcp__yougile__get_task` / `mcp__yougile__get_task_chat` (или `get_task_messages`) на реальной задаче с вложением:
- зафиксировать JSON-форму файла в задаче и/или в сообщении чата (ключи имени, url, mime/размера);
- зафиксировать рабочие REST-пути (для `self._client.get(...)`).
Записать вывод в комментарий к PR/коммиту. **Тесты ниже моделируют подтверждённую форму через fake-клиент** — обновить канонические dict'ы в тесте под реальную форму, если она отличается от гипотезы.

- [ ] **Step 2: Написать падающие тесты (fake-клиент моделирует подтверждённую форму)**

В `tests/tasks/boards/test_yougile_normalize.py` добавить (подставив реальные пути/ключи из Step 1):
```python
from reviewer.tasks.boards.yougile import YougileBoard


class _FakeYResp:
    def __init__(self, json_data=None, content=b"", headers=None):
        self._json = json_data or {}
        self.content = content
        self.headers = headers or {"Content-Length": str(len(content))}

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeYClient:
    """Маршрутизирует GET по точному пути на заранее заданный ответ."""
    def __init__(self, routes):
        self._routes = routes      # {path: _FakeYResp}
        self.requested = []

    def get(self, path, params=None, timeout=None):
        self.requested.append(path)
        return self._routes[path]

    def close(self):
        pass


def _board_with(routes):
    b = YougileBoard.__new__(YougileBoard)
    b._key_pattern = KP
    b._url_template = URL
    b._att_max_bytes = 10 * 1024 * 1024
    b._att_timeout = 10.0
    b._att_store_chars = 200000
    b._client = _FakeYClient(routes)
    return b


def test_yougile_normalize_pulls_chat_file():
    # ПУТИ/КЛЮЧИ — заменить на подтверждённые в Step 1.
    routes = {
        "/tasks/ID-10": _FakeYResp(json_data={"files": []}),
        "/chats/ID-10/messages": _FakeYResp(json_data={"content": [
            {"text": "вот ТЗ", "files": [
                {"name": "tz.md", "url": "https://yougile.com/f/abc", "size": 6}]}]}),
        "https://yougile.com/f/abc": _FakeYResp(content=b"тело ТЗ"),
    }
    board = _board_with(routes)
    raw = _raw(key="ID-10", subtask_ids=[])
    brief = board.normalize(raw)
    names = [a["name"] for a in brief["attachments"]]
    assert "tz.md" in names
    att = next(a for a in brief["attachments"] if a["name"] == "tz.md")
    assert att["content_text"] == "тело ТЗ"


def test_yougile_normalize_failsoft_when_chat_unavailable():
    # Чат недоступен (ключа в routes нет → KeyError внутри fetch → fail-soft).
    routes = {"/tasks/ID-10": _FakeYResp(json_data={"files": []})}
    board = _board_with(routes)
    brief = board.normalize(_raw(key="ID-10", subtask_ids=[]))
    assert brief["attachments"] == []   # источник упал, но normalize не упал
```

- [ ] **Step 3: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_normalize.py -k "chat_file or failsoft" -v`
Expected: FAIL.

- [ ] **Step 4: Реализовать фетч файлов + скачивание в `YougileBoard`**

В `reviewer/tasks/boards/yougile.py`:
- Добавить импорт `from reviewer.tasks.boards.attachments import fetch_attachment`.
- В `YougileBoard.__init__` (`:78-87`) добавить параметры и поля (с дефолтами):
```python
    def __init__(self, *, api_key: str, api_base: str, key_pattern: str,
                 url_template: str,
                 attachment_max_bytes: int = 10 * 1024 * 1024,
                 attachment_timeout: float = 10.0,
                 attachment_store_chars: int = 200000) -> None:
```
  и в теле:
```python
        self._att_max_bytes = attachment_max_bytes
        self._att_timeout = attachment_timeout
        self._att_store_chars = attachment_store_chars
```
- Добавить два defensive-хелпера и расширить `normalize` (формы/пути — из Step 1):
```python
    def _file_dicts_from_task(self, task_id: str) -> list[dict]:
        """Файлы, прикреплённые к карточке (best-effort; форма из discovery)."""
        try:
            r = self._client.get(f"/tasks/{task_id}")
            r.raise_for_status()
            return list(r.json().get("files", []) or [])
        except Exception:
            log.warning("yougile: файлы задачи %s недоступны", task_id, exc_info=True)
            return []

    def _file_dicts_from_chat(self, task_id: str) -> list[dict]:
        """Файлы из сообщений чата (best-effort; форма/путь из discovery)."""
        try:
            r = self._client.get(f"/chats/{task_id}/messages")
            r.raise_for_status()
            out: list[dict] = []
            for msg in r.json().get("content", []) or []:
                out.extend(msg.get("files", []) or [])
            return out
        except Exception:
            log.warning("yougile: чат задачи %s недоступен", task_id, exc_info=True)
            return []

    def _fetch_attachments(self, task_id: str) -> list[dict]:
        files = self._file_dicts_from_task(task_id) + self._file_dicts_from_chat(task_id)
        out: list[dict] = []
        for f in files:
            url = f.get("url")
            if not url:
                continue
            out.append(fetch_attachment(
                self._client, name=f.get("name") or "", mime=f.get("type"),
                size=f.get("size"), url=url, timeout=self._att_timeout,
                max_bytes=self._att_max_bytes, store_chars=self._att_store_chars))
        return out
```
- В `YougileBoard.normalize` (`:134-146`) перед `return` собрать вложения и прокинуть:
```python
        attachments = self._fetch_attachments(raw.key)
        return normalize_yougile(raw, self._key_pattern, self._url_template,
                                 subtask_titles, attachments=attachments)
```

- [ ] **Step 5: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/tasks/boards/test_yougile_normalize.py -v`
Expected: PASS (новые + существующие).

- [ ] **Step 6: Коммит**

```bash
.venv/bin/ruff check reviewer/tasks/boards/yougile.py tests/tasks/boards/test_yougile_normalize.py
git add reviewer/tasks/boards/yougile.py tests/tasks/boards/test_yougile_normalize.py
git commit -m "feat(tasks): скачивание вложений YouGile из задачи и чата (PRI-196)"
```

---

### Task 9: Вайринг лимитов в провайдеры + докстринги MCP + финальный прогон

**Files:**
- Modify: `reviewer/tasks/boards/__init__.py:20-35` (`make_board_provider`)
- Modify: `reviewer/entrypoints/mcp_server.py:77-82` (`index_task` docstring), `:128-134` (`get_task` docstring)
- Test: весь набор + ruff

**Interfaces:**
- Consumes: `Settings.task_attachment_*` (Task 1); board-конструкторы с attachment-параметрами (Tasks 7, 8).
- Produces: провайдеры получают реальные лимиты из настроек.

- [ ] **Step 1: Прокинуть настройки в конструкторы провайдеров**

В `reviewer/tasks/boards/__init__.py`, в `make_board_provider` (`:20-35`), передать лимиты обоим конструкторам:
```python
    if type_ == "yougile":
        from reviewer.tasks.boards.yougile import YougileBoard
        return YougileBoard(
            api_key=api_key,
            api_base=api_base,
            key_pattern=key_pattern,
            url_template=settings.task_board_url_template,
            attachment_max_bytes=settings.task_attachment_max_bytes,
            attachment_timeout=settings.task_attachment_timeout,
            attachment_store_chars=settings.task_attachment_store_chars,
        )
    if type_ == "youtrack":
        from reviewer.tasks.boards.youtrack import YouTrackBoard
        return YouTrackBoard(
            token=api_key,
            base_url=api_base,
            key_pattern=key_pattern,
            attachment_max_bytes=settings.task_attachment_max_bytes,
            attachment_timeout=settings.task_attachment_timeout,
            attachment_store_chars=settings.task_attachment_store_chars,
        )
```

- [ ] **Step 2: Обновить докстринги MCP-тулов (схема не меняется — тулы уже `dict`)**

В `reviewer/entrypoints/mcp_server.py`:
- В докстринг `index_task` (`:78-81`) добавить строку: `TaskBrief может содержать поле attachments: [{name, mime_type, size, content_text}].`
- В докстринг `get_task` (`:128-133`) дополнить перечень возвращаемых полей: `..., url, criteria, attachments}.`

- [ ] **Step 3: Проверить, что guard-тесты скилов не сломаны**

Run: `.venv/bin/pytest tests/skills/ -v`
Expected: PASS (изменения докстрингов не затрагивают include-маркеры/структуру).

- [ ] **Step 4: Полный прогон unit-тестов + линт**

Run: `.venv/bin/pytest -q`
Expected: PASS (все unit; integration исключены по умолчанию).
Run: `.venv/bin/ruff check .`
Expected: чисто по затронутым файлам (если на main есть посторонние замечания — не гнаться за repo-wide clean, см. практику проекта).

- [ ] **Step 5: Integration-прогон при поднятой инфраструктуре (если доступна)**

```bash
docker compose up -d            # ParadeDB :5433 + Neo4j :7687
.venv/bin/pytest -m integration -k "tasks or attachments" -v
```
Expected: PASS, либо явно отметить в отчёте, что БД не поднималась и integration пропущены.

- [ ] **Step 6: Коммит**

```bash
git add reviewer/tasks/boards/__init__.py reviewer/entrypoints/mcp_server.py
git commit -m "feat(tasks): вайринг лимитов вложений в провайдеры и докстринги MCP (PRI-196)"
```

---

## Самопроверка плана (выполнена при написании)

**Покрытие спеки:**
- Форматы md/txt/docx/pdf → Task 2 (`extract_text`).
- Лимиты (max_bytes/timeout) + fail-soft → Task 3 (`download`/`fetch_attachment`), Task 1 (Settings).
- Два потолка (jsonb store_chars / embed_chars) → Task 3 (store_chars в fetch), Task 5 (embed_chars в build_task_text).
- Сквозное поле через 6 слоёв → Tasks 4 (RawTask+normalize), 5 (store+schema), 6 (service), 7/8 (boards).
- YouTrack `_FIELDS`+скачивание origin+url+sign → Task 7.
- YouGile задача+чат best-effort → Task 8 (+discovery).
- get_task возвращает attachments → Task 6.
- build_task_text включает усечённый текст → Task 5.
- Тесты normalize с fixtures + round-trip → Tasks 4, 5, 7, 8.
- Зависимости python-docx/pypdf → Task 1.
- Вайринг настроек → Tasks 6 (service), 9 (providers).
- MCP докстринги → Task 9.
- Граница watermark — документирована в спеке (поведение системы не меняется; кода не требует).

**Сквозные типы согласованы:** `extract_text(name, mime, data)`, `download(client, url, *, timeout, max_bytes)`, `fetch_attachment(client, *, name, mime, size, url, timeout, max_bytes, store_chars) -> {name, mime_type, size, content_text}`, `build_task_text(..., attachments=None, *, embed_chars=8000)`, `TaskRow.attachments`, `normalize_*(..., attachments=None)`, `TaskService(..., attachment_embed_chars=...)` — имена совпадают между задачами-производителями и потребителями.

**Плейсхолдеры:** единственная намеренная незакрытая точка — реальные REST-пути/форма файлов YouGile (Task 8), вынесена в явный Discovery-шаг с defensive-кодом и fake-моделируемыми тестами; логика тестов конкретна.
