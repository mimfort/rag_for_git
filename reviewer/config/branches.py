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
from typing import TYPE_CHECKING, Literal

import yaml

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
RepositoryBlockAction = Literal["create", "append", "noop"]


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
    raw_primary = block.get("primary_branch")

    # Если index_branches не задан, но задан primary_branch, используем его как
    # единственный элемент index
    if raw_index is None and raw_primary is not None:
        if not isinstance(raw_primary, str) or not raw_primary.strip():
            raise HomePolicyError(
                f"{source}: repository.primary_branch должен быть непустой строкой"
            )
        primary = raw_primary.strip()
        return primary, (primary,)

    # Если index_branches не задан и primary_branch не задан, это ошибка
    if raw_index is None:
        raise HomePolicyError(
            f"{source}: repository.index_branches должен быть непустым списком"
        )

    if not isinstance(raw_index, list) or not raw_index:
        raise HomePolicyError(
            f"{source}: repository.index_branches должен быть непустым списком"
        )
    index: list[str] = []
    for item in raw_index:
        if not isinstance(item, str) or not item.strip():
            raise HomePolicyError(
                f"{source}: repository.index_branches содержит не-строку или пустую "
                "строку"
            )
        name = item.strip()
        if name in index:
            raise HomePolicyError(
                f"{source}: repository.index_branches содержит дубль {name!r}"
            )
        index.append(name)

    # Если primary_branch не задан, используем первый элемент index
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


@dataclass(frozen=True)
class BranchMigrationResult:
    """Итог переноса REVIEW_BRANCHES в домашний per-repo слой."""

    path: Path
    created: bool
    noop: bool


def render_repository_block(
    primary: str,
    index: tuple[str, ...],
    *,
    migrated: bool = False,
) -> str:
    """Сформировать YAML-блок repository через safe_dump.

    Имена веток попадают в YAML как есть — легальное git-имя вроде
    ``feature{x}`` ломает flow-список без кавычек, а ``2.0``/``on``/``no``
    парсится как не-строка. ``safe_dump`` сам кавычит там, где нужно.
    """
    body = yaml.safe_dump(
        {
            BRANCHES_KEY: {
                "primary_branch": primary,
                "index_branches": list(index),
            }
        },
        allow_unicode=True,
        sort_keys=False,
    )
    if not migrated:
        return body
    return "# Отслеживаемые ветки репозитория (перенесено из REVIEW_BRANCHES).\n" + body


def publish_repository_block(
    path: Path,
    source: str,
    block: str,
) -> RepositoryBlockAction:
    """Создать или дописать repository-блок, не меняя существующий блок."""
    try:
        existing_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "x", encoding="utf-8") as handle:
                handle.write(block)
        except FileExistsError:
            # Параллельный процесс уже создал файл: не рискуем менять его вслепую.
            return "noop"
        return "create"

    if BRANCHES_KEY in _read_mapping(existing_text, source):
        return "noop"

    if not existing_text:
        updated = block
    else:
        separator = "\n" if existing_text.endswith("\n") else "\n\n"
        updated = existing_text + separator + block
    path.write_text(updated, encoding="utf-8")
    return "append"


def migrate_repo_branches(
    repo: str,
    *,
    settings: Settings,
    config_root: Path | None = None,
) -> BranchMigrationResult:
    """Перенести env REVIEW_BRANCHES в repository.index_branches домашнего слоя.

    `.env` не изменяется: env остаётся рабочим фолбэком. Существующий блок
    `repository` не перезаписывается — это noop. Ветки берутся не напрямую из
    env, а через ``resolve_repo_branches``: если эффективные ветки уже заданы
    ЛЮБЫМ домашним слоем (per-repo ИЛИ глобальный ``home:review.yml``), это
    тоже noop — иначе перенос молча подменил бы эффективные ветки значением
    из env, которое в порядке резолва стоит ниже домашних слоёв.
    """
    repo = normalize_repo(repo)
    root = config_root or reviewer_config_root()
    destination = home_repo_path(repo, root)
    source = f"home:repos/{repo}.yml"
    effective = resolve_repo_branches(repo, settings=settings, config_root=root)
    if effective.source.startswith("home:"):
        return BranchMigrationResult(destination, False, True)
    index = effective.index
    block = render_repository_block(index[0], index, migrated=True)
    action = publish_repository_block(destination, source, block)
    return BranchMigrationResult(
        path=destination,
        created=action == "create",
        noop=action == "noop",
    )


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
