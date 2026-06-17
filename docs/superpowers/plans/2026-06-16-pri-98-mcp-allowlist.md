# PRI-98 — Auto-allowlist reviewer MCP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать так, чтобы тулы `mcp__reviewer__*` работали в Claude Code «из коробки» — через committed `.claude/settings.json` и через `reviewer install`, который мёрджит allow-правило в settings целевого репо.

**Architecture:** Одно anchored-glob правило `mcp__reviewer__*` в `permissions.allow`. `reviewer/install.py` получает функции построения/применения allowlist-плана (по образцу `InstallPlan`/`apply_plan`, с общим `_write_with_backup`). CLI-команда `install` для клиента `claude-code` дополнительно пишет `.claude/settings.json`. Плюс committed settings-файлы, `plugin/GEMINI.md`, фикс докстринга.

**Tech Stack:** Python 3.11, Click CLI, pytest, ruff (line-length 100). Тесты — на фейках/`tmp_path`, без сети/БД.

---

## Контекст для исполнителя (прочитать перед началом)

- `reviewer/install.py` — кроссплатформенная установка MCP-сервера в конфиги клиентов.
  Паттерн: `build_plan()` готовит `InstallPlan(path, content, …)`, `apply_plan()`
  пишет на диск с `.bak`-бэкапом и идемпотентностью (не пишет, если контент совпал).
- `apply_plan` (строки ~242–258) и наш новый `apply_allowlist_plan` должны делить
  общую запись-с-бэкапом — выделяем `_write_with_backup(path, content)`.
- CLI-команда `install` — `reviewer/entrypoints/cli.py:234-322`. Внутри `for c in targets:`
  есть ветка `if dry_run:` (печать + `continue`) и обычная ветка с `apply_plan`.
  Клиент `claude-code` пишет MCP-конфиг в `Path(".mcp.json")` (CWD), скилы НЕ тянет
  (`skills_fn=None`), поэтому сети в тесте не будет.
- Правильная схема Claude Code (сверено с docs): `{"permissions":{"allow":[...]}}`,
  правило `mcp__reviewer__*` разрешает весь сервер. НЕ `allowedTools`.
- Стиль тестов — `tests/install/test_install.py` (фикстура `fake_uvx`, `tmp_path`,
  проверка `plan.content` через `json.loads`). Комментарии/докстринги — на русском.
- 15 тулов сервера: prepare_review, search_code, get_related_symbols, read_file,
  get_definition, find_callers, get_changed_file_diff, index_task, index_tasks_batch,
  purge_orphaned_tasks, search_tasks, get_task_context, get_board_config,
  search_codebase, publish_review.

Запуск тестов: `.venv/bin/pytest tests/install/test_install.py -q`. Линт:
`.venv/bin/ruff check reviewer/install.py tests/install/test_install.py`.

---

## Task 1: allowlist-план в `reviewer/install.py`

**Files:**
- Modify: `reviewer/install.py` (добавить константу, `claude_settings_path`,
  `AllowlistPlan`, `build_allowlist_plan`, `_write_with_backup`, `apply_allowlist_plan`;
  отрефакторить `apply_plan`)
- Test: `tests/install/test_install.py`

- [ ] **Step 1: Написать падающие тесты**

В конец `tests/install/test_install.py` добавить (вверху файла уже есть `import json`,
`import pytest`, `from reviewer import install as inst`; добавить `from pathlib import Path`):

```python
# --------------------------------------------------------------------------- #
# allowlist (permissions.allow в .claude/settings.json)
# --------------------------------------------------------------------------- #
def test_allowlist_plan_creates_file(tmp_path):
    cfg = tmp_path / ".claude" / "settings.json"
    plan = inst.build_allowlist_plan(cfg)
    assert plan.created is True and plan.already is False
    data = json.loads(plan.content)
    assert data["permissions"]["allow"] == ["mcp__reviewer__*"]


def test_allowlist_plan_preserves_existing(tmp_path):
    cfg = tmp_path / ".claude" / "settings.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls:*)"], "deny": ["mcp__evil"]},
        "model": "opus",
    }))
    plan = inst.build_allowlist_plan(cfg)
    data = json.loads(plan.content)
    assert data["model"] == "opus"                                # чужой ключ сохранён
    assert data["permissions"]["deny"] == ["mcp__evil"]           # чужое правило сохранено
    assert data["permissions"]["allow"] == ["Bash(ls:*)", "mcp__reviewer__*"]
    assert plan.already is False


def test_allowlist_plan_idempotent(tmp_path):
    cfg = tmp_path / ".claude" / "settings.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"permissions": {"allow": ["mcp__reviewer__*"]}}))
    plan = inst.build_allowlist_plan(cfg)
    assert plan.already is True
    assert json.loads(plan.content)["permissions"]["allow"].count("mcp__reviewer__*") == 1


def test_apply_allowlist_plan_writes_and_backs_up(tmp_path):
    cfg = tmp_path / ".claude" / "settings.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))
    plan = inst.build_allowlist_plan(cfg)
    backup = inst.apply_allowlist_plan(plan)
    assert backup is not None and backup.exists()
    written = json.loads(cfg.read_text())
    assert "mcp__reviewer__*" in written["permissions"]["allow"]


def test_apply_allowlist_plan_no_write_when_unchanged(tmp_path):
    cfg = tmp_path / ".claude" / "settings.json"
    inst.apply_allowlist_plan(inst.build_allowlist_plan(cfg))     # создаём
    plan2 = inst.build_allowlist_plan(cfg)
    assert plan2.already is True
    assert inst.apply_allowlist_plan(plan2) is None               # без изменений — без .bak
```

- [ ] **Step 2: Запустить — убедиться, что падают**

Run: `.venv/bin/pytest tests/install/test_install.py -q -k allowlist`
Expected: FAIL — `AttributeError: module 'reviewer.install' has no attribute 'build_allowlist_plan'`.

- [ ] **Step 3: Реализовать в `reviewer/install.py`**

Сразу после строки `SERVER_NAME = "reviewer"` (около строки 24) добавить:

```python
# anchored-glob правило, разрешающее ВЕСЬ сервер reviewer в Claude Code
# (permissions.allow). Wildcard покрывает новые тулы автоматически — список
# тулов синхронизировать не нужно.
REVIEWER_PERMISSION_RULE = "mcp__reviewer__*"
```

Добавить функции (рядом с `default_env_path`, до или после — не важно):

```python
def claude_settings_path() -> Path:
    """Проектный settings.json Claude Code (рядом с .mcp.json в корне проекта)."""
    return Path(".claude") / "settings.json"
```

Рядом с `InstallPlan` добавить датакласс и билдер:

```python
@dataclass
class AllowlistPlan:
    path: Path
    content: str          # что будет записано в файл целиком
    created: bool         # файла не было — создаём
    already: bool         # правило уже было в permissions.allow


def build_allowlist_plan(
    path: Path | None = None, rule: str = REVIEWER_PERMISSION_RULE
) -> AllowlistPlan:
    """План мёрджа allow-правила reviewer в permissions.allow Claude Code settings.

    Сохраняет чужие ключи и существующие правила; идемпотентен (already=True,
    если правило уже присутствует). Запись на диск — apply_allowlist_plan.
    """
    path = path or claude_settings_path()
    existed = path.exists()
    raw = path.read_text(encoding="utf-8") if existed else ""
    cfg = json.loads(raw) if raw.strip() else {}
    if not isinstance(cfg, dict):
        raise ValueError(f"{path}: ожидался JSON-объект на верхнем уровне")
    perms = cfg.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
    allow = perms.get("allow")
    if not isinstance(allow, list):
        allow = []
    already = rule in allow
    if not already:
        allow = [*allow, rule]
    perms["allow"] = allow
    cfg["permissions"] = perms
    content = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    return AllowlistPlan(path, content, not existed, already)
```

Заменить тело `apply_plan` (строки ~242–258) — выделить общий хелпер и делегировать:

```python
def _write_with_backup(path: Path, content: str) -> Path | None:
    """Записать content в path; при изменении существующего файла сделать .bak.

    Возвращает путь к .bak (если был бэкап) или None. Ничего не пишет и не
    бэкапит, если содержимое не изменилось.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == content:
            return None
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(existing, encoding="utf-8")
        path.write_text(content, encoding="utf-8")
        return backup
    path.write_text(content, encoding="utf-8")
    return None


def apply_plan(plan: InstallPlan) -> Path | None:
    """Записать MCP-план на диск. Возвращает путь к .bak (если был бэкап)."""
    return _write_with_backup(plan.path, plan.content)


def apply_allowlist_plan(plan: AllowlistPlan) -> Path | None:
    """Записать allowlist-план на диск. Возвращает путь к .bak (если был бэкап)."""
    return _write_with_backup(plan.path, plan.content)
```

- [ ] **Step 4: Запустить — убедиться, что проходят**

Run: `.venv/bin/pytest tests/install/test_install.py -q`
Expected: PASS (новые allowlist-тесты + все старые, включая `test_apply_plan_*`).

- [ ] **Step 5: Линт**

Run: `.venv/bin/ruff check reviewer/install.py tests/install/test_install.py`
Expected: чисто (на этих файлах).

- [ ] **Step 6: Commit**

```bash
git add reviewer/install.py tests/install/test_install.py
git commit -m "feat(install): allowlist-план reviewer MCP (permissions.allow)"
```

---

## Task 2: CLI `install claude-code` пишет allowlist

**Files:**
- Modify: `reviewer/entrypoints/cli.py` (ветка `claude-code` в команде `install`)
- Test: `tests/install/test_install.py`

- [ ] **Step 1: Написать падающий CLI-тест**

Добавить в конец `tests/install/test_install.py`:

```python
def test_cli_install_claude_code_writes_allowlist(monkeypatch):
    from click.testing import CliRunner

    from reviewer.entrypoints.cli import cli

    monkeypatch.setattr(inst.shutil, "which",
                        lambda name: "/fake/bin/uvx" if name == "uvx" else None)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["install", "claude-code"])
        assert result.exit_code == 0, result.output
        assert Path(".mcp.json").exists()
        settings = json.loads(Path(".claude/settings.json").read_text())
        assert "mcp__reviewer__*" in settings["permissions"]["allow"]
        # идемпотентность: повторный запуск не плодит дубли
        result2 = runner.invoke(cli, ["install", "claude-code"])
        assert result2.exit_code == 0, result2.output
        settings2 = json.loads(Path(".claude/settings.json").read_text())
        assert settings2["permissions"]["allow"].count("mcp__reviewer__*") == 1
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `.venv/bin/pytest tests/install/test_install.py::test_cli_install_claude_code_writes_allowlist -q`
Expected: FAIL — `.claude/settings.json` не создаётся (файла нет).

- [ ] **Step 3: Реализовать в `reviewer/entrypoints/cli.py`**

В команде `install`, внутри `for c in targets:`. В ветке `if dry_run:` — перед
печатью скилов добавить печать allowlist для claude-code:

```python
            if c.key == "claude-code":
                al_plan = inst.build_allowlist_plan(plan.path.parent / ".claude" / "settings.json")
                click.echo(f"# {c.label} allowlist → {al_plan.path}")
                click.echo(al_plan.content)
```

В обычной ветке — после блока `click.echo(f"✓ {c.label}: …")` / `if backup:` и до
`if c.note:` (или сразу после него), но ДО `_ensure_skills(c)` — добавить:

```python
        if c.key == "claude-code":
            al_plan = inst.build_allowlist_plan(plan.path.parent / ".claude" / "settings.json")
            al_backup = inst.apply_allowlist_plan(al_plan)
            if al_plan.already:
                al_status = "правило уже есть"
            elif al_plan.created:
                al_status = "создан settings.json"
            else:
                al_status = "добавлено правило"
            click.echo(f"  allowlist: {al_status} → {al_plan.path} "
                       f"({inst.REVIEWER_PERMISSION_RULE})")
            if al_backup:
                click.echo(f"  бэкап: {al_backup}")
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `.venv/bin/pytest tests/install/test_install.py -q`
Expected: PASS (включая новый CLI-тест).

- [ ] **Step 5: Линт**

Run: `.venv/bin/ruff check reviewer/entrypoints/cli.py`
Expected: чисто (на этом файле).

- [ ] **Step 6: Commit**

```bash
git add reviewer/entrypoints/cli.py tests/install/test_install.py
git commit -m "feat(install): claude-code мёрджит allowlist в .claude/settings.json"
```

---

## Task 3: Committed `settings.json` (plugin + корень репо)

**Files:**
- Create: `plugin/.claude/settings.json`
- Create: `.claude/settings.json`

- [ ] **Step 1: Создать `plugin/.claude/settings.json`**

```json
{
  "permissions": {
    "allow": [
      "mcp__reviewer__*"
    ]
  }
}
```

- [ ] **Step 2: Создать `.claude/settings.json` (корень репо)**

Тот же контент:

```json
{
  "permissions": {
    "allow": [
      "mcp__reviewer__*"
    ]
  }
}
```

- [ ] **Step 3: Убедиться, что файлы не игнорируются git**

Run: `git check-ignore .claude/settings.json plugin/.claude/settings.json; echo "exit=$?"`
Expected: пустой вывод, `exit=1` (не игнорируются). Если `.claude/settings.json`
игнорируется — добавить `git add -f` на следующем шаге для него.

- [ ] **Step 4: Commit**

```bash
git add plugin/.claude/settings.json .claude/settings.json
git commit -m "feat(plugin): committed allowlist settings.json (permissions.allow)"
```

---

## Task 4: `plugin/GEMINI.md`

**Files:**
- Create: `plugin/GEMINI.md`

- [ ] **Step 1: Создать `plugin/GEMINI.md`**

```markdown
# Gemini CLI — reviewer MCP

Плагин подключает MCP-сервер `reviewer` (RAG + граф кода + публикация ревью).
Скилы `/solve-task`, `/review-pr`, `/sync-tasks` вызывают его тулы. Чтобы они
работали без ручного подтверждения на каждый вызов, пометьте сервер `reviewer`
доверенным в Gemini CLI (`~/.gemini/settings.json`, поле `trust` у MCP-сервера)
— тогда все его тулы предодобрены.

## Pre-approved тулы reviewer

- `mcp__reviewer__prepare_review`
- `mcp__reviewer__search_code`
- `mcp__reviewer__get_related_symbols`
- `mcp__reviewer__read_file`
- `mcp__reviewer__get_definition`
- `mcp__reviewer__find_callers`
- `mcp__reviewer__get_changed_file_diff`
- `mcp__reviewer__index_task`
- `mcp__reviewer__index_tasks_batch`
- `mcp__reviewer__purge_orphaned_tasks`
- `mcp__reviewer__search_tasks`
- `mcp__reviewer__get_task_context`
- `mcp__reviewer__get_board_config`
- `mcp__reviewer__search_codebase`
- `mcp__reviewer__publish_review`

Эквивалент для целого сервера — доверять `reviewer` целиком; в Claude Code это
одно правило `mcp__reviewer__*` в `permissions.allow` (см. `.claude/settings.json`).
```

- [ ] **Step 2: Commit**

```bash
git add plugin/GEMINI.md
git commit -m "docs(plugin): GEMINI.md со списком pre-approved тулов reviewer"
```

---

## Task 5: Фикс докстринга «14 → 15 тулов»

**Files:**
- Modify: `reviewer/entrypoints/mcp_server.py:18`

- [ ] **Step 1: Заменить число в докстринге `create_server`**

Было: `"""Создать и вернуть сконфигурированный FastMCP-сервер с 14 тулами.`
Стало: `"""Создать и вернуть сконфигурированный FastMCP-сервер с 15 тулами.`

- [ ] **Step 2: Проверить, что тестовый счётчик тулов согласован**

Run: `.venv/bin/pytest tests/mcp -q -k "tool or count" 2>/dev/null; .venv/bin/pytest tests/mcp -q`
Expected: PASS (тест на число тулов уже ожидает 15 — см. коммит `b9eeb8b`).

- [ ] **Step 3: Commit**

```bash
git add reviewer/entrypoints/mcp_server.py
git commit -m "docs(mcp): докстринг create_server — 15 тулов"
```

---

## Task 6: Заметка в `README.md`

**Files:**
- Modify: `README.md` (секция про `reviewer install`, около строк 150–170)

- [ ] **Step 1: Найти место**

Run: `grep -n "reviewer install" README.md | head`
Выбрать абзац-врезку про кроссплатформенность `reviewer install` (около строки 165).

- [ ] **Step 2: Добавить предложение во врезку**

После описания того, что `reviewer install` инжектит MCP-команду, добавить:

```markdown
> Для **Claude Code** `reviewer install claude-code` дополнительно прописывает
> allowlist `mcp__reviewer__*` в `.claude/settings.json` проекта — тулы reviewer
> работают из коробки (без обращения к safety-classifier в режиме `auto`).
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): reviewer install прописывает allowlist для Claude Code"
```

---

## Task 7: Полная проверка

- [ ] **Step 1: Весь unit-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены дефолтным маркером).

- [ ] **Step 2: Линт по затронутым файлам**

Run: `.venv/bin/ruff check reviewer/install.py reviewer/entrypoints/cli.py reviewer/entrypoints/mcp_server.py tests/install/test_install.py`
Expected: чисто.

- [ ] **Step 3: Финальный статус**

Run: `git log --oneline -7 && git status`
Expected: коммиты Task 1–6 на ветке `feat/pri-98-mcp-allowlist`, рабочее дерево чистое.

---

## Self-Review (выполнено при написании плана)

- **Покрытие спеки:** wildcard-правило (Task 1,3,4), committed settings ×2 (Task 3),
  install-мёрдж (Task 1+2), GEMINI.md (Task 4), докстринг-фикс (Task 5), README (Task 6),
  тесты (Task 1,2). Вне scope (Copilot, Cursor/VSCode, Gemini-trust-запись) — по спеке.
- **Плейсхолдеры:** нет — весь код приведён.
- **Согласованность типов:** `AllowlistPlan(path, content, created, already)`,
  `build_allowlist_plan(path, rule)`, `apply_allowlist_plan(plan)`, `_write_with_backup`,
  `REVIEWER_PERMISSION_RULE`, `claude_settings_path()` — имена едины во всех тасках.
