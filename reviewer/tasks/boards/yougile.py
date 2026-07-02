"""Провайдер доски Yougile: REST-клиент (httpx) + нормализация в TaskBrief.

REST API v2: base https://yougile.com/api-v2, заголовок Authorization: Bearer <key>.
Перечисление: projects → boards → columns → tasks (listing-эндпоинты дают полные
объекты задач + проход columns для title статусов). normalize резолвит title
подзадач best-effort и зовёт чистую normalize_yougile.

Нормализация — порт плейбука plugin/skills/review-pr/references/task-context-yougile.md.
"""
from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterable
from urllib.parse import quote, unquote, urlsplit

import httpx

from reviewer.tasks.boards.attachments import fetch_attachment, host_allowed, _registrable_domain
from reviewer.tasks.boards.base import RawTask, project_prefix

log = logging.getLogger(__name__)

_PAGE = 1000

# Структурного списка файлов YouGile в API не отдаёт. Файл попадает в задачу двумя путями:
#  1) прикреплённый к задаче — HTML-ссылкой <a href="…/user-data/…"> в description
#     (реальная модель, подтверждена на живой доске — PRI-196/PRI-197);
#  2) прикреплённый в чате — маркером /root/#file:<url> в text/textHtml сообщения
#     (POST /upload-file → {url}; см. send_task_file).
_FILE_MARKER = re.compile(r"/root/#file:(\S+)")
_HREF = re.compile(r"""href=["']([^"']+)["']""")
_USER_DATA = "/user-data/"  # путь хранилища загруженных файлов YouGile


def _file_urls_from_text(text: str | None) -> list[str]:
    """URL загруженных в YouGile файлов из текста/HTML сообщения или описания задачи.

    Два источника: маркер ``/root/#file:<url>`` (доверяем — это всегда файл) и HTML-ссылка
    ``<a href="…">``, ограниченная путём ``/user-data/`` (хранилище файлов), чтобы не качать
    произвольные ссылки описания — на связанные задачи и т.п. ``&amp;`` в href разэкранируется.
    """
    text = text or ""
    out: list[str] = []
    for m in _FILE_MARKER.finditer(text):
        url = re.split(r"[\"'<>\s]", m.group(1))[0]
        if url:
            out.append(url)
    for m in _HREF.finditer(text):
        url = html.unescape(m.group(1)).strip()
        if url and _USER_DATA in urlsplit(url).path:
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
        "status": "done" if raw.completed else raw.status,
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
        self._att_domains = (_registrable_domain(urlsplit(self._base).netloc.split("@")[-1].split(":")[0]),)
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
                            completed=bool(t.get("completed", False)),
                        )
                        count += 1
                        if limit and count >= limit:
                            return

    def _chat_texts(self, task_uuid: str) -> list[str]:
        """text + textHtml всех сообщений чата задачи (best-effort, fail-soft → []).

        chatId == внутренний UUID задачи. Пустой/недоступный чат → [].
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
        texts: list[str] = []
        for msg in msgs:
            props = msg.get("properties") or {}
            texts.append(msg.get("text") or "")
            texts.append(props.get("textHtml") or "")
        return texts

    def _fetch_urls(self, urls: list[str], seen: set[str], out: list[dict]) -> None:
        """Скачать+распарсить новые (вне seen) URL; off-host пропустить (токен/SSRF).

        fetch_attachment сам fail-soft (битый файл → только метаданные). Имя файла —
        basename URL; mime неизвестен (диспатч парсинга по расширению).
        """
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            if not host_allowed(url, self._att_domains):
                log.warning("yougile: файл %s вне домена доски — пропуск", url)
                continue
            out.append(fetch_attachment(
                self._client, name=_filename_from_url(url), mime=None,
                size=None, url=url, timeout=self._att_timeout,
                max_bytes=self._att_max_bytes, store_chars=self._att_store_chars))

    def _collect_attachments(self, description: str | None, task_uuid: str) -> list[dict]:
        """Вложения задачи: файл-ссылки на /user-data/ из description + файлы из чата.

        Источники объединяются и дедупятся по URL. best-effort: сбой любого слоя не валит
        normalize (см. _chat_texts/_fetch_urls). Описание сканируется первым — это основной
        путь прикрепления файла к задаче в YouGile (PRI-196).
        """
        seen: set[str] = set()
        out: list[dict] = []
        self._fetch_urls(_file_urls_from_text(description), seen, out)
        for text in self._chat_texts(task_uuid):
            self._fetch_urls(_file_urls_from_text(text), seen, out)
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
        attachments = self._collect_attachments(raw.description, raw.board_id)
        return normalize_yougile(raw, self._key_pattern, self._url_template,
                                 subtask_titles, attachments=attachments)

    def fetch_one(self, key: str) -> RawTask | None:
        """Один RawTask по ключу (проектный/компанийный код) — write-through после finish.

        GET /tasks/{key} (тот же вызов, что и в finish); title колонки резолвится
        best-effort через GET /columns/{columnId}. fail-soft: сбой/404 → None."""
        try:
            r = self._client.get(f"/tasks/{quote(key, safe='')}")
            r.raise_for_status()
            t = r.json()
        except Exception:
            log.warning("yougile: fetch_one(%s) не удался", key, exc_info=True)
            return None
        status = None
        col_id = t.get("columnId")
        if col_id:
            try:
                rc = self._client.get(f"/columns/{quote(str(col_id), safe='')}")
                rc.raise_for_status()
                status = rc.json().get("title")
            except Exception:
                log.warning("yougile: колонка задачи %s недоступна — status=None",
                            key, exc_info=True)
        return RawTask(
            key=t.get("idTaskCommon") or t.get("id") or key,
            project_code=t.get("idTaskProject", "") or "",
            title=t.get("title", "") or "",
            description=t.get("description", "") or "",
            status=status,
            subtask_ids=list(t.get("subtasks", []) or []),
            timestamp=int(t.get("timestamp", 0) or 0),
            board_id=t.get("id") or key,
            completed=bool(t.get("completed", False)),
        )

    def _resolve_column_id(self, current_col_id: str, title: str) -> str | None:
        """id колонки с заданным title на той же доске, что и current_col_id.

        GET /columns/{cur} → boardId; GET /columns?boardId=… → match по title.
        fail-soft: сетевой сбой/не найдено → None (задачу не двигаем)."""
        try:
            r = self._client.get(f"/columns/{quote(str(current_col_id), safe='')}")
            r.raise_for_status()
            board_id = r.json().get("boardId")
            if not board_id:
                log.warning("yougile: не определить доску колонки '%s' — задачу не двигаем", title)
                return None
            for col in self._get_all("/columns", {"boardId": board_id}):
                if col.get("title") == title:
                    return col.get("id")
        except Exception:
            log.warning("yougile: резолв колонки '%s' не удался", title, exc_info=True)
        return None

    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, done_state: str | None = None,
               done_column: str | None = None) -> dict:
        """Закрыть задачу YouGile: completed:true + PR-ссылка в описание (идемпотентно)
        + опциональный перенос в done-колонку (done_column).

        GET /tasks/{key} резолвит проектный/компанийный код в объект (+ uuid, columnId).
        PUT обновляет задачу — двигает её timestamp (watermark синка). done_state не
        применим (у YouGile булев completed)."""
        r = self._client.get(f"/tasks/{quote(key, safe='')}")
        r.raise_for_status()
        task = r.json()
        uuid = task.get("id") or key
        desc = task.get("description", "") or ""
        completed = bool(task.get("completed", False))
        cur_col = task.get("columnId")

        payload: dict = {}
        warnings: list[str] = []
        pr_link_added = False
        # PR-ссылка и note уходят в HTML-описание доски — экранируем во избежание
        # HTML/XSS-инъекции (note приходит от пользователя). Idempotency-проверка
        # сравнивает с экранированной формой (для обычных URL совпадает с сырой).
        safe_url = html.escape(pr_url, quote=True)
        if pr_url and safe_url not in desc:
            block = f'\n<div>PR: <a href="{safe_url}">{safe_url}</a></div>'
            if note:
                block += f"\n<div>{html.escape(note)}</div>"
            payload["description"] = desc + block
            pr_link_added = True

        column_moved = False
        if done_column and cur_col:
            target = self._resolve_column_id(cur_col, done_column)
            if target is None:
                warnings.append(f"колонка '{done_column}' не найдена — задача не перенесена")
            elif target != cur_col:
                payload["columnId"] = target
                column_moved = True

        done_set = False
        if mark_done and not completed:
            payload["completed"] = True
            done_set = True

        if payload:
            rr = self._client.put(f"/tasks/{quote(str(uuid), safe='')}", json=payload)
            rr.raise_for_status()
        return {"key": key, "board_id": uuid, "done_set": done_set,
                "pr_link_added": pr_link_added, "column_moved": column_moved,
                "already_closed": not payload, "warnings": warnings}

    def list_done_targets(self, project: str | None) -> dict:
        """Колонки досок проекта (read-only, fail-soft). project — код-префикс задач
        (напр. PRI): доска включается, если на ней есть хоть одна задача проекта. Пустой
        project → все доски всех проектов. НИКОГДА не бросает."""
        warnings: list[str] = []
        boards: list[dict] = []           # [{board_id, board_title, columns:[{id,title}]}]
        hosts: set[str] = set()           # board_id, где встречена задача проекта
        scanned = 0
        _CAP = 500                        # предохранитель на число просканированных задач
        try:
            for proj in self._get_all("/projects"):
                for brd in self._get_all("/boards", {"projectId": proj["id"]}):
                    bid = brd["id"]
                    cols = [{"id": c["id"], "title": c.get("title", "")}
                            for c in self._get_all("/columns", {"boardId": bid})]
                    boards.append({"board_id": bid, "board_title": brd.get("title", ""),
                                   "columns": cols})
                    if not project:
                        continue
                    for c in cols:
                        if scanned >= _CAP:
                            break
                        hit = False
                        for t in self._get_all("/tasks", {"columnId": c["id"]}):
                            scanned += 1
                            if project_prefix(t.get("idTaskProject", "")) == project:
                                hit = True
                                break
                            if scanned >= _CAP:
                                break
                        if hit:
                            hosts.add(bid)
                            break
        except Exception:
            log.warning("yougile: discovery колонок не удался", exc_info=True)
            warnings.append("не удалось перечислить колонки доски")
        kept = boards if not project else [b for b in boards if b["board_id"] in hosts]
        columns = [{"title": col["title"], "id": col["id"],
                    "board_id": b["board_id"], "board_title": b["board_title"]}
                   for b in kept for col in b["columns"]]
        if project and not hosts and not warnings:
            warnings.append(f"колонки для проекта {project!r} не найдены")
        return {"columns": columns, "warnings": warnings}
