"""Канонизация и вывод идентификатора репозитория 'owner/name'."""
from __future__ import annotations

import re

_REMOTE_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")

_SSH_HOST_RE = re.compile(r"^[\w.-]+@([\w.-]+):")
_HTTP_HOST_RE = re.compile(r"^https?://([^/]+)/")


def normalize_repo(repo: str) -> str:
    """Привести 'Owner/Repo' к канону 'owner/name' (нижний регистр).

    :raises ValueError: пустая строка или не ровно один '/'.
    """
    s = (repo or "").strip().lower()
    parts = s.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Некорректный repo (ожидается owner/name): {repo!r}")
    return f"{parts[0]}/{parts[1]}"


def derive_repo_from_remote(remote_url: str) -> str | None:
    """Вывести 'owner/name' из git remote URL GitHub. None, если не распознан."""
    m = _REMOTE_RE.search((remote_url or "").strip())
    if not m:
        return None
    return f"{m.group(1).lower()}/{m.group(2).lower()}"


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
