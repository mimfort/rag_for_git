"""Канонизация и вывод идентификатора репозитория 'owner/name'."""
from __future__ import annotations

import re
from dataclasses import dataclass

_REMOTE_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")

_SSH_HOST_RE = re.compile(r"^[\w.-]+@([\w.-]+):")
_HTTP_HOST_RE = re.compile(r"^https?://([^/]+)/")


def normalize_repo(repo: str) -> str:
    """Привести repo к канону 'owner/name' или 'group/.../name' (нижний регистр).

    :raises ValueError: недостаточно сегментов или небезопасный путь.
    """
    s = (repo or "").strip().lower()
    if "\\" in s or "\x00" in s:
        raise ValueError(f"Некорректный repo: {repo!r}")
    parts = s.split("/")
    if len(parts) < 2 or any(not part or part in {".", ".."} for part in parts):
        raise ValueError(
            f"Некорректный repo (ожидается owner/name или group/.../name): {repo!r}"
        )
    return "/".join(parts)


def derive_repo_from_remote(remote_url: str) -> str | None:
    """Вывести 'owner/name' из git remote URL GitHub. None, если не распознан."""
    m = _REMOTE_RE.search((remote_url or "").strip())
    if not m:
        return None
    return f"{m.group(1).lower()}/{m.group(2).lower()}"


@dataclass(frozen=True)
class RepoResolution:
    """Идентификатор репозитория и происхождение имени.

    `source` — тот же словарь, что у `RepositoryDetection` в config/onboarding.py,
    расширенный третьим значением: 'cli' | 'git:origin' | 'env:DEFAULT_REPO'.
    """

    repo: str
    source: str


def resolve_repo_id(repo_opt: str | None, remote: str | None,
                    default_repo: str | None) -> RepoResolution | None:
    """Резолв repo-тега с явным происхождением: --repo → origin → DEFAULT_REPO.

    Принимает УЖЕ прочитанный URL origin (а не путь), поэтому модуль остаётся
    без git/subprocess и тестируется без моков. None = резолвить нечем.

    :raises ValueError: repo_opt или default_repo не приводятся к 'owner/name'.
    """
    if repo_opt:
        return RepoResolution(normalize_repo(repo_opt), "cli")
    derived = derive_repo_from_remote(remote or "")
    if derived:
        return RepoResolution(derived, "git:origin")
    if default_repo:
        return RepoResolution(normalize_repo(default_repo), "env:DEFAULT_REPO")
    return None


def _remote_host(remote_url: str) -> str | None:
    """Хост из git remote URL (ssh `git@host:...` или https `https://host/...`)."""
    u = (remote_url or "").strip()
    m = _SSH_HOST_RE.match(u)
    if m:
        return m.group(1).lower()
    m = _HTTP_HOST_RE.match(u)
    if m:
        # отрезаем возможные userinfo@ и :port
        return m.group(1).lower().split("@")[-1].split(":")[0]
    return None


def derive_vcs_from_remote(remote_url: str) -> tuple[str, str] | None:
    """(provider, base_url) из git remote URL; None если платформа не распознана.

    github.com → ('github', '') (base_url не нужен — зашит в GitHubProvider);
    хост с 'gitlab' (gitlab.com или self-hosted) → ('gitlab', 'https://<host>').
    """
    host = _remote_host(remote_url)
    if host is None:
        return None
    if "gitlab" in host:
        return ("gitlab", f"https://{host}")
    if "github" in host:
        return ("github", "")
    return None
