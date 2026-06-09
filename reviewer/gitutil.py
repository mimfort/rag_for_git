import subprocess

def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args],
                          check=True, capture_output=True, text=True).stdout

def changed_files(repo: str, base: str, head: str) -> list[str]:
    out = _git(repo, "diff", "--name-only", f"{base}..{head}")
    return [l for l in out.splitlines() if l]

def file_at_ref(repo: str, path: str, ref: str) -> str | None:
    try:
        return _git(repo, "show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return None   # файла нет на этом ref (добавлен/удалён)

def list_python_files(repo: str, ref: str) -> list[str]:
    out = _git(repo, "ls-tree", "-r", "--name-only", ref)
    return [l for l in out.splitlines() if l.endswith(".py")]

def rev_parse(repo: str, ref: str) -> str:
    """Полный SHA коммита, на который указывает ref (`git rev-parse <ref>`)."""
    return _git(repo, "rev-parse", ref).strip()
