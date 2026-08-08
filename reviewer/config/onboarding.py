"""Локальное обнаружение репозитория и чистый план домашнего repo-конфига."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from reviewer.config.branches import (
    publish_repository_block,
    render_repository_block,
    resolve_repo_branches,
)
from reviewer.config.layers import _read_mapping, home_repo_path, reviewer_config_root
from reviewer.config.settings import Settings
from reviewer.gitutil import remote_default_branch, remote_url, repo_root
from reviewer.services.repo_id import derive_repo_from_remote, normalize_repo

PlanAction = Literal["create", "append", "noop"]


@dataclass(frozen=True)
class RepositoryDetection:
    """Локально обнаруженные repo/primary и происхождение каждого значения."""

    root: Path
    repo: str
    repo_source: str
    primary: str
    primary_source: str


@dataclass(frozen=True)
class RepositoryConfigPlan:
    """Неизменяемый план записи repository-блока домашнего конфига."""

    path: Path
    repo: str
    primary: str
    index: tuple[str, ...]
    repo_source: str
    primary_source: str
    action: PlanAction
    preview: str


def _validate_branches(index: tuple[str, ...], primary: str) -> tuple[str, ...]:
    if not isinstance(primary, str) or not primary.strip():
        raise ValueError("primary branch должна быть непустой строкой")
    if not index:
        raise ValueError("index branches должны быть непустым списком")
    if any(
        not isinstance(branch, str) or not branch.strip() or branch != branch.strip()
        for branch in index
    ):
        raise ValueError("index branches содержат пустое или неочищенное имя")
    if len(index) != len(set(index)):
        raise ValueError("index branches содержат дубли")
    if primary not in index:
        raise ValueError(f"primary branch {primary!r} отсутствует в index branches")
    return index


def parse_branch_csv(raw: str, primary: str) -> tuple[str, ...]:
    """Разобрать CSV отслеживаемых веток и проверить primary/index инварианты."""
    index = tuple(part.strip() for part in raw.split(","))
    return _validate_branches(index, primary)


def detect_repository(
    path: str,
    repo_override: str | None,
    *,
    settings: Settings,
) -> RepositoryDetection | None:
    """Локально определить git root, repo id и primary без сетевых запросов."""
    root_value = repo_root(path)
    if root_value is None:
        return None

    if repo_override is not None:
        repo = normalize_repo(repo_override)
        repo_source = "cli"
    else:
        repo = derive_repo_from_remote(remote_url(root_value) or "")
        if repo is None:
            return None
        repo_source = "git:origin"

    effective = resolve_repo_branches(repo, settings=settings)
    detected_primary = remote_default_branch(root_value)
    return RepositoryDetection(
        root=Path(root_value),
        repo=repo,
        repo_source=repo_source,
        primary=detected_primary or effective.primary,
        primary_source="git:origin/HEAD" if detected_primary else effective.source,
    )


def plan_repository_config(
    detection: RepositoryDetection,
    *,
    settings: Settings,
    primary: str | None = None,
    index: tuple[str, ...] | None = None,
    config_root: Path | None = None,
) -> RepositoryConfigPlan:
    """Вычислить create/append/noop и preview, ничего не записывая на диск."""
    repo = normalize_repo(detection.repo)
    root = config_root if config_root is not None else reviewer_config_root()
    path = home_repo_path(repo, root)
    source = f"home:repos/{repo}.yml"
    effective = resolve_repo_branches(repo, settings=settings, config_root=root)

    if effective.source == source:
        selected_primary = effective.primary
        selected_index = effective.index
        action: PlanAction = "noop"
    else:
        selected_primary = primary if primary is not None else detection.primary
        selected_index = index if index is not None else (
            effective.index if selected_primary in effective.index else (selected_primary,)
        )
        _validate_branches(selected_index, selected_primary)
        try:
            existing_text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            action = "create"
        else:
            action = "noop" if "repository" in _read_mapping(
                existing_text,
                source,
            ) else "append"

    preview = render_repository_block(selected_primary, selected_index)
    return RepositoryConfigPlan(
        path=path,
        repo=repo,
        primary=selected_primary,
        index=selected_index,
        repo_source=detection.repo_source,
        primary_source=detection.primary_source,
        action=action,
        preview=preview,
    )


def apply_repository_config(plan: RepositoryConfigPlan) -> None:
    """Применить рассчитанный план; конкурентная запись может дать безопасный noop."""
    if plan.action == "noop":
        return
    action = publish_repository_block(
        plan.path,
        f"home:repos/{plan.repo}.yml",
        plan.preview,
    )
    if action not in {plan.action, "noop"}:
        raise RuntimeError(f"ожидалось действие {plan.action}, выполнено {action}")
