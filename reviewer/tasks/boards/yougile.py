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
from urllib.parse import unquote, urlsplit

import httpx

from reviewer.tasks.boards.attachments import fetch_attachment
from reviewer.tasks.boards.base import RawTask, project_prefix

log = logging.getLogger(__name__)

_PAGE = 1000

# YouGile кодирует прикреплённый к чату файл маркером /root/#file:<url> в text сообщения
# (POST /upload-file → {url}; см. send_task_file). Структурного списка файлов в API нет.
_FILE_MARKER = re.compile(r"/root/#file:(\S+)")


def _file_urls_from_text(text: str | None) -> list[str]:
    """Абсолютные URL файлов из YouGile-маркера /root/#file:<url> в тексте сообщения.

    Если маркер пришёл из textHtml — обрезаем возможный HTML/кавычки-хвост.
    """
    out: list[str] = []
    for m in _FILE_MARKER.finditer(text or ""):
        url = re.split(r"[\"'<>\s]", m.group(1))[0]
        if url:
            out.append(url)
    return out


def _filename_from_url(url: str) -> str:
    """Имя файла из basename URL (для диспатча парсинга по расширению)."""
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return name or "file"


def normalize_yougile(
    raw: RawTask,
    key_pattern: str,
    url_template: str,
    subtask_titles: dict[str, str] | None = None,
    attachments: list[dict] | None = None,
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
        "project": project_prefix(raw.project_code or key),
        "attachments": attachments or [],
    }


class YougileBoard:
    """REST-провайдер Yougile. iter_raw перечисляет доску; normalize резолвит
    title подзадач (best-effort) и нормализует через normalize_yougile."""

    board_type = "yougile"

    def __init__(self, *, api_key: str, api_base: str, key_pattern: str,
                 url_template: str,
                 attachment_max_bytes: int = 10 * 1024 * 1024,
                 attachment_timeout: float = 10.0,
                 attachment_store_chars: int = 200000) -> None:
        self._key_pattern = key_pattern
        self._url_template = url_template
        self._att_max_bytes = attachment_max_bytes
        self._att_timeout = attachment_timeout
        self._att_store_chars = attachment_store_chars
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
                col_title = {c["id"]: c.get("title")
                             for c in self._get_all("/columns", {"boardId": brd["id"]})}
                for col_id in col_title:
                    for t in self._get_all("/tasks", {"columnId": col_id}):
                        project_code = t.get("idTaskProject", "")
                        # PRI-170: scoped-синк ограничивает доску одним проектом по
                        # префиксу кода (board == project_prefix), а не по title.
                        if board and project_prefix(project_code) != board:
                            continue
                        yield RawTask(
                            key=t.get("idTaskCommon") or t["id"],
                            project_code=project_code,
                            title=t.get("title", ""),
                            description=t.get("description", "") or "",
                            status=col_title.get(t.get("columnId")),
                            subtask_ids=list(t.get("subtasks", []) or []),
                            timestamp=int(t.get("timestamp", 0) or 0),
                            board_id=t["id"],
                        )
                        count += 1
                        if limit and count >= limit:
                            return

    def _attachments_from_chat(self, task_uuid: str) -> list[dict]:
        """Вложения из сообщений чата задачи (best-effort, fail-soft).

        YouGile не отдаёт структурного списка файлов — они вшиты маркером
        /root/#file:<url> в text/textHtml сообщений. chatId == внутренний UUID задачи.
        Пустой/недоступный чат → []. fetch_attachment сам fail-soft (битый файл →
        только метаданные). Имя файла — basename URL; mime неизвестен (диспатч по расширению).
        """
        if not task_uuid:
            return []
        try:
            r = self._client.get(f"/chats/{task_uuid}/messages")
            r.raise_for_status()
            msgs = r.json().get("content", []) or []
        except Exception:
            log.warning("yougile: чат задачи %s недоступен", task_uuid, exc_info=True)
            return []
        seen: set[str] = set()
        out: list[dict] = []
        for msg in msgs:
            props = msg.get("properties") or {}
            for field_text in (msg.get("text"), props.get("textHtml")):
                for url in _file_urls_from_text(field_text):
                    if url in seen:
                        continue
                    seen.add(url)
                    out.append(fetch_attachment(
                        self._client, name=_filename_from_url(url), mime=None,
                        size=None, url=url, timeout=self._att_timeout,
                        max_bytes=self._att_max_bytes, store_chars=self._att_store_chars))
        return out

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
        attachments = self._attachments_from_chat(raw.board_id)
        return normalize_yougile(raw, self._key_pattern, self._url_template,
                                 subtask_titles, attachments=attachments)
