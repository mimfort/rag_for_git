"""Trusted resolution for committed and user-home review-policy layers."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import stat
import tempfile

import yaml

from reviewer.policy.policy import ReviewPolicy
from reviewer.services.repo_id import normalize_repo


class HomeConfigError(ValueError):
    """A home configuration cannot safely participate in resolution."""


class HomeCredentialError(HomeConfigError):
    """A home configuration contains a credential-shaped key."""


@dataclass(frozen=True)
class ResolutionMeta:
    sources: dict[str, str]
    shadowed: dict[str, tuple[str, ...]]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "sources": dict(self.sources),
            "shadowed": {key: list(value) for key, value in self.shadowed.items()},
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
    return (
        Path(xdg).expanduser() / "rag-reviewer"
        if xdg
        else Path.home() / ".config/rag-reviewer"
    )


def home_repo_path(repo: str, config_root: Path | None = None) -> Path:
    root = config_root or reviewer_config_root()
    return root.joinpath("repos", *normalize_repo(repo).split("/")).with_suffix(".yml")


def _read_mapping(text: str | None, source: str) -> dict[str, object]:
    try:
        data = yaml.safe_load(text) if text else {}
    except yaml.YAMLError as exc:
        raise HomeConfigError(f"{source}: конфиг не прочитан: YAML") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise HomeConfigError(f"{source}: верхний уровень должен быть mapping")
    return data


_SECRET_SUFFIXES = (
    "_token",
    "_password",
    "_secret",
    "_api_key",
    "_private_key",
    "_client_secret",
    "_access_key",
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


def _credential_path(
    value: object,
    prefix: tuple[str, ...] = (),
    ancestors: frozenset[int] = frozenset(),
) -> tuple[str, ...] | None:
    if isinstance(value, Mapping):
        if id(value) in ancestors:
            raise HomeConfigError("циклическая YAML структура запрещена")
        ancestors = ancestors | {id(value)}
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            current = (*prefix, key)
            if key in _secret_names() or key.endswith(_SECRET_SUFFIXES):
                return current
            nested = _credential_path(child, current, ancestors)
            if nested:
                return nested
    elif isinstance(value, list):
        if id(value) in ancestors:
            raise HomeConfigError("циклическая YAML структура запрещена")
        ancestors = ancestors | {id(value)}
        for child in value:
            nested = _credential_path(child, prefix, ancestors)
            if nested:
                return nested
    return None


def resolve_policy_data(
    repo: str,
    ref: str,
    fetch_repo_yaml: Callable[[str], str | None],
    *,
    config_root: Path | None = None,
    strict_home: bool = False,
) -> tuple[dict[str, object], ResolutionMeta]:
    """Resolve global home, committed, and repository home policy layers."""
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
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                return
            data = _read_mapping(path.read_text(encoding="utf-8"), source)
            credential = _credential_path(data)
            if credential:
                raise HomeCredentialError(
                    f"{source}: credential key {'.'.join(credential)} запрещён"
                )
            merge(data, source)
        except FileNotFoundError:
            return
        except HomeCredentialError as exc:
            warnings.append(str(exc))
        except (
            OSError,
            UnicodeError,
            RecursionError,
            yaml.YAMLError,
            HomeConfigError,
        ) as exc:
            wrapped = HomeConfigError(
                f"{source}: конфиг не прочитан: {type(exc).__name__}"
            )
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


def policy_to_public_data(policy: ReviewPolicy) -> dict[str, object]:
    """Return the serialized public fields of an effective review policy."""
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


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _validate_mapping_shape(
    value: object, shape: Mapping[str, Callable[[object], bool]]
) -> None:
    if not isinstance(value, Mapping) or set(value) != set(shape):
        raise TypeError("invalid public mapping")
    if any(not validator(value[key]) for key, validator in shape.items()):
        raise TypeError("invalid public mapping value")


def _validate_public_policy_data(effective: Mapping[str, object]) -> None:
    """Проверить полную публичную форму до любого CLI-rendering."""
    expected = {
        "categories", "enabled_only", "severity_threshold", "paths", "max_comments",
        "min_confidence", "output_language", "task_board", "grounding_max_distance",
        "summary_cluster_depth", "summary_topk_threshold",
        "summary_cluster_depth_overrides", "context_limits",
    }
    if set(effective) != expected:
        raise TypeError("incomplete public policy")
    categories = effective["categories"]
    if not isinstance(categories, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, bool)
        for key, value in categories.items()
    ):
        raise TypeError("invalid categories")
    enabled_only = effective["enabled_only"]
    if not isinstance(enabled_only, list) or not all(isinstance(item, str) for item in enabled_only):
        raise TypeError("invalid enabled_only")
    if effective["severity_threshold"] not in {"low", "medium", "high", "critical"}:
        raise TypeError("invalid severity")
    _validate_mapping_shape(effective["paths"], {"ignore": lambda value: (
        isinstance(value, list) and all(isinstance(item, str) for item in value)
    )})
    if not _is_int(effective["max_comments"]) or not _is_number(effective["min_confidence"]):
        raise TypeError("invalid review limits")
    if not isinstance(effective["output_language"], str):
        raise TypeError("invalid output language")
    if effective["task_board"] is not None and not isinstance(effective["task_board"], Mapping):
        raise TypeError("invalid task board")
    for key in (
        "grounding_max_distance",
        "summary_cluster_depth",
        "summary_topk_threshold",
    ):
        if not _is_int(effective[key]):
            raise TypeError("invalid integer policy value")
    overrides = effective["summary_cluster_depth_overrides"]
    if not isinstance(overrides, Mapping) or any(
        not isinstance(key, str) or not _is_int(value) for key, value in overrides.items()
    ):
        raise TypeError("invalid summary overrides")
    _validate_mapping_shape(effective["context_limits"], {
        "search_codebase": lambda value: _validate_context_limits(
            value,
            {
                "floor": _is_int,
                "ceiling": _is_int,
                "ratio": _is_number,
                "abs_floor": _is_number,
                "candidate_pool": _is_int,
                "ann_distance_max": _is_number,
            },
        ),
        "search_tasks": lambda value: _validate_context_limits(
            value, {"floor": _is_int, "ceiling": _is_int}
        ),
        "graph": lambda value: _validate_context_limits(
            value, {"hops": _is_int, "callers_topk": _is_int}
        ),
    })
    json.dumps(effective, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _validate_context_limits(
    value: object, shape: Mapping[str, Callable[[object], bool]]
) -> bool:
    try:
        _validate_mapping_shape(value, shape)
    except TypeError:
        return False
    return True


def build_config_report(
    repo: str,
    branch: str,
    settings,
    data: Mapping[str, object],
    meta: ResolutionMeta,
) -> dict[str, object]:
    """Собрать безопасный диагностический отчёт об эффективной policy."""
    try:
        policy = ReviewPolicy.load_data(settings, data)
        effective = policy_to_public_data(policy)
        _validate_public_policy_data(effective)
    except (AttributeError, KeyError, OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise HomeConfigError("effective policy: недопустимые значения") from exc
    sources = {key: meta.sources.get(key, "env") for key in effective}
    return {
        "repo": normalize_repo(repo),
        "branch": branch,
        "effective": effective,
        "sources": sources,
        "shadowed": {key: list(value) for key, value in meta.shadowed.items()},
        "warnings": list(meta.warnings),
    }


def _empty_meta() -> ResolutionMeta:
    return ResolutionMeta({}, {}, ())


def _read_destination_mapping(path: Path, source: str) -> dict[str, object] | None:
    """Прочитать существующий regular destination, не следуя symlink на POSIX."""
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise HomeConfigError(f"{source}: symlink запрещён")
    except FileNotFoundError:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HomeConfigError(f"{source}: конфиг не прочитан: {type(exc).__name__}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HomeConfigError(f"{source}: regular file обязателен")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            return _read_mapping(handle.read(), source)
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _existing_migration_result(
    destination: Path,
    existing: dict[str, object],
    candidate: dict[str, object],
    repo: str,
    ref: str,
    fetch_repo_yaml: Callable[[str], str | None],
    root: Path,
    before_data: dict[str, object],
) -> MigrationResult:
    if _credential_path(existing):
        raise HomeConfigError(f"home:repos/{repo}.yml: credential key запрещён")
    if existing != candidate:
        conflicts = tuple(sorted(
            key
            for key in set(existing) | set(candidate)
            if existing.get(key, _MISSING) != candidate.get(key, _MISSING)
        ))
        return MigrationResult(
            destination, False, False, conflicts, before_data, _empty_meta()
        )
    data, meta = resolve_policy_data(
        repo, ref, fetch_repo_yaml, config_root=root, strict_home=True
    )
    return MigrationResult(destination, False, True, (), data, meta)


def _path_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat_result = os.lstat(path)
    except FileNotFoundError:
        return None
    return stat_result.st_dev, stat_result.st_ino


def _remove_owned_destination(destination: Path, identity: tuple[int, int]) -> None:
    """Удалить destination только пока оно всё ещё inode нашей публикации."""
    if _path_identity(destination) == identity:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass


def _publish_new_config(destination: Path, content: str) -> tuple[int, int] | None:
    """Опубликовать новый config без перезаписи уже созданного destination."""
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=destination.parent, delete=False
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        temp_path.chmod(0o600)
        try:
            os.link(temp_path, destination)
        except FileExistsError:
            return None
        identity = _path_identity(destination)
        if identity is None:
            raise HomeConfigError("destination исчез после публикации")
        return identity
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def migrate_repo_config(
    repo: str,
    ref: str,
    fetch_repo_yaml: Callable[[str], str | None],
    *,
    config_root: Path | None = None,
) -> MigrationResult:
    """Copy a safe committed policy to its repository-scoped home layer."""
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
    source = f"home:repos/{repo}.yml"
    existing = _read_destination_mapping(destination, source)
    if existing is not None:
        return _existing_migration_result(
            destination, existing, candidate, repo, ref, fetch_repo_yaml, root, before_data
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    identity = _publish_new_config(destination, source_text or "")
    if identity is None:
        existing = _read_destination_mapping(destination, source)
        if existing is None:
            raise HomeConfigError(f"{source}: destination исчез во время миграции")
        return _existing_migration_result(
            destination, existing, candidate, repo, ref, fetch_repo_yaml, root, before_data
        )
    try:
        after_data, meta = resolve_policy_data(
            repo, ref, fetch_repo_yaml, config_root=root, strict_home=True
        )
        if after_data != before_data:
            raise HomeConfigError("effective config изменился после миграции")
    except Exception:
        _remove_owned_destination(destination, identity)
        raise
    return MigrationResult(destination, True, False, (), after_data, meta)
