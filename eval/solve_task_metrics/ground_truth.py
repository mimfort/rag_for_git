"""Ground truth задачи: файлы, изменённые её НАСТОЯЩИМИ PR-мержами.

Git вызывается через инъектируемый callable (GitRunner), поэтому фильтрация и
разбор тестируются на чистых данных, без git-репозитория.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

GitRunner = Callable[[list], str]


class GitError(RuntimeError):
    """Git-вызов завершился ненулевым кодом."""


# Настоящий PR-мерж: только «Merge pull request #N from <owner>/<ветка>».
# Синхронизационные мержи («Merge remote-tracking branch 'origin/dev' into
# feature/pri-N», «merge: dev в …») содержат тот же ключ задачи, но их diff
# первого родителя тащит ВСЕ файлы, попавшие в целевую ветку от чужих задач:
# у PRI-134 знаменатель раздувался с 17 реальных файлов PR #148 до 195, занижая
# core-recall с 50% до 8%. Считать их работой задачи нельзя.
PR_MERGE_SUBJECT_RE = re.compile(r"^Merge pull request #\d+ from ", re.IGNORECASE)


def filter_pr_merges(rows: list) -> tuple:
    """Оставить только настоящие PR-мержи.

    Args:
        rows: пары (sha, субъект коммита).

    Returns:
        (shas настоящих PR-мержей, число отброшенных синхронизационных мержей).
    """
    shas: list = []
    skipped = 0
    for sha, subject in rows:
        if PR_MERGE_SUBJECT_RE.match(subject.strip()):
            shas.append(sha)
        else:
            skipped += 1
    return shas, skipped


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
    merge_shas: list = field(default_factory=list)
    sync_merges_skipped: int = 0
    changed: set = field(default_factory=set)
    parent_ref: str | None = None
    diff_failures: int = 0


def collect(task_key: str, run_git: GitRunner) -> TaskGroundTruth:
    """Собрать ground truth задачи: PR-мержи и объединение их изменённых файлов."""
    shas, skipped = filter_pr_merges(merge_rows(task_key, run_git))
    changed: set = set()
    failures = 0
    for sha in shas:
        files = changed_files(sha, run_git)
        if not files:
            failures += 1
        changed |= files
    return TaskGroundTruth(
        task_key=task_key,
        merge_shas=shas,
        sync_merges_skipped=skipped,
        changed=changed,
        parent_ref=f"{shas[0]}^1" if shas else None,
        diff_failures=failures,
    )
