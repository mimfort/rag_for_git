"""Провайдер доски Yougile: REST-клиент (httpx) + нормализация в TaskBrief.

REST API v2: base https://yougile.com/api-v2, заголовок Authorization: Bearer <key>.
Перечисление: projects → boards → columns → tasks (listing-эндпоинты дают полные
объекты задач + проход columns для title статусов). normalize резолвит title
подзадач best-effort и зовёт чистую normalize_yougile.

Нормализация — порт плейбука plugin/skills/review-pr/references/task-context-yougile.md.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable

import httpx

from reviewer.tasks.boards.base import RawTask

log = logging.getLogger(__name__)

_PAGE = 1000


def normalize_yougile(
    raw: RawTask,
    key_pattern: str,
    url_template: str,
    subtask_titles: dict[str, str] | None = None,
) -> dict:
    """RawTask → TaskBrief dict. Чистая: без I/O (titles подзадач инжектятся)."""
    subtask_titles = subtask_titles or {}
    key = raw.key
    aliases = [raw.project_code] if raw.project_code and raw.project_code != key else []

    links: list[dict] = []
    covered: set[str] = {key, *aliases}
    for sid in raw.subtask_ids:
        link = {"type": "subtask", "key": sid}
        title = subtask_titles.get(sid)
        if title:
            link["title"] = title
        links.append(link)
        covered.add(sid)

    if key_pattern:
        seen_rel: set[str] = set()
        for m in re.finditer(key_pattern, raw.description or ""):
            code = m.group(0)
            if code in covered or code in seen_rel:
                continue
            seen_rel.add(code)
            links.append({"type": "related", "key": code})

    url = None
    if url_template and raw.project_code:
        url = url_template.replace("{code}", raw.project_code)

    return {
        "key": key,
        "aliases": aliases,
        "title": raw.title,
        "description": raw.description,
        "criteria": [],
        "status": raw.status,
        "url": url,
        "links": links,
    }


class YougileBoard:
    """REST-провайдер Yougile. iter_raw перечисляет доску; normalize резолвит
    title подзадач (best-effort) и нормализует через normalize_yougile."""

    def __init__(self, *, api_key: str, api_base: str, key_pattern: str,
                 url_template: str) -> None:
        self._key_pattern = key_pattern
        self._url_template = url_template
        self._base = (api_base or "https://yougile.com/api-v2").rstrip("/")
        self._client = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        """Пагинированный GET listing-эндпоинта (yougile: {content, paging})."""
        out: list[dict] = []
        offset = 0
        while True:
            p = dict(params or {})
            p.update({"limit": _PAGE, "offset": offset})
            r = self._client.get(path, params=p)
            r.raise_for_status()
            content = r.json().get("content", [])
            out.extend(content)
            if len(content) < _PAGE:
                break
            offset += len(content)
        return out

    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        count = 0
        for proj in self._get_all("/projects"):
            for brd in self._get_all("/boards", {"projectId": proj["id"]}):
                if board and board not in (brd.get("title", ""), proj.get("title", "")):
                    continue
                col_title = {c["id"]: c.get("title")
                             for c in self._get_all("/columns", {"boardId": brd["id"]})}
                for col_id in col_title:
                    for t in self._get_all("/tasks", {"columnId": col_id}):
                        yield RawTask(
                            key=t.get("idTaskCommon") or t["id"],
                            project_code=t.get("idTaskProject", ""),
                            title=t.get("title", ""),
                            description=t.get("description", "") or "",
                            status=col_title.get(t.get("columnId")),
                            subtask_ids=list(t.get("subtasks", []) or []),
                            timestamp=int(t.get("timestamp", 0) or 0),
                        )
                        count += 1
                        if limit and count >= limit:
                            return

    def normalize(self, raw: RawTask) -> dict:
        subtask_titles: dict[str, str] = {}
        for sid in raw.subtask_ids:
            try:
                r = self._client.get(f"/tasks/{sid}")
                r.raise_for_status()
                st = r.json()
                code = st.get("idTaskCommon") or sid
                subtask_titles[sid] = f"{code}:{st.get('title', '')}"
            except Exception:
                log.warning("yougile: не резолвится подзадача %s", sid, exc_info=True)
        return normalize_yougile(raw, self._key_pattern, self._url_template,
                                 subtask_titles)
