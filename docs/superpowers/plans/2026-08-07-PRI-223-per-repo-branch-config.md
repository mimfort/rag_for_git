# PRI-223 (часть A) — Per-repo branch config: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Каждый репозиторий получает собственные отслеживаемые ветки из домашнего YAML-слоя, а не из единственного глобального `REVIEW_BRANCHES`.

**Architecture:** Новый модуль `reviewer/config/branches.py` резолвит блок `repository` из домашних слоёв (per-repo → global → env → `main`), не читая committed `.review.yml` и не ходя в сеть — это конструктивно устраняет цикл «чтобы узнать ветку, надо прочитать файл из ветки». Все места, читавшие ветки из `Settings`, переходят на этот резолвер и передают ему `repo`. Диагностика (`config show`) и миграция (`config migrate`) расширяются branch-секцией.

**Tech Stack:** Python 3.11+, pydantic-settings, PyYAML, Click, pytest.

## Global Constraints

- Язык кода: комментарии, докстринги и сообщения CLI — на русском.
- `ruff check .` — line-length 100, target py311. Репозиторий не чист на main: не гнаться за repo-wide clean, но новые/изменённые файлы должны проходить.
- Все тесты этого плана — unit: без Postgres, Neo4j, сети и localhost-сокетов. Любой тест с реальной сетью обязан иметь `@pytest.mark.integration` (таких здесь нет).
- Запуск: `.venv/bin/pytest`, линт: `.venv/bin/ruff check`.
- Коммиты: Conventional Commits на русском, **без** self-attribution (никаких `Co-Authored-By` / упоминаний Claude).
- Обратная совместимость: ни одно env-поле не удаляется. При отсутствии домашних файлов поведение обязано быть бит-в-бит прежним.
- Ветка работы: `feat/pri-223-per-repo-branch-config` (уже создана, содержит бриф и спеку).

---

### Task 1: Модуль резолва веток

**Files:**
- Create: `reviewer/config/branches.py`
- Test: `tests/config/test_branches.py`

**Interfaces:**
- Consumes: `reviewer.config.layers.home_repo_path`, `reviewer_config_root`, `_read_mapping`, `HomePolicyError`, `HomeConfigError`; `reviewer.config.settings.Settings`.
- Produces: `RepoBranches(primary: str, index: tuple[str, ...], source: str, warnings: tuple[str, ...])` и `resolve_repo_branches(repo, *, settings, config_root=None, strict_home=False) -> RepoBranches`. Все последующие задачи зовут только их.

- [ ] **Step 1: Написать падающий тест приоритета слоёв**

```python
# tests/config/test_branches.py
import pytest

from reviewer.config.branches import RepoBranches, resolve_repo_branches
from reviewer.config.layers import HomePolicyError
from reviewer.config.settings import Settings


def _settings(monkeypatch, branches="main"):
    monkeypatch.setenv("REVIEW_BRANCHES", branches)
    return Settings(_env_file=None)


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_per_repo_layer_wins_over_global_and_env(tmp_path, monkeypatch):
    _write(tmp_path, "review.yml", "repository:\n  index_branches: [main]\n")
    _write(
        tmp_path,
        "repos/o/r.yml",
        "repository:\n  primary_branch: dev\n  index_branches: [dev, main]\n",
    )
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch, "trunk"), config_root=tmp_path
    )
    assert result == RepoBranches(
        primary="dev", index=("dev", "main"), source="home:repos/o/r.yml", warnings=()
    )


def test_global_layer_used_when_no_per_repo_file(tmp_path, monkeypatch):
    _write(tmp_path, "review.yml", "repository:\n  index_branches: [release, main]\n")
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch, "trunk"), config_root=tmp_path
    )
    assert result.primary == "release"
    assert result.index == ("release", "main")
    assert result.source == "home:review.yml"


def test_env_fallback_when_no_home_files(tmp_path, monkeypatch):
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch, "dev,main"), config_root=tmp_path
    )
    assert result.primary == "dev"
    assert result.index == ("dev", "main")
    assert result.source == "env"


def test_default_main_when_env_empty(tmp_path, monkeypatch):
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch, ""), config_root=tmp_path
    )
    assert result == RepoBranches(
        primary="main", index=("main",), source="default", warnings=()
    )


def test_block_replaced_whole_not_merged_per_key(tmp_path, monkeypatch):
    _write(tmp_path, "review.yml", "repository:\n  index_branches: [main, release]\n")
    _write(tmp_path, "repos/o/r.yml", "repository:\n  primary_branch: dev\n")
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch), config_root=tmp_path
    )
    assert result.index == ("dev",)
    assert result.primary == "dev"


def test_primary_defaults_to_first_index_branch(tmp_path, monkeypatch):
    _write(tmp_path, "repos/o/r.yml", "repository:\n  index_branches: [dev, main]\n")
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch), config_root=tmp_path
    )
    assert result.primary == "dev"


def test_primary_outside_index_is_config_error(tmp_path, monkeypatch):
    _write(
        tmp_path,
        "repos/o/r.yml",
        "repository:\n  primary_branch: qa\n  index_branches: [dev, main]\n",
    )
    with pytest.raises(HomePolicyError) as exc:
        resolve_repo_branches(
            "o/r", settings=_settings(monkeypatch), config_root=tmp_path
        )
    assert "qa" in str(exc.value)
    assert "repos/o/r.yml" in str(exc.value)


@pytest.mark.parametrize(
    "body",
    [
        "repository:\n  index_branches: []\n",
        "repository:\n  index_branches: [dev, dev]\n",
        "repository:\n  index_branches: [dev, 7]\n",
        "repository:\n  index_branches: dev\n",
        "repository:\n  index_branches: ['']\n",
        "repository:\n  primary_branch: 7\n  index_branches: [dev]\n",
        "repository: []\n",
    ],
)
def test_invalid_block_is_config_error(tmp_path, monkeypatch, body):
    _write(tmp_path, "repos/o/r.yml", body)
    with pytest.raises(HomePolicyError):
        resolve_repo_branches(
            "o/r", settings=_settings(monkeypatch), config_root=tmp_path
        )


def test_broken_yaml_does_not_silently_fall_back_to_env(tmp_path, monkeypatch):
    _write(tmp_path, "repos/o/r.yml", "repository: [unclosed\n")
    with pytest.raises(Exception):
        resolve_repo_branches(
            "o/r", settings=_settings(monkeypatch, "dev"), config_root=tmp_path
        )


def test_unknown_subkeys_are_preserved_not_rejected(tmp_path, monkeypatch):
    _write(
        tmp_path,
        "repos/o/r.yml",
        "repository:\n  index_branches: [dev]\n  future_key: whatever\n",
    )
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch), config_root=tmp_path
    )
    assert result.index == ("dev",)


def test_missing_repository_block_falls_through_to_next_layer(tmp_path, monkeypatch):
    _write(tmp_path, "repos/o/r.yml", "max_comments: 10\n")
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch, "dev"), config_root=tmp_path
    )
    assert result.source == "env"
    assert result.index == ("dev",)


def test_owner_subgroups_and_dotted_names(tmp_path, monkeypatch):
    _write(tmp_path, "repos/group/sub/r.yml", "repository:\n  index_branches: [a]\n")
    _write(tmp_path, "repos/o/r.io.yml", "repository:\n  index_branches: [b]\n")
    nested = resolve_repo_branches(
        "group/sub/r", settings=_settings(monkeypatch), config_root=tmp_path
    )
    dotted = resolve_repo_branches(
        "o/r.io", settings=_settings(monkeypatch), config_root=tmp_path
    )
    assert nested.index == ("a",)
    assert dotted.index == ("b",)


def test_credential_key_inside_repository_block_is_rejected(tmp_path, monkeypatch):
    _write(
        tmp_path,
        "repos/o/r.yml",
        "repository:\n  index_branches: [dev]\n  github_token: ghp_secret\n",
    )
    with pytest.raises(Exception) as exc:
        resolve_repo_branches(
            "o/r", settings=_settings(monkeypatch), config_root=tmp_path
        )
    assert "ghp_secret" not in str(exc.value)
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/config/test_branches.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'reviewer.config.branches'`

- [ ] **Step 3: Реализовать модуль**

```python
# reviewer/config/branches.py
"""Резолв отслеживаемых веток репозитория из домашних слоёв конфигурации.

Ветки нужны ДО чтения committed `.review.yml` (чтобы знать, из какой ветки его
читать), поэтому этот резолв намеренно не принимает `fetch_repo_yaml` и не ходит
в сеть: только домашние YAML-файлы и env. Так цикл bootstrap↔`.review.yml`
невозможен конструктивно, а не по договорённости.
"""
from __future__ import annotations

import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from reviewer.config.layers import (
    HomeConfigError,
    HomeCredentialError,
    HomePolicyError,
    _credential_path,
    _read_mapping,
    home_repo_path,
    reviewer_config_root,
)
from reviewer.services.repo_id import normalize_repo

if TYPE_CHECKING:
    from reviewer.config.settings import Settings

BRANCHES_KEY = "repository"


@dataclass(frozen=True)
class RepoBranches:
    """Эффективные ветки репозитория и происхождение значения."""

    primary: str
    index: tuple[str, ...]
    source: str
    warnings: tuple[str, ...] = ()


def _parse_block(block: object, source: str) -> tuple[str, tuple[str, ...]]:
    """Провалидировать блок `repository` и вернуть (primary, index)."""
    if not isinstance(block, Mapping):
        raise HomePolicyError(f"{source}: repository должен быть mapping")
    raw_index = block.get("index_branches")
    if not isinstance(raw_index, list) or not raw_index:
        raise HomePolicyError(
            f"{source}: repository.index_branches должен быть непустым списком"
        )
    index: list[str] = []
    for item in raw_index:
        if not isinstance(item, str) or not item.strip():
            raise HomePolicyError(
                f"{source}: repository.index_branches содержит не-строку или пустую строку"
            )
        name = item.strip()
        if name in index:
            raise HomePolicyError(
                f"{source}: repository.index_branches содержит дубль {name!r}"
            )
        index.append(name)
    raw_primary = block.get("primary_branch")
    if raw_primary is None:
        return index[0], tuple(index)
    if not isinstance(raw_primary, str) or not raw_primary.strip():
        raise HomePolicyError(
            f"{source}: repository.primary_branch должен быть непустой строкой"
        )
    primary = raw_primary.strip()
    if primary not in index:
        raise HomePolicyError(
            f"{source}: repository.primary_branch {primary!r} отсутствует в "
            f"index_branches {index}"
        )
    return primary, tuple(index)


def _load_layer(path: Path, source: str) -> tuple[str, tuple[str, ...]] | None:
    """Прочитать блок `repository` одного домашнего файла, либо None."""
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return None
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise HomeConfigError(
            f"{source}: конфиг не прочитан: {type(exc).__name__}"
        ) from None
    data = _read_mapping(text, source)
    if BRANCHES_KEY not in data:
        return None
    block = data[BRANCHES_KEY]
    credential = _credential_path({BRANCHES_KEY: block})
    if credential:
        raise HomeCredentialError(
            f"{source}: credential key {'.'.join(credential)} запрещён"
        )
    return _parse_block(block, source)


def resolve_repo_branches(
    repo: str,
    *,
    settings: Settings,
    config_root: Path | None = None,
    strict_home: bool = False,
) -> RepoBranches:
    """Вернуть эффективные ветки репозитория.

    Порядок слоёв (первый заданный выигрывает целиком, поключевого мержа нет):
    home per-repo → home global → env REVIEW_BRANCHES → ["main"].
    `strict_home` здесь не смягчает ошибки конфига: существующий, но невалидный
    файл всегда ошибка — тихий откат на env индексировал бы не ту ветку.
    """
    repo = normalize_repo(repo)
    root = config_root or reviewer_config_root()
    layers = (
        (home_repo_path(repo, root), f"home:repos/{repo}.yml"),
        (root / "review.yml", "home:review.yml"),
    )
    for path, source in layers:
        parsed = _load_layer(path, source)
        if parsed is not None:
            primary, index = parsed
            return RepoBranches(primary=primary, index=index, source=source)
    env_index = settings.review_branches_list()
    source = "env" if settings.review_branches.strip() else "default"
    return RepoBranches(primary=env_index[0], index=tuple(env_index), source=source)
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest tests/config/test_branches.py -q`
Expected: PASS (все тесты)

- [ ] **Step 5: Линт**

Run: `.venv/bin/ruff check reviewer/config/branches.py tests/config/test_branches.py`
Expected: `All checks passed!`

- [ ] **Step 6: Коммит**

```bash
git add reviewer/config/branches.py tests/config/test_branches.py
git commit -m "feat(config): резолв веток репозитория из домашних слоёв"
```

---

### Task 2: `repository` — не policy-ключ

Committed `.review.yml` не вправе задавать ветки, а policy-миграция не должна видеть в блоке `repository` конфликт.

**Files:**
- Modify: `reviewer/config/layers.py:314-323` (`resolve_policy_data`), `reviewer/config/layers.py:727-758` (`_existing_migration_result`)
- Test: `tests/config/test_layers.py`

**Interfaces:**
- Consumes: `reviewer.config.branches.BRANCHES_KEY` из Task 1.
- Produces: инвариант «ключ `repository` не участвует в policy-резолве»; `resolve_policy_data` добавляет warning вида `.review.yml: ключ repository игнорируется (ветки задаются домашним слоем)`.

- [ ] **Step 1: Написать падающие тесты**

```python
# добавить в tests/config/test_layers.py
def test_committed_repository_key_is_ignored_with_warning(tmp_path):
    committed = "repository:\n  index_branches: [evil]\nmax_comments: 5\n"
    data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: committed, config_root=tmp_path
    )
    assert "repository" not in data
    assert data["max_comments"] == 5
    assert any("repository" in warning for warning in meta.warnings)
    assert not any("evil" in warning for warning in meta.warnings)


def test_home_repository_block_is_not_a_policy_key(tmp_path):
    home = tmp_path / "repos" / "o" / "r.yml"
    home.parent.mkdir(parents=True)
    home.write_text(
        "repository:\n  index_branches: [dev]\nmax_comments: 7\n", encoding="utf-8"
    )
    data, meta = resolve_policy_data(
        "o/r", "main", lambda _ref: None, config_root=tmp_path
    )
    assert "repository" not in data
    assert data["max_comments"] == 7


def test_migration_ignores_repository_block_when_comparing(tmp_path):
    committed = "max_comments: 5\n"
    home = tmp_path / "repos" / "o" / "r.yml"
    home.parent.mkdir(parents=True)
    home.write_text(
        "max_comments: 5\nrepository:\n  index_branches: [dev]\n", encoding="utf-8"
    )
    result = migrate_repo_config(
        "o/r", "main", lambda _ref: committed, config_root=tmp_path
    )
    assert result.conflicting_keys == ()
    assert result.noop is True
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/config/test_layers.py -q -k "repository"`
Expected: FAIL — `repository` присутствует в `data`, а миграция даёт `conflicting_keys == ('repository',)`

- [ ] **Step 3: Реализовать**

В `resolve_policy_data` (`layers.py`), после `committed = _read_mapping(fetch_repo_yaml(ref), ".review.yml")` и перед `merge(committed, ".review.yml")`:

```python
    from reviewer.config.branches import BRANCHES_KEY

    if BRANCHES_KEY in committed:
        committed = {k: v for k, v in committed.items() if k != BRANCHES_KEY}
        warnings.append(
            ".review.yml: ключ repository игнорируется "
            "(ветки задаются домашним слоем, см. reviewer config show)"
        )
```

В `merge_home` — отбрасывать тот же ключ, чтобы он не попадал в policy-data:

```python
            _validate_known_policy_data(data, source)
            merge({k: v for k, v in data.items() if k != BRANCHES_KEY}, source)
```

В `_existing_migration_result` — исключить ключ из сравнения:

```python
    existing_policy = {k: v for k, v in existing.items() if k != BRANCHES_KEY}
    if not _semantic_equal(existing_policy, candidate):
        conflicts = tuple(sorted(
            key
            for key in set(existing_policy) | set(candidate)
            if not _semantic_equal(
                existing_policy.get(key, _MISSING),
                candidate.get(key, _MISSING),
            )
        ))
```

Импорт `BRANCHES_KEY` делать локально внутри функций — `branches.py` импортирует `layers.py`, и модульный импорт создал бы цикл.

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest tests/config/test_layers.py tests/config/test_branches.py -q`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add reviewer/config/layers.py tests/config/test_layers.py
git commit -m "fix(config): исключить ключ repository из policy-резолва и миграции"
```

---

### Task 3: `resolve_branch` на `RepoBranches`

**Files:**
- Modify: `reviewer/services/branch.py:15-25`
- Test: `tests/services/test_branch_resolve.py`

**Interfaces:**
- Consumes: `RepoBranches` из Task 1.
- Produces: `resolve_branch(requested: str | None, current: str | None, branches: RepoBranches) -> str` — единственная точка валидации ветки; Tasks 4-5 зовут именно её.

- [ ] **Step 1: Переписать тесты под новую сигнатуру**

```python
# tests/services/test_branch_resolve.py — заменить импорт Settings-фикстуры
import pytest

from reviewer.config.branches import RepoBranches
from reviewer.services.branch import current_git_branch, resolve_branch


def _branches(*names):
    return RepoBranches(primary=names[0], index=tuple(names), source="test")


def test_requested_branch_is_used_when_tracked():
    assert resolve_branch("master", "main", _branches("main", "master")) == "master"


def test_requested_branch_outside_index_raises():
    with pytest.raises(ValueError) as exc:
        resolve_branch("develop", "main", _branches("main"))
    assert "develop" in str(exc.value)


def test_current_git_branch_used_when_tracked():
    assert resolve_branch(None, "master", _branches("main", "master")) == "master"


def test_untracked_current_branch_falls_back_to_primary():
    assert resolve_branch(None, "feature/x", _branches("main")) == "main"


def test_no_signal_falls_back_to_primary():
    assert resolve_branch(None, None, _branches("dev", "main")) == "dev"
```

Тесты `current_git_branch` (`test_current_git_branch_returns_stripped_name`, `..._none_on_oserror`, `..._none_on_detached_head`) остаются без изменений.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/services/test_branch_resolve.py -q`
Expected: FAIL — `resolve_branch` ожидает `Settings`

- [ ] **Step 3: Переписать функцию**

```python
def resolve_branch(requested: str | None, current: str | None,
                   branches: RepoBranches) -> str:
    """Выбрать ветку: явный запрос → текущая git-ветка → первичная."""
    if requested:
        if requested not in branches.index:
            raise ValueError(
                f"ветка {requested!r} не отслеживается для этого репозитория "
                f"({list(branches.index)}; источник: {branches.source})"
            )
        return requested
    if current and current in branches.index:
        return current
    return branches.primary
```

Импорт в шапке модуля: `from reviewer.config.branches import RepoBranches` (под `TYPE_CHECKING` держать не нужно — используется в рантайме только как аннотация, но явный импорт делает модуль самодостаточным).

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest tests/services/test_branch_resolve.py -q`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add reviewer/services/branch.py tests/services/test_branch_resolve.py
git commit -m "refactor(services): resolve_branch принимает RepoBranches вместо Settings"
```

---

### Task 4: CLI-команды `index`, `search`, `status`

**Files:**
- Modify: `reviewer/entrypoints/cli.py:630` (`index`), `:711-714` (`search`), `:743` (`status`)
- Test: `tests/entrypoints/test_cli_branches.py` (создать)

**Interfaces:**
- Consumes: `resolve_repo_branches` (Task 1), `resolve_branch` (Task 3).
- Produces: поведение CLI, на которое опирается Task 6 (`config show`) в части текста источника.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/entrypoints/test_cli_branches.py
from click.testing import CliRunner

from reviewer.entrypoints.cli import cli


def _home(tmp_path, repo, body):
    path = tmp_path / "repos" / f"{repo}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_search_rejects_branch_outside_per_repo_index(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main,release")
    _home(tmp_path / "rag-reviewer", "o/r", "repository:\n  index_branches: [dev]\n")
    result = CliRunner().invoke(
        cli, ["search", "q", "--repo", "o/r", "--branch", "release"]
    )
    assert result.exit_code != 0
    assert "release" in result.output
    assert "REVIEW_BRANCHES" not in result.output


def test_two_repos_get_disjoint_index_branches(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    root = tmp_path / "rag-reviewer"
    _home(root, "o/a", "repository:\n  index_branches: [dev]\n")
    _home(root, "o/b", "repository:\n  index_branches: [trunk]\n")
    rejected = CliRunner().invoke(
        cli, ["search", "q", "--repo", "o/a", "--branch", "trunk"]
    )
    assert rejected.exit_code != 0
    assert "trunk" in rejected.output
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_branches.py -q`
Expected: FAIL — `--branch release` принимается (валидация идёт по env `REVIEW_BRANCHES`)

- [ ] **Step 3: Перевести три места**

`index` (`cli.py:630`) — заменить `ref = ref or s.primary_branch()`:

```python
    branches = resolve_repo_branches(repo_id, settings=s)
    ref = resolve_branch(ref, None, branches)
```

`search` (`cli.py:711-714`) — заменить проверку и дефолт:

```python
    branches = resolve_repo_branches(repo_id, settings=s)
    try:
        branch = resolve_branch(branch_opt, None, branches)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
```

`status` (`cli.py:743`) — заменить `branches = [branch_opt] if branch_opt else s.review_branches_list()`:

```python
    repo_branches = resolve_repo_branches(repo, settings=s)
    branches = [branch_opt] if branch_opt else list(repo_branches.index)
```

Импорты в шапке `cli.py`:

```python
from reviewer.config.branches import resolve_repo_branches
from reviewer.services.branch import resolve_branch
```

Обновить тексты `--help`: у `search` и `status` убрать упоминание `REVIEW_BRANCHES`, заменив на «отслеживаемые ветки репозитория (см. reviewer config show)».

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest tests/entrypoints/ -q`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add reviewer/entrypoints/cli.py tests/entrypoints/test_cli_branches.py
git commit -m "feat(cli): index/search/status выбирают ветки по репозиторию"
```

---

### Task 5: MCP session-less тулы и гейт PR

**Files:**
- Modify: `reviewer/mcp/service.py:1331-1349` (`_resolve_repo_branch`), `reviewer/services/review_service.py:202`
- Test: `tests/mcp/test_resolve_repo_branch.py` (создать), `tests/services/test_review_service_branch_gate.py` (создать)

**Interfaces:**
- Consumes: `resolve_repo_branches` (Task 1), `resolve_branch` (Task 3).
- Produces: гейт `BranchNotTrackedError` по per-repo списку.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/mcp/test_resolve_repo_branch.py
def test_session_less_branch_validated_per_repo(tmp_path, monkeypatch, mcp_service):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "rag-reviewer" / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("repository:\n  index_branches: [dev]\n", encoding="utf-8")
    note = mcp_service._resolve_repo_branch("o/r", "main")
    assert isinstance(note, str)
    assert "main" in note
    assert mcp_service._resolve_repo_branch("o/r", None) == ("o/r", "dev")
```

```python
# tests/services/test_review_service_branch_gate.py
def test_pr_into_untracked_branch_is_skipped_per_repo(tmp_path, monkeypatch, service, fake_vcs):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "rag-reviewer" / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("repository:\n  index_branches: [dev]\n", encoding="utf-8")
    fake_vcs.pull_request.base_ref = "main"
    with pytest.raises(BranchNotTrackedError):
        service.prepare("o", "r", 1, vcs_provider=fake_vcs)
```

Фикстуры `mcp_service`, `service`, `fake_vcs` брать из существующих conftest соответствующих каталогов; если их нет — собрать по образцу ближайшего теста в `tests/mcp/` и `tests/services/`, замокав store/graph/embedder (сети быть не должно).

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/mcp/test_resolve_repo_branch.py tests/services/test_review_service_branch_gate.py -q`
Expected: FAIL — валидация идёт по глобальному `REVIEW_BRANCHES`

- [ ] **Step 3: Реализовать**

`_resolve_repo_branch` (`mcp/service.py:1346-1349`) — заменить хвост:

```python
        from reviewer.config.branches import resolve_repo_branches
        from reviewer.services.branch import resolve_branch

        try:
            branches = resolve_repo_branches(repo, settings=self.settings)
            return (repo, resolve_branch(branch, None, branches))
        except ValueError as exc:
            return f"({exc})"
```

Обновить докстринг: ветка валидируется по отслеживаемым веткам репозитория, а не по `REVIEW_BRANCHES`.

`review_service.py:202` — заменить условие:

```python
            from reviewer.config.branches import resolve_repo_branches

            branch = prq.base_ref
            if branch not in resolve_repo_branches(repo, settings=self.settings).index:
                raise BranchNotTrackedError(branch)
```

`HomeConfigError` здесь намеренно не глушится: битый конфиг обязан быть громким, иначе PR молча останется без ревью.

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest tests/mcp/ tests/services/ -q`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add reviewer/mcp/service.py reviewer/services/review_service.py tests/mcp/test_resolve_repo_branch.py tests/services/test_review_service_branch_gate.py
git commit -m "feat(mcp): session-less тулы и гейт PR используют ветки репозитория"
```

---

### Task 6: `config show` — секция веток без сети

**Files:**
- Modify: `reviewer/entrypoints/cli.py:113-156` (`_config_context`, `config_show`, `_render_config_report`)
- Test: `tests/entrypoints/test_cli_config_show_branches.py` (создать)

**Interfaces:**
- Consumes: `resolve_repo_branches` (Task 1).
- Produces: ключ `branches` отчёта — `{"primary": str, "index": [str], "source": str}`; Task 7 показывает его после миграции.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/entrypoints/test_cli_config_show_branches.py
import json

from click.testing import CliRunner

from reviewer.entrypoints.cli import cli


def test_branches_shown_even_when_policy_part_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    path = tmp_path / "rag-reviewer" / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "repository:\n  primary_branch: dev\n  index_branches: [dev, main]\n",
        encoding="utf-8",
    )

    def boom(*args, **kwargs):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(
        "reviewer.services.review_service.ReviewService._create_vcs_provider", boom
    )
    result = CliRunner().invoke(cli, ["config", "show", "--repo", "o/r", "--json"])
    payload = json.loads(result.output)
    assert payload["branches"] == {
        "primary": "dev",
        "index": ["dev", "main"],
        "source": "home:repos/o/r.yml",
    }
    assert "policy_error" in payload


def test_branch_used_for_policy_ref_comes_from_home_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    path = tmp_path / "rag-reviewer" / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("repository:\n  index_branches: [dev]\n", encoding="utf-8")
    seen = []

    def fake_provider(self, owner, name):
        class _V:
            def get_file_at_ref(self, _path, ref):
                seen.append(ref)
                return "max_comments: 5\n"

            def close(self):
                return None

        return _V()

    monkeypatch.setattr(
        "reviewer.services.review_service.ReviewService._create_vcs_provider",
        fake_provider,
    )
    CliRunner().invoke(cli, ["config", "show", "--repo", "o/r", "--json"])
    assert seen == ["dev"]
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/entrypoints/test_cli_config_show_branches.py -q`
Expected: FAIL — в отчёте нет ключа `branches`, а падение VCS роняет команду целиком

- [ ] **Step 3: Реализовать**

В `_config_context` (`cli.py:113-127`) заменить `branch = branch_opt or settings.primary_branch()` на резолв через домашний слой и сделать создание VCS не-фатальным:

```python
        repo = _resolve_config_repo(repo_opt)
        branches = resolve_repo_branches(repo, settings=settings)
        branch = branch_opt or branches.primary
        owner, name = repo.split("/", 1)
        vcs_error: Exception | None = None
        try:
            vcs = ReviewService(settings, components)._create_vcs_provider(owner, name)
        except Exception as exc:  # noqa: BLE001 — диагностика не должна падать целиком
            vcs_error = exc
        yield settings, components, vcs, repo, branch, branches, vcs_error
```

Обе распаковки (`config_show`, `config_migrate`) обновить под семиэлементный кортеж. `config_migrate` при `vcs_error is not None` обязан упасть с `click.ClickException` — миграция policy без чтения `.review.yml` бессмысленна; `config_show` продолжает работу.

В `config_show` собрать отчёт двумя частями —

```python
    payload: dict[str, object] = {
        "branches": {
            "primary": branches.primary,
            "index": list(branches.index),
            "source": branches.source,
        }
    }
    try:
        if vcs_error is not None:
            raise vcs_error
        data, meta = resolve_policy_data(
            repo_id,
            ref,
            lambda selected_ref: vcs.get_file_at_ref(".review.yml", selected_ref),
            strict_home=True,
        )
        payload.update(build_config_report(repo_id, ref, settings, data, meta))
    except Exception as exc:  # noqa: BLE001 — диагностика не должна падать целиком
        payload["policy_error"] = f"{type(exc).__name__}: {exc}"
```

Существующий внешний `except (HomeConfigError, yaml.YAMLError)` вокруг `with` при этом теряет смысл для policy-части, но остаётся для ошибок branch-резолва — их прятать нельзя.

В `_render_config_report` добавить печать секции перед policy-частью:

```python
    branches = report.get("branches")
    if branches:
        click.echo("branches:")
        click.echo(f"  primary: {branches['primary']}  ({branches['source']})")
        click.echo(f"  index:   {', '.join(branches['index'])}  ({branches['source']})")
```

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest tests/entrypoints/ -q`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add reviewer/entrypoints/cli.py tests/entrypoints/test_cli_config_show_branches.py
git commit -m "feat(cli): config show печатает эффективные ветки и их источник"
```

---

### Task 7: `config migrate` — перенос `REVIEW_BRANCHES`

**Files:**
- Modify: `reviewer/config/branches.py` (добавить `migrate_repo_branches`), `reviewer/entrypoints/cli.py:159-182` (`config_migrate`)
- Test: `tests/config/test_branches_migrate.py` (создать)

**Interfaces:**
- Consumes: `resolve_repo_branches`, `home_repo_path`.
- Produces: `migrate_repo_branches(repo, *, settings, config_root=None) -> BranchMigrationResult(path, created, noop)`.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/config/test_branches_migrate.py
import pytest
import yaml

from reviewer.config.branches import migrate_repo_branches, resolve_repo_branches
from reviewer.config.settings import Settings


def _settings(monkeypatch, branches="dev,main"):
    monkeypatch.setenv("REVIEW_BRANCHES", branches)
    return Settings(_env_file=None)


def test_migration_creates_file_with_env_branches(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    result = migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert result.created is True
    data = yaml.safe_load(result.path.read_text(encoding="utf-8"))
    assert data["repository"]["index_branches"] == ["dev", "main"]


def test_effective_branches_unchanged_by_migration(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    before = resolve_repo_branches("o/r", settings=settings, config_root=tmp_path)
    migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    after = resolve_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert (after.primary, after.index) == (before.primary, before.index)


def test_second_call_is_noop(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    second = migrate_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert second.noop is True
    assert second.created is False


def test_existing_repository_block_is_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("repository:\n  index_branches: [trunk]\n", encoding="utf-8")
    result = migrate_repo_branches(
        "o/r", settings=_settings(monkeypatch), config_root=tmp_path
    )
    assert result.noop is True
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["repository"]["index_branches"] == ["trunk"]


def test_block_appended_to_existing_file_preserving_comments(tmp_path, monkeypatch):
    path = tmp_path / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("# важный комментарий\nmax_comments: 5\n", encoding="utf-8")
    migrate_repo_branches("o/r", settings=_settings(monkeypatch), config_root=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "# важный комментарий" in text
    assert "max_comments: 5" in text
    assert yaml.safe_load(text)["repository"]["index_branches"] == ["dev", "main"]


def test_env_file_is_not_touched(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("REVIEW_BRANCHES=dev,main\n", encoding="utf-8")
    migrate_repo_branches("o/r", settings=_settings(monkeypatch), config_root=tmp_path)
    assert env.read_text(encoding="utf-8") == "REVIEW_BRANCHES=dev,main\n"
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `.venv/bin/pytest tests/config/test_branches_migrate.py -q`
Expected: FAIL — `ImportError: cannot import name 'migrate_repo_branches'`

- [ ] **Step 3: Реализовать**

В `reviewer/config/branches.py`:

```python
@dataclass(frozen=True)
class BranchMigrationResult:
    """Итог переноса REVIEW_BRANCHES в домашний per-repo слой."""

    path: Path
    created: bool
    noop: bool


def _render_block(index: tuple[str, ...]) -> str:
    """Сформировать YAML-блок repository (ветки — простые имена без кавычек)."""
    names = ", ".join(index)
    return (
        "\n# Отслеживаемые ветки репозитория (перенесено из REVIEW_BRANCHES).\n"
        "# Первая ветка списка — первичная, если primary_branch не задан явно.\n"
        "repository:\n"
        f"  index_branches: [{names}]\n"
    )


def migrate_repo_branches(
    repo: str,
    *,
    settings: Settings,
    config_root: Path | None = None,
) -> BranchMigrationResult:
    """Перенести env REVIEW_BRANCHES в repository.index_branches домашнего слоя.

    `.env` не изменяется: env остаётся рабочим фолбэком. Существующий блок
    `repository` не перезаписывается — это noop.
    """
    repo = normalize_repo(repo)
    root = config_root or reviewer_config_root()
    destination = home_repo_path(repo, root)
    source = f"home:repos/{repo}.yml"
    index = tuple(settings.review_branches_list())
    block = _render_block(index)
    try:
        existing_text = destination.read_text(encoding="utf-8")
    except FileNotFoundError:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(destination, "x", encoding="utf-8") as handle:
                handle.write(block.lstrip("\n"))
        except FileExistsError:
            return BranchMigrationResult(destination, False, True)
        return BranchMigrationResult(destination, True, False)
    if BRANCHES_KEY in _read_mapping(existing_text, source):
        return BranchMigrationResult(destination, False, True)
    suffix = "" if existing_text.endswith("\n") else "\n"
    destination.write_text(existing_text + suffix + block, encoding="utf-8")
    return BranchMigrationResult(destination, False, False)
```

В `config_migrate` (`cli.py`) — вызвать branch-миграцию **после** существующей policy-миграции (порядок обязателен: `migrate_repo_config` при уже существующем файле уходит в `_existing_migration_result`, поэтому создавать файл раньше неё нельзя) и напечатать итог:

```python
    branch_result = migrate_repo_branches(repo_id, settings=settings)
    if branch_result.noop:
        click.echo(f"Ветки уже заданы в {branch_result.path}")
    else:
        click.echo(f"Ветки перенесены в {branch_result.path} (.env не изменён)")
```

Порядок и обработка ошибок:

1. Выполнить существующую policy-миграцию, обернув её в свой `try/except (HomeConfigError, yaml.YAMLError)` и сохранив исключение в переменную вместо немедленного `raise`. Причина порядка: `migrate_repo_config` вызывает `_publish_new_config`, который создаёт файл только при его отсутствии; если branch-миграция создаст файл раньше, policy-перенос уйдёт в `_existing_migration_result`.
2. Затем выполнить `migrate_repo_branches` и напечатать её итог — перенос веток от committed `.review.yml` не зависит (а `layers.py:885-886` роняет policy-часть на пустом файле).
3. Если policy-часть сохранила исключение — поднять его после печати branch-итога. Так пользователь видит, что ветки перенесены, и одновременно получает ненулевой код возврата.

То же для `vcs_error` из Task 6: policy-миграция без VCS невозможна, но branch-миграция выполняется и печатается до падения команды.

- [ ] **Step 4: Прогнать тесты**

Run: `.venv/bin/pytest tests/config/ tests/entrypoints/ -q`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add reviewer/config/branches.py reviewer/entrypoints/cli.py tests/config/test_branches_migrate.py
git commit -m "feat(config): config migrate переносит REVIEW_BRANCHES в домашний слой"
```

---

### Task 8: Guard-тест и обратная совместимость

**Files:**
- Test: `tests/config/test_branches_guard.py` (создать)
- Modify: `tests/config/test_review_branches.py`

**Interfaces:**
- Consumes: всё выше.
- Produces: инвариант «глобальные branch-методы `Settings` вызываются только из `branches.py`».

- [ ] **Step 1: Написать падающий тест**

```python
# tests/config/test_branches_guard.py
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "reviewer"
PATTERN = re.compile(r"\b(review_branches_list|primary_branch)\s*\(")

# migrate-branches — одноразовая legacy-миграция base-индекса; она repo-агностична
# (у `store.migrate_legacy_base` нет параметра repo), поэтому остаётся на env-слое.
ALLOWED = {
    Path("config/branches.py"),
    Path("config/settings.py"),
}


def test_branch_methods_are_called_only_from_branches_module():
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel in ALLOWED:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if PATTERN.search(line) and "def " not in line:
                offenders.append(f"{rel}:{number}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)
```

Команда `migrate-branches` (`cli.py:684`) на момент написания плана вызывает `s.primary_branch()`. В рамках Task 8 её нужно привести в соответствие: заменить на `resolve_repo_branches(_resolve_repo(None, ".", s), settings=s).primary`, если репозиторий определяется, и оставить `Settings`-фолбэк внутри `branches.py`. Если репозиторий определить нельзя (нет remote, нет `DEFAULT_REPO`) — команда падает с понятным сообщением, что для миграции нужен `--repo`; добавить ей опцию `--repo`.

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/bin/pytest tests/config/test_branches_guard.py -q`
Expected: FAIL — перечислены оставшиеся вызовы (как минимум `entrypoints/cli.py` в `migrate-branches`)

- [ ] **Step 3: Устранить оставшиеся вызовы**

Добавить `--repo` команде `migrate-branches` и перевести её на резолвер:

```python
@cli.command("migrate-branches")
@click.option("--repo", "repo_tag", default=None,
              help="owner/name; по умолчанию из git remote origin")
def migrate_branches(repo_tag: str | None) -> None:
    """Один раз после апгрейда: перенести legacy base-индекс на первичную ветку."""
    s = Settings()
    c = build_components(s)
    repo_id = _resolve_repo(repo_tag, ".", s)
    primary = resolve_repo_branches(repo_id, settings=s).primary
```

- [ ] **Step 4: Дополнить тесты обратной совместимости**

```python
# добавить в tests/config/test_review_branches.py
def test_env_behaviour_unchanged_without_home_files(tmp_path, monkeypatch):
    """Деплой без домашних файлов ведёт себя ровно как раньше."""
    monkeypatch.setenv("REVIEW_BRANCHES", "dev,main")
    settings = Settings(_env_file=None)
    resolved = resolve_repo_branches("o/r", settings=settings, config_root=tmp_path)
    assert list(resolved.index) == settings.review_branches_list()
    assert resolved.primary == settings.primary_branch()
```

- [ ] **Step 5: Прогнать весь unit-набор**

Run: `.venv/bin/pytest -q`
Expected: PASS (integration исключены автоматически через `addopts`)

- [ ] **Step 6: Линт изменённых файлов**

Run: `.venv/bin/ruff check reviewer/config/branches.py reviewer/config/layers.py reviewer/entrypoints/cli.py reviewer/mcp/service.py reviewer/services/branch.py reviewer/services/review_service.py tests/config/ tests/entrypoints/`
Expected: `All checks passed!`

- [ ] **Step 7: Коммит**

```bash
git add tests/config/test_branches_guard.py tests/config/test_review_branches.py reviewer/entrypoints/cli.py
git commit -m "test(config): guard на вызовы глобальных branch-методов вне branches.py"
```

---

## Проверка спеки

| Требование спеки | Задача |
|---|---|
| Модель слоёв, приоритет, замена блока целиком | 1 |
| Валидация блока, ошибка вместо тихого отката | 1 |
| Запрет `repository` в committed `.review.yml` | 2 |
| `resolve_branch` оживлён и применён | 3, 4, 5 |
| Переход `cli.py:120,630,684,711-714,743` | 4, 6, 8 |
| Переход `mcp/service.py:1346-1349`, `review_service.py:202` | 5 |
| `config show` с ветками, устойчивый к падению policy | 6 |
| `config migrate` без правки `.env`, идемпотентный | 7 |
| Guard-тест | 8 |
| Изоляция двух репозиториев (критерий 4) | 4 |
| Обратная совместимость (критерий 12) | 8 |
