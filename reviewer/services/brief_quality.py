"""Онлайн-съём качества ретрива под бриф solve-task (PRI-249).

Единственный модуль метрики с вводом-выводом: читает бриф из дерева коммита
измеряемого PR в локальном клоне. Формулы не свои — берутся из reviewer.metrics.brief_quality,
общего с офлайн-харнессом eval/solve_task_metrics (PRI-250): только тождество
кода делает числа «до/после» сравнимыми.

Ground truth не требует git: PreparedReview.changed_status уже несёт статус
файла (added/modified/removed/renamed/copied), а «existed_before» — это
`status not in {added, renamed, copied}`. Знаменатель `expected` строится по
ПОЛНОМУ diff'у PR (ключи `changed_status`), а не по подмножеству, отобранному
на ревью, — иначе числа «до/после» с офлайн-харнессом несравнимы.
"""
from __future__ import annotations

import logging
import os
import pathlib
import re
from dataclasses import dataclass, field

from reviewer.metrics.brief_quality import briefs, classify, recall
from reviewer.metrics.brief_quality.config import BriefQualityConfig
from reviewer.services.brief_provenance import GitCheckout

log = logging.getLogger(__name__)

STATUS_MEASURED = "measured"
STATUS_NO_TASK_KEY = "no_task_key"
STATUS_NO_BRIEF = "no_brief"
STATUS_BRIEF_REVISION_UNAVAILABLE = "brief_revision_unavailable"
STATUS_BRIEF_UNREADABLE = "brief_unreadable"
STATUS_EMPTY_CORE = "empty_core_denominator"
STATUS_UNCONFIGURED_CORE = "unconfigured_core_denominator"


@dataclass(frozen=True)
class BriefQualityMeasurement:
    """Одно измерение качества брифа. status != measured — точки измерения нет."""

    status: str
    task_key: str | None = None
    brief_path: str | None = None
    expected: int = 0
    expected_core: int = 0
    predicted: int = 0
    hit_core: int = 0
    core_recall: float | None = None
    raw_recall: float | None = None
    precision: float | None = None
    misses: dict = field(default_factory=dict)
    predicted_paths: tuple = ()
    expected_core_paths: tuple = ()
    hit_core_paths: tuple = ()


def _brief_name_pattern(task_key: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(task_key)}(?![A-Za-z0-9])", re.IGNORECASE)


def find_brief(
    clone_path: str | None, task_key: str, config: BriefQualityConfig
) -> pathlib.Path | None:
    """Файл брифа задачи в клоне или None.

    Каталог брифов — `config.briefs_dir`: своей константы у сервиса больше
    нет, чтобы значение не могло разъехаться с конфигом репозитория.

    Совпадение по ключу регистронезависимо: имена брифов пишутся и как PRI-249,
    и как pri-249. При нескольких файлах берётся лексикографически последний —
    имя начинается с даты, поэтому это самый свежий бриф задачи.

    Ключ ищется по границе токена, а не подстрокой: `PRI-24` не должен
    совпадать с брифом `PRI-249`. Такая ошибка тихая — измерение вышло бы
    успешным, но посчитанным по чужому брифу.

    Никогда не бросает: битый или недоступный путь клона (null-байт, снятые
    права на каталог) даёт `None`, а не исключение наружу — вызывающий код
    иначе получил бы «молчание» вместо статуса `no_brief`.
    """
    if not clone_path or not task_key:
        return None
    directory = pathlib.Path(clone_path) / config.briefs_dir
    try:
        if not directory.is_dir():
            return None
        pattern = _brief_name_pattern(task_key)
        matches = sorted(
            path for path in directory.glob("*.md") if pattern.search(path.name)
        )
    except (OSError, ValueError):
        log.warning("Не удалось прочитать каталог брифов %s", directory, exc_info=True)
        return None
    return matches[-1] if matches else None


def _brief_checkout(clone_path: str) -> GitCheckout:
    git = GitCheckout(clone_path)
    git.env["GIT_ALLOW_PROTOCOL"] = ""
    return git


def _snapshot_brief_paths(
    git: GitCheckout, sha: str, config: BriefQualityConfig,
) -> list[pathlib.PurePosixPath]:
    """Пути обычных файлов брифов из дерева одного коммита."""
    directory = pathlib.PurePosixPath(config.briefs_dir)
    if directory.is_absolute() or ".." in directory.parts:
        raise OSError("Каталог брифов находится за пределами репозитория")
    scope = ("--", f"{directory}/") if directory != pathlib.PurePosixPath(".") else ()
    tree = git.git("ls-tree", "-z", "--full-tree", sha, *scope)
    if tree is None:
        raise OSError("Дерево брифов недоступно")
    paths = []
    for entry in tree.split(b"\0"):
        meta, _, raw_path = entry.partition(b"\t")
        if not meta.startswith((b"100644 blob ", b"100755 blob ")):
            continue
        path = pathlib.PurePosixPath(os.fsdecode(raw_path))
        if path.parent == directory and path.suffix == ".md":
            paths.append(path)
    return paths


def corpus_brief_paths(
    clone_path: str | os.PathLike[str], config: BriefQualityConfig,
) -> list[pathlib.PurePosixPath]:
    """Зафиксировать состав корпуса по файлам в текущем коммите HEAD."""
    git = _brief_checkout(os.fspath(clone_path))
    if (sha := git.commit("HEAD")) is None:
        raise OSError("Текущий снимок корпуса брифов недоступен")
    return sorted(_snapshot_brief_paths(git, sha, config))


def _snapshot_brief(
    git: GitCheckout, head_sha: str, task_key: str, config: BriefQualityConfig,
) -> str | None:
    """Выбрать бриф из дерева полного SHA. Ошибка Git отличается от пустого списка."""
    if git.commit(head_sha) != head_sha:
        raise OSError("Git-коммит измеряемого PR недоступен")
    pattern = _brief_name_pattern(task_key)
    return max((path.as_posix() for path in _snapshot_brief_paths(git, head_sha, config)
                if pattern.search(path.name)), default=None)


def measure(
    *,
    task_key: str | None,
    clone_path: str | None,
    changed_paths: list,
    changed_status: dict,
    config: BriefQualityConfig,
    head_sha: str | None = None,
) -> BriefQualityMeasurement:
    """Посчитать качество брифа задачи против фактического diff'а PR.

    Никогда не бросает: каждый отказ — именованный status, потому что молчание
    неотличимо от «метрика сломалась».

    При переданном head_sha бриф берётся из этого снимка. Рабочая копия
    используется только при вызове без SHA.

    `config` обязателен без значения по умолчанию: молчаливый дефолт (ядро
    rag_for_git) — и есть тихий провал, ради которого делается задача. Вызывающий
    из reviewer/mcp/service.py обязан передавать реально отрезолвленную политику
    репозитория, иначе чужой репозиторий молча считался бы по чужой линейке.
    """
    if not task_key:
        return BriefQualityMeasurement(status=STATUS_NO_TASK_KEY)

    if head_sha is None:
        brief = find_brief(clone_path, task_key, config)
        relative = f"{config.briefs_dir}/{brief.name}" if brief is not None else None
    else:
        try:
            if not clone_path:
                raise OSError("Локальный клон недоступен")
            git = _brief_checkout(clone_path)
            relative = _snapshot_brief(git, head_sha, task_key, config)
        except (OSError, ValueError):
            return BriefQualityMeasurement(status=STATUS_BRIEF_REVISION_UNAVAILABLE, task_key=task_key)
    if relative is None:
        return BriefQualityMeasurement(status=STATUS_NO_BRIEF, task_key=task_key)

    try:
        if head_sha is None:
            text = brief.read_text(encoding="utf-8")
        else:
            raw = git.git("show", f"{head_sha}:{relative}")
            if raw is None:
                raise OSError("Блоб брифа недоступен")
            text = raw.decode("utf-8")
        predicted = briefs.extract_section_paths(text, briefs.RELEVANT_HEADER)
        briefs.parse_rendered_citations(text, briefs.TEST_HEADER)
    except (OSError, ValueError):
        log.warning("Не удалось прочитать бриф %s", relative, exc_info=True)
        return BriefQualityMeasurement(
            status=STATUS_BRIEF_UNREADABLE, task_key=task_key, brief_path=relative
        )
    if not briefs.has_section(text, briefs.RELEVANT_HEADER):
        return BriefQualityMeasurement(
            status=STATUS_BRIEF_UNREADABLE, task_key=task_key, brief_path=relative
        )

    # Знаменатель — ПОЛНЫЙ diff PR, а не отобранное на ревью подмножество:
    # `changed_status` строится из `vcs.get_changed_files(pr)` целиком (все
    # расширения, включая удалённые файлы), тогда как `changed_paths` — это
    # уже отфильтрованный `_select_changed_files` список (только *.py, без
    # removed, обрезанный `review_max_files`). Офлайн-харнесс считает по
    # полному diff'у (`git diff --name-only`), поэтому знаменатель здесь
    # обязан быть тем же множеством — иначе линейки «до/после» несравнимы.
    # `changed_paths` остаётся подстраховкой: пустой/отсутствующий
    # `changed_status` не должен обнулять измерение.
    expected = set(changed_status) or set(changed_paths)

    def existed_before(path: str) -> bool:
        # Эмуляция офлайновой проверки `git cat-file -e <parent>:<path>`:
        # переименованного/скопированного пути в родительском коммите нет,
        # поэтому и `renamed`, и `copied` — это «не существовал до PR», как
        # и `added`.
        return changed_status.get(path) not in {"added", "renamed", "copied"}

    expected_core = {
        path
        for path in expected
        if classify.is_core_production_path(path, config) and existed_before(path)
    }
    row = recall.evaluate_task(task_key, predicted, expected, expected_core)

    misses: dict = {}
    for missed in expected - predicted:
        category = classify.categorize_miss(missed, existed_before(missed), config)
        misses[category] = misses.get(category, 0) + 1

    if expected_core:
        status = STATUS_MEASURED
    elif config.configured:
        status = STATUS_EMPTY_CORE
    else:
        # Ядро пусто И ключа `metrics.brief_quality.core_paths` в `.review.yml`
        # нет: скорее всего репозиторий просто не настроен, и молчаливый ноль
        # здесь неотличим от честного «в диффе только тесты и доки» (критерий
        # 3 PRI-271).
        status = STATUS_UNCONFIGURED_CORE
    return BriefQualityMeasurement(
        status=status,
        task_key=task_key,
        brief_path=relative,
        expected=row.expected,
        expected_core=row.expected_core,
        predicted=row.predicted,
        hit_core=row.hit_core,
        core_recall=row.core_recall,
        raw_recall=row.raw_recall,
        precision=row.precision,
        misses=misses,
        predicted_paths=tuple(sorted(predicted)),
        expected_core_paths=tuple(sorted(expected_core)),
        hit_core_paths=tuple(sorted(predicted & expected_core)),
    )


def measure_and_record(
    *,
    task_key: str | None,
    repo: str,
    pr_number: int,
    head_sha: str | None,
    changed_paths: list,
    changed_status: dict,
    clone_path: str | None,
    config: BriefQualityConfig,
    history,
    run_id: int | None = None,
) -> str | None:
    """Посчитать качество брифа и записать строку. Никогда не бросает.

    Общий путь трёх точек съёма: publish_review (с run_id), finish_task и CLI
    measure-briefs (без него). Возвращает status измерения — его показывает
    вызывающий; None означает, что записи не было (нет истории или сбой).
    """
    if history is None:
        return None
    try:
        measurement = measure(
            task_key=task_key,
            clone_path=clone_path,
            changed_paths=changed_paths,
            changed_status=changed_status,
            config=config,
            head_sha=head_sha,
        )
        recorded_id = history.record_brief_quality(run_id, repo, pr_number, head_sha, measurement)
        return measurement.status if recorded_id is not None else None
    except Exception:  # noqa: BLE001 — наблюдаемость не роняет вызывающий поток
        log.warning("Не удалось снять качество брифа для %s pr:%s", repo, pr_number,
                    exc_info=True)
        return None
