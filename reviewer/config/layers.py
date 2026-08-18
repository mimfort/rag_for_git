"""Trusted resolution for committed and user-home review-policy layers."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile

import yaml

from reviewer.config.deepmerge import ATOMIC_KEYS, leaf_paths, merge_layer
from reviewer.config.fetch_errors import classify_fetch_error
from reviewer.config.task_board import normalize_task_board_config
from reviewer.policy.policy import ReviewPolicy
from reviewer.services.repo_id import normalize_repo


class HomeConfigError(ValueError):
    """A home configuration cannot safely participate in resolution."""


class HomeCredentialError(HomeConfigError):
    """A home configuration contains a credential-shaped key."""


class HomePolicyError(HomeConfigError):
    """A home configuration contains an invalid value for a known policy key."""


@dataclass(frozen=True)
class SkippedLayer:
    """Слой политики, который существовал (или мог существовать), но не применён.

    Отсутствие слоя записи не создаёт: фетчер, вернувший None, означает «слоя
    нет в репозитории», а запись здесь — «слой не прочитан» (PRI-234).
    """

    layer: str
    repo: str
    ref: str | None
    category: str
    transport: str | None = None
    http_status: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer,
            "repo": self.repo,
            "ref": self.ref,
            "category": self.category,
            "transport": self.transport,
            "http_status": self.http_status,
        }


@dataclass(frozen=True)
class ResolutionMeta:
    sources: dict[str, str]
    shadowed: dict[str, tuple[str, ...]]
    warnings: tuple[str, ...]
    # Дефолт обязателен: frozen dataclass без него сломал бы _empty_meta()
    # и существующие конструкторы в тестах.
    skipped: tuple[SkippedLayer, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "sources": dict(self.sources),
            "shadowed": {key: list(value) for key, value in self.shadowed.items()},
            "warnings": list(self.warnings),
            "skipped": [item.as_dict() for item in self.skipped],
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
    segments = normalize_repo(repo).split("/")
    return root.joinpath("repos", *segments[:-1], f"{segments[-1]}.yml")


def _read_mapping(text: str | None, source: str) -> dict[str, object]:
    try:
        data = yaml.safe_load(text) if text else {}
    except (yaml.YAMLError, ValueError, TypeError, ArithmeticError):
        # PyYAML-конструкторы implicit-скаляров (timestamp/int/float) бросают
        # голый ValueError/TypeError/ArithmeticError, не подкласс YAMLError
        # (например `2020-13-45` -> ValueError('month must be in 1..12')).
        raise HomeConfigError(f"{source}: конфиг не прочитан: YAML") from None
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

# Префикс warning'а «коммиченный слой не прочитан» (PRI-234 fail-soft): вынесен
# как модульная константа, чтобы потребители вроде sync_board могли отличить
# этот НЕ блокирующий warning от остальных (credential/invalid/malformed
# домашнего слоя, игнор ключа repository) без матчинга произвольного текста
# чужого модуля.
COMMITTED_UNREAD_WARNING_PREFIX = ".review.yml: слой не прочитан ("


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


def _validate_context_limits_layer(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, Mapping):
        return False
    sections: dict[str, Mapping[str, Callable[[object], bool]]] = {
        "search_codebase": {
            "floor": _is_int,
            "ceiling": _is_int,
            "ratio": _is_number,
            "abs_floor": _is_number,
            "candidate_pool": _is_int,
            "ann_distance_max": _is_number,
        },
        "search_tasks": {
            "floor": _is_int,
            "ceiling": _is_int,
        },
        "graph": {
            "hops": _is_int,
            "callers_topk": _is_int,
        },
        "code_section": {
            "max_files": _is_int,
            "max_chunks_per_file": _is_int,
            "chars_per_file": _is_int,
            "max_augmented_files": _is_int,
        },
    }
    for section, shape in sections.items():
        if section not in value or value[section] is None:
            continue
        section_value = value[section]
        if not isinstance(section_value, Mapping):
            return False
        for key, validator in shape.items():
            if key in section_value and not validator(section_value[key]):
                return False
    return True


def _validate_known_policy_data(
    data: Mapping[str, object],
    source: str,
) -> None:
    """Проверить только известные policy-поля, сохранив future keys."""
    try:
        if any(not isinstance(key, str) for key in data):
            raise TypeError
        categories = data.get("categories", _MISSING)
        if categories is not _MISSING and categories is not None and (
            not isinstance(categories, Mapping)
            or any(
                not isinstance(key, str) or not isinstance(value, bool)
                for key, value in categories.items()
            )
        ):
            raise TypeError
        severity = data.get("severity_threshold", _MISSING)
        if severity is not _MISSING and severity not in {
            "low",
            "medium",
            "high",
            "critical",
        }:
            raise TypeError
        paths = data.get("paths", _MISSING)
        if paths is not _MISSING:
            if paths is not None and not isinstance(paths, Mapping):
                raise TypeError
            if isinstance(paths, Mapping) and "ignore" in paths:
                ignore = paths["ignore"]
                if not isinstance(ignore, list) or not all(
                    isinstance(item, str) for item in ignore
                ):
                    raise TypeError
        summary_paths = data.get("summary_paths", _MISSING)
        if summary_paths is not _MISSING:
            if summary_paths is not None and not isinstance(summary_paths, Mapping):
                raise TypeError
            if isinstance(summary_paths, Mapping) and "ignore" in summary_paths:
                ignore = summary_paths["ignore"]
                if ignore is not None and (
                    not isinstance(ignore, list)
                    or not all(isinstance(item, str) for item in ignore)
                ):
                    raise TypeError
        for key in (
            "max_comments",
            "grounding_max_distance",
            "summary_cluster_depth",
            "summary_topk_threshold",
        ):
            if key in data and not _is_int(data[key]):
                raise TypeError
        if "min_confidence" in data and not _is_number(data["min_confidence"]):
            raise TypeError
        if "output_language" in data and not isinstance(data["output_language"], str):
            raise TypeError
        if "task_board" in data:
            normalize_task_board_config(data["task_board"])
        overrides = data.get("summary_cluster_depth_overrides", _MISSING)
        if overrides is not _MISSING:
            if overrides is not None and not isinstance(overrides, Mapping):
                raise TypeError
            if isinstance(overrides, Mapping) and any(
                not isinstance(key, str) or not _is_int(value)
                for key, value in overrides.items()
            ):
                raise TypeError
        if "context_limits" in data and not _validate_context_limits_layer(
            data["context_limits"]
        ):
            raise TypeError
    except (AttributeError, KeyError, OverflowError, RecursionError, TypeError, ValueError):
        raise HomePolicyError(
            f"{source}: policy содержит недопустимые значения"
        ) from None


def resolve_policy_data(
    repo: str,
    ref: str,
    fetch_repo_yaml: Callable[[str], str | None],
    *,
    config_root: Path | None = None,
    strict_home: bool = False,
    strict_committed: bool = False,
) -> tuple[dict[str, object], ResolutionMeta]:
    """Resolve global home, committed, and repository home policy layers.

    `strict_committed` управляет ТОЛЬКО коммиченным слоем и намеренно отделён
    от `strict_home`: `config show` вызывает резолвер со `strict_home=True`, и
    расширение того флага заставило бы его падать там, где он обязан печатать
    эффективную политику из домашних слоёв (PRI-234).
    """
    # Локальный импорт: branches.py импортирует layers.py, модульный импорт
    # создал бы цикл.
    from reviewer.config.branches import BRANCHES_KEY

    repo = normalize_repo(repo)
    root = config_root or reviewer_config_root()
    merged: dict[str, object] = {}
    sources: dict[str, str] = {}
    shadowed: dict[str, list[str]] = {}
    warnings: list[str] = []
    skipped: list[SkippedLayer] = []

    def record_skip(
        layer: str,
        category: str,
        *,
        ref_value: str | None = None,
        transport: str | None = None,
        http_status: int | None = None,
    ) -> None:
        skipped.append(
            SkippedLayer(layer, repo, ref_value, category, transport, http_status)
        )

    def merge(data: Mapping[str, object], source: str) -> None:
        merge_layer(merged, sources, shadowed, data, source)

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
            _validate_known_policy_data(data, source)
            merge({k: v for k, v in data.items() if k != BRANCHES_KEY}, source)
        except FileNotFoundError:
            return
        except HomeCredentialError as exc:
            record_skip(source, "credential")
            warnings.append(str(exc))
        except HomePolicyError as exc:
            record_skip(source, "invalid")
            if strict_home:
                raise
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
            record_skip(source, "malformed")
            if strict_home:
                raise wrapped from None
            warnings.append(str(wrapped))

    def merge_committed() -> None:
        """Прочитать коммиченный слой, не обнуляя уже смерженные домашние.

        Два раздельных try: сбой доставки (сеть, токен, 404) и сбой разбора
        уже полученного текста — разные категории, но обе fail-soft, как у
        merge_home. Диагностик собирается только из структурированных полей:
        текст исключения VCS-клиента содержит URL и токен.
        """
        def fail(category: str, transport: str | None, http_status: int | None) -> None:
            record_skip(
                ".review.yml",
                category,
                ref_value=ref,
                transport=transport,
                http_status=http_status,
            )
            message = (
                f"{COMMITTED_UNREAD_WARNING_PREFIX}repo={repo}, ref={ref}, "
                f"category={category}, transport={transport}, "
                f"http_status={http_status})"
            )
            warnings.append(message)
            if strict_committed:
                # from None обязателен: цепочка исключений вынесла бы URL
                # VCS-клиента в трейсбек.
                raise HomeConfigError(message) from None

        try:
            text = fetch_repo_yaml(ref)
        except Exception as exc:  # noqa: BLE001 — сбой доставки слоя не должен
            # уничтожать резолв целиком (PRI-234)
            transport, http_status = classify_fetch_error(exc)
            fail("unavailable", transport, http_status)
            return
        try:
            committed = _read_mapping(text, ".review.yml")
            # Проба обхода до слияния: рекурсивный YAML (самоссылочный якорь)
            # переживает разбор и валит RecursionError уже на обходе вложенных
            # mapping'ов — это тот же дефект слоя, что и битый YAML. Проба, а не
            # merge внутри try: упавшее на середине слияние оставило бы в
            # `merged` половину пропускаемого слоя.
            leaf_paths(committed)
        except (HomeConfigError, RecursionError, UnicodeError):
            fail("malformed", None, None)
            return
        if BRANCHES_KEY in committed:
            committed = {k: v for k, v in committed.items() if k != BRANCHES_KEY}
            warnings.append(
                ".review.yml: ключ repository игнорируется "
                "(ветки задаются домашним слоем, см. reviewer config show)"
            )
        merge(committed, ".review.yml")

    merge_home(root / "review.yml", "home:review.yml")
    merge_committed()
    repo_source = f"home:repos/{repo}.yml"
    merge_home(home_repo_path(repo, root), repo_source)
    return merged, ResolutionMeta(
        sources=dict(sources),
        shadowed={key: tuple(value) for key, value in shadowed.items()},
        warnings=tuple(warnings),
        skipped=tuple(skipped),
    )


def policy_to_public_data(policy: ReviewPolicy) -> dict[str, object]:
    """Return the serialized public fields of an effective review policy."""
    return {
        "categories": dict(policy.categories),
        "enabled_only": list(policy.enabled_only),
        "severity_threshold": policy.severity_threshold,
        "paths": {"ignore": list(policy.ignore)},
        "summary_paths": {"ignore": list(policy.summary_paths_ignore)},
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
        "categories", "enabled_only", "severity_threshold", "paths", "summary_paths",
        "max_comments", "min_confidence", "output_language", "task_board",
        "grounding_max_distance", "summary_cluster_depth", "summary_topk_threshold",
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
    _validate_mapping_shape(effective["summary_paths"], {"ignore": lambda value: (
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
        "code_section": lambda value: _validate_context_limits(
            value,
            {
                "max_files": _is_int,
                "max_chunks_per_file": _is_int,
                "chars_per_file": _is_int,
                "max_augmented_files": _is_int,
            },
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


def leaves_of(mapping: Mapping[str, object], key: str) -> dict[str, object]:
    """Записи листовой диагностики (``sources``/``shadowed``), относящиеся к
    верхнему ключу ``key``: сам ``key`` (атомарный/скалярный случай) и все
    ``key.<путь>`` (PRI-260)."""
    prefix = f"{key}."
    return {
        path: value
        for path, value in mapping.items()
        if path == key or path.startswith(prefix)
    }


def source_of(sources: Mapping[str, str], key: str) -> str:
    """Единый источник верхнего ключа ``key`` из листовой карты ``sources``.

    ``"env"`` — под ключом нет ни одной записи; сам слой — все листья ключа
    указывают на один слой; ``"mixed"`` — слои расходятся. Общий хелпер для
    диагностических потребителей `meta.sources` по верхнему ключу (`config
    show`, `_resolve_summary_layout` в `mcp/service.py`) — литеральный
    дубль этой развёртки на потребителе однажды уже разошёлся с PRI-260
    (см. "summary_cluster_depth_overrides" в mcp/service.py).
    """
    leaves = leaves_of(sources, key)
    if not leaves:
        return "env"
    values = set(leaves.values())
    if len(values) == 1:
        return next(iter(values))
    return "mixed"


def build_config_report(
    repo: str,
    branch: str,
    settings,
    data: Mapping[str, object],
    meta: ResolutionMeta,
    committed_source: str | None = None,
) -> dict[str, object]:
    """Собрать безопасный диагностический отчёт об эффективной policy.

    `committed_source` — способ чтения коммиченного слоя (`git-blob`/`vcs`) или
    None, если слой не читался вовсе (PRI-235). Путь к клону в отчёт не
    попадает: диагностике достаточно способа, путь — лишний факт о машине.

    `sources`/`shadowed` листовой гранулярности (PRI-260): ключ — dotted-путь
    до листа, а не верхний ключ policy, иначе слой, не высказавшийся о
    подсекции, в отчёте выглядел бы так, будто он её затенил целиком.
    Записи, отсутствующие в `meta.sources` (ни один слой явно не задал этот
    лист), получают фолбэк `"env"` — тоже на листовом уровне, кроме ключей из
    `ATOMIC_KEYS` (`task_board`): те сливаются и читаются как единое целое
    (`deepmerge.py::_replace`), поэтому и фолбэк для них — одна запись на
    сам ключ, без разложения на листья.
    """
    try:
        policy = ReviewPolicy.load_data(settings, data)
        effective = policy_to_public_data(policy)
        _validate_public_policy_data(effective)
    except (AttributeError, KeyError, OverflowError, RecursionError, TypeError, ValueError):
        raise HomeConfigError("effective policy: недопустимые значения") from None
    sources = {
        path: layer
        for path, layer in meta.sources.items()
        if path.split(".", 1)[0] in effective
    }
    # Фолбэк "env" — на уровне листа, а не верхнего ключа: иначе лист,
    # заполненный дефолтом ReviewPolicy, оставался бы вовсе без записи, если у
    # соседнего листа того же ключа источник уже есть (PRI-260). Записи из
    # meta.sources (setdefault) имеют приоритет над фолбэком. Атомарные ключи
    # (ATOMIC_KEYS, напр. task_board) не раскладываются на листья — сливаются
    # и читаются как единое целое, поэтому и фолбэк для них один на весь
    # ключ; иначе `sources` вернул бы task_board.type/.project/... для
    # ключа, реально пришедшего из одного слоя, и `config show` соврал бы
    # про mixed.
    for key in effective:
        # Ключ, о котором слой высказался целиком (mapping заменён скаляром или
        # null), уже атрибутирован записью на самом ключе. Раскладывать его
        # эффективное значение на листья нельзя: листья дефолта получили бы
        # "env" рядом с записью слоя, и `config show` соврал бы про mixed на
        # ключе, пришедшем из одного слоя (PRI-260).
        if key in ATOMIC_KEYS or key in sources:
            sources.setdefault(key, "env")
            continue
        for path in leaf_paths(effective[key], key):
            sources.setdefault(path, "env")
    return {
        "repo": normalize_repo(repo),
        "branch": branch,
        "effective": effective,
        "sources": sources,
        "shadowed": {key: list(value) for key, value in meta.shadowed.items()},
        "warnings": list(meta.warnings),
        "skipped": [item.as_dict() for item in meta.skipped],
        "committed_source": committed_source,
    }


def _empty_meta() -> ResolutionMeta:
    return ResolutionMeta({}, {}, ())


def _semantic_equal(left: object, right: object) -> bool:
    """Сравнить YAML-значения рекурсивно, не смешивая bool/int/float."""
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        assert isinstance(right, Mapping)
        if len(left) != len(right):
            return False
        unmatched = list(right.items())
        for left_key, left_value in left.items():
            for index, (right_key, right_value) in enumerate(unmatched):
                if _semantic_equal(left_key, right_key):
                    if not _semantic_equal(left_value, right_value):
                        return False
                    unmatched.pop(index)
                    break
            else:
                return False
        return not unmatched
    if isinstance(left, (list, tuple)):
        assert isinstance(right, (list, tuple))
        return len(left) == len(right) and all(
            _semantic_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _effective_policy_snapshot(
    data: Mapping[str, object],
    settings,
) -> object:
    """Провалидировать effective data и вернуть каноническую форму сравнения."""
    _validate_known_policy_data(data, "effective policy")
    if settings is None:
        return dict(data)
    try:
        policy = ReviewPolicy.load_data(settings, data)
        effective = policy_to_public_data(policy)
        _validate_public_policy_data(effective)
        return effective
    except (AttributeError, KeyError, OverflowError, RecursionError, TypeError, ValueError):
        raise HomePolicyError(
            "effective policy: policy содержит недопустимые значения"
        ) from None


@dataclass(frozen=True)
class _DestinationParent:
    path: Path
    descriptor: int | None


def _supports_secure_directory_walk() -> bool:
    return (
        bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.link in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
    )


def _prepare_destination_parent_fallback(
    root: Path,
    segments: list[str],
    source: str,
) -> Path:
    """Cross-platform fallback с lstat-проверкой каждого компонента под root."""
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HomeConfigError(
            f"{source}: destination не подготовлен: {type(exc).__name__}"
        ) from None
    current = root
    for segment in segments:
        current = current / segment
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
                metadata = os.lstat(current)
            except OSError as exc:
                raise HomeConfigError(
                    f"{source}: destination не подготовлен: {type(exc).__name__}"
                ) from None
        except OSError as exc:
            raise HomeConfigError(
                f"{source}: destination не подготовлен: {type(exc).__name__}"
            ) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise HomeConfigError(f"{source}: symlink в destination запрещён")
        if not stat.S_ISDIR(metadata.st_mode):
            raise HomeConfigError(f"{source}: parent destination не является directory")
    return current


@contextmanager
def _prepare_destination_parent(
    root: Path,
    repo: str,
    source: str,
):
    """Создать и открыть parent, не следуя symlink-компонентам ниже config root."""
    segments = ["repos", *repo.split("/")[:-1]]
    if not _supports_secure_directory_walk():
        yield _DestinationParent(
            _prepare_destination_parent_fallback(root, segments, source),
            None,
        )
        return

    try:
        root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise HomeConfigError(
            f"{source}: destination не подготовлен: {type(exc).__name__}"
        ) from None
    current_path = root
    try:
        for segment in segments:
            try:
                os.mkdir(segment, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            except OSError as exc:
                raise HomeConfigError(
                    f"{source}: destination не подготовлен: {type(exc).__name__}"
                ) from None
            try:
                child = os.open(
                    segment,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
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
                raise HomeConfigError(
                    f"{source}: destination descriptor не закрыт"
                ) from None
            descriptor = child
            current_path /= segment
        yield _DestinationParent(current_path, descriptor)
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            if sys.exc_info()[0] is None:
                raise HomeConfigError(
                    f"{source}: destination descriptor не закрыт: {type(exc).__name__}"
                ) from None


def _read_destination_mapping(
    path: Path,
    source: str,
    *,
    parent_fd: int | None = None,
) -> dict[str, object] | None:
    """Прочитать существующий regular destination, не следуя symlink на POSIX."""
    try:
        before = (
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if parent_fd is not None
            else os.lstat(path)
        )
        if stat.S_ISLNK(before.st_mode):
            raise HomeConfigError(f"{source}: symlink запрещён")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HomeConfigError(
            f"{source}: конфиг не прочитан: {type(exc).__name__}"
        ) from None
    if not stat.S_ISREG(before.st_mode):
        raise HomeConfigError(f"{source}: destination должен быть regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = (
            os.open(path.name, flags, dir_fd=parent_fd)
            if parent_fd is not None
            else os.open(path, flags)
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HomeConfigError(
            f"{source}: конфиг не прочитан: {type(exc).__name__}"
        ) from None
    try:
        opened = os.fstat(descriptor)
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            text = handle.read()
        fresh = (
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if parent_fd is not None
            else os.lstat(path)
        )
    except (OSError, UnicodeError) as exc:
        raise HomeConfigError(
            f"{source}: конфиг не прочитан: {type(exc).__name__}"
        ) from None
    finally:
        if descriptor != -1:
            try:
                os.close(descriptor)
            except OSError as exc:
                if sys.exc_info()[0] is None:
                    raise HomeConfigError(
                        f"{source}: конфиг не прочитан: {type(exc).__name__}"
                    ) from None
    before_identity = before.st_dev, before.st_ino
    opened_identity = opened.st_dev, opened.st_ino
    fresh_identity = fresh.st_dev, fresh.st_ino
    if (
        not stat.S_ISREG(before.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or stat.S_ISLNK(fresh.st_mode)
        or before_identity != opened_identity
        or opened_identity != fresh_identity
    ):
        raise HomeConfigError(f"{source}: race при чтении destination")
    return _read_mapping(text, source)


def _existing_migration_result(
    destination: Path,
    existing: dict[str, object],
    candidate: dict[str, object],
    repo: str,
    ref: str,
    fetch_repo_yaml: Callable[[str], str | None],
    root: Path,
    before_data: dict[str, object],
    settings,
) -> MigrationResult:
    # Локальный импорт: branches.py импортирует layers.py, модульный импорт
    # создал бы цикл.
    from reviewer.config.branches import BRANCHES_KEY

    if _credential_path(existing):
        raise HomeConfigError(f"home:repos/{repo}.yml: credential key запрещён")
    source = f"home:repos/{repo}.yml"
    _validate_known_policy_data(existing, source)
    existing_policy = {k: v for k, v in existing.items() if k != BRANCHES_KEY}
    candidate_policy = {k: v for k, v in candidate.items() if k != BRANCHES_KEY}
    if not _semantic_equal(existing_policy, candidate_policy):
        conflicts = tuple(sorted(
            key
            for key in set(existing_policy) | set(candidate_policy)
            if not _semantic_equal(
                existing_policy.get(key, _MISSING),
                candidate_policy.get(key, _MISSING),
            )
        ))
        return MigrationResult(
            destination, False, False, conflicts, before_data, _empty_meta()
        )
    data, meta = resolve_policy_data(
        repo, ref, fetch_repo_yaml, config_root=root,
        strict_home=True, strict_committed=True,
    )
    _effective_policy_snapshot(data, settings)
    return MigrationResult(destination, False, True, (), data, meta)


def _publish_new_config(
    destination: Path,
    content: str,
    source: str,
    *,
    parent_fd: int | None = None,
) -> bool:
    """Опубликовать новый config без перезаписи уже созданного destination."""
    if parent_fd is not None:
        temp_name = f".reviewer-migrate-{secrets.token_hex(12)}"
        descriptor = -1
        try:
            descriptor = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(
                    temp_name,
                    destination.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                return False
            except OSError as exc:
                raise HomeConfigError(
                    f"{source}: публикация не выполнена: {type(exc).__name__}"
                ) from None
            return True
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
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                if sys.exc_info()[0] is None:
                    raise HomeConfigError(
                        f"{source}: очистка temporary file: {type(exc).__name__}"
                    ) from None

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
            return False
        except OSError as exc:
            raise HomeConfigError(
                f"{source}: публикация не выполнена: {type(exc).__name__}"
            ) from None
        return True
    except OSError as exc:
        raise HomeConfigError(
            f"{source}: публикация не выполнена: {type(exc).__name__}"
        ) from None
    finally:
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


def _simulated_repo_layer(
    data: Mapping[str, object], meta: ResolutionMeta, candidate: Mapping[str, object], source: str
) -> tuple[dict[str, object], ResolutionMeta]:
    """Вычислить результат repository-home layer до его атомарной публикации."""
    merged = dict(data)
    sources = dict(meta.sources)
    shadowed = {key: list(value) for key, value in meta.shadowed.items()}
    merge_layer(merged, sources, shadowed, candidate, source)
    return merged, ResolutionMeta(
        sources,
        {key: tuple(value) for key, value in shadowed.items()},
        meta.warnings,
        skipped=meta.skipped,
    )


def migrate_repo_config(
    repo: str,
    ref: str,
    fetch_repo_yaml: Callable[[str], str | None],
    *,
    config_root: Path | None = None,
    settings=None,
) -> MigrationResult:
    """Copy a safe committed policy to its repository-scoped home layer."""
    # Локальный импорт: branches.py импортирует layers.py, модульный импорт
    # создал бы цикл.
    from reviewer.config.branches import BRANCHES_KEY

    repo = normalize_repo(repo)
    root = config_root or reviewer_config_root()
    try:
        source_text = fetch_repo_yaml(ref)
    except Exception as exc:  # noqa: BLE001 — единый бессекретный диагностик
        transport, http_status = classify_fetch_error(exc)
        raise HomeConfigError(
            f".review.yml: слой не прочитан (repo={repo}, ref={ref}, "
            f"category=unavailable, transport={transport}, "
            f"http_status={http_status})"
        ) from None
    candidate = _read_mapping(source_text, ".review.yml")
    if not candidate:
        raise HomeConfigError(".review.yml отсутствует или пуст")
    credential = _credential_path(candidate)
    if credential:
        raise HomeConfigError(
            f".review.yml: credential key {'.'.join(credential)} нельзя мигрировать"
        )
    _validate_known_policy_data(candidate, ".review.yml")
    # Ключ repository — не policy-ключ (ветки задаются домашним слоем): вырезаем
    # его и из data, используемых для сравнения effective policy, и из текста,
    # который будет опубликован в новый home-файл.
    candidate_policy = {k: v for k, v in candidate.items() if k != BRANCHES_KEY}
    published_text = source_text or ""
    if BRANCHES_KEY in candidate:
        published_text = yaml.safe_dump(
            candidate_policy, allow_unicode=True, sort_keys=False
        )

    def fetch_snapshot(_selected_ref: str) -> str | None:
        return source_text

    before_data, before_meta = resolve_policy_data(
        repo, ref, fetch_snapshot, config_root=root,
        strict_home=True, strict_committed=True,
    )
    before_effective = _effective_policy_snapshot(before_data, settings)
    destination = home_repo_path(repo, root)
    source = f"home:repos/{repo}.yml"
    with _prepare_destination_parent(root, repo, source) as parent:
        existing = _read_destination_mapping(
            destination,
            source,
            parent_fd=parent.descriptor,
        )
        if existing is not None:
            return _existing_migration_result(
                destination,
                existing,
                candidate_policy,
                repo,
                ref,
                fetch_snapshot,
                root,
                before_data,
                settings,
            )
        published_data, published_meta = _simulated_repo_layer(
            before_data, before_meta, candidate_policy, source
        )
        published_effective = _effective_policy_snapshot(published_data, settings)
        if not _semantic_equal(before_effective, published_effective):
            raise HomeConfigError("effective policy изменился после миграции")
        if not _publish_new_config(
            destination,
            published_text,
            source,
            parent_fd=parent.descriptor,
        ):
            existing = _read_destination_mapping(
                destination,
                source,
                parent_fd=parent.descriptor,
            )
            if existing is None:
                raise HomeConfigError(f"{source}: destination исчез во время миграции")
            return _existing_migration_result(
                destination,
                existing,
                candidate_policy,
                repo,
                ref,
                fetch_snapshot,
                root,
                before_data,
                settings,
            )
    return MigrationResult(destination, True, False, (), published_data, published_meta)
