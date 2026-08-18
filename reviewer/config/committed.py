"""Чтение коммиченного слоя политики: локальный клон с фолбэком на VCS (PRI-235).

`resolve_policy_data` принимает фетчер коммиченного `.review.yml` извне, поэтому
источник слоя меняется здесь, а не в резолвере. `reviewer index` читал файл
локально (`git show <ref>:.review.yml`), а `config show` и MCP ходили за ним в
API хостинга — лишний сетевой вызов там, где клон лежит рядом, и полная
недостижимость слоя для репозитория без рабочего remote.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
import os

import yaml

from reviewer.config.deepmerge import leaf_paths
from reviewer.gitutil import file_at_ref, remote_url, repo_root, rev_parse
from reviewer.services.repo_id import derive_repo_from_remote, normalize_repo

log = logging.getLogger(__name__)

POLICY_FILE = ".review.yml"

# Метки способа чтения слоя для диагностики (`config show`). Метка называет
# ПРОЧИТАННЫЙ ОБЪЕКТ — git-блоб на ref, а не файл в рабочем дереве клона:
# разница с рабочим деревом (см. `worktree_drift`) должна быть видна уже из
# самой метки, а не требовать отдельного объяснения.
SOURCE_LOCAL = "git-blob"
SOURCE_VCS = "vcs"


class CommittedLayerUnavailable(RuntimeError):
    """Коммиченный слой нечем прочитать: нет ни клона, ни VCS-провайдера."""


def validate_clone(path: str | None, repo: str) -> str | None:
    """Вернуть корень клона, пригодного для чтения политики ``repo``, иначе None.

    Клон БЕЗ распознаваемого remote принимается намеренно: именно такие
    репозитории сегодня теряют коммиченный слой целиком. А вот клон с чужим
    remote отвергается — путь мог устареть (в БД он живёт между запусками) или
    указывать на соседний проект.
    """
    # isinstance намеренно: путь приходит из БД и из CLI, и не-строка здесь
    # означает битую запись, а не клон — уводить её в subprocess нельзя.
    if not path or not isinstance(path, str):
        return None
    root = repo_root(path)
    if not root or not os.path.isdir(root):
        return None
    derived = derive_repo_from_remote(remote_url(root) or "")
    if derived and derived != normalize_repo(repo):
        log.debug("Клон пропущен: remote указывает на %s, ожидался %s", derived, repo)
        return None
    return root


class CommittedLayerFetcher:
    """Фетчер коммиченного `.review.yml`: локальный клон → VCS.

    Совместим по сигнатуре с ``fetch_repo_yaml`` из ``resolve_policy_data``.
    VCS-фетчер инъектируется ленивой фабрикой: при живом клоне провайдер не
    создаётся вовсе, иначе экономия сетевого вызова обнулилась бы созданием
    клиента.
    """

    def __init__(
        self,
        repo: str,
        *,
        clone_path: str | None = None,
        vcs_fetch_factory: Callable[[], Callable[[str], str | None]] | None = None,
        policy_file: str = POLICY_FILE,
    ) -> None:
        self._repo = repo
        self._clone_root = validate_clone(clone_path, repo)
        self._vcs_fetch_factory = vcs_fetch_factory
        self._policy_file = policy_file
        self.source: str | None = None

    @property
    def clone_available(self) -> bool:
        return self._clone_root is not None

    @property
    def clone_root(self) -> str | None:
        """Корень пригодного клона или None. Нужен `config show` для дрейфа."""
        return self._clone_root

    def __call__(self, ref: str) -> str | None:
        root = self._clone_root
        if root is not None and self._ref_present(root, ref):
            self.source = SOURCE_LOCAL
            return file_at_ref(root, self._policy_file, ref)
        if self._vcs_fetch_factory is None:
            raise CommittedLayerUnavailable(
                f"{self._repo}: коммиченный слой недоступен — нет ни клона, ни VCS"
            )
        self.source = SOURCE_VCS
        return self._vcs_fetch_factory()(ref)

    def _ref_present(self, root: str, ref: str) -> bool:
        """Резолвится ли ``ref`` в клоне.

        Проверка обязательна ДО чтения: `file_at_ref` возвращает None и когда
        файла нет на ref (легитимное отсутствие слоя), и когда самого ref в
        клоне нет (ветка не выкачана) — второе обязано уводить в VCS-фолбэк, а
        не молча обнулять слой.
        """
        try:
            return bool(rev_parse(root, ref))
        except Exception:  # noqa: BLE001 — любой сбой git = «читаем через VCS»
            log.debug("ref %s не резолвится в клоне %s: фолбэк на VCS", ref, root)
            return False


@dataclass(frozen=True)
class DriftReport:
    """Расхождение рабочего дерева клона с коммиченным `.review.yml` (PRI-260).

    Диагностика, а не источник данных: эффективная политика по-прежнему целиком
    из коммиченного слоя — иначе PR ослаблял бы собственное ревью правкой
    своего же конфига.
    """

    status: str
    keys: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {"status": self.status, "keys": list(self.keys)}


def worktree_drift(root: str, ref: str, policy_file: str = POLICY_FILE) -> DriftReport:
    """Сравнить рабочее дерево клона с коммиченным блобом на ``ref``.

    Сравнение проводится только когда ``ref`` резолвится в тот же коммит, что
    HEAD клона: при `config show --branch dev` из ветки `feat/...` разница
    была бы про другую ветку, а не про несохранённую правку. Возвращаются
    только ключи — значения политики в диагностику не попадают.
    """
    try:
        head = rev_parse(root, "HEAD")
        target = rev_parse(root, ref)
        if not head or not target:
            return DriftReport("unknown")
        if head != target:
            return DriftReport("ref_not_head")
        blob = file_at_ref(root, policy_file, ref)
        path = os.path.join(root, policy_file)
        working = None
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                working = handle.read()
        if working is None:
            return DriftReport("absent_in_worktree" if blob is not None else "clean")
        if blob is None:
            return DriftReport("absent_in_blob")
        if working == blob:
            return DriftReport("clean")
        keys = _diverging_keys(blob, working)
    except Exception:  # noqa: BLE001 — диагностика не должна ронять config show
        log.debug("дрейф рабочего дерева не вычислен для %s", root, exc_info=True)
        return DriftReport("unknown")
    return DriftReport("drifted", keys)


def _diverging_keys(blob: str, working: str) -> tuple[str, ...]:
    """Листовые пути, значения которых различаются между блобом и деревом."""
    left = yaml.safe_load(blob) or {}
    right = yaml.safe_load(working) or {}
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        raise ValueError("policy layer is not a mapping")
    paths = sorted(set(leaf_paths(left)) | set(leaf_paths(right)))
    return tuple(path for path in paths if _at(left, path) != _at(right, path))


_MISSING = object()


def _at(data: object, path: str) -> object:
    for key in path.split("."):
        if not isinstance(data, Mapping) or key not in data:
            return _MISSING
        data = data[key]
    return data
