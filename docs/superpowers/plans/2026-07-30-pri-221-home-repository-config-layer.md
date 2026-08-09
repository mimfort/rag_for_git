# PRI-221 Home Repository Config Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add global and per-repository home YAML layers, one provenance-aware resolver, safe config inspection/migration, and runtime/audit integration without requiring `.review.yml` in every repository.

**Architecture:** `reviewer/config/layers.py` owns path resolution, top-level merging, provenance, home-file validation, public rendering, and non-destructive migration. `ReviewPolicy` remains free of filesystem/VCS I/O and gains `load_data`; review, MCP, index, and CLI inject committed YAML through a callback. Runtime callers skip broken home layers with sanitized warnings, while diagnostic CLI calls use strict mode.

**Tech Stack:** Python 3.11–3.13, dataclasses, pathlib, PyYAML, Click, pydantic-settings, PostgreSQL JSONB, pytest, Ruff.

## Global Constraints

- Keep Python support exactly `>=3.11,<3.14`; add no dependency.
- Preserve layer order exactly: ENV → `home:review.yml` → committed `.review.yml` → `home:repos/<repo>.yml`.
- Merge YAML only by top-level key; mappings, lists, and `null` replace the previous value wholesale.
- Never read an uncommitted repository worktree file for review/config resolution.
- Runtime review/index/MCP skips only a malformed home layer and emits a sanitized warning; `config show` and `config migrate` fail on malformed home YAML.
- Skip an entire home file containing credential-like keys; never output YAML values from that file.
- `config migrate` is semantic no-op for an equivalent destination and refuses a differing destination without overwrite or merge.
- Keep committed `.review.yml` behavior unchanged when home files are absent.
- Unit tests must use fakes/tmp paths and the default `--disable-socket -m 'not integration'` policy.
- Follow TDD for every task: observe the new test fail before implementation, then rerun it green.

---

## File Map

- Create `reviewer/config/layers.py`: home paths, safe repo segments, YAML loading, credential checks, top-level merge, provenance, public rendering, migration.
- Create `tests/config/test_layers.py`: resolver, security, rendering, migration, and path tests.
- Create `tests/entrypoints/test_config_commands.py`: Click behavior for `config show/migrate`.
- Modify `reviewer/policy/policy.py`: `ReviewPolicy.load_data`.
- Modify `reviewer/services/repo_id.py`: nested GitLab group repo identifiers.
- Modify `reviewer/services/review_service.py`: resolve once at `base_sha`, reuse policy, attach provenance.
- Modify `reviewer/mcp/session_serde.py`: persisted provenance round-trip.
- Modify `reviewer/mcp/service.py`: one policy resolver for depth/top-k/context limits and history metadata.
- Modify `reviewer/entrypoints/cli.py`: home-aware index plus `config` command group.
- Modify `reviewer/web/schema.sql`: `review_runs.config_sources JSONB`.
- Modify `reviewer/web/history.py`: write/read audit metadata.
- Modify `plugin/skills/configure-review/SKILL.md`: choose home per-repo by default or committed repo target explicitly.
- Modify `README.md` and `README.ru.md`: layer order, commands, migration, shadowing, service-account risk.
- Modify focused existing tests under `tests/policy`, `tests/services`, `tests/mcp`, `tests/entrypoints`, and `tests/web`.

---

### Task 1: Data-Based Policy Loading and Nested Repo IDs

**Files:**
- Modify: `reviewer/policy/policy.py:84-117`
- Modify: `reviewer/services/repo_id.py:12-23`
- Modify: `tests/policy/test_policy.py`
- Modify: `tests/services/test_repo_id.py`

**Interfaces:**
- Produces: `ReviewPolicy.load_data(settings, data: Mapping[str, object]) -> ReviewPolicy`
- Produces: `normalize_repo(repo: str) -> str` accepting two or more safe slash-separated segments
- Consumes: existing `ReviewPolicy.from_settings`, `ContextLimits.from_review_yaml`, and `normalize_task_board_config`

- [ ] **Step 1: Write failing policy and repo-id tests**

Add to `tests/policy/test_policy.py`:

```python
import yaml


def test_load_data_matches_load_for_nested_policy() -> None:
    settings = Settings(_env_file=None, review_severity_threshold="medium")
    data = {
        "severity_threshold": "high",
        "paths": {"ignore": ["vendor/**"]},
        "context_limits": {"graph": {"hops": 2}},
        "task_board": None,
    }

    from_data = ReviewPolicy.load_data(settings, data)
    from_text = ReviewPolicy.load(settings, yaml.safe_dump(data))

    assert from_data == from_text
    assert from_data.ignore == ["vendor/**"]
    assert from_data.context_limits.graph.hops == 2
    assert from_data.task_board is None


```

Add/update in `tests/services/test_repo_id.py`:

```python
@pytest.mark.parametrize("raw,expected", [
    ("Owner/Repo", "owner/repo"),
    ("Group/Sub/Repo", "group/sub/repo"),
])
def test_normalize_repo(raw, expected):
    assert normalize_repo(raw) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "noslash", "/a/b", "a/b/", "a/../b", "a/./b", "a\\b/c", "a/\x00/b"],
)
def test_normalize_repo_rejects_bad(bad):
    with pytest.raises(ValueError):
        normalize_repo(bad)
```

- [ ] **Step 2: Run the focused tests and confirm the red state**

Run:

```bash
uv run pytest -q -p no:cacheprovider \
  tests/policy/test_policy.py::test_load_data_matches_load_for_nested_policy \
  tests/services/test_repo_id.py
```

Expected: failures because `ReviewPolicy.load_data` does not exist and nested repo IDs are currently
rejected.

- [ ] **Step 3: Extract `load_data` without changing policy semantics**

In `reviewer/policy/policy.py`, import `Mapping` and move the body of `load` after YAML parsing:

```python
from collections.abc import Mapping

@classmethod
def load_data(
    cls,
    settings,
    data: Mapping[str, object] | None,
) -> "ReviewPolicy":
    """Apply explicit policy keys over Settings-backed defaults."""
    policy = cls.from_settings(settings)
    data = dict(data or {})
    if "categories" in data:
        policy.categories = data["categories"] or {}
        policy.enabled_only = []
    if "severity_threshold" in data and data["severity_threshold"] in _SEV:
        policy.severity_threshold = data["severity_threshold"]
    if "max_comments" in data:
        policy.max_comments = data["max_comments"]
    if "min_confidence" in data:
        policy.min_confidence = data["min_confidence"]
    ignore = (data.get("paths") or {}).get("ignore")
    if ignore is not None:
        policy.ignore = ignore
    if "output_language" in data:
        policy.output_language = str(data["output_language"])
    if "task_board" in data:
        policy.task_board, policy.task_board_warnings = cls._normalized_task_board(
            data["task_board"]
        )
    if "grounding_max_distance" in data:
        policy.grounding_max_distance = data["grounding_max_distance"]
    if "summary_cluster_depth" in data:
        policy.summary_cluster_depth = int(data["summary_cluster_depth"])
    if "summary_topk_threshold" in data:
        policy.summary_topk_threshold = int(data["summary_topk_threshold"])
    if "summary_cluster_depth_overrides" in data:
        policy.summary_cluster_depth_overrides = dict(
            data["summary_cluster_depth_overrides"] or {}
        )
    if "context_limits" in data:
        policy.context_limits = ContextLimits.from_review_yaml(data)
    return policy

@classmethod
def load(cls, settings, yaml_text: str | None) -> "ReviewPolicy":
    data = yaml.safe_load(yaml_text) if yaml_text else {}
    data = data or {}
    if not isinstance(data, dict):
        raise ValueError("review policy YAML must contain a mapping")
    return cls.load_data(settings, data)
```

Preserve the existing `from_yaml` method unchanged.

- [ ] **Step 4: Extend canonical repo normalization safely**

Replace the fixed two-part check in `reviewer/services/repo_id.py`:

```python
def normalize_repo(repo: str) -> str:
    s = (repo or "").strip().lower()
    if "\\" in s or "\x00" in s:
        raise ValueError(f"Некорректный repo: {repo!r}")
    parts = s.split("/")
    if len(parts) < 2 or any(not part or part in {".", ".."} for part in parts):
        raise ValueError(
            f"Некорректный repo (ожидается owner/name или group/.../name): {repo!r}"
        )
    return "/".join(parts)
```

- [ ] **Step 5: Run policy and repo-id regression tests**

Run:

```bash
uv run pytest -q -p no:cacheprovider \
  tests/policy/test_policy.py tests/policy/test_policy_depth_overrides.py \
  tests/services/test_repo_id.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add reviewer/policy/policy.py reviewer/services/repo_id.py \
  tests/policy/test_policy.py tests/services/test_repo_id.py
git commit -m "refactor: load review policy from merged data"
```

---

### Task 2: Layer Resolver, Provenance, Security, and Migration Primitive

**Files:**
- Create: `reviewer/config/layers.py`
- Create: `tests/config/test_layers.py`

**Interfaces:**
- Consumes: `normalize_repo`, `ReviewPolicy`, provider registry secret metadata
- Produces: `ResolutionMeta(sources, shadowed, warnings)`
- Produces: `resolve_policy_data(repo, ref, fetch_repo_yaml, *, config_root=None, strict_home=False) -> tuple[dict, ResolutionMeta]`
- Produces: `policy_to_public_data(policy) -> dict[str, object]`
- Produces: `migrate_repo_config(repo, ref, fetch_repo_yaml, *, config_root=None) -> MigrationResult`

- [ ] **Step 1: Write failing precedence and provenance tests**

Create `tests/config/test_layers.py` with:

```python
from pathlib import Path

import pytest
import yaml

from reviewer.config.layers import (
    HomeConfigError,
    home_repo_path,
    migrate_repo_config,
    resolve_policy_data,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_layers_replace_top_level_values_and_report_sources(tmp_path: Path) -> None:
    _write(
        tmp_path / "review.yml",
        "paths: {ignore: [global]}\nmax_comments: 5\ncontext_limits: {graph: {hops: 2}}\n",
    )
    _write(
        tmp_path / "repos/o/r.yml",
        "paths: {ignore: [home-repo]}\ntask_board:\n",
    )
    committed = (
        "paths: {ignore: [committed]}\n"
        "max_comments: 7\n"
        "context_limits: {search_codebase: {ceiling: 25}}\n"
    )

    data, meta = resolve_policy_data(
        "O/R", "main", lambda ref: committed, config_root=tmp_path
    )

    assert data["paths"] == {"ignore": ["home-repo"]}
    assert data["context_limits"] == {"search_codebase": {"ceiling": 25}}
    assert data["task_board"] is None
    assert meta.sources["paths"] == "home:repos/o/r.yml"
    assert meta.sources["max_comments"] == ".review.yml"
    assert meta.shadowed["paths"] == ("home:review.yml", ".review.yml")
    assert meta.shadowed["max_comments"] == ("home:review.yml",)


def test_subgroup_repo_uses_nested_home_path(tmp_path: Path) -> None:
    assert home_repo_path("group/sub/repo", tmp_path) == (
        tmp_path / "repos/group/sub/repo.yml"
    )
```

- [ ] **Step 2: Add failing malformed and credential tests**

Append:

```python
def test_runtime_skips_bad_home_but_strict_mode_raises(tmp_path: Path) -> None:
    _write(tmp_path / "review.yml", "[not-a-mapping]\n")

    data, meta = resolve_policy_data(
        "o/r", "main", lambda ref: "max_comments: 9\n", config_root=tmp_path
    )
    assert data == {"max_comments": 9}
    assert len(meta.warnings) == 1
    assert "home:review.yml" in meta.warnings[0]

    with pytest.raises(HomeConfigError):
        resolve_policy_data(
            "o/r",
            "main",
            lambda ref: "max_comments: 9\n",
            config_root=tmp_path,
            strict_home=True,
        )


def test_credential_file_is_skipped_without_echoing_value(tmp_path: Path) -> None:
    secret = "do-not-echo"
    _write(
        tmp_path / "repos/o/r.yml",
        f"max_comments: 4\nnested:\n  github_token: {secret}\n",
    )

    data, meta = resolve_policy_data(
        "o/r", "main", lambda ref: "max_comments: 8\n", config_root=tmp_path
    )

    assert data["max_comments"] == 8
    rendered = "\n".join(meta.warnings)
    assert "github_token" in rendered
    assert secret not in rendered


def test_max_tokens_is_not_misclassified_as_secret(tmp_path: Path) -> None:
    _write(tmp_path / "review.yml", "future: {max_tokens: 100}\n")
    data, meta = resolve_policy_data(
        "o/r", "main", lambda ref: None, config_root=tmp_path
    )
    assert data["future"] == {"max_tokens": 100}
    assert meta.warnings == ()
```

- [ ] **Step 3: Add failing migration behavior tests**

Append:

```python
def test_migrate_creates_file_and_second_call_is_noop(tmp_path: Path) -> None:
    source = "# keep comment\npaths:\n  ignore: [vendor]\n"
    first = migrate_repo_config(
        "o/r", "main", lambda ref: source, config_root=tmp_path
    )
    second = migrate_repo_config(
        "o/r", "main", lambda ref: source, config_root=tmp_path
    )

    destination = tmp_path / "repos/o/r.yml"
    assert first.created is True
    assert second.noop is True
    assert destination.read_text(encoding="utf-8") == source


def test_migrate_refuses_different_destination(tmp_path: Path) -> None:
    destination = tmp_path / "repos/o/r.yml"
    _write(destination, "max_comments: 3\n")

    result = migrate_repo_config(
        "o/r",
        "main",
        lambda ref: "max_comments: 7\npaths: {ignore: [vendor]}\n",
        config_root=tmp_path,
    )

    assert result.created is False
    assert result.noop is False
    assert result.conflicting_keys == ("max_comments", "paths")
    assert destination.read_text(encoding="utf-8") == "max_comments: 3\n"


def test_migrate_rejects_secret_candidate_before_write(tmp_path: Path) -> None:
    with pytest.raises(HomeConfigError) as exc:
        migrate_repo_config(
            "o/r",
            "main",
            lambda ref: "github_token: do-not-write\n",
            config_root=tmp_path,
        )
    assert "do-not-write" not in str(exc.value)
    assert not (tmp_path / "repos/o/r.yml").exists()
```

- [ ] **Step 4: Run the new test module and confirm the red state**

Run:

```bash
uv run pytest -q -p no:cacheprovider tests/config/test_layers.py
```

Expected: collection fails because `reviewer.config.layers` does not exist.

- [ ] **Step 5: Implement the focused resolver API**

Create `reviewer/config/layers.py` with these concrete types and flow:

```python
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from functools import lru_cache
import os
from pathlib import Path
import tempfile

import yaml

from reviewer.policy.policy import ReviewPolicy
from reviewer.services.repo_id import normalize_repo


class HomeConfigError(ValueError):
    pass


class HomeCredentialError(HomeConfigError):
    pass


@dataclass(frozen=True)
class ResolutionMeta:
    sources: dict[str, str]
    shadowed: dict[str, tuple[str, ...]]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "sources": dict(self.sources),
            "shadowed": {k: list(v) for k, v in self.shadowed.items()},
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class MigrationResult:
    path: Path
    created: bool
    noop: bool
    conflicting_keys: tuple[str, ...]
    data: dict[str, object]
    meta: ResolutionMeta


def reviewer_config_root() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg).expanduser() / "rag-reviewer" if xdg else Path.home() / ".config/rag-reviewer"


def home_repo_path(repo: str, config_root: Path | None = None) -> Path:
    root = config_root or reviewer_config_root()
    return root.joinpath("repos", *normalize_repo(repo).split("/")).with_suffix(".yml")
```

Implement private helpers:

```python
def _read_mapping(text: str | None, source: str) -> dict[str, object]:
    data = yaml.safe_load(text) if text else {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise HomeConfigError(f"{source}: верхний уровень должен быть mapping")
    return data


_SECRET_SUFFIXES = (
    "_token", "_password", "_secret", "_api_key",
    "_private_key", "_client_secret", "_access_key",
)

_SETTINGS_SECRET_NAMES = frozenset({
    "voyage_api_key",
    "github_token",
    "gitlab_token",
    "neo4j_password",
    "pg_dsn",
    "web_admin_password",
    "task_board_api_key",
    "yougile_api_key",
    "youtrack_token",
})
_MISSING = object()


@lru_cache(maxsize=1)
def _secret_names() -> frozenset[str]:
    from reviewer.tasks.boards.registry import default_board_registry

    registry = default_board_registry()
    provider_names = {
        env.lower()
        for board_type in registry.registered_types()
        for field in registry.get(board_type).credential_fields
        if field.secret
        for env in (field.env, *field.aliases)
    }
    return _SETTINGS_SECRET_NAMES | frozenset(provider_names)


def _credential_path(value: object, prefix: tuple[str, ...] = ()) -> tuple[str, ...] | None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            current = (*prefix, key)
            if key in _secret_names() or key.endswith(_SECRET_SUFFIXES):
                return current
            nested = _credential_path(child, current)
            if nested:
                return nested
    elif isinstance(value, list):
        for child in value:
            nested = _credential_path(child, prefix)
            if nested:
                return nested
    return None
```

Implement `resolve_policy_data` as four ordered layers where home reads are caught according to
`strict_home`, committed callback/parse errors propagate, and merge updates source/shadowed
metadata exactly as the tests assert. Use this control flow:

```python
def resolve_policy_data(
    repo,
    ref,
    fetch_repo_yaml,
    *,
    config_root=None,
    strict_home=False,
):
    repo = normalize_repo(repo)
    root = config_root or reviewer_config_root()
    merged: dict[str, object] = {}
    sources: dict[str, str] = {}
    shadowed: dict[str, list[str]] = {}
    warnings: list[str] = []

    def merge(data: Mapping[str, object], source: str) -> None:
        for key, value in data.items():
            if key in sources:
                shadowed.setdefault(key, []).append(sources[key])
            merged[key] = value
            sources[key] = source

    def merge_home(path: Path, source: str) -> None:
        if not path.is_file():
            return
        try:
            data = _read_mapping(path.read_text(encoding="utf-8"), source)
            credential = _credential_path(data)
            if credential:
                raise HomeCredentialError(
                    f"{source}: credential key {'.'.join(credential)} запрещён"
                )
            merge(data, source)
        except HomeCredentialError as exc:
            warnings.append(str(exc))
        except (OSError, UnicodeError, yaml.YAMLError, HomeConfigError) as exc:
            wrapped = HomeConfigError(f"{source}: конфиг не прочитан: {type(exc).__name__}")
            if strict_home:
                raise wrapped from exc
            warnings.append(str(wrapped))

    merge_home(root / "review.yml", "home:review.yml")
    committed = _read_mapping(fetch_repo_yaml(ref), ".review.yml")
    merge(committed, ".review.yml")
    repo_source = f"home:repos/{repo}.yml"
    merge_home(home_repo_path(repo, root), repo_source)
    return merged, ResolutionMeta(
        sources=dict(sources),
        shadowed={key: tuple(value) for key, value in shadowed.items()},
        warnings=tuple(warnings),
    )
```

Implement `policy_to_public_data` by explicitly returning public policy fields:

```python
def policy_to_public_data(policy: ReviewPolicy) -> dict[str, object]:
    return {
        "categories": dict(policy.categories),
        "enabled_only": list(policy.enabled_only),
        "severity_threshold": policy.severity_threshold,
        "paths": {"ignore": list(policy.ignore)},
        "max_comments": policy.max_comments,
        "min_confidence": policy.min_confidence,
        "output_language": policy.output_language,
        "task_board": policy.task_board,
        "grounding_max_distance": policy.grounding_max_distance,
        "summary_cluster_depth": policy.summary_cluster_depth,
        "summary_topk_threshold": policy.summary_topk_threshold,
        "summary_cluster_depth_overrides": dict(policy.summary_cluster_depth_overrides),
        "context_limits": asdict(policy.context_limits),
    }
```

Implement `migrate_repo_config` with strict validation, semantic dict comparison, symlink refusal,
same-directory `NamedTemporaryFile(delete=False)`, `chmod(0o600)` where supported, `os.replace`,
and a second strict resolution after the write. Compare merged policy data before/after; Settings
does not change during the call, so equality guarantees effective-policy equivalence. Delete only
a destination created by the current call when equivalence fails, and return the post-write
`data`/`meta` in `MigrationResult`. The write path follows:

```python
def migrate_repo_config(repo, ref, fetch_repo_yaml, *, config_root=None):
    repo = normalize_repo(repo)
    root = config_root or reviewer_config_root()
    source_text = fetch_repo_yaml(ref)
    candidate = _read_mapping(source_text, ".review.yml")
    if not candidate:
        raise HomeConfigError(".review.yml отсутствует или пуст")
    credential = _credential_path(candidate)
    if credential:
        raise HomeConfigError(
            f".review.yml: credential key {'.'.join(credential)} нельзя мигрировать"
        )
    before_data, _ = resolve_policy_data(
        repo, ref, fetch_repo_yaml, config_root=root, strict_home=True
    )
    destination = home_repo_path(repo, root)
    if destination.is_symlink():
        raise HomeConfigError(f"{destination}: symlink запрещён")
    if destination.exists():
        existing = _read_mapping(destination.read_text(encoding="utf-8"), str(destination))
        if _credential_path(existing):
            raise HomeConfigError(f"{destination}: credential key запрещён")
        if existing != candidate:
            conflicts = tuple(sorted(set(existing) | set(candidate)))
            return MigrationResult(destination, False, False, conflicts, before_data, _empty_meta())
        data, meta = resolve_policy_data(
            repo, ref, fetch_repo_yaml, config_root=root, strict_home=True
        )
        return MigrationResult(destination, False, True, (), data, meta)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=destination.parent, delete=False
        ) as handle:
            handle.write(source_text)
            temp_path = Path(handle.name)
        temp_path.chmod(0o600)
        os.replace(temp_path, destination)
        after_data, meta = resolve_policy_data(
            repo, ref, fetch_repo_yaml, config_root=root, strict_home=True
        )
        if after_data != before_data:
            destination.unlink()
            raise HomeConfigError("effective config изменился после миграции")
        return MigrationResult(destination, True, False, (), after_data, meta)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
```

Define `_empty_meta()` as `ResolutionMeta({}, {}, ())`. Compute `conflicting_keys` more precisely
as keys whose presence or value differs:

```python
tuple(sorted(
    key for key in set(existing) | set(candidate)
    if existing.get(key, _MISSING) != candidate.get(key, _MISSING)
))
```

- [ ] **Step 6: Run resolver tests and Ruff**

Run:

```bash
uv run pytest -q -p no:cacheprovider tests/config/test_layers.py
uv run ruff check reviewer/config/layers.py tests/config/test_layers.py
```

Expected: all tests pass and Ruff reports no violations.

- [ ] **Step 7: Commit Task 2**

```bash
git add reviewer/config/layers.py tests/config/test_layers.py
git commit -m "feat: resolve layered repository policy"
```

---

### Task 3: Review Preparation, Index Wiring, and Session Persistence

**Files:**
- Modify: `reviewer/services/review_service.py:82-101,180-350`
- Modify: `reviewer/entrypoints/cli.py:490-525`
- Modify: `reviewer/mcp/session_serde.py`
- Modify: `tests/services/test_prepare_ignore.py`
- Modify: `tests/entrypoints/test_index_ignore.py`
- Modify: `tests/mcp/test_session_serde.py`

**Interfaces:**
- Consumes: Task 2 `resolve_policy_data`, `ResolutionMeta.as_dict`
- Produces: `PreparedReview.config_sources: dict`
- Produces: home-aware `reviewer index`
- Preserves: exact committed read at `prq.base_sha`, session JSON backward compatibility

- [ ] **Step 1: Add failing review and index tests for home-only policy**

In `tests/services/test_prepare_ignore.py`, add:

```python
def test_prepare_uses_home_policy_without_committed_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    home = tmp_path / "rag-reviewer/repos/o/r.yml"
    home.parent.mkdir(parents=True)
    home.write_text(
        "paths: {ignore: [vendor]}\nmax_comments: 4\ntask_board: null\n",
        encoding="utf-8",
    )
    captured = {}
    monkeypatch.setattr(rs, "build_overlay", lambda *a, **k: captured.update(ignore=k["ignore"]))
    monkeypatch.setattr(rs, "chunk_python", lambda path, src: [])
    monkeypatch.setattr(rs, "_structural_summary", lambda *a, **k: "")
    vcs = _minimal_vcs_for_prepare()
    vcs.get_file_at_ref.side_effect = (
        lambda path, ref: None if path == ".review.yml" else "def f():\n    pass\n"
    )
    components = MagicMock()
    components.store.get_index_meta.return_value = None

    prepared = rs.ReviewService(_settings(), components).prepare(
        "o", "r", 7, vcs_provider=vcs
    )

    assert captured["ignore"] == ["vendor"]
    assert prepared.policy.max_comments == 4
    assert prepared.config_sources["sources"]["paths"] == "home:repos/o/r.yml"
    committed_calls = [
        call for call in vcs.get_file_at_ref.call_args_list if call.args[0] == ".review.yml"
    ]
    assert committed_calls == [call(".review.yml", "b")]
```

Refactor the existing PR/VCS fixture into `_minimal_vcs_for_prepare()` so both tests use identical
PR data.

In `tests/entrypoints/test_index_ignore.py`, add `tmp_path`/`monkeypatch.setenv` and a second test
where `fake_file_at_ref` returns `None` for `.review.yml` while
`$XDG_CONFIG_HOME/rag-reviewer/repos/o/r.yml` contains `paths.ignore: [vendor]`.

- [ ] **Step 2: Add failing session provenance tests**

Update `_prepared` in `tests/mcp/test_session_serde.py`:

```python
config_sources={
    "sources": {"paths": "home:repos/o/r.yml"},
    "shadowed": {"paths": [".review.yml"]},
    "warnings": [],
},
```

Add:

```python
def test_from_payload_without_config_sources_is_backward_compatible() -> None:
    payload = json.loads(json.dumps(to_payload(_prepared(_DummyVCS()))))
    payload.pop("config_sources")
    restored = from_payload(payload, _DummyVCS())
    assert restored.config_sources == {}
```

- [ ] **Step 3: Run the focused tests and confirm the red state**

Run:

```bash
uv run pytest -q -p no:cacheprovider \
  tests/services/test_prepare_ignore.py \
  tests/entrypoints/test_index_ignore.py \
  tests/mcp/test_session_serde.py
```

Expected: home-only assertions and missing `PreparedReview.config_sources` fail.

- [ ] **Step 4: Resolve once in `ReviewService.prepare` and reuse the policy**

Import `field` alongside `dataclass`, then add the defaulted field at the end of
`PreparedReview`:

```python
config_sources: dict = field(default_factory=dict)
```

Immediately after branch validation:

```python
from reviewer.config.layers import resolve_policy_data

policy_data, policy_meta = resolve_policy_data(
    repo,
    prq.base_sha,
    lambda ref: vcs.get_file_at_ref(".review.yml", ref),
)
policy = ReviewPolicy.load_data(self.settings, policy_data)
ignore = policy.ignore
for warning in policy_meta.warnings:
    log.warning("Домашний слой policy пропущен: %s", warning)
```

Delete both old direct `.review.yml` reads and the later `ReviewPolicy.load` call. Pass
`config_sources=policy_meta.as_dict()` into `PreparedReview`.

- [ ] **Step 5: Wire index and session serialization**

In `reviewer/entrypoints/cli.py`, replace direct `ReviewPolicy.from_yaml`:

```python
policy_data, policy_meta = resolve_policy_data(
    repo_id,
    ref,
    lambda selected_ref: file_at_ref(repo, ".review.yml", selected_ref),
)
policy = ReviewPolicy.load_data(s, policy_data)
ignore = policy.ignore
for warning in policy_meta.warnings:
    log.warning("Домашний слой policy пропущен: %s", warning)
```

In `reviewer/mcp/session_serde.py`, include `"config_sources"` in `to_payload` and use:

```python
config_sources=d.get("config_sources", {}),
```

in `from_payload`.

- [ ] **Step 6: Run focused and adjacent regression tests**

Run:

```bash
uv run pytest -q -p no:cacheprovider \
  tests/services/test_prepare_ignore.py \
  tests/services/test_review_service.py \
  tests/entrypoints/test_index_ignore.py \
  tests/mcp/test_session_serde.py
```

Expected: all selected tests pass, including the guard that every `PreparedReview` field is
serialized.

- [ ] **Step 7: Commit Task 3**

```bash
git add reviewer/services/review_service.py reviewer/entrypoints/cli.py \
  reviewer/mcp/session_serde.py tests/services/test_prepare_ignore.py \
  tests/entrypoints/test_index_ignore.py tests/mcp/test_session_serde.py
git commit -m "feat: apply home policy to review and index"
```

---

### Task 4: Unified MCP Policy Resolution and Source Reporting

**Files:**
- Modify: `reviewer/mcp/service.py:720-825`
- Modify: `tests/mcp/test_context_limits_wiring.py`
- Modify: `tests/mcp/test_subsystem_summaries.py`

**Interfaces:**
- Consumes: Task 2 `resolve_policy_data` and Task 1 `ReviewPolicy.load_data`
- Produces: `MCPReviewService._resolve_policy(repo, branch) -> tuple[ReviewPolicy, ResolutionMeta]`
- Preserves: current fail-soft defaults and VCS ownership/closing behavior

- [ ] **Step 1: Add failing home-layer MCP tests**

Add to `tests/mcp/test_context_limits_wiring.py`:

```python
def test_resolve_context_limits_uses_home_repo_layer(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("context_limits: {graph: {hops: 2}}\n", encoding="utf-8")
    s = _settings()
    components = MagicMock()
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = None
    svc = MCPReviewService(s, components, vcs_factory=lambda o, n: vcs)

    limits = svc._resolve_context_limits("o/r", "dev")

    assert limits.graph.hops == 2
```

Add to `tests/mcp/test_subsystem_summaries.py`:

```python
def test_summary_threshold_reports_home_repo_source(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "rag-reviewer/repos/o/r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("summary_topk_threshold: 7\n", encoding="utf-8")
    svc, vcs = _service_for_summary_resolution()
    vcs.get_file_at_ref.return_value = None

    value, source = svc._resolve_summary_topk_threshold("o/r", "main")

    assert value == 7
    assert source == "home:repos/o/r.yml"
```

- [ ] **Step 2: Run tests and confirm the old direct YAML paths fail**

Run:

```bash
uv run pytest -q -p no:cacheprovider \
  tests/mcp/test_context_limits_wiring.py \
  tests/mcp/test_subsystem_summaries.py
```

Expected: new home-layer assertions fail because MCP only reads committed `.review.yml`.

- [ ] **Step 3: Add one shared MCP resolver**

In `reviewer/mcp/service.py`:

```python
def _resolve_policy(self, repo: str, branch: str):
    from reviewer.config.layers import resolve_policy_data
    from reviewer.policy.policy import ReviewPolicy

    owner, name = repo.split("/", 1)
    vcs = None
    try:
        vcs = (
            self._vcs_factory(owner, name)
            if self._vcs_factory
            else self._review_service._create_vcs_provider(owner, name)
        )
        data, meta = resolve_policy_data(
            repo,
            branch,
            lambda ref: vcs.get_file_at_ref(".review.yml", ref),
        )
        for warning in meta.warnings:
            log.warning("Домашний слой policy пропущен: %s", warning)
        return ReviewPolicy.load_data(self.settings, data), meta
    finally:
        if vcs is not None and self._vcs_factory is None:
            try:
                vcs.close()
            except Exception:
                log.warning("_resolve_policy: не удалось закрыть VCS", exc_info=True)
```

Replace parsing in the three wrappers. Each wrapper retains its existing `try/except` and default:

```python
policy, meta = self._resolve_policy(repo, branch)
source = meta.sources.get("summary_topk_threshold", "env")
return policy.summary_topk_threshold, source
```

For depth:

```python
source = meta.sources.get(
    "summary_cluster_depth",
    meta.sources.get("summary_cluster_depth_overrides", "env"),
)
return policy.summary_cluster_depth, policy.summary_cluster_depth_overrides, source
```

For context limits return only `policy.context_limits`.

- [ ] **Step 4: Verify fail-soft, sources, and provider ownership**

Run:

```bash
uv run pytest -q -p no:cacheprovider \
  tests/mcp/test_context_limits_wiring.py \
  tests/mcp/test_subsystem_summaries.py \
  tests/mcp/test_service.py
```

Expected: all selected tests pass; existing network-error tests still return env/default values.

- [ ] **Step 5: Commit Task 4**

```bash
git add reviewer/mcp/service.py tests/mcp/test_context_limits_wiring.py \
  tests/mcp/test_subsystem_summaries.py
git commit -m "feat: resolve MCP policy from home layers"
```

---

### Task 5: `reviewer config show` and Safe Migration CLI

**Files:**
- Modify: `reviewer/config/layers.py`
- Modify: `reviewer/entrypoints/cli.py`
- Create: `tests/entrypoints/test_config_commands.py`
- Modify: `tests/config/test_layers.py`

**Interfaces:**
- Consumes: `resolve_policy_data`, `ReviewPolicy.load_data`, `policy_to_public_data`, `migrate_repo_config`
- Produces: Click commands `reviewer config show` and `reviewer config migrate`
- Produces JSON contract: `repo`, `branch`, `effective`, `sources`, `shadowed`, `warnings`

- [ ] **Step 1: Write failing `config show` tests**

Create `tests/entrypoints/test_config_commands.py`:

```python
import json
from unittest.mock import MagicMock

from click.testing import CliRunner

import reviewer.entrypoints.cli as cli_mod


def _install_fake_vcs(monkeypatch, committed):
    vcs = MagicMock()
    vcs.get_file_at_ref.return_value = committed
    vcs.close = MagicMock()
    components = MagicMock()
    monkeypatch.setattr(cli_mod, "build_components", lambda settings: components)
    monkeypatch.setattr(
        cli_mod.ReviewService,
        "_create_vcs_provider",
        lambda self, owner, name: vcs,
    )
    return vcs


def test_config_show_json_reports_effective_sources_and_shadowing(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    global_path = tmp_path / "rag-reviewer/review.yml"
    repo_path = tmp_path / "rag-reviewer/repos/o/r.yml"
    global_path.parent.mkdir(parents=True)
    repo_path.parent.mkdir(parents=True)
    global_path.write_text("max_comments: 5\npaths: {ignore: [global]}\n", encoding="utf-8")
    repo_path.write_text("paths: {ignore: [home]}\n", encoding="utf-8")
    _install_fake_vcs(monkeypatch, "max_comments: 7\npaths: {ignore: [repo]}\n")

    result = CliRunner().invoke(
        cli_mod.cli,
        ["config", "show", "--repo", "o/r", "--branch", "main", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["effective"]["max_comments"] == 7
    assert payload["effective"]["paths"]["ignore"] == ["home"]
    assert payload["sources"]["paths"] == "home:repos/o/r.yml"
    assert payload["shadowed"]["paths"] == ["home:review.yml", ".review.yml"]
```

Add two tests: malformed home YAML must return non-zero in strict mode; a home file containing
`github_token: do-not-echo` must be skipped with a warning while `config show` succeeds. In the
credential test assert the secret value is absent from both `result.output` and
`repr(result.exception)`.

- [ ] **Step 2: Write failing migrate CLI tests**

Append:

```python
def test_config_migrate_creates_home_file_and_reports_shadowing(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    source = "# repo policy\npaths: {ignore: [vendor]}\n"
    _install_fake_vcs(monkeypatch, source)

    result = CliRunner().invoke(
        cli_mod.cli,
        ["config", "migrate", "--repo", "o/r", "--branch", "main"],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "rag-reviewer/repos/o/r.yml").read_text(
        encoding="utf-8"
    ) == source
    assert "shadowed" in result.output


def test_config_migrate_refuses_conflicting_home_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    destination = tmp_path / "rag-reviewer/repos/o/r.yml"
    destination.parent.mkdir(parents=True)
    destination.write_text("max_comments: 3\n", encoding="utf-8")
    _install_fake_vcs(monkeypatch, "max_comments: 7\n")

    result = CliRunner().invoke(
        cli_mod.cli,
        ["config", "migrate", "--repo", "o/r", "--branch", "main"],
    )

    assert result.exit_code != 0
    assert "max_comments" in result.output
    assert destination.read_text(encoding="utf-8") == "max_comments: 3\n"
```

- [ ] **Step 3: Run CLI tests and confirm commands are missing**

Run:

```bash
uv run pytest -q -p no:cacheprovider tests/entrypoints/test_config_commands.py
```

Expected: failures with `No such command 'config'`.

- [ ] **Step 4: Add report construction and the Click group**

Add a pure helper in `layers.py`:

```python
def build_config_report(
    repo: str,
    branch: str,
    settings,
    data: Mapping[str, object],
    meta: ResolutionMeta,
) -> dict[str, object]:
    policy = ReviewPolicy.load_data(settings, data)
    effective = policy_to_public_data(policy)
    top_level = set(effective)
    sources = {key: meta.sources.get(key, "env") for key in top_level}
    return {
        "repo": normalize_repo(repo),
        "branch": branch,
        "effective": effective,
        "sources": sources,
        "shadowed": {k: list(v) for k, v in meta.shadowed.items()},
        "warnings": list(meta.warnings),
    }
```

Add the CLI group:

```python
@cli.group("config")
def config_group() -> None:
    """Inspect and migrate layered repository policy."""
```

Both subcommands must:

1. Build `Settings` and components.
2. Normalize `--repo`.
3. Default branch to `settings.primary_branch()`.
4. Create a VCS provider through `ReviewService`.
5. Call strict resolver/migration.
6. Close VCS and component stores in `finally`.
7. Convert `HomeConfigError`, YAML errors, and migration conflicts to `click.ClickException`.

Use `json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)` for `--json`. Human output
prints one top-level key per block:

```text
paths: {"ignore": ["home"]}
  source: home:repos/o/r.yml
  shadowed: home:review.yml, .review.yml
```

- [ ] **Step 5: Run CLI, resolver, and launcher contract tests**

Run:

```bash
uv run pytest -q -p no:cacheprovider \
  tests/entrypoints/test_config_commands.py \
  tests/config/test_layers.py \
  tests/entrypoints/test_launcher_contract.py
```

Expected: all selected tests pass and the top-level launcher still exposes the Click command.

- [ ] **Step 6: Commit Task 5**

```bash
git add reviewer/config/layers.py reviewer/entrypoints/cli.py \
  tests/config/test_layers.py tests/entrypoints/test_config_commands.py
git commit -m "feat: inspect and migrate repository config"
```

---

### Task 6: Persist Effective Config Sources in Review History

**Files:**
- Modify: `reviewer/web/schema.sql`
- Modify: `reviewer/web/history.py:94-170,205-270`
- Modify: `reviewer/mcp/service.py:1480-1560`
- Modify: `tests/web/test_history.py`
- Modify: `tests/mcp/test_publish.py`

**Interfaces:**
- Consumes: `PreparedReview.config_sources`
- Produces: `review_runs.config_sources JSONB`
- Preserves: old `_sample_run` callers and old persisted rows by defaulting to `{}`

- [ ] **Step 1: Add failing history and publish tests**

Add to `_sample_run()` in `tests/web/test_history.py`:

```python
"config_sources": {
    "sources": {"paths": "home:repos/owner/repo.yml"},
    "shadowed": {"paths": [".review.yml"]},
    "warnings": [],
},
```

Add an integration assertion to the existing record/get test:

```python
assert result["config_sources"]["sources"]["paths"] == "home:repos/owner/repo.yml"
```

Add a unit test around the fake connection used by `test_record_run_defaults_missing_outcome_keys`
which captures the run row and asserts `config_sources` is JSON-encoded before execute.

In `tests/mcp/test_publish.py`, set `prepared.config_sources` and assert the fake history’s captured
run includes the same object.

- [ ] **Step 2: Run focused tests and confirm missing-column/data failures**

Run:

```bash
uv run pytest -q -p no:cacheprovider \
  tests/mcp/test_publish.py \
  tests/web/test_history.py -m "not integration"
```

Expected: publish metadata assertion fails and the history SQL does not consume
`config_sources`.

- [ ] **Step 3: Add idempotent schema and history SQL wiring**

In `reviewer/web/schema.sql`:

```sql
ALTER TABLE review_runs ADD COLUMN IF NOT EXISTS config_sources JSONB;
```

Include `config_sources` in INSERT, list, and get SELECT statements. Before insert:

```python
run_row.setdefault("config_sources", {})
for field in ("usage", "config_sources"):
    if not isinstance(run_row.get(field), str):
        run_row[field] = json.dumps(run_row.get(field), ensure_ascii=False)
```

The API row converter already handles JSONB driver values; no JSONB index is added.

- [ ] **Step 4: Pass prepared provenance from publish**

In `_record_history`’s run mapping in `reviewer/mcp/service.py`:

```python
"config_sources": p.config_sources,
```

Keep the existing outer fail-soft exception handling unchanged.

- [ ] **Step 5: Run unit and integration history verification**

Run unit tests:

```bash
uv run pytest -q -p no:cacheprovider \
  tests/mcp/test_publish.py \
  tests/web/test_history.py -m "not integration"
```

If the isolated test database is already available, also run:

```bash
uv run pytest -q -p no:cacheprovider -m integration \
  tests/web/test_history.py::test_record_and_get_run
```

Expected: unit tests pass; integration either passes against the configured isolated database or is
reported as not run when infrastructure is unavailable.

- [ ] **Step 6: Commit Task 6**

```bash
git add reviewer/web/schema.sql reviewer/web/history.py reviewer/mcp/service.py \
  tests/web/test_history.py tests/mcp/test_publish.py
git commit -m "feat: audit effective config sources"
```

---

### Task 7: Configure Skill, Bilingual Documentation, and Full Regression

**Files:**
- Modify: `plugin/skills/configure-review/SKILL.md`
- Modify: `README.md`
- Modify: `README.ru.md`

**Interfaces:**
- Consumes: CLI and layer behavior from Tasks 2–6
- Produces: home per-repo as the recommended configure-review write target
- Produces: bilingual operator documentation validated by paired self-review

- [ ] **Step 1: Run a RED pressure scenario against the current skill**

Use `superpowers:writing-skills` with a fresh subagent and this application scenario:

```text
You are configuring reviewer for group/service. The operator wants paths.ignore=[vendor],
does not want to commit configuration, and says “choose the recommended destination”.
Follow the current reviewer_configure-review skill. State the destination, visibility
trade-off, confirmation you request before writing, and any credential handling.
```

Expected RED: the current skill only offers committed `.review.yml`, so it cannot select
`home:repos/group/service.yml` as the recommended no-commit destination. Save the baseline
response verbatim in the task report; do not add a source-grep test.

- [ ] **Step 2: Update configure-review workflow**

Change its frontmatter description and pipeline so it:

1. Resolves canonical repo-id and branch.
2. Presents `home:repos/<owner>/<name>.yml` first as recommended/default.
3. Presents committed `.review.yml` second for team-visible policy.
4. Explains “home: no commit, not visible to team” versus “repo: committed, team-visible”.
5. Reads and preserves unrelated keys/comments in the selected file.
6. Retains explicit confirmation for every `paths.ignore` candidate and before final write.
7. Never reads/writes credentials and never triggers index rebuild itself.

Keep the existing retrieval profile and task-board discovery sections intact.

- [ ] **Step 3: Run the GREEN pressure scenario against the edited skill**

Dispatch a fresh-context subagent with the same scenario and require it to read the edited skill
file from this worktree. The response must:

- select `home:repos/group/service.yml`;
- explain “no commit / not visible to team” versus committed `.review.yml`;
- request confirmation before writing `paths.ignore`;
- refuse credentials in either target.

Save the response verbatim in the task report. If any requirement is missed, tighten only the
relevant skill wording and rerun the same scenario until it passes.

- [ ] **Step 4: Update both README files symmetrically**

Add a “Layered repository policy” subsection near current per-repo `.review.yml` documentation:

```text
ENV
  < $XDG_CONFIG_HOME/rag-reviewer/review.yml
  < committed .review.yml at the selected target ref
  < $XDG_CONFIG_HOME/rag-reviewer/repos/<owner>/<name>.yml
```

Document top-level replacement, `config show`, non-destructive `config migrate`, shadowing, no
worktree reads, credential rejection, and the service-account risk. Update the skill catalog entry
to name both write targets. Human-facing prose earns no change-detector test; verify it by reading
the paired English/Russian sections during self-review.

- [ ] **Step 5: Run focused docs/skill tests**

Run:

```bash
uv run pytest -q -p no:cacheprovider \
  tests/skills/test_configure_review_skill.py \
  tests/docs/test_readme_onboarding.py \
  tests/docs/test_board_provider_docs.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Run the complete non-integration suite and Ruff**

Run:

```bash
uv run pytest -q -p no:cacheprovider
uv run ruff check .
git diff --check
```

Expected: pytest reports zero failures, Ruff reports no violations, and `git diff --check` emits
no output.

- [ ] **Step 7: Commit Task 7**

```bash
git add plugin/skills/configure-review/SKILL.md \
  README.md README.ru.md
git commit -m "docs: explain layered repository config"
```

---

## Final Verification

- [ ] Confirm the commit sequence contains one independently testable commit per task.
- [ ] Run `uv run pytest -q -p no:cacheprovider`.
- [ ] Run `uv run ruff check .`.
- [ ] Run `git diff --check`.
- [ ] Run `git status --short` and verify only pre-existing user-owned untracked files remain.
- [ ] Review the final diff against all twelve PRI-221 acceptance criteria in the approved spec.
- [ ] Use `superpowers:requesting-code-review` before branch integration.
