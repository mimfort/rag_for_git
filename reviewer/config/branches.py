"""Резолв отслеживаемых веток репозитория из домашних слоёв конфигурации.

Ветки нужны ДО чтения committed `.review.yml` (чтобы знать, из какой ветки его
читать), поэтому этот резолв намеренно не принимает `fetch_repo_yaml` и не ходит
в сеть: только домашние YAML-файлы и env. Так цикл bootstrap↔`.review.yml`
невозможен конструктивно, а не по договорённости.
"""
from __future__ import annotations

import os
import secrets
import stat
import sys
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from yaml.tokens import (
    BlockMappingStartToken,
    DocumentEndToken,
    FlowEntryToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    FlowSequenceEndToken,
    FlowSequenceStartToken,
)

from reviewer.config.layers import (
    HomeConfigError,
    HomeCredentialError,
    HomePolicyError,
    _credential_path,
    _prepare_destination_parent,
    _publish_new_config,
    _read_mapping,
    _supports_secure_directory_walk,
    home_repo_path,
    reviewer_config_root,
)
from reviewer.services.repo_id import normalize_repo

if TYPE_CHECKING:
    from reviewer.config.settings import Settings

BRANCHES_KEY = "repository"
RepositoryBlockAction = Literal["create", "append", "noop"]
_PUBLISH_ATTEMPTS = 3

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback relies on identity checks.
    fcntl = None


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


def _load_layer(
    path: Path,
    source: str,
    *,
    root: Path,
    parent_segments: tuple[str, ...],
) -> tuple[str, tuple[str, ...]] | None:
    """Прочитать блок `repository` одного домашнего файла, либо None."""
    existing = _read_home_file(path, source, root, parent_segments)
    if existing is None:
        return None
    data = existing.data
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


@dataclass(frozen=True)
class _LockedDestination:
    text: str
    data: dict[str, object]
    identity: tuple[int, int]
    mode: int


@dataclass(frozen=True)
class _ExistingParent:
    exists: bool
    descriptor: int | None


@contextmanager
def _open_existing_parent(
    root: Path,
    segments: tuple[str, ...],
    source: str,
):
    """Открыть существующий parent без создания и symlink-переходов ниже root."""
    if not _supports_secure_directory_walk():
        try:
            root_metadata = root.stat()
        except FileNotFoundError:
            yield _ExistingParent(False, None)
            return
        except OSError as exc:
            raise HomeConfigError(
                f"{source}: parent не проверен: {type(exc).__name__}"
            ) from None
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise HomeConfigError(f"{source}: config root не является directory")
        current = root
        for segment in segments:
            current /= segment
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                yield _ExistingParent(False, None)
                return
            except OSError as exc:
                raise HomeConfigError(
                    f"{source}: parent не проверен: {type(exc).__name__}"
                ) from None
            if stat.S_ISLNK(metadata.st_mode):
                raise HomeConfigError(f"{source}: symlink в destination запрещён")
            if not stat.S_ISDIR(metadata.st_mode):
                raise HomeConfigError(f"{source}: parent destination не является directory")
        yield _ExistingParent(True, None)
        return

    descriptor = -1
    try:
        try:
            descriptor = os.open(
                root,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
            )
        except FileNotFoundError:
            yield _ExistingParent(False, None)
            return
        except OSError as exc:
            raise HomeConfigError(
                f"{source}: parent не проверен: {type(exc).__name__}"
            ) from None
        for segment in segments:
            try:
                child = os.open(
                    segment,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                yield _ExistingParent(False, None)
                return
            except OSError:
                raise HomeConfigError(
                    f"{source}: symlink или non-directory parent запрещён"
                ) from None
            try:
                metadata = os.fstat(child)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise HomeConfigError(
                        f"{source}: parent destination не является directory"
                    )
            except Exception:
                try:
                    os.close(child)
                except OSError:
                    pass
                raise
            try:
                os.close(descriptor)
            except OSError:
                try:
                    os.close(child)
                except OSError:
                    pass
                raise HomeConfigError(f"{source}: parent descriptor не закрыт") from None
            descriptor = child
        yield _ExistingParent(True, descriptor)
    finally:
        if descriptor != -1:
            try:
                os.close(descriptor)
            except OSError as exc:
                if sys.exc_info()[0] is None:
                    raise HomeConfigError(
                        f"{source}: parent descriptor не закрыт: {type(exc).__name__}"
                    ) from None


def _destination_context(path: Path, source: str) -> tuple[Path, str]:
    prefix = "home:repos/"
    suffix = ".yml"
    if not source.startswith(prefix) or not source.endswith(suffix):
        raise HomeConfigError("repository destination: source имеет недопустимый формат")
    try:
        repo = normalize_repo(source[len(prefix) : -len(suffix)])
        root = path.parents[len(repo.split("/"))]
    except (IndexError, ValueError):
        raise HomeConfigError("repository destination: путь имеет недопустимый формат") from None
    if home_repo_path(repo, root) != path:
        raise HomeConfigError("repository destination: путь не соответствует source")
    return root, repo


@contextmanager
def _read_locked_destination(
    path: Path,
    source: str,
    *,
    parent_fd: int | None,
):
    """Прочитать и удерживать regular destination без следования symlink."""
    try:
        before = (
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if parent_fd is not None
            else os.lstat(path)
        )
    except FileNotFoundError:
        yield None
        return
    except OSError as exc:
        raise HomeConfigError(
            f"{source}: конфиг не прочитан: {type(exc).__name__}"
        ) from None
    if stat.S_ISLNK(before.st_mode):
        raise HomeConfigError(f"{source}: symlink запрещён")
    if not stat.S_ISREG(before.st_mode):
        raise HomeConfigError(f"{source}: destination должен быть regular file")

    descriptor = -1
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = (
            os.open(path.name, flags, dir_fd=parent_fd)
            if parent_fd is not None
            else os.open(path, flags)
        )
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        opened = os.fstat(descriptor)
        fresh = (
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if parent_fd is not None
            else os.lstat(path)
        )
        identities = {
            (before.st_dev, before.st_ino),
            (opened.st_dev, opened.st_ino),
            (fresh.st_dev, fresh.st_ino),
        }
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(fresh.st_mode)
            or len(identities) != 1
        ):
            raise HomeConfigError(f"{source}: race при чтении destination")
        with os.fdopen(os.dup(descriptor), encoding="utf-8", newline="") as handle:
            text = handle.read()
        after = (
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if parent_fd is not None
            else os.lstat(path)
        )
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise HomeConfigError(f"{source}: race при чтении destination")
        yield _LockedDestination(
            text=text,
            data=_read_mapping(text, source),
            identity=(opened.st_dev, opened.st_ino),
            mode=stat.S_IMODE(opened.st_mode),
        )
    except HomeConfigError:
        raise
    except (OSError, UnicodeError) as exc:
        raise HomeConfigError(
            f"{source}: конфиг не прочитан: {type(exc).__name__}"
        ) from None
    finally:
        if descriptor != -1:
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            except OSError as exc:
                if sys.exc_info()[0] is None:
                    raise HomeConfigError(
                        f"{source}: конфиг не закрыт: {type(exc).__name__}"
                    ) from None


def _read_home_file(
    path: Path,
    source: str,
    root: Path,
    parent_segments: tuple[str, ...],
) -> _LockedDestination | None:
    with _open_existing_parent(root, parent_segments, source) as parent:
        if not parent.exists:
            return None
        with _read_locked_destination(
            path,
            source,
            parent_fd=parent.descriptor,
        ) as existing:
            return existing


def read_repository_config_file(path: Path, source: str) -> str | None:
    """Без записи и symlink-переходов прочитать домашний repo config."""
    root, repo = _destination_context(path, source)
    existing = _read_home_file(
        path,
        source,
        root,
        ("repos", *repo.split("/")[:-1]),
    )
    return None if existing is None else existing.text


def _flow_mapping_end(tokens: list[object]) -> tuple[int, bool] | None:
    flow_depth = 0
    root_flow = False
    for position, token in enumerate(tokens):
        if isinstance(token, (FlowMappingStartToken, FlowSequenceStartToken)):
            if flow_depth == 0:
                if not isinstance(token, FlowMappingStartToken):
                    return None
                root_flow = True
            flow_depth += 1
        elif isinstance(token, (FlowMappingEndToken, FlowSequenceEndToken)):
            flow_depth -= 1
            if root_flow and flow_depth == 0:
                return (
                    token.start_mark.index,
                    isinstance(tokens[position - 1], FlowEntryToken),
                )
        elif flow_depth == 0 and isinstance(token, BlockMappingStartToken):
            return None
    return None


def _append_repository_candidate(
    existing: _LockedDestination,
    block: str,
    source: str,
) -> str:
    block_data = _read_mapping(block, source)
    if BRANCHES_KEY not in block_data:
        raise HomeConfigError(f"{source}: repository block отсутствует")
    repository = block_data[BRANCHES_KEY]
    tokens = list(yaml.scan(existing.text))
    flow_mapping = _flow_mapping_end(tokens)
    if flow_mapping is not None:
        flow_end, has_trailing_comma = flow_mapping
        flow = yaml.safe_dump(
            {BRANCHES_KEY: repository},
            allow_unicode=True,
            default_flow_style=True,
            sort_keys=False,
            width=10**9,
        ).strip()
        prefix = existing.text[:flow_end]
        separator = " " if has_trailing_comma else ", " if existing.data else ""
        candidate = prefix + separator + flow[1:-1] + existing.text[flow_end:]
    else:
        document_end = next(
            (
                token.start_mark.index
                for token in tokens
                if isinstance(token, DocumentEndToken)
            ),
            None,
        )
        insertion = len(existing.text) if document_end is None else document_end
        prefix = existing.text[:insertion]
        suffix = existing.text[insertion:]
        if not prefix:
            separator = ""
        elif prefix.endswith("\n"):
            separator = "\n" if document_end is None else ""
        else:
            separator = "\n\n" if document_end is None else "\n"
        candidate = prefix + separator + block + suffix

    parsed = _read_mapping(candidate, source)
    if BRANCHES_KEY not in parsed:
        raise HomeConfigError(f"{source}: repository block не опубликован")
    return candidate


def _replace_destination(
    path: Path,
    content: str,
    source: str,
    existing: _LockedDestination,
    *,
    parent_fd: int | None,
) -> None:
    temp_name: str | None = None
    temp_path: Path | None = None
    descriptor = -1
    try:
        if parent_fd is not None:
            temp_name = f".reviewer-repository-{secrets.token_hex(12)}"
            descriptor = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                existing.mode,
                dir_fd=parent_fd,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            fresh = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (fresh.st_dev, fresh.st_ino) != existing.identity:
                raise HomeConfigError(f"{source}: race при публикации destination")
            os.replace(
                temp_name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temp_name = None
            return

        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        temp_path.chmod(existing.mode)
        fresh = os.lstat(path)
        if (fresh.st_dev, fresh.st_ino) != existing.identity:
            raise HomeConfigError(f"{source}: race при публикации destination")
        os.replace(temp_path, path)
        temp_path = None
    except HomeConfigError:
        raise
    except OSError as exc:
        raise HomeConfigError(
            f"{source}: публикация не выполнена: {type(exc).__name__}"
        ) from None
    finally:
        if descriptor != -1:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temp_name is not None and parent_fd is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                if sys.exc_info()[0] is None:
                    raise HomeConfigError(
                        f"{source}: очистка temporary file: {type(exc).__name__}"
                    ) from None
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                if sys.exc_info()[0] is None:
                    raise HomeConfigError(
                        f"{source}: очистка temporary file: {type(exc).__name__}"
                    ) from None


def publish_repository_block(
    path: Path,
    source: str,
    block: str,
) -> RepositoryBlockAction:
    """Создать или дописать repository-блок, не меняя существующий блок."""
    root, repo = _destination_context(path, source)
    block_data = _read_mapping(block, source)
    if BRANCHES_KEY not in block_data:
        raise HomeConfigError(f"{source}: repository block отсутствует")
    with _prepare_destination_parent(root, repo, source) as parent:
        for _attempt in range(_PUBLISH_ATTEMPTS):
            with _read_locked_destination(
                path,
                source,
                parent_fd=parent.descriptor,
            ) as existing:
                if existing is None:
                    if _publish_new_config(
                        path,
                        block,
                        source,
                        parent_fd=parent.descriptor,
                    ):
                        return "create"
                    continue
                if BRANCHES_KEY in existing.data:
                    return "noop"
                candidate = _append_repository_candidate(existing, block, source)
                _replace_destination(
                    path,
                    candidate,
                    source,
                    existing,
                    parent_fd=parent.descriptor,
                )
                return "append"
    raise HomeConfigError(f"{source}: race при публикации destination")


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
        (
            home_repo_path(repo, root),
            f"home:repos/{repo}.yml",
            ("repos", *repo.split("/")[:-1]),
        ),
        (root / "review.yml", "home:review.yml", ()),
    )
    for path, source, parent_segments in layers:
        parsed = _load_layer(
            path,
            source,
            root=root,
            parent_segments=parent_segments,
        )
        if parsed is not None:
            primary, index = parsed
            return RepoBranches(primary=primary, index=index, source=source)
    env_index = settings.review_branches_list()
    source = "env" if settings.review_branches.strip() else "default"
    return RepoBranches(primary=env_index[0], index=tuple(env_index), source=source)
