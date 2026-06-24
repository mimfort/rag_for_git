# reviewer init: новые поля + связка с configure-review — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Расширить wizard `reviewer init` группами «GitLab VCS» и «Веб-админка» + полем `YOUGILE_API_BASE`; сделать `ENV_TEMPLATE` полным зеркалом `.env.example` (с каноническими per-type ключами доски); починить board-секцию `.env.example`; добавить в `configure-review` мягкую проверку `.env`.

**Architecture:** Изменения в `reviewer/install.py` (WIZARD_GROUPS +2 группы +1 поле = 21, _GROUP_HEADERS +2, ENV_TEMPLATE — полная замена), `.env.example` (board-секция → per-type), `plugin/skills/configure-review/SKILL.md` (+шаг 1.5), тестах. Никаких новых файлов кода или CLI-команд. `ENV_TEMPLATE` — встроенная эталонная документация (мёртвый код: реальный генератор `.env` — `render_env`); guard-тест держит его в паритете с `.env.example`. configure-review остаётся standalone (парсинг `KEY=VALUE`).

**Tech Stack:** Python, click (CLI), pytest. Скилл — markdown.

**Spec:** `docs/superpowers/specs/2026-06-24-reviewer-init-new-fields-design.md`
**Task:** PRI-169

## Global Constraints

- **Канон имён ключей доски — per-type (форма A):** `YOUGILE_API_KEY` / `YOUGILE_API_BASE` / `YOUTRACK_TOKEN` / `YOUTRACK_BASE_URL`. `TASK_BOARD_API_KEY` / `TASK_BOARD_API_BASE` — legacy-алиас (yougile фолбэчит на них). В `.env.example` и `ENV_TEMPLATE` legacy-ключи — только в комментарии, не активные `KEY=` строки.
- **Не менять сигнатуры** `EnvGroup`, `EnvField`, `read_env`, `render_env`, `prompt_groups`. `_GROUP_HEADERS` только дополняется.
- **Итог wizard — 21 поле** (2 обязательных + 4 хранилища + 2 мульти-репо + 3 GitLab VCS + 8 доска задач + 2 веб-админка).
- **CI-режим (`--yes`):** новые опциональные группы пропускаются (current или default).
- **configure-review остаётся standalone:** проверка `.env` — чистый парсинг `KEY=VALUE`, без reviewer MCP / Postgres / Neo4j.
- **Коммиты:** Conventional Commits на русском, без self-attribution (никаких `Co-Authored-By` / упоминаний Claude). Ветка `dev`.
- **Тесты:** не хардкодить общий счётчик тестов файла (`tests/install/test_install_wizard.py` уже содержит 14). Ассертить только конкретику: наличие ключей, `total == 21`, паритет ENV_TEMPLATE ↔ .env.example.

---

## File Structure

| Файл | Роль | Действие |
|------|------|----------|
| `reviewer/install.py` | Wizard-группы, заголовки, шаблон .env | Расширить WIZARD_GROUPS (+2 группы, +1 поле), _GROUP_HEADERS (+2), заменить ENV_TEMPLATE на полное зеркало |
| `.env.example` | Эталонный пример конфигурации | Board-секция → канонические per-type ключи (+ комментарий про legacy-алиас) |
| `plugin/skills/configure-review/SKILL.md` | Инструкции скилла configure-review | Добавить шаг 1.5 (проверка .env) |
| `tests/test_install_wizard.py` (root) | Состав ключей wizard | Добавить ассерты на новые группы + `total == 21` |
| `tests/install/test_install_wizard.py` | Unit-тесты wizard | Добавить тесты на новые поля render_env + guard паритета ENV_TEMPLATE ↔ .env.example |

**Порядок:** сначала wizard-группы (Task 1–4), затем чиним `.env.example` (Task 5), затем `ENV_TEMPLATE` зеркалит его (Task 6), затем тесты-обёртки и скилл (Task 7–8), затем финальная проверка (Task 9).

---

### Task 1: WIZARD_GROUPS — группа «GitLab VCS»

**Files:**
- Modify: `reviewer/install.py` (WIZARD_GROUPS, после группы «Мульти-репо / ветки», стр. ~135)
- Test: `tests/test_install_wizard.py`

**Interfaces:**
- Consumes: `EnvGroup`, `EnvField` (существующие dataclass'ы, сигнатуры не меняем).
- Produces: в `WIZARD_GROUPS` появляется группа `title="GitLab VCS"` с полями `GITLAB_TOKEN`, `GITLAB_URL`, `VCS_PROVIDER`.

- [ ] **Step 1: Написать падающий тест**

В `tests/test_install_wizard.py` добавить:

```python
def test_wizard_has_gitlab_vcs_fields():
    keys = _keys()
    assert "GITLAB_TOKEN" in keys
    assert "GITLAB_URL" in keys
    assert "VCS_PROVIDER" in keys
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/test_install_wizard.py::test_wizard_has_gitlab_vcs_fields -v`
Expected: FAIL (`assert "GITLAB_TOKEN" in keys`).

- [ ] **Step 3: Добавить группу «GitLab VCS» в WIZARD_GROUPS**

Вставить в `WIZARD_GROUPS` **после** группы «Мульти-репо / ветки» (закрывающая `),` группы DEFAULT_REPO/REVIEW_BRANCHES) и **перед** группой «Доска задач»:

```python
    EnvGroup(
        title="GitLab VCS",
        optional=True,
        fields=[
            EnvField(
                key="GITLAB_TOKEN",
                prompt_text="GITLAB_TOKEN (PAT GitLab: api scope)",
                default="",
                secret=True,
            ),
            EnvField(
                key="GITLAB_URL",
                prompt_text="GITLAB_URL (self-hosted base URL)",
                default="https://gitlab.com",
            ),
            EnvField(
                key="VCS_PROVIDER",
                prompt_text="VCS_PROVIDER (фолбэк-платформа: github)",
                default="github",
            ),
        ],
    ),
```

- [ ] **Step 4: Прогнать — тест проходит**

Run: `.venv/bin/pytest tests/test_install_wizard.py::test_wizard_has_gitlab_vcs_fields -v`
Expected: PASS.

- [ ] **Step 5: Проверить порядок групп**

Run: `.venv/bin/python -c "from reviewer.install import WIZARD_GROUPS; print([g.title for g in WIZARD_GROUPS])"`
Expected: `['Обязательные', 'Хранилища (Postgres / Neo4j)', 'Мульти-репо / ветки', 'GitLab VCS', 'Доска задач']`

- [ ] **Step 6: Commit**

```bash
git add reviewer/install.py tests/test_install_wizard.py
git commit -m "feat(install): добавить группу GitLab VCS в WIZARD_GROUPS"
```

---

### Task 2: WIZARD_GROUPS — группа «Веб-админка»

**Files:**
- Modify: `reviewer/install.py` (WIZARD_GROUPS, в конец списка)
- Test: `tests/test_install_wizard.py`

**Interfaces:**
- Produces: в `WIZARD_GROUPS` последней идёт группа `title="Веб-админка"` с полями `WEB_ADMIN_USER`, `WEB_ADMIN_PASSWORD`.

- [ ] **Step 1: Написать падающий тест**

В `tests/test_install_wizard.py` добавить:

```python
def test_wizard_has_web_admin_fields():
    keys = _keys()
    assert "WEB_ADMIN_USER" in keys
    assert "WEB_ADMIN_PASSWORD" in keys
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/test_install_wizard.py::test_wizard_has_web_admin_fields -v`
Expected: FAIL.

- [ ] **Step 3: Добавить группу «Веб-админка» последней в WIZARD_GROUPS**

Вставить **в конец** списка `WIZARD_GROUPS` (после группы «Доска задач»):

```python
    EnvGroup(
        title="Веб-админка",
        optional=True,
        fields=[
            EnvField(
                key="WEB_ADMIN_USER",
                prompt_text="WEB_ADMIN_USER (basic-auth логин)",
                default="",
            ),
            EnvField(
                key="WEB_ADMIN_PASSWORD",
                prompt_text="WEB_ADMIN_PASSWORD (basic-auth пароль)",
                default="",
                secret=True,
            ),
        ],
    ),
```

- [ ] **Step 4: Прогнать — тест проходит**

Run: `.venv/bin/pytest tests/test_install_wizard.py::test_wizard_has_web_admin_fields -v`
Expected: PASS.

- [ ] **Step 5: Проверить полный порядок групп**

Run:
```bash
.venv/bin/python -c "from reviewer.install import WIZARD_GROUPS; assert [g.title for g in WIZARD_GROUPS] == ['Обязательные', 'Хранилища (Postgres / Neo4j)', 'Мульти-репо / ветки', 'GitLab VCS', 'Доска задач', 'Веб-админка']; print('OK')"
```
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add reviewer/install.py tests/test_install_wizard.py
git commit -m "feat(install): добавить группу Веб-админка в WIZARD_GROUPS"
```

---

### Task 3: WIZARD_GROUPS — поле YOUGILE_API_BASE + счёт полей == 21

**Files:**
- Modify: `reviewer/install.py` (группа «Доска задач», после `YOUGILE_API_KEY`)
- Test: `tests/test_install_wizard.py`

**Interfaces:**
- Produces: в группе «Доска задач» поле `YOUGILE_API_BASE` идёт сразу после `YOUGILE_API_KEY`; суммарно по всем группам — 21 поле.

- [ ] **Step 1: Написать падающие тесты**

В `tests/test_install_wizard.py` добавить:

```python
def test_wizard_has_yougile_api_base():
    assert "YOUGILE_API_BASE" in _keys()


def test_wizard_total_field_count():
    total = sum(len(g.fields) for g in WIZARD_GROUPS)
    assert total == 21, f"Expected 21 fields, got {total}"
```

- [ ] **Step 2: Прогнать — убедиться, что падают**

Run: `.venv/bin/pytest tests/test_install_wizard.py::test_wizard_has_yougile_api_base tests/test_install_wizard.py::test_wizard_total_field_count -v`
Expected: оба FAIL (`YOUGILE_API_BASE` отсутствует; total == 20).

- [ ] **Step 3: Вставить YOUGILE_API_BASE после YOUGILE_API_KEY**

В группе «Доска задач», **сразу после** поля `YOUGILE_API_KEY` (закрывающая `),`) и **перед** `YOUTRACK_TOKEN`, вставить:

```python
            EnvField(
                key="YOUGILE_API_BASE",
                prompt_text="YOUGILE_API_BASE (base URL, пусто=дефолт)",
                default="",
            ),
```

- [ ] **Step 4: Прогнать — тесты проходят**

Run: `.venv/bin/pytest tests/test_install_wizard.py::test_wizard_has_yougile_api_base tests/test_install_wizard.py::test_wizard_total_field_count -v`
Expected: оба PASS.

- [ ] **Step 5: Commit**

```bash
git add reviewer/install.py tests/test_install_wizard.py
git commit -m "feat(install): добавить YOUGILE_API_BASE в группу Доска задач"
```

---

### Task 4: _GROUP_HEADERS — заголовки для новых групп

**Files:**
- Modify: `reviewer/install.py` (`_GROUP_HEADERS`, стр. ~198-209)
- Test: `tests/install/test_install_wizard.py`

**Interfaces:**
- Consumes: `render_env(values, extra)` (читает `_GROUP_HEADERS` по `group.title`).
- Produces: в `_GROUP_HEADERS` появляются ключи `"GitLab VCS"` и `"Веб-админка"`; `render_env` печатает их заголовки.

- [ ] **Step 1: Написать падающий тест**

В `tests/install/test_install_wizard.py` (в конец) добавить. Ассерты на **отличительный текст** кастомного заголовка GitLab (`"автоопределяется из git remote"`) — он отсутствует в дефолтном заголовке `# --- GitLab VCS ---`, поэтому даёт настоящий red→green:

```python
def test_render_env_includes_gitlab_and_web_admin():
    values = {f.key: f.default for g in inst.WIZARD_GROUPS for f in g.fields}
    values["GITLAB_TOKEN"] = "glpat-secret"
    values["WEB_ADMIN_PASSWORD"] = "admin-secret"
    result = inst.render_env(values, extra={})
    # отличительный текст многострочного заголовка GitLab VCS (нет в дефолтном):
    assert "автоопределяется из git remote" in result
    assert "GITLAB_TOKEN=glpat-secret" in result
    assert "GITLAB_URL=https://gitlab.com" in result
    assert "VCS_PROVIDER=github" in result
    assert "WEB_ADMIN_USER=" in result
    assert "WEB_ADMIN_PASSWORD=admin-secret" in result
    assert "YOUGILE_API_BASE=" in result
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py::test_render_env_includes_gitlab_and_web_admin -v`
Expected: FAIL (`assert "автоопределяется из git remote" in result` — кастомного заголовка GitLab VCS ещё нет в `_GROUP_HEADERS`, `render_env` подставляет дефолт `# --- GitLab VCS ---` без этой фразы).

- [ ] **Step 3: Заменить `_GROUP_HEADERS` на расширенную версию**

```python
_GROUP_HEADERS: dict[str, str] = {
    "Обязательные": "# --- Voyage / GitHub ---",
    "Хранилища (Postgres / Neo4j)": "# --- Postgres (ParadeDB :5433) / Neo4j (:7687) ---",
    "Мульти-репо / ветки": "# --- Мульти-репо / ветки (опционально) ---",
    "GitLab VCS": (
        "# --- GitLab VCS (опционально; multi-platform: ревью GitLab MR) ---\n"
        "# VCS_PROVIDER — фолбэк, когда репо не индексирован. Тип VCS\n"
        "# автоопределяется из git remote при `reviewer index`."
    ),
    "Доска задач": (
        "# --- Доска задач (опционально; server-side sync_board, связка ключей) ---\n"
        "# Тип доски репо выбирается в его .review.yml (task_board.type); ключи —\n"
        "# здесь, под каждую доску свой. YOUGILE_API_KEY: конфигуратор yougile (Ctrl+~)\n"
        "# → API. YOUTRACK_TOKEN: permanent token, YOUTRACK_BASE_URL инстанс-специфичен.\n"
        "# TASK_BOARD_API_KEY/BASE — legacy-алиас для yougile (обратная совместимость)."
    ),
    "Веб-админка": "# --- Веб-админка наблюдаемости (опционально; пусто = без аутентификации) ---",
}
```

- [ ] **Step 4: Прогнать — тест проходит**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py::test_render_env_includes_gitlab_and_web_admin -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add reviewer/install.py tests/install/test_install_wizard.py
git commit -m "feat(install): _GROUP_HEADERS для GitLab VCS и Веб-админка"
```

---

### Task 5: `.env.example` — board-секция на канонических per-type ключах

**Files:**
- Modify: `.env.example` (секция «Доска задач», текущие строки ~75-94)

**Interfaces:**
- Produces: в `.env.example` активные `KEY=` строки board-секции — `TASK_BOARD_TYPE`, `TASK_BOARD_MCP`, `TASK_BOARD_KEY_PATTERN`, `TASK_BOARD_URL_TEMPLATE`, `YOUGILE_API_KEY`, `YOUGILE_API_BASE`, `YOUTRACK_TOKEN`, `YOUTRACK_BASE_URL`. `TASK_BOARD_API_KEY`/`TASK_BOARD_API_BASE` — только в комментарии. Итог: 41 активный ключ в файле (нужно для паритета в Task 6).

- [ ] **Step 1: Заменить board-секцию `.env.example`**

Заменить блок (от `# Доска задач (опционально) — ГЛОБАЛЬНЫЙ дефолт деплоя` до строки `TASK_BOARD_API_BASE=...` включительно) на:

```
# ============================================================================
# Доска задач (опционально) — ГЛОБАЛЬНЫЙ дефолт деплоя
# ----------------------------------------------------------------------------
# Подключение к доске одинаково для всех репозиториев команды/орга, поэтому
# задаётся ОДИН раз здесь, а не дублируется в .review.yml каждого репо.
# Используется скилами solve-task/sync-tasks (через MCP-тул get_board_config)
# и review-pr. Per-repo .review.yml `task_board` переопределяет; пустой
# `task_board:` в .review.yml явно выключает доску для конкретного репо.
# Селекторы (TYPE/MCP/KEY_PATTERN/URL_TEMPLATE) пусты = доска не настроена.
# ============================================================================
TASK_BOARD_TYPE=                   # yougile | youtrack | ...
TASK_BOARD_MCP=                    # имя MCP-сервера доски (инструменты mcp__<mcp>__*), напр. yougile
TASK_BOARD_KEY_PATTERN=            # регэксп ключа задачи, напр. [A-Z]+-\d+
TASK_BOARD_URL_TEMPLATE=           # шаблон ссылки на задачу, напр. https://ru.yougile.com/team/<id>/#{code}
# per-type креды REST-доски (форма A) — только здесь, в env reviewer-mcp (клиентам
# не утекают). Server-side болк-синк (sync_board) ходит на доску по REST сам.
# yougile: ключ из конфигуратора Ctrl+~ → API. youtrack: permanent token (perm:...).
# Legacy-алиас (обратная совместимость): TASK_BOARD_API_KEY / TASK_BOARD_API_BASE —
# yougile фолбэчит на них, если YOUGILE_API_KEY / YOUGILE_API_BASE пусты.
YOUGILE_API_KEY=                   # ключ REST yougile (приоритет над legacy TASK_BOARD_API_KEY)
YOUGILE_API_BASE=                  # base URL yougile; пусто → дефолт https://yougile.com/api-v2
YOUTRACK_TOKEN=                    # permanent token youtrack (Profile → Account Security)
YOUTRACK_BASE_URL=                 # base URL youtrack API, напр. https://company.youtrack.cloud/api
```

- [ ] **Step 2: Проверить набор активных ключей `.env.example`**

```bash
.venv/bin/python -c "
from reviewer.install import read_env
from pathlib import Path
keys = set(read_env(Path('.env.example')))
assert {'YOUGILE_API_KEY','YOUGILE_API_BASE','YOUTRACK_TOKEN','YOUTRACK_BASE_URL'} <= keys, 'нет per-type ключей'
assert 'TASK_BOARD_API_KEY' not in keys, 'legacy TASK_BOARD_API_KEY всё ещё активная строка'
assert 'TASK_BOARD_API_BASE' not in keys, 'legacy TASK_BOARD_API_BASE всё ещё активная строка'
print('OK', len(keys), 'ключей')
"
```
Expected: `OK 41 ключей`

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs(env): board-секция .env.example на канонических per-type ключах"
```

---

### Task 6: ENV_TEMPLATE — полное зеркало `.env.example` + guard-тест паритета

**Files:**
- Modify: `reviewer/install.py` (`ENV_TEMPLATE`, стр. ~42-64)
- Test: `tests/install/test_install_wizard.py`

**Interfaces:**
- Consumes: `read_env(path)` (для парсинга `.env.example` в тесте), `ENV_TEMPLATE` (строка).
- Produces: `ENV_TEMPLATE` содержит ровно тот же набор `KEY=` ключей, что и `.env.example` (41 ключ); все ключи `WIZARD_GROUPS` ⊆ ключей `ENV_TEMPLATE`.

- [ ] **Step 1: Написать падающий guard-тест паритета**

В `tests/install/test_install_wizard.py` добавить (в начало — рядом с импортами — helper, затем тесты в конец файла):

```python
def _keys_from_text(text: str) -> set[str]:
    """Имена KEY из текста .env-вида: пропускаем комментарии и пустые строки."""
    keys = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def test_env_template_mirrors_env_example():
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[2]
    example_keys = _keys_from_text((repo_root / ".env.example").read_text(encoding="utf-8"))
    template_keys = _keys_from_text(inst.ENV_TEMPLATE)
    assert template_keys == example_keys, (
        f"ENV_TEMPLATE и .env.example разошлись:\n"
        f"  только в .env.example: {sorted(example_keys - template_keys)}\n"
        f"  только в ENV_TEMPLATE: {sorted(template_keys - example_keys)}"
    )


def test_env_template_contains_all_wizard_keys():
    template_keys = _keys_from_text(inst.ENV_TEMPLATE)
    wizard_keys = {f.key for g in inst.WIZARD_GROUPS for f in g.fields}
    missing = wizard_keys - template_keys
    assert not missing, f"в ENV_TEMPLATE нет ключей wizard: {sorted(missing)}"
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py::test_env_template_mirrors_env_example tests/install/test_install_wizard.py::test_env_template_contains_all_wizard_keys -v`
Expected: оба FAIL (старый ENV_TEMPLATE содержит лишь ~9 ключей).

- [ ] **Step 3: Заменить `ENV_TEMPLATE` на полное зеркало**

Заменить текущий `ENV_TEMPLATE` (стр. ~42-64) на:

```python
ENV_TEMPLATE = """\
# rag_for_git — конфигурация. Обязателен только VOYAGE_API_KEY; GITHUB_TOKEN
# нужен для ревью PR. Остальное имеет дефолты в reviewer/config/settings.py.
# Полный справочник полей — зеркало .env.example.

# --- Voyage (эмбеддинги + реранкер) — обязательно ---
VOYAGE_API_KEY=
EMBEDDING_MODEL=voyage-code-3
EMBEDDING_DIM=1024
EMBEDDING_BATCH_SIZE=256
RERANK_MODEL=rerank-2.5

# --- GitHub (PAT: Pull requests read/write, Contents read) ---
GITHUB_TOKEN=
GITHUB_RETRY_ATTEMPTS=3
GITHUB_RETRY_BACKOFF_BASE=1.0

# --- multi-platform VCS: тип определяется из git remote при `reviewer index` ---
VCS_PROVIDER=github
GITLAB_TOKEN=
GITLAB_URL=https://gitlab.com

# --- Postgres (ParadeDB :5433) / Neo4j (:7687) — дефолты docker-compose ---
PG_DSN=postgresql://reviewer:reviewer@localhost:5433/reviewer
PG_POOL_MIN_SIZE=1
PG_POOL_MAX_SIZE=4
NEO4J_URI=neo4j://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=reviewerpass

# --- Граф кода: auto|scip|treesitter ---
GRAPH_BACKEND=auto
SUMMARY_CLUSTER_DEPTH=2

# --- Мульти-репо / ветки (опционально) ---
DEFAULT_REPO=
REVIEW_BRANCHES=main,master

# --- Настройка ревью (дефолты; per-repo .review.yml может переопределить) ---
REVIEW_SEVERITY_THRESHOLD=medium
REVIEW_MIN_CONFIDENCE=0.5
REVIEW_MAX_COMMENTS=25
REVIEW_MAX_FILES=50
REVIEW_CATEGORIES=
REVIEW_SUGGESTIONS=apply
REVIEW_OUTPUT_LANGUAGE=ru
REVIEW_SKIP_DRAFTS=true
REVIEW_HISTORY=true
MAX_TOOL_RESULT_CHARS=8000

# --- Доска задач (опционально; per-type креды, legacy-алиас TASK_BOARD_API_KEY/BASE) ---
TASK_BOARD_TYPE=
TASK_BOARD_MCP=
TASK_BOARD_KEY_PATTERN=
TASK_BOARD_URL_TEMPLATE=
YOUGILE_API_KEY=
YOUGILE_API_BASE=
YOUTRACK_TOKEN=
YOUTRACK_BASE_URL=

# --- Веб-админка наблюдаемости (опционально; пусто = без аутентификации) ---
WEB_ADMIN_USER=
WEB_ADMIN_PASSWORD=
"""
```

- [ ] **Step 4: Прогнать — оба теста проходят**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py::test_env_template_mirrors_env_example tests/install/test_install_wizard.py::test_env_template_contains_all_wizard_keys -v`
Expected: оба PASS.

> Если `test_env_template_mirrors_env_example` падает с разницей ключей — привести `ENV_TEMPLATE` к набору `.env.example` (или наоборот): набор активных `KEY=` строк обоих файлов должен совпадать. Это и есть смысл «зеркала».

- [ ] **Step 5: Commit**

```bash
git add reviewer/install.py tests/install/test_install_wizard.py
git commit -m "feat(install): ENV_TEMPLATE — полное зеркало .env.example + guard паритета"
```

---

### Task 7: Тесты — обновить состав root-файла и smoke `reviewer init --yes`

**Files:**
- Modify: `tests/test_install_wizard.py` (root) — убедиться, что есть helper `_keys()` и импорт
- Verify: `tests/install/test_install_wizard.py` (init --yes уже покрыт)

**Interfaces:**
- Consumes: `WIZARD_GROUPS`.

- [ ] **Step 1: Убедиться, что root-файл самодостаточен**

`tests/test_install_wizard.py` должен начинаться с:

```python
"""Тесты wizard-групп installer — проверяют набор ключей в WIZARD_GROUPS."""
from reviewer.install import WIZARD_GROUPS


def _keys():
    return {f.key for g in WIZARD_GROUPS for f in g.fields}
```

…и далее существующие тесты (`test_wizard_has_per_type_board_creds`, `test_wizard_keeps_board_selectors`) + добавленные в Task 1-3 (`test_wizard_has_gitlab_vcs_fields`, `test_wizard_has_web_admin_fields`, `test_wizard_has_yougile_api_base`, `test_wizard_total_field_count`). Helper `_keys()` уже есть в файле — не дублировать.

- [ ] **Step 2: Прогнать весь root-файл**

Run: `.venv/bin/pytest tests/test_install_wizard.py -v`
Expected: все тесты PASS (2 существующих + 4 новых = 6).

- [ ] **Step 3: Прогнать smoke `reviewer init --yes` (уже покрыт в tests/install)**

Run: `.venv/bin/pytest tests/install/test_install_wizard.py -k "init_yes" -v`
Expected: `test_init_yes_creates_env_file`, `test_init_yes_preserves_existing_secret`, `test_init_yes_preserves_extra_keys` — PASS (новые опциональные группы не ломают `--yes`).

- [ ] **Step 4: Commit (если были правки)**

```bash
git add tests/test_install_wizard.py
git commit -m "test(install): состав ключей wizard (GitLab, веб-админка, YOUGILE_API_BASE)"
```

> Если в Step 1 правок не потребовалось (файл уже самодостаточен после Task 1-3) — коммит пропустить.

---

### Task 8: configure-review SKILL.md — шаг 1.5 (проверка .env)

**Files:**
- Modify: `plugin/skills/configure-review/SKILL.md` (после Preflight, шаг 1, стр. ~42-45)

**Interfaces:**
- Standalone: шаг — read-only парсинг `KEY=VALUE`, без reviewer MCP / Postgres / Neo4j.

- [ ] **Step 1: Вставить шаг 1.5 после Preflight (шаг 1), перед Scan (шаг 2)**

После абзаца Preflight (заканчивается на `… No\n   database or reviewer MCP is required.`) вставить:

```

1.5. **Check .env completeness (offer `reviewer init` if needed).**
   Resolve the canonical .env path:
   ```bash
   echo "${REVIEWER_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/rag-reviewer/.env}"
   ```
   (fallback: `~/.config/rag-reviewer/.env`, then `./.env` for dev). Read and parse `KEY=VALUE` lines
   (skip comments and blank lines). If the file doesn't exist — tell the user (Russian):
   > .env не найден по пути `<path>`. Запустить `reviewer init` для первоначальной настройки?

   If the file exists, check critical groups:
   - **GitLab VCS:** `GITLAB_TOKEN` — if empty, warn.
   - **Доска задач:** `YOUGILE_API_KEY` and `YOUTRACK_TOKEN` — if both empty, warn.
   If any are missing → tell the user (Russian):
   > В .env не хватает полей: `<list>`. Запустить `reviewer init` чтобы дополнить?

   User can decline — skill continues normal pipeline. This check is **read-only** (parse
   `KEY=VALUE` lines); no reviewer MCP / Postgres / Neo4j needed. **Do NOT run** `reviewer init`
   automatically — only offer.
```

- [ ] **Step 2: Проверить валидность YAML frontmatter**

```bash
.venv/bin/python -c "
content = open('plugin/skills/configure-review/SKILL.md', encoding='utf-8').read()
assert content.startswith('---')
assert len(content.split('---', 2)) >= 3, 'Invalid frontmatter'
assert '1.5. **Check .env completeness' in content
print('OK')
"
```
Expected: `OK`

- [ ] **Step 3: Проверить guard-тесты скиллов (если есть)**

Run: `.venv/bin/pytest tests/skills/ -q`
Expected: PASS (или «no tests ran», если каталог не покрывает configure-review) — изменение текстовое, schema не трогаем.

- [ ] **Step 4: Commit**

```bash
git add plugin/skills/configure-review/SKILL.md
git commit -m "feat(skills): configure-review — шаг 1.5 проверка .env + предложение reviewer init"
```

---

### Task 9: Финальная проверка

**Files:** none (verification only)

- [ ] **Step 1: Прогнать все тесты wizard**

Run: `.venv/bin/pytest tests/test_install_wizard.py tests/install/test_install_wizard.py -v`
Expected: все PASS (root: 6; install: 14 существующих + `test_render_env_includes_gitlab_and_web_admin` + `test_env_template_mirrors_env_example` + `test_env_template_contains_all_wizard_keys` = 17).

- [ ] **Step 2: Сгенерировать `.env` через CLI и глазами проверить 6 секций**

```bash
TMP_ENV=$(mktemp)
.venv/bin/python -m reviewer.entrypoints.cli init --yes --path "$TMP_ENV"
grep -E '^(VOYAGE_API_KEY|PG_DSN|REVIEW_BRANCHES|GITLAB_TOKEN|YOUGILE_API_BASE|WEB_ADMIN_USER)=' "$TMP_ENV"
rm "$TMP_ENV"
```
Expected: присутствуют `VOYAGE_API_KEY=`, `PG_DSN=…`, `REVIEW_BRANCHES=main,master`, `GITLAB_TOKEN=`, `YOUGILE_API_BASE=`, `WEB_ADMIN_USER=` (все 6 секций, 21 ключ wizard).

- [ ] **Step 3: Прогнать весь unit-набор — нет регрессий**

Run: `.venv/bin/pytest -q`
Expected: no regressions (integration-тесты исключены `addopts`).

- [ ] **Step 4: Линт**

Run: `.venv/bin/ruff check reviewer/install.py tests/test_install_wizard.py tests/install/test_install_wizard.py`
Expected: чисто по затронутым файлам (line-length 100).

- [ ] **Step 5: Commit (если были финальные правки)**

```bash
git add -u
git commit -m "chore: финальные правки reviewer init новые поля"
```

---

## Self-Review notes

- **Spec coverage:** §3 (новые группы) → Task 1-3; §3.3 (YOUGILE_API_BASE) → Task 3; _GROUP_HEADERS → Task 4; §4.2 (.env.example) → Task 5; §4.1 (ENV_TEMPLATE зеркало) → Task 6; §7 (guard паритета, total==21, без хардкода счётчиков) → Task 3/6/7; §5 (configure-review шаг 1.5) → Task 8.
- **Счёт полей 21:** 2+4+2+3+8+2 = 21 (Task 3 ассертит).
- **Паритет ключей:** `.env.example` (Task 5: 41 активный ключ) == `ENV_TEMPLATE` (Task 6: 41 ключ); guard-тест следит.
- **Без хардкода счётчиков тестов:** ассерты только на конкретные ключи и `total==21`; общий счёт тестов файла не фиксируется (Step 1 Task 9 даёт ориентир 17, но это не assert).
