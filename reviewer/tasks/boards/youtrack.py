"""Провайдер доски YouTrack (JetBrains): REST-клиент (httpx) + нормализация.

REST API: base <instance>/api, заголовок Authorization: Bearer perm:<token>.
Один list-эндпоинт /issues с богатым `fields` отдаёт всё (idReadable, summary,
description, updated, State, links) без доп. запросов, поэтому normalize чистая,
а links резолвятся уже в iter_raw. Порт client-side маппинга задачи в TaskBrief.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from urllib.parse import quote, urljoin, urlsplit

import httpx

from reviewer.tasks.boards.attachments import fetch_attachment, host_allowed, _registrable_domain
from reviewer.tasks.boards.base import RawTask, project_prefix

log = logging.getLogger(__name__)

_PAGE = 200

_FIELDS = (
    "idReadable,summary,description,updated,"
    "customFields(name,value(name)),"
    "links(direction,linkType(name),issues(idReadable)),"
    "attachments(name,size,mimeType,extension,url)"
)


def _state_of(issue: dict, field: str = "State") -> str | None:
    """Статус задачи — кастом-поле `field` (дефолт «State»), его value.name."""
    for cf in issue.get("customFields") or []:
        if cf.get("name") == field:
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


def _attachments_of(issue: dict) -> list[dict]:
    """Метаданные вложений из issue (url относительный с подписью; без url — пропуск)."""
    out: list[dict] = []
    for a in issue.get("attachments") or []:
        url = a.get("url")
        if not url:
            continue
        out.append({"name": a.get("name") or "", "mime": a.get("mimeType"),
                    "size": a.get("size"), "url": url})
    return out


def _origin(base_url: str) -> str:
    """scheme://host из base_url (отбрасывает путь /api) — для абсолютного URL файла."""
    p = urlsplit(base_url)
    return f"{p.scheme}://{p.netloc}"


def _issue_to_raw(issue: dict, status_field: str = "State") -> RawTask:
    """YouTrack issue JSON → RawTask. Чистая: без I/O."""
    key = issue.get("idReadable", "")
    return RawTask(
        key=key,
        project_code=key,                       # один счётчик idReadable, второго кода нет
        title=issue.get("summary", "") or "",
        description=issue.get("description", "") or "",
        status=_state_of(issue, status_field),
        subtask_ids=[],
        timestamp=int(issue.get("updated", 0) or 0),
        links=_links_of(issue),
        attachments=_attachments_of(issue),
    )


def normalize_youtrack(raw: RawTask, key_pattern: str, base_url: str,
                       attachments: list[dict] | None = None) -> dict:
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
        "attachments": attachments or [],
    }


class YouTrackBoard:
    """REST-провайдер YouTrack. iter_raw — один пагинированный /issues-запрос
    с богатым `fields`; normalize чистая (всё уже в RawTask)."""

    board_type = "youtrack"

    def __init__(self, *, token: str, base_url: str, key_pattern: str,
                 status_field: str = "State",
                 attachment_max_bytes: int = 10 * 1024 * 1024,
                 attachment_timeout: float = 10.0,
                 attachment_store_chars: int = 200000) -> None:
        """Инициализация REST-клиента YouTrack.

        ``token`` — **полный** постоянный токен YouTrack, включая префикс ``perm:``,
        например ``perm:abc123...``. Именно в таком виде YouTrack выдаёт токены;
        значение env-переменной ``YOUTRACK_TOKEN`` передаётся сюда напрямую.
        Токен отправляется verbatim в заголовке ``Authorization: Bearer <token>``
        — код *не добавляет* «perm:» самостоятельно (иначе вышло бы ``Bearer perm:perm:...``).
        """
        self._key_pattern = key_pattern
        self._status_field = status_field or "State"
        self._base = base_url.rstrip("/")
        self._att_domains = (_registrable_domain(urlsplit(self._base).netloc.split("@")[-1].split(":")[0]),)
        self._att_max_bytes = attachment_max_bytes
        self._att_timeout = attachment_timeout
        self._att_store_chars = attachment_store_chars
        self._client = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def set_status_field(self, field: str | None) -> None:
        """Переустановить имя поля статуса (per-repo из .review.yml) для синка.

        Провайдер синка — долгоживущий singleton; SyncService выставляет поле
        перед iter_raw и сбрасывает к «State» при отсутствии конфига.
        """
        self._status_field = field or "State"

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
                yield _issue_to_raw(issue, self._status_field)
                count += 1
                if limit and count >= limit:
                    return
            if len(page) < _PAGE:
                return
            skip += len(page)

    def normalize(self, raw: RawTask) -> dict:
        origin = _origin(self._base)
        contents: list[dict] = []
        for a in raw.attachments:
            full = urljoin(origin + "/", a["url"])
            if not host_allowed(full, self._att_domains):
                log.warning("youtrack: вложение %s вне домена доски — пропуск", full)
                continue
            contents.append(fetch_attachment(
                self._client, name=a["name"], mime=a.get("mime"), size=a.get("size"),
                url=full, timeout=self._att_timeout,
                max_bytes=self._att_max_bytes, store_chars=self._att_store_chars))
        return normalize_youtrack(raw, self._key_pattern, self._base,
                                  attachments=contents)

    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, done_state: str | None = None,
               done_column: str | None = None) -> dict:
        """Закрыть задачу YouTrack: правка описания (PR-ссылка) + команда State.

        POST /issues/{key} правит описание (двигает `updated` — watermark синка).
        POST /commands шлёт `State <done_state or 'Fixed'>` — YouTrack сам резолвит
        значение в проекте; неуспех команды fail-soft (warnings, без краха).
        """
        safe_key = quote(key, safe="")
        r = self._client.get(f"/issues/{safe_key}", params={"fields": "description"})
        r.raise_for_status()
        desc = r.json().get("description", "") or ""

        pr_link_added = False
        if pr_url and pr_url not in desc:
            block = f"\n\nPR: {pr_url}" + (f"\n\n{note}" if note else "")
            new_desc = desc + block if desc else block.lstrip("\n")
            rr = self._client.post(f"/issues/{safe_key}", json={"description": new_desc})
            rr.raise_for_status()
            pr_link_added = True

        warnings: list[str] = []
        done_set = False
        if mark_done:
            # done_state И имя поля приходят из .review.yml → чужой командной строкой
            # в DSL YouTrack не должны становиться (command-injection). Убираем фигурные
            # скобки и оборачиваем значение в {…} — YouTrack трактует его как единый литерал.
            field = self._status_field.replace("{", "").replace("}", "")
            state = (done_state or "Fixed").replace("{", "").replace("}", "")
            cmd = self._client.post(
                "/commands",
                json={"query": f"{field} {{{state}}}", "issues": [{"idReadable": key}]})
            if getattr(cmd, "status_code", 200) >= 400:
                warnings.append(f"команда '{field} {state}' не выполнена: HTTP {cmd.status_code}")
            else:
                done_set = True

        return {"key": key, "board_id": key, "done_set": done_set,
                "pr_link_added": pr_link_added,
                "already_closed": not pr_link_added and not done_set,
                "warnings": warnings}
