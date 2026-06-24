"""Провайдер доски YouTrack (JetBrains): REST-клиент (httpx) + нормализация.

REST API: base <instance>/api, заголовок Authorization: Bearer perm:<token>.
Один list-эндпоинт /issues с богатым `fields` отдаёт всё (idReadable, summary,
description, updated, State, links) без доп. запросов, поэтому normalize чистая,
а links резолвятся уже в iter_raw. Порт client-side маппинга задачи в TaskBrief.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

import httpx

from reviewer.tasks.boards.base import RawTask, project_prefix

_PAGE = 200

_FIELDS = (
    "idReadable,summary,description,updated,"
    "customFields(name,value(name)),"
    "links(direction,linkType(name),issues(idReadable))"
)


def _state_of(issue: dict) -> str | None:
    """Статус задачи — кастом-поле «State» (его value.name)."""
    for cf in issue.get("customFields") or []:
        if cf.get("name") == "State":
            val = cf.get("value")
            return val.get("name") if isinstance(val, dict) else None
    return None


def _links_of(issue: dict) -> list[dict]:
    """Ссылки из issueLinks: linkType «Subtask» → subtask, иначе related."""
    out: list[dict] = []
    for ln in issue.get("links") or []:
        name = ((ln.get("linkType") or {}).get("name") or "")
        typ = "subtask" if "subtask" in name.lower() else "related"
        for iss in ln.get("issues") or []:
            key = iss.get("idReadable")
            if key:
                out.append({"type": typ, "key": key})
    return out


def _issue_to_raw(issue: dict) -> RawTask:
    """YouTrack issue JSON → RawTask. Чистая: без I/O."""
    key = issue.get("idReadable", "")
    return RawTask(
        key=key,
        project_code=key,                       # один счётчик idReadable, второго кода нет
        title=issue.get("summary", "") or "",
        description=issue.get("description", "") or "",
        status=_state_of(issue),
        subtask_ids=[],
        timestamp=int(issue.get("updated", 0) or 0),
        links=_links_of(issue),
    )


def normalize_youtrack(raw: RawTask, key_pattern: str, base_url: str) -> dict:
    """RawTask → TaskBrief dict. Чистая: без I/O. url выводится из base_url."""
    key = raw.key
    links: list[dict] = list(raw.links)
    covered: set[str] = {key} | {lk["key"] for lk in links}
    if key_pattern:
        seen: set[str] = set()
        for m in re.finditer(key_pattern, raw.description or ""):
            code = m.group(0)
            if code in covered or code in seen:
                continue
            seen.add(code)
            links.append({"type": "related", "key": code})

    web = re.sub(r"/api/?$", "", base_url.rstrip("/"))   # web-база = api-база без /api
    url = f"{web}/issue/{key}" if web and key else None
    return {
        "key": key,
        "aliases": [],
        "title": raw.title,
        "description": raw.description,
        "criteria": [],
        "status": raw.status,
        "url": url,
        "links": links,
        "project": project_prefix(raw.key),
    }


class YouTrackBoard:
    """REST-провайдер YouTrack. iter_raw — один пагинированный /issues-запрос
    с богатым `fields`; normalize чистая (всё уже в RawTask)."""

    board_type = "youtrack"

    def __init__(self, *, token: str, base_url: str, key_pattern: str) -> None:
        """Инициализация REST-клиента YouTrack.

        ``token`` — **полный** постоянный токен YouTrack, включая префикс ``perm:``,
        например ``perm:abc123...``. Именно в таком виде YouTrack выдаёт токены;
        значение env-переменной ``YOUTRACK_TOKEN`` передаётся сюда напрямую.
        Токен отправляется verbatim в заголовке ``Authorization: Bearer <token>``
        — код *не добавляет* «perm:» самостоятельно (иначе вышло бы ``Bearer perm:perm:...``).
        """
        self._key_pattern = key_pattern
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        count = 0
        skip = 0
        while True:
            params: dict = {"fields": _FIELDS, "$top": _PAGE, "$skip": skip}
            if board:
                params["query"] = f"project: {board}"
            r = self._client.get("/issues", params=params)
            r.raise_for_status()
            page = r.json()
            for issue in page:
                yield _issue_to_raw(issue)
                count += 1
                if limit and count >= limit:
                    return
            if len(page) < _PAGE:
                return
            skip += len(page)

    def normalize(self, raw: RawTask) -> dict:
        return normalize_youtrack(raw, self._key_pattern, self._base)
