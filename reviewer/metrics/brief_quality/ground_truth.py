"""Ground truth задачи: файлы, изменённые её НАСТОЯЩИМИ PR-мержами.

Git вызывается через инъектируемый callable (GitRunner), поэтому фильтрация и
разбор тестируются на чистых данных, без git-репозитория.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable, NamedTuple

GitRunner = Callable[[list], str]


class GitError(RuntimeError):
    """Git-вызов завершился ненулевым кодом."""


class PRMerge(NamedTuple):
    """Настоящий PR-мерж: sha коммита и номер PR из его субъекта."""

    sha: str
    number: int


# Настоящий PR-мерж: только «Merge pull request #N from <owner>/<ветка>».
# Синхронизационные мержи («Merge remote-tracking branch 'origin/dev' into
# feature/pri-N», «merge: dev в …») содержат тот же ключ задачи, но их diff
# первого родителя тащит ВСЕ файлы, попавшие в целевую ветку от чужих задач:
# у PRI-134 знаменатель раздувался с 17 реальных файлов PR #148 до 195, занижая
# core-recall с 50% до 8%. Считать их работой задачи нельзя.
PR_MERGE_SUBJECT_RE = re.compile(r"^Merge pull request #(\d+) from ", re.IGNORECASE)


def filter_pr_merges(rows: list) -> tuple:
    """Оставить только настоящие PR-мержи, вытащив номер PR из субъекта.

    Номер нужен строке измерения: её идентичность — (repo, pr_number, task_key).

    Args:
        rows: пары (sha, субъект коммита).

    Returns:
        (настоящие PR-мержи, число отброшенных синхронизационных мержей).
    """
    merges: list[PRMerge] = []
    skipped = 0
    for sha, subject in rows:
        match = PR_MERGE_SUBJECT_RE.match(subject.strip())
        if match:
            merges.append(PRMerge(sha, int(match.group(1))))
        else:
            skipped += 1
    return merges, skipped


def git_runner(repo_root: pathlib.Path) -> GitRunner:
    """GitRunner поверх subprocess для реального прогона."""

    def run(args: list) -> str:
        result = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or f"git {' '.join(args)}")
        return result.stdout

    return run


def merge_rows(task_key: str, run_git: GitRunner) -> list:
    """Пары (sha, субъект) merge-коммитов, упоминающих ключ задачи."""
    out = run_git(
        ["log", "--merges", "--all", "--format=%H %s", "-i", f"--grep={task_key}"]
    )
    rows: list = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        rows.append((sha, subject))
    return rows


def changed_files(sha: str, run_git: GitRunner) -> set:
    """Файлы merge-коммита по diff первого родителя (реальный контент PR).

    Сбой diff'а даёт пустое множество: у задачи просто не окажется ground truth.
    Число таких сбоев считает `collect` — молчаливая потеря знаменателя иначе
    неотличима от «PR ничего не менял».
    """
    try:
        out = run_git(["diff", "--name-only", f"{sha}^1", sha])
    except GitError:
        return set()
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def changed_status(sha: str, run_git: GitRunner) -> dict:
    """Статусы файлов PR-мержа: путь → added|modified|removed|renamed|copied.

    Источник — `git diff --name-status <sha>^1 <sha>`; для переименования и
    копирования берётся НОВЫЙ путь, как и в `vcs.get_changed_files`, чтобы
    офлайн и онлайн считали одним множеством.
    """
    letters = {"A": "added", "M": "modified", "D": "removed",
               "R": "renamed", "C": "copied", "T": "modified"}
    try:
        out = run_git(["diff", "--name-status", f"{sha}^1", sha])
    except GitError:
        return {}
    result: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = letters.get(parts[0][0], "modified")
        result[parts[-1]] = status
    return result


def path_existed(parent_ref, path: str, run_git: GitRunner) -> bool:
    """Существовал ли путь в родителе merge-коммита (то есть не новый файл).

    Без parent_ref проверить нельзя — считаем «существовал», чтобы не завышать
    число исключённых из знаменателя файлов.
    """
    if not parent_ref:
        return True
    try:
        run_git(["cat-file", "-e", f"{parent_ref}:{path}"])
    except GitError:
        return False
    return True


@dataclass
class TaskGroundTruth:
    """Ground truth одной задачи."""

    task_key: str
    merges: list = field(default_factory=list)
    sync_merges_skipped: int = 0
    changed: set = field(default_factory=set)
    parent_ref: str | None = None
    diff_failures: int = 0

    @property
    def merge_shas(self) -> list:
        """Только sha — для кода, которому номер PR не нужен."""
        return [m.sha for m in self.merges]


def collect(task_key: str, run_git: GitRunner) -> TaskGroundTruth:
    """Собрать ground truth задачи: PR-мержи и объединение их изменённых файлов."""
    merges, skipped = filter_pr_merges(merge_rows(task_key, run_git))
    changed: set = set()
    failures = 0
    for merge in merges:
        files = changed_files(merge.sha, run_git)
        if not files:
            failures += 1
        changed |= files
    return TaskGroundTruth(
        task_key=task_key,
        merges=merges,
        sync_merges_skipped=skipped,
        changed=changed,
        parent_ref=f"{merges[0].sha}^1" if merges else None,
        diff_failures=failures,
    )
