# reviewer init: новые поля + связка с configure-review — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Расширить wizard `reviewer init` новыми группами (GitLab VCS, веб-админка) и полем YOUGILE_API_BASE; синхронизировать ENV_TEMPLATE с `.env.example`; добавить в configure-review проверку `.env` с предложением `reviewer init`.

**Architecture:** Изменения только в `reviewer/install.py` (WIZARD_GROUPS +2 группы, _GROUP_HEADERS +2, ENV_TEMPLATE замена), `plugin/skills/configure-review/SKILL.md` (+шаг 1.5), тестах. Никаких новых файлов или CLI-команд. configure-review остаётся standalone — проверяет .env парсингом KEY=VALUE.

**Tech Stack:** Python, click (CLI), pytest. Скилл — markdown.

**Spec:** `docs/superpowers/specs/2026-06-24-reviewer-init-new-fields-design.md`
**Task:** PRI-169

---

## File Structure

| Файл | Роль | Действие |
|------|------|----------|
| `reviewer/install.py` | Wizard-группы, заголовки, шаблон .env, рендер | Расширить WIZARD_GROUPS, _GROUP_HEADERS, заменить ENV_TEMPLATE |
| `plugin/skills/configure-review/SKILL.md` | Инструкции скилла configure-review | Добавить шаг 1.5 |
| `tests/test_install_wizard.py` (root) | Проверка состава ключей wizard | Добавить ассерты на новые группы |
| `tests/install/test_install_wizard.py` | Unit-тесты wizard | Обновить фикстуры, добавить тесты на новые поля |

---

### Task 1: WIZARD_GROUPS — группа «GitLab VCS»

**Files:**
- Modify: `reviewer/install.py:82-180`

- [ ] **Step 1: Добавить группу «GitLab VCS» в WIZARD_GROUPS**

Вставить после группы «Мульти-репо / ветки» (после строки 135), перед «Доска задач» (строка 136):

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

- [ ] **Step 2: Проверить, что группа на месте**

```bash
python -c "from reviewer.install import WIZARD_GROUPS; print([g.title for g in WIZARD_GROUPS])"
```

Expected: `['Обязательные', 'Хранилища (Postgres / Neo4j)', 'Мульти-репо / ветки', 'GitLab VCS', 'Доска задач']`

- [ ] **Step 3: Commit**

```bash
git add reviewer/install.py
git commit -m "feat(install): добавить группу GitLab VCS в WIZARD_GROUPS"
```

---

### Task 2: WIZARD_GROUPS — группа «Веб-админка»

**Files:**
- Modify: `reviewer/install.py:82-180`

- [ ] **Step 1: Добавить группу «Веб-админка» в WIZARD_GROUPS**

Вставить последней (после группы «Доска задач», в конец списка):

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

- [ ] **Step 2: Проверить порядок групп**

```bash
python -c "from reviewer.install import WIZARD_GROUPS; assert [g.title for g in WIZARD_GROUPS] == ['Обязательные', 'Хранилища (Postgres / Neo4j)', 'Мульти-репо / ветки', 'GitLab VCS', 'Доска задач', 'Веб-админка']; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add reviewer/install.py
git commit -m "feat(install): добавить группу Веб-админка в WIZARD_GROUPS"
```

---

### Task 3: WIZARD_GROUPS — поле YOUGILE_API_BASE в «Доска задач»

**Files:**
- Modify: `reviewer/install.py:136-179` (группа «Доска задач»)

- [ ] **Step 1: Добавить YOUGILE_API_BASE после YOUGILE_API_KEY**

В группе «Доска задач» после поля `YOUGILE_API_KEY` (строки 160-165), перед `YOUTRACK_TOKEN` (строка 166), вставить:

```python
            EnvField(
                key="YOUGILE_API_BASE",
                prompt_text="YOUGILE_API_BASE (base URL, пусто=дефолт)",
                default="",
            ),
```

- [ ] **Step 2: Проверить общее число полей**

```bash
python -c "from reviewer.install import WIZARD_GROUPS; total = sum(len(g.fields) for g in WIZARD_GROUPS); print(total)"
```

Expected: `21` (2 обязательных + 4 хранилища + 2 мульти-репо + 3 GitLab VCS + 8 доска задач + 2 веб-админка = 21).

```bash
python -c "from reviewer.install import WIZARD_GROUPS; total = sum(len(g.fields) for g in WIZARD_GROUPS); assert total == 21, total; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add reviewer/install.py
git commit -m "feat(install): добавить YOUGILE_API_BASE в группу Доска задач"
```

---

### Task 4: _GROUP_HEADERS — заголовки для новых групп

**Files:**
- Modify: `reviewer/install.py:198-209`

- [ ] **Step 1: Добавить заголовки для GitLab VCS и Веб-админка**

В словарь `_GROUP_HEADERS` добавить:

```python
_GROUP_HEADERS: dict[str, str] = {
    "Обязательные": "# --- Voyage / GitHub ---",
    "Хранилища (Postgres / Neo4j)": "# --- Postgres (ParadeDB :5433) / Neo4j (:7687) ---",
    "Мульти-репо / ветки": "# --- Мульти-репо / ветки (опционально) ---",
    "GitLab VCS": (
        "# --- GitLab VCS (опционально; multi-platform: GitLab MR review) ---\n"
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

- [ ] **Step 2: Проверить render_env с новыми заголовками**

```bash
python -c "
from reviewer.install import WIZARD_GROUPS, render_env
values = {f.key: f.default for g in WIZARD_GROUPS for f in g.fields}
result = render_env(values, extra={})
assert 'GitLab VCS' in result
assert 'Веб-админка' in result
assert 'GITLAB_TOKEN=' in result
assert 'WEB_ADMIN_USER=' in result
print('OK')
"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add reviewer/install.py
git commit -m "feat(install): добавить _GROUP_HEADERS для GitLab VCS и Веб-админка"
```

---

### Task 5: ENV_TEMPLATE — синхронизировать с .env.example

**Files:**
- Modify: `reviewer/install.py:42-64`

- [ ] **Step 1: Заменить ENV_TEMPLATE на полную версию**

Заменить текущий `ENV_TEMPLATE` (строки 42-64) на:

```python
ENV_TEMPLATE = """\
# rag_for_git — конфигурация. Обязателен только VOYAGE_API_KEY; GITHUB_TOKEN
# нужен для ревью PR. Остальное имеет дефолты в reviewer/config/settings.py.

# --- Voyage (эмбеддинги + реранкер) — обязательно ---
VOYAGE_API_KEY=

# --- GitHub (PAT: Pull requests read/write, Contents read) ---
GITHUB_TOKEN=

# --- Postgres (ParadeDB :5433) / Neo4j (:7687) — дефолты docker-compose ---
PG_DSN=postgresql://reviewer:reviewer@localhost:5433/reviewer
NEO4J_URI=neo4j://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=reviewerpass

# --- Граф кода: auto|scip|treesitter ---
GRAPH_BACKEND=auto

# --- Мульти-репо / ветки (опционально) ---
DEFAULT_REPO=
REVIEW_BRANCHES=main,master

# --- GitLab VCS (опционально; multi-platform: GitLab MR review) ---
VCS_PROVIDER=github
GITLAB_TOKEN=
GITLAB_URL=https://gitlab.com

# --- Доска задач (опционально) ---
TASK_BOARD_TYPE=
TASK_BOARD_MCP=
TASK_BOARD_KEY_PATTERN=
TASK_BOARD_URL_TEMPLATE=
YOUGILE_API_KEY=
YOUGILE_API_BASE=
YOUTRACK_TOKEN=
YOUTRACK_BASE_URL=

# --- Веб-админка наблюдаемости (опционально) ---
WEB_ADMIN_USER=
WEB_ADMIN_PASSWORD=
"""
```

- [ ] **Step 2: Проверить, что ENV_TEMPLATE содержит все поля из WIZARD_GROUPS**

```bash
python -c "
from reviewer.install import WIZARD_GROUPS, ENV_TEMPLATE
for g in WIZARD_GROUPS:
    for f in g.fields:
        assert f.key + '=' in ENV_TEMPLATE, f'MISSING: {f.key}'
print('OK')
"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add reviewer/install.py
git commit -m "feat(install): синхронизировать ENV_TEMPLATE с .env.example"
```

---

### Task 6: Тесты — test_install_wizard.py (root)

**Files:**
- Modify: `tests/test_install_wizard.py`

- [ ] **Step 1: Добавить тесты на новые группы**

Заменить содержимое файла:

```python
"""Тесты wizard-групп installer — проверяют набор ключей в WIZARD_GROUPS."""
from reviewer.install import WIZARD_GROUPS


def _keys():
    return {f.key for g in WIZARD_GROUPS for f in g.fields}


def test_wizard_has_per_type_board_creds():
    keys = _keys()
    assert "YOUGILE_API_KEY" in keys
    assert "YOUTRACK_TOKEN" in keys
    assert "YOUTRACK_BASE_URL" in keys


def test_wizard_keeps_board_selectors():
    keys = _keys()
    assert "TASK_BOARD_TYPE" in keys
    assert "TASK_BOARD_KEY_PATTERN" in keys


def test_wizard_has_gitlab_vcs_fields():
    keys = _keys()
    assert "GITLAB_TOKEN" in keys
    assert "GITLAB_URL" in keys
    assert "VCS_PROVIDER" in keys


def test_wizard_has_web_admin_fields():
    keys = _keys()
    assert "WEB_ADMIN_USER" in keys
    assert "WEB_ADMIN_PASSWORD" in keys


def test_wizard_has_yougile_api_base():
    keys = _keys()
    assert "YOUGILE_API_BASE" in keys


def test_wizard_total_field_count():
    total = sum(len(g.fields) for g in WIZARD_GROUPS)
    assert total == 21, f"Expected 21 fields, got {total}"
```

- [ ] **Step 2: Запустить тесты**

```bash
pytest tests/test_install_wizard.py -v
```

Expected: all 6 tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_install_wizard.py
git commit -m "test(install): ключи GitLab, веб-админки и YOUGILE_API_BASE в wizard"
```

---

### Task 7: Тесты — обновить test_install_wizard.py (tests/install/)

**Files:**
- Modify: `tests/install/test_install_wizard.py`

- [ ] **Step 1: Обновить test_render_env_contains_wizard_keys — добавить новые поля в values**

Вставить недостающие поля в values-словарь (строки 34-47). Текущий код:

```python
    values = {
        "VOYAGE_API_KEY": "sk-test",
        "GITHUB_TOKEN": "",
        "PG_DSN": "postgresql://reviewer:reviewer@localhost:5433/reviewer",
        "NEO4J_URI": "neo4j://localhost:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "reviewerpass",
        "DEFAULT_REPO": "",
        "REVIEW_BRANCHES": "main,master",
        "TASK_BOARD_TYPE": "",
        "TASK_BOARD_MCP": "",
        "TASK_BOARD_KEY_PATTERN": "",
        "TASK_BOARD_URL_TEMPLATE": "",
    }
```

Дополнить значениями для новых полей:

```python
    values = {
        "VOYAGE_API_KEY": "sk-test",
        "GITHUB_TOKEN": "",
        "PG_DSN": "postgresql://reviewer:reviewer@localhost:5433/reviewer",
        "NEO4J_URI": "neo4j://localhost:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "reviewerpass",
        "DEFAULT_REPO": "",
        "REVIEW_BRANCHES": "main,master",
        "VCS_PROVIDER": "github",
        "GITLAB_TOKEN": "",
        "GITLAB_URL": "https://gitlab.com",
        "TASK_BOARD_TYPE": "",
        "TASK_BOARD_MCP": "",
        "TASK_BOARD_KEY_PATTERN": "",
        "TASK_BOARD_URL_TEMPLATE": "",
        "YOUGILE_API_KEY": "",
        "YOUGILE_API_BASE": "",
        "YOUTRACK_TOKEN": "",
        "YOUTRACK_BASE_URL": "",
        "WEB_ADMIN_USER": "",
        "WEB_ADMIN_PASSWORD": "",
    }
```

Аналогично обновить values в `test_render_env_extra_keys_preserved` (строки 53-67) — добавить туда же недостающие поля.

- [ ] **Step 2: Заменить `{f.key: f.default for g in inst.WIZARD_GROUPS for f in g.fields}` на полный словарь в test_render_env_no_extra_no_extra_block и test_render_env_includes_board_api_key_and_hint**

Вместо генерации из WIZARD_GROUPS (которая не даёт читаемых значений) — оставить как есть. Эти тесты используют генерацию `.default` и не требуют изменений.

- [ ] **Step 3: Запустить тесты**

```bash
pytest tests/install/test_install_wizard.py -v
```

Expected: all 10 tests PASS

- [ ] **Step 4: Добавить тест на render_env с GitLab и веб-админкой**

Добавить в конец файла:

```python
def test_render_env_includes_gitlab_and_web_admin():
    values = {f.key: f.default for g in inst.WIZARD_GROUPS for f in g.fields}
    values["GITLAB_TOKEN"] = "glpat-secret"
    values["WEB_ADMIN_PASSWORD"] = "admin-secret"
    result = inst.render_env(values, extra={})
    assert "GITLAB_TOKEN=glpat-secret" in result
    assert "GITLAB_URL=https://gitlab.com" in result
    assert "VCS_PROVIDER=github" in result
    assert "WEB_ADMIN_USER=" in result
    assert "WEB_ADMIN_PASSWORD=admin-secret" in result
    assert "YOUGILE_API_BASE=" in result
```

- [ ] **Step 5: Запустить новый тест**

```bash
pytest tests/install/test_install_wizard.py::test_render_env_includes_gitlab_and_web_admin -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/install/test_install_wizard.py
git commit -m "test(install): обновить тесты wizard под новые группы GitLab и веб-админка"
```

---

### Task 8: configure-review SKILL.md — шаг 1.5

**Files:**
- Modify: `plugin/skills/configure-review/SKILL.md:40-50`

- [ ] **Step 1: Вставить шаг 1.5 после Preflight (шаг 1), перед Scan (шаг 2)**

После строки:

```
   repo: `git -C <path> rev-parse --git-dir`. Not a repo → tell the user (in Russian) and stop. No
   database or reviewer MCP is required.
```

Вставить:

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

- [ ] **Step 2: Обновить нумерацию шагов**

Шаг 2 (Scan) → остаётся 2. Все последующие шаги без изменений.

- [ ] **Step 3: Проверить файл на валидность YAML frontmatter**

```bash
python -c "
import re
with open('plugin/skills/configure-review/SKILL.md') as f:
    content = f.read()
# check frontmatter
assert content.startswith('---')
parts = content.split('---', 2)
assert len(parts) >= 3, 'Invalid frontmatter'
print('OK')
"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add plugin/skills/configure-review/SKILL.md
git commit -m "feat(skills): configure-review — шаг 1.5 проверка .env + предложение reviewer init"
```

---

### Task 9: Финальная проверка — прогон всех тестов

**Files:** none (verification only)

- [ ] **Step 1: Запустить все тесты wizard**

```bash
pytest tests/test_install_wizard.py tests/install/test_install_wizard.py -v
```

Expected: all tests PASS (6 + 11 = 17 tests)

- [ ] **Step 2: Запустить reviewer init --yes в CI-режиме и проверить результат**

```bash
export TMP_ENV=$(mktemp)
python -m reviewer.entrypoints.cli init --yes --path "$TMP_ENV"
cat "$TMP_ENV" | head -30
rm "$TMP_ENV"
```

Expected: файл содержит все 6 секций (Voyage/GitHub, Postgres/Neo4j, Мульти-репо, GitLab VCS, Доска задач, Веб-админка), 21 ключ.

- [ ] **Step 3: Проверить, что существующие тесты не сломаны**

```bash
pytest tests/ -x -q --tb=short
```

Expected: no regressions.

- [ ] **Step 4: Commit (если были правки)**

```bash
git add -u
git commit -m "chore: финальные правки после прогона тестов"
```
