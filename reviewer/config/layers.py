"""Trusted resolution for committed and user-home review-policy layers."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from functools import lru_cache
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
    data = yaml.safe_load(text) if text else {}
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


def build_config_report(
    repo: str,
    branch: str,
    settings,
    data: Mapping[str, object],
    meta: ResolutionMeta,
) -> dict[str, object]:
    """Собрать безопасный диагностический отчёт об эффективной policy."""
    policy = ReviewPolicy.load_data(settings, data)
    effective = policy_to_public_data(policy)
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
    if destination.is_symlink():
        raise HomeConfigError(f"{destination}: symlink запрещён")
    if destination.exists():
        existing = _read_mapping(destination.read_text(encoding="utf-8"), str(destination))
        if _credential_path(existing):
            raise HomeConfigError(f"{destination}: credential key запрещён")
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

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=destination.parent, delete=False
        ) as handle:
            handle.write(source_text or "")
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
