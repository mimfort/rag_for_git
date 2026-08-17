import os
import shutil
import subprocess
import tempfile


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args],
                          check=True, capture_output=True, text=True).stdout


def repo_root(path: str = ".") -> str | None:
    try:
        root = _git(path, "rev-parse", "--show-toplevel").strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return os.path.abspath(root) if root else None


def remote_default_branch(repo: str) -> str | None:
    try:
        ref = _git(
            repo,
            "symbolic-ref",
            "--quiet",
            "refs/remotes/origin/HEAD",
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None

    prefix = "refs/remotes/origin/"
    branch = ref.removeprefix(prefix)
    return branch if ref.startswith(prefix) and branch else None


def changed_files(repo: str, base: str, head: str) -> list[str]:
    out = _git(repo, "diff", "--name-only", f"{base}..{head}")
    return [line for line in out.splitlines() if line]

def file_at_ref(repo: str, path: str, ref: str) -> str | None:
    try:
        return _git(repo, "show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return None   # файла нет на этом ref (добавлен/удалён)

def list_python_files(repo: str, ref: str) -> list[str]:
    out = _git(repo, "ls-tree", "-r", "--name-only", ref)
    return [line for line in out.splitlines() if line.endswith(".py")]

def rev_parse(repo: str, ref: str) -> str:
    """Полный SHA коммита, на который указывает ref (`git rev-parse <ref>`)."""
    return _git(repo, "rev-parse", ref).strip()


def commits_behind(repo: str, sha: str, ref: str) -> int | None:
    """На сколько коммитов ``ref`` опережает ``sha`` (`git rev-list --count <sha>..<ref>`).

    Возвращает None, если ``repo`` не git-репо либо ``sha``/``ref`` недостижимы —
    дрейф просто считается «неизвестным» (fail-soft, без падения вызывающего)."""
    try:
        out = _git(repo, "rev-list", "--count", f"{sha}..{ref}")
    except subprocess.CalledProcessError:
        return None
    return int(out.strip())


def remote_url(repo: str) -> str | None:
    """URL remote 'origin' или None, если remote нет."""
    try:
        return _git(repo, "remote", "get-url", "origin").strip()
    except subprocess.CalledProcessError:
        return None


def add_worktree(repo: str, ref: str) -> str:
    """Создать временный git worktree на ``ref`` и вернуть путь к нему.

    Worktree создаётся в временном каталоге; по завершении работы следует
    вызвать :func:`remove_worktree`.
    """
    parent = tempfile.mkdtemp()
    wt = os.path.join(parent, "wt")  # каталог «wt» — git создаст сам
    _git(repo, "worktree", "add", "--detach", wt, ref)
    return wt


def remove_worktree(repo: str, path: str) -> None:
    """Удалить git worktree по пути ``path`` и очистить временный каталог."""
    try:
        _git(repo, "worktree", "remove", "--force", path)
    except Exception:
        pass  # worktree мог уже не существовать — не падаем
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def paths_touched_by_grep(repo: str, pattern: str, *, limit: int) -> list[str]:
    """Пути коммитов, чьё сообщение содержит ``pattern`` (ключ задачи).

    Фолбэк к истории прогонов: ключ задачи присутствует в именах веток
    (feat/pri-256-…), сообщениях merge-коммитов и телах PR. В этом репозитории
    ключ обычно живёт именно в имени ветки, попадающем в сообщение
    merge-коммита («Merge pull request #N from owner/feat/PRI-…»), а обычные
    коммиты его не содержат — поэтому нужен ``--diff-merges=first-parent``:
    без него `--name-only` для merge-коммита файлов не печатает и фолбэк
    тихо вернул бы пустой список. Fail-soft.
    """
    try:
        out = _git(repo, "log", f"-n{limit}", "--name-only",
                   "--diff-merges=first-parent", "--pretty=format:",
                   "-i", f"--grep={pattern}")
    except (OSError, subprocess.CalledProcessError):
        return []
    seen: dict[str, None] = {}
    for line in out.splitlines():
        if line:
            seen.setdefault(line, None)
    return list(seen)
