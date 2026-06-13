import os
import shutil
import subprocess
import tempfile


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args],
                          check=True, capture_output=True, text=True).stdout

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
