# reviewer init wizard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Превратить `reviewer init` в интерактивный пошаговый wizard, который читает текущий `.env`, запрашивает ключевые значения с дефолтами и поддерживает CI-режим (`--yes`).

**Architecture:** Логика wizard (структуры данных, чтение/запись `.env`, интерактивные prompt'ы) размещается в `reviewer/install.py` рядом с уже существующими утилитами (`ENV_TEMPLATE`, `default_env_path`). Команда `init` в `cli.py` остаётся тонкой обёрткой (~25 строк). Data-driven подход: группы и поля описаны как датаклассы — добавить новое поле = одна строка в `WIZARD_GROUPS`.

**Tech Stack:** Python 3.11+, Click (уже в зависимостях), dataclasses (stdlib), pytest + click.testing.CliRunner

## Global Constraints

- Python 3.11–3.13; никаких новых runtime-зависимостей (Click уже есть)
- Комментарии, имена, сообщения — на русском (стиль проекта)
- Коммиты — Conventional Commits на русском, без self-attribution
- Тесты: unit, без внешних сервисов; лежат в `tests/install/`
- `ruff check .` (line-length 100, target py311) должен проходить

---

## File Map

| Файл | Действие | Ответственность |
|---|---|---|
| `reviewer/install.py` | Modify | Добавить `EnvField`, `EnvGroup`, `WIZARD_GROUPS`, `read_env`, `prompt_groups`, `render_env` |
| `reviewer/entrypoints/cli.py` | Modify | Переписать команду `init` (строки 354–372): убрать `--force`, добавить `--yes` |
| `tests/install/test_install_wizard.py` | Create | Unit-тесты wizard-функций и интеграционный тест `init --yes` |

---

## Task 1: Структуры данных + `read_env` + `WIZARD_GROUPS`

**Files:**
- Modify: `reviewer/install.py` (после строки 21 — блок импортов; после `ENV_TEMPLATE` на строке 63)
- Create: `tests/install/test_install_wizard.py`

**Interfaces:**
- Produces:
  - `EnvField(key: str, prompt_text: str, default: str = "", secret: bool = False, required: bool = False)`
  - `EnvGroup(title: str, fields: list[EnvField], optional: bool = False)`
  - `WIZARD_GROUPS: list[EnvGroup]`
  - `read_env(path: Path) -> dict[str, str]`

- [ ] **Шаг 1: Написать падающие тесты для `read_env`**

Создать `tests/install/test_install_wizard.py`:

```python
from pathlib import Path

import pytest

from reviewer import install as inst


def test_read_env_parses_key_value(tmp_path):
    f = tmp_path / ".env"
    f.write_text("VOYAGE_API_KEY=sk-abc\nGITHUB_TOKEN=ghp-xyz\n", encoding="utf-8")
    result = inst.read_env(f)
    assert result == {"VOYAGE_API_KEY": "sk-abc", "GITHUB_TOKEN": "ghp-xyz"}


def test_read_env_skips_comments_and_empty(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# комментарий\n\nFOO=bar\n", encoding="utf-8")
    result = inst.read_env(f)
    assert result == {"FOO": "bar"}
    assert len(result) == 1


def test_read_env_missing_file(tmp_path):
    result = inst.read_env(tmp_path / "nonexistent.env")
    assert result == {}


def test_read_env_value_with_equals(tmp_path):
    f = tmp_path / ".env"
    f.write_text("PG_DSN=postgresql://u:p@localhost:5433/db\n", encoding="utf-8")
    result = inst.read_env(f)
    assert result["PG_DSN"] == "postgresql://u:p@localhost:5433/db"
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

```bash
.venv/bin/pytest tests/install/test_install_wizard.py -v
```

Ожидаемо: `AttributeError: module 'reviewer.install' has no attribute 'read_env'`

- [ ] **Шаг 3: Добавить датаклассы и `read_env` в `reviewer/install.py`**

После блока импортов (после строки 21, перед `PACKAGE = ...`), добавить:

```python
from dataclasses import dataclass, field as _field
```

Заменить строку `from dataclasses import dataclass` (она уже есть на строке 19) на:

```python
from dataclasses import dataclass, field as _field
```

После `ENV_TEMPLATE` (после строки 63), перед `def default_env_path()`, добавить:

```python
@dataclass
class EnvField:
    key: str
    prompt_text: str
    default: str = ""
    secret: bool = False
    required: bool = False


@dataclass
class EnvGroup:
    title: str
    fields: list[EnvField] = _field(default_factory=list)
    optional: bool = False


WIZARD_GROUPS: list[EnvGroup] = [
    EnvGroup(
        title="Обязательные",
        optional=False,
        fields=[
            EnvField(
                key="VOYAGE_API_KEY",
                prompt_text="VOYAGE_API_KEY (эмбеддинги + реранкер)",
                secret=True,
                required=True,
            ),
            EnvField(
                key="GITHUB_TOKEN",
                prompt_text="GITHUB_TOKEN (PAT: Pull requests read/write, Contents read)",
                secret=True,
            ),
        ],
    ),
    EnvGroup(
        title="Хранилища (Postgres / Neo4j)",
        optional=True,
        fields=[
            EnvField(
                key="PG_DSN",
                prompt_text="PG_DSN",
                default="postgresql://reviewer:reviewer@localhost:5433/reviewer",
            ),
            EnvField(key="NEO4J_URI", prompt_text="NEO4J_URI", default="neo4j://localhost:7687"),
            EnvField(key="NEO4J_USER", prompt_text="NEO4J_USER", default="neo4j"),
            EnvField(
                key="NEO4J_PASSWORD",
                prompt_text="NEO4J_PASSWORD",
                default="reviewerpass",
                secret=True,
            ),
        ],
    ),
    EnvGroup(
        title="Мульти-репо / ветки",
        optional=True,
        fields=[
            EnvField(
                key="DEFAULT_REPO",
                prompt_text="DEFAULT_REPO (owner/name или пусто)",
                default="",
            ),
            EnvField(
                key="REVIEW_BRANCHES",
                prompt_text="REVIEW_BRANCHES (CSV, первая — первичная)",
                default="main,master",
            ),
        ],
    ),
    EnvGroup(
        title="Доска задач",
        optional=True,
        fields=[
            EnvField(
                key="TASK_BOARD_TYPE",
                prompt_text="TASK_BOARD_TYPE (yougile | jira | ...)",
                default="",
            ),
            EnvField(
                key="TASK_BOARD_MCP",
                prompt_text="TASK_BOARD_MCP (имя MCP-сервера доски)",
                default="",
            ),
            EnvField(
                key="TASK_BOARD_KEY_PATTERN",
                prompt_text=r"TASK_BOARD_KEY_PATTERN (напр. [A-Z]+-\d+)",
                default="",
            ),
            EnvField(
                key="TASK_BOARD_URL_TEMPLATE",
                prompt_text="TASK_BOARD_URL_TEMPLATE (напр. https://.../{code})",
                default="",
            ),
        ],
    ),
]


def read_env(path: Path) -> dict[str, str]:
    """Прочитать KEY=VALUE из .env, пропуская комментарии и пустые строки."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result
```

- [ ] **Шаг 4: Убедиться, что тесты проходят**

```bash
.venv/bin/pytest tests/install/test_install_wizard.py -v
```

Ожидаемо: 4 теста PASS

- [ ] **Шаг 5: Проверить линтер**

```bash
.venv/bin/ruff check reviewer/install.py tests/install/test_install_wizard.py
```

Ожидаемо: no errors

- [ ] **Шаг 6: Коммит**

```bash
git add reviewer/install.py tests/install/test_install_wizard.py
git commit -m "feat(install): EnvField/EnvGroup, WIZARD_GROUPS, read_env"
```

---

## Task 2: `render_env`

**Files:**
- Modify: `reviewer/install.py` (добавить функцию и константу после `read_env`)
- Modify: `tests/install/test_install_wizard.py` (добавить тесты)

**Interfaces:**
- Consumes: `WIZARD_GROUPS: list[EnvGroup]`, `EnvField.key`, `EnvField.default`
- Produces:
  - `render_env(values: dict[str, str], extra: dict[str, str]) -> str`

- [ ] **Шаг 1: Написать падающие тесты для `render_env`**

Добавить в `tests/install/test_install_wizard.py`:

```python
def test_render_env_contains_wizard_keys():
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
    result = inst.render_env(values, extra={})
    assert "VOYAGE_API_KEY=sk-test" in result
    assert "PG_DSN=postgresql://reviewer:reviewer@localhost:5433/reviewer" in result


def test_render_env_extra_keys_preserved():
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
    extra = {"REVIEW_MAX_COMMENTS": "30", "REVIEW_HISTORY": "true"}
    result = inst.render_env(values, extra=extra)
    assert "REVIEW_MAX_COMMENTS=30" in result
    assert "REVIEW_HISTORY=true" in result
    assert "Прочие настройки" in result


def test_render_env_no_extra_no_extra_block():
    values = {f.key: f.default for g in inst.WIZARD_GROUPS for f in g.fields}
    result = inst.render_env(values, extra={})
    assert "Прочие настройки" not in result
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

```bash
.venv/bin/pytest tests/install/test_install_wizard.py::test_render_env_contains_wizard_keys -v
```

Ожидаемо: `AttributeError: module 'reviewer.install' has no attribute 'render_env'`

- [ ] **Шаг 3: Добавить `_GROUP_HEADERS` и `render_env` в `reviewer/install.py`**

Добавить после `read_env` (перед `def default_env_path()`):

```python
_GROUP_HEADERS: dict[str, str] = {
    "Обязательные": "# --- Voyage / GitHub ---",
    "Хранилища (Postgres / Neo4j)": "# --- Postgres (ParadeDB :5433) / Neo4j (:7687) ---",
    "Мульти-репо / ветки": "# --- Мульти-репо / ветки (опционально) ---",
    "Доска задач": "# --- Доска задач (опционально) ---",
}


def render_env(values: dict[str, str], extra: dict[str, str]) -> str:
    """Сгенерировать содержимое .env по wizard-группам и прочим (extra) ключам."""
    lines: list[str] = [
        "# rag_for_git — конфигурация (сгенерировано reviewer init)",
        "# Обязательный ключ: VOYAGE_API_KEY; GITHUB_TOKEN нужен для ревью PR.",
        "# Остальные переменные имеют дефолты в reviewer/config/settings.py.",
        "",
    ]
    for group in WIZARD_GROUPS:
        header = _GROUP_HEADERS.get(group.title, f"# --- {group.title} ---")
        lines.append(header)
        for field in group.fields:
            lines.append(f"{field.key}={values.get(field.key, field.default)}")
        lines.append("")

    if extra:
        lines.append("# Прочие настройки")
        for key, value in extra.items():
            lines.append(f"{key}={value}")
        lines.append("")

    return "\n".join(lines)
```

- [ ] **Шаг 4: Убедиться, что тесты проходят**

```bash
.venv/bin/pytest tests/install/test_install_wizard.py -v
```

Ожидаемо: все тесты PASS (4 старых + 3 новых = 7)

- [ ] **Шаг 5: Проверить линтер**

```bash
.venv/bin/ruff check reviewer/install.py tests/install/test_install_wizard.py
```

Ожидаемо: no errors

- [ ] **Шаг 6: Коммит**

```bash
git add reviewer/install.py tests/install/test_install_wizard.py
git commit -m "feat(install): render_env — генерация .env из wizard-групп"
```

---

## Task 3: `prompt_groups`

**Files:**
- Modify: `reviewer/install.py` (добавить функцию после `render_env`)
- Modify: `tests/install/test_install_wizard.py` (добавить тесты)

**Interfaces:**
- Consumes:
  - `WIZARD_GROUPS: list[EnvGroup]`
  - `EnvGroup(title, fields, optional)`
  - `EnvField(key, prompt_text, default, secret, required)`
- Produces:
  - `prompt_groups(groups: list[EnvGroup], current: dict[str, str], yes: bool) -> dict[str, str]`

- [ ] **Шаг 1: Написать падающие тесты**

Добавить в `tests/install/test_install_wizard.py`:

```python
def test_prompt_groups_yes_uses_current_values():
    current = {"VOYAGE_API_KEY": "sk-existing", "GITHUB_TOKEN": "ghp-existing"}
    result = inst.prompt_groups(inst.WIZARD_GROUPS, current=current, yes=True)
    assert result["VOYAGE_API_KEY"] == "sk-existing"
    assert result["GITHUB_TOKEN"] == "ghp-existing"


def test_prompt_groups_yes_uses_field_default_when_no_current():
    result = inst.prompt_groups(inst.WIZARD_GROUPS, current={}, yes=True)
    assert result["PG_DSN"] == "postgresql://reviewer:reviewer@localhost:5433/reviewer"
    assert result["REVIEW_BRANCHES"] == "main,master"
    assert result["VOYAGE_API_KEY"] == ""


def test_prompt_groups_yes_skips_optional_groups():
    # При yes=True опциональные группы сохраняют current или default — не вызывают confirm
    current = {"TASK_BOARD_TYPE": "yougile"}
    result = inst.prompt_groups(inst.WIZARD_GROUPS, current=current, yes=True)
    assert result["TASK_BOARD_TYPE"] == "yougile"
    # Остальные поля доски — пустые (default)
    assert result["TASK_BOARD_MCP"] == ""
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

```bash
.venv/bin/pytest tests/install/test_install_wizard.py::test_prompt_groups_yes_uses_current_values -v
```

Ожидаемо: `AttributeError: module 'reviewer.install' has no attribute 'prompt_groups'`

- [ ] **Шаг 3: Добавить `prompt_groups` в `reviewer/install.py`**

Добавить после `render_env`:

```python
def prompt_groups(
    groups: list[EnvGroup],
    current: dict[str, str],
    yes: bool,
) -> dict[str, str]:
    """Интерактивно запросить значения полей по группам.

    yes=True — CI-режим: без prompt'ов, берём current или field.default.
    Секрет с существующим значением: показываем «уже задан», пустой ввод = оставить.
    """
    import click

    values: dict[str, str] = {}

    for group in groups:
        # Опциональную группу предваряем вопросом (в интерактивном режиме)
        if group.optional:
            if yes:
                # CI: сохраняем текущее или дефолт, не спрашиваем
                for f in group.fields:
                    values[f.key] = current.get(f.key, "") or f.default
                continue
            if not click.confirm(f"\nНастроить {group.title}?", default=False):
                for f in group.fields:
                    values[f.key] = current.get(f.key, "") or f.default
                continue
        elif not yes:
            click.echo(f"\n[{group.title}]")

        for field in group.fields:
            cur = current.get(field.key, "")
            effective_default = cur or field.default

            if yes:
                values[field.key] = effective_default
                continue

            if field.secret:
                if cur:
                    label = f"{field.prompt_text} (уже задан — Enter чтобы оставить)"
                    val = click.prompt(label, default="", hide_input=True, show_default=False)
                    values[field.key] = val if val else cur
                else:
                    val = click.prompt(field.prompt_text, default="", hide_input=True,
                                       show_default=False)
                    values[field.key] = val
            else:
                values[field.key] = click.prompt(field.prompt_text, default=effective_default)

    return values
```

- [ ] **Шаг 4: Убедиться, что тесты проходят**

```bash
.venv/bin/pytest tests/install/test_install_wizard.py -v
```

Ожидаемо: все тесты PASS (7 старых + 3 новых = 10)

- [ ] **Шаг 5: Проверить линтер**

```bash
.venv/bin/ruff check reviewer/install.py
```

Ожидаемо: no errors

- [ ] **Шаг 6: Коммит**

```bash
git add reviewer/install.py tests/install/test_install_wizard.py
git commit -m "feat(install): prompt_groups — интерактивный обход групп wizard"
```

---

## Task 4: Переписать команду `init` + интеграционный тест

**Files:**
- Modify: `reviewer/entrypoints/cli.py` (строки 354–372 — команда `init`)
- Modify: `tests/install/test_install_wizard.py` (добавить CliRunner-тест)

**Interfaces:**
- Consumes (из install.py):
  - `default_env_path() -> Path`
  - `read_env(path: Path) -> dict[str, str]`
  - `WIZARD_GROUPS: list[EnvGroup]`
  - `prompt_groups(groups, current, yes) -> dict[str, str]`
  - `render_env(values, extra) -> str`

- [ ] **Шаг 1: Написать падающий интеграционный тест**

Добавить в `tests/install/test_install_wizard.py`:

```python
from click.testing import CliRunner
from reviewer.entrypoints.cli import cli


def test_init_yes_creates_env_file(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    assert dest.exists()
    content = dest.read_text(encoding="utf-8")
    assert "VOYAGE_API_KEY=" in content
    assert "PG_DSN=" in content


def test_init_yes_preserves_existing_secret(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    dest.write_text("VOYAGE_API_KEY=sk-existing\n", encoding="utf-8")
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    content = dest.read_text(encoding="utf-8")
    assert "VOYAGE_API_KEY=sk-existing" in content


def test_init_yes_preserves_extra_keys(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    dest.write_text("VOYAGE_API_KEY=sk-x\nREVIEW_MAX_COMMENTS=42\n", encoding="utf-8")
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    content = dest.read_text(encoding="utf-8")
    assert "REVIEW_MAX_COMMENTS=42" in content
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

```bash
.venv/bin/pytest tests/install/test_install_wizard.py::test_init_yes_creates_env_file -v
```

Ожидаемо: тест падает (старая `init` не поддерживает `--yes`)

- [ ] **Шаг 3: Переписать команду `init` в `reviewer/entrypoints/cli.py`**

Заменить строки 354–372 (весь блок `@cli.command() ... def init`) на:

```python
@cli.command()
@click.option("--path", "path_opt", default=None,
              help="куда писать .env (по умолчанию ~/.config/rag-reviewer/.env)")
@click.option("--yes", "yes", is_flag=True,
              help="принять все дефолты без интерактива (CI-режим)")
def init(path_opt: str | None, yes: bool) -> None:
    """Интерактивный мастер настройки .env для rag-reviewer."""
    import subprocess
    from pathlib import Path
    from reviewer import install as inst

    dest = Path(path_opt).expanduser() if path_opt else inst.default_env_path()
    dest.parent.mkdir(parents=True, exist_ok=True)

    current = inst.read_env(dest)
    wizard_keys = {f.key for g in inst.WIZARD_GROUPS for f in g.fields}

    if not yes:
        click.echo(f"Настройка rag-reviewer: {dest}")
        click.echo("─" * 52)

    try:
        values = inst.prompt_groups(inst.WIZARD_GROUPS, current=current, yes=yes)
    except click.Abort:
        click.echo("\nОтменено — файл не изменён.")
        return

    extra = {k: v for k, v in current.items() if k not in wizard_keys}
    content = inst.render_env(values, extra)
    dest.write_text(content, encoding="utf-8")
    click.echo(f"\n✓ Записан {dest}")

    if not yes and click.confirm("\nЗапустить reviewer check сейчас?", default=True):
        subprocess.run(["reviewer", "check"], check=False)
    elif not yes:
        click.echo("Запустите: reviewer check")
    else:
        click.echo("Готово. Запустите: reviewer check")
```

- [ ] **Шаг 4: Убедиться, что все тесты проходят**

```bash
.venv/bin/pytest tests/install/test_install_wizard.py -v
```

Ожидаемо: все 13 тестов PASS

- [ ] **Шаг 5: Проверить линтер**

```bash
.venv/bin/ruff check reviewer/entrypoints/cli.py tests/install/test_install_wizard.py
```

Ожидаемо: no errors

- [ ] **Шаг 6: Прогнать весь unit-suite**

```bash
.venv/bin/pytest -q
```

Ожидаемо: все unit-тесты PASS (новые + регрессия)

- [ ] **Шаг 7: Коммит**

```bash
git add reviewer/entrypoints/cli.py tests/install/test_install_wizard.py
git commit -m "feat(cli): reviewer init — интерактивный wizard настройки .env (PRI-122)"
```
