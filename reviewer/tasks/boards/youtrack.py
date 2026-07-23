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
_SAMPLE = 200  # число задач в выборке для fallback-агрегации значений полей статуса

_FIELDS = (
    "idReadable,summary,description,updated,"
    "customFields(name,value(name)),"
    "links(direction,linkType(name),issues(idReadable)),"
    "attachments(name,size,mimeType,extension,url)"
)

# Тип элемента бандла по типу кастом-поля YouTrack — нужен, когда текущее значение
# поля null (нельзя вывести `$type` элемента из value). Если тип поля не в маппинге —
# значение уходит без `$type` (YouTrack часто резолвит элемент по имени).
_FIELD_TO_ELEMENT = {
    "StateIssueCustomField": "StateBundleElement",
    "SingleEnumIssueCustomField": "EnumBundleElement",
    "SingleVersionIssueCustomField": "VersionBundleElement",
    "SingleBuildIssueCustomField": "BuildBundleElement",
    "SingleOwnedIssueCustomField": "OwnedBundleElement",
}


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

    def normalize_meta(self, raw: RawTask) -> dict:
        """Дешёвая нормализация без I/O (PRI-207): чистый normalize_youtrack без
        резолва вложений. Плоские поля (project/url/status) корректны."""
        return normalize_youtrack(raw, self._key_pattern, self._base)

    def _set_status(self, safe_key: str, state: str,
                    custom_fields: list[dict]) -> tuple[bool, list[str]]:
        """Структурно выставить поле статуса задачи (без command-DSL).

        custom_fields — уже прочитанные поля задачи (name/$type/value). Возвращает
        (успех, warnings). Поле не найдено или REST отверг значение → (False, warning).
        """
        warnings: list[str] = []
        field = next((cf for cf in custom_fields if cf.get("name") == self._status_field), None)
        if field is None:
            warnings.append(
                f"поле статуса {self._status_field!r} не найдено на задаче — статус не изменён")
            return False, warnings
        cur = field.get("value")
        value_type = (cur.get("$type") if isinstance(cur, dict) and cur.get("$type")
                      else _FIELD_TO_ELEMENT.get(field.get("$type")))
        value_obj: dict = {"name": state}
        if value_type:
            value_obj["$type"] = value_type
        payload = {"name": self._status_field, "$type": field.get("$type"), "value": value_obj}
        resp = self._client.post(f"/issues/{safe_key}", json={"customFields": [payload]})
        if getattr(resp, "status_code", 200) >= 400:
            warnings.append(
                f"не удалось установить {self._status_field}={state}: HTTP {resp.status_code}")
            return False, warnings
        return True, warnings

    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, done_state: str | None = None,
               done_column: str | None = None) -> dict:
        """Закрыть задачу YouTrack: правка описания (PR-ссылка) + структурная смена статуса.

        Первый GET тянет `description` и `customFields` (с типами). PR-ссылка дописывается
        отдельным `POST /issues/{key}` json={"description": …} (идемпотентный append,
        двигает `updated` — watermark синка).

        Смена статуса — **структурное REST-обновление кастом-поля**, а не command-DSL:
        `POST /issues/{key}` json={"customFields": [{"name": <status_field>, "$type": …,
        "value": {"name": <done_state>, …}}]}. Имя поля берётся из `self._status_field`
        (дефолт «State»), значение — из `done_state` (дефолт «Fixed»). YouTrack матчит
        значение по имени против существующих элементов бандла. **DSL здесь нет вообще**,
        поэтому инъекция через `done_state`/`status_field` структурно невозможна (значения
        идут в JSON, не в командную строку), а многословные имена полей («Kanban State»)
        и статусов («Готово к сдаче») работают без экранирования.

        Fail-soft: если поле `status_field` не найдено на задаче — warning, `done_set=False`,
        поле не трогаем; если REST-обновление вернуло ошибку (невалидное значение) — warning,
        `done_set=False`. PR-ссылка в любом случае уже записана. `done_column` принимается
        ради совместимости с Protocol и игнорируется (у YouTrack нет колонок).
        """
        safe_key = quote(key, safe="")
        r = self._client.get(
            f"/issues/{safe_key}",
            params={"fields": "description,customFields(name,$type,value($type,name))"})
        r.raise_for_status()
        body = r.json()
        desc = body.get("description", "") or ""
        custom_fields = body.get("customFields") or []

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
            done_set, w = self._set_status(safe_key, done_state or "Fixed", custom_fields)
            warnings.extend(w)

        return {"key": key, "board_id": key, "done_set": done_set,
                "pr_link_added": pr_link_added,
                "already_closed": not pr_link_added and not done_set,
                "warnings": warnings}

    def create(self, doc_md: str, *, title: str, target: str | None,
               project: str | None) -> dict:
        """Создать задачу YouTrack: POST /issues с markdown как есть.

        project (shortName) обязателен — без него YouTrack не примет задачу; это
        единственный не-fail-soft случай. target, если задан, выставляется как
        значение self._status_field тем же структурным REST, что в finish.
        """
        if not project:
            raise ValueError("project обязателен для создания задачи в YouTrack")
        pr = self._client.get("/admin/projects",
                              params={"fields": "id,shortName", "query": project})
        pr.raise_for_status()
        pid = next((p["id"] for p in (pr.json() or []) if p.get("shortName") == project), None)
        if not pid:
            raise ValueError(f"проект {project!r} не найден в YouTrack")

        r = self._client.post("/issues", params={"fields": "idReadable"},
                              json={"project": {"id": pid}, "summary": title,
                                    "description": doc_md})
        r.raise_for_status()
        key = (r.json() or {}).get("idReadable") or ""
        warnings: list[str] = []
        target_resolved = None
        if target and key:
            safe_key = quote(key, safe="")
            try:
                rr = self._client.get(
                    f"/issues/{safe_key}",
                    params={"fields": "customFields(name,$type,value($type,name))"})
                rr.raise_for_status()
                fields = (rr.json() or {}).get("customFields") or []
            except Exception:
                log.warning("youtrack: поля задачи %s недоступны", key, exc_info=True)
                fields = []
            ok, w = self._set_status(safe_key, target, fields)
            warnings.extend(w)
            target_resolved = target if ok else None
        web = re.sub(r"/api/?$", "", self._base.rstrip("/"))
        return {"key": key, "url": f"{web}/issue/{key}" if key else None,
                "board_id": key, "target_resolved": target_resolved,
                "warnings": warnings}

    def fetch_one(self, key: str) -> RawTask | None:
        """Один RawTask по idReadable — write-through после finish.

        GET /issues/{key} с богатым `fields` (_FIELDS) → _issue_to_raw. Имя поля
        статуса — self._status_field (per-repo). fail-soft: сбой/пусто → None."""
        try:
            r = self._client.get(f"/issues/{quote(key, safe='')}",
                                  params={"fields": _FIELDS})
            r.raise_for_status()
            issue = r.json()
        except Exception:
            log.warning("youtrack: fetch_one(%s) не удался", key, exc_info=True)
            return None
        if not issue:
            return None
        return _issue_to_raw(issue, self._status_field)

    def list_done_targets(self, project: str | None) -> dict:
        """Поля статуса + значения (read-only, fail-soft). Try admin customFields;
        при недоступности — агрегация distinct значений из выборки задач проекта.
        НИКОГДА не бросает."""
        warnings: list[str] = []
        try:
            fields = self._admin_status_fields(project)
            if fields:
                return {"status_fields": fields, "source": "admin", "warnings": warnings}
        except Exception:
            log.warning("youtrack: admin customFields недоступны — fallback", exc_info=True)
            warnings.append("admin customFields недоступны (нет прав?) — "
                            "значения собраны из задач")
        try:
            fields = self._sampled_status_fields(project)
        except Exception:
            log.warning("youtrack: discovery полей из выборки не удался", exc_info=True)
            warnings.append("не удалось собрать поля статуса из задач")
            fields = []
        return {"status_fields": fields, "source": "sample", "warnings": warnings}

    def _admin_status_fields(self, project: str | None) -> list[dict]:
        """Bundle-поля (state/enum) проекта из admin API + их значения. [] если project пуст
        или проект не найден. Бросает при ошибке HTTP — вызывающий ловит и фолбэкает."""
        if not project:
            return []
        pr = self._client.get("/admin/projects",
                              params={"fields": "id,shortName", "query": project})
        pr.raise_for_status()
        pid = next((p["id"] for p in (pr.json() or [])
                    if p.get("shortName") == project), None)
        if not pid:
            return []
        r = self._client.get(
            f"/admin/projects/{quote(str(pid), safe='')}/customFields",
            params={"fields": "field(name),$type,bundle(values(name,$type))"})
        r.raise_for_status()
        out: list[dict] = []
        for pcf in (r.json() or []):
            bundle = pcf.get("bundle") or {}
            values = [v.get("name") for v in (bundle.get("values") or []) if v.get("name")]
            if not values:
                continue  # не bundle-поле — не кандидат статуса
            name = (pcf.get("field") or {}).get("name")
            if name:
                out.append({"field": name, "values": values, "$type": pcf.get("$type")})
        return out

    def _sampled_status_fields(self, project: str | None) -> list[dict]:
        """Distinct значения single-value кастом-полей из выборки задач проекта.
        Бросает при ошибке HTTP — вызывающий ловит."""
        params: dict = {"fields": "customFields(name,value(name),$type)", "$top": _SAMPLE}
        if project:
            params["query"] = f"project: {project}"
        r = self._client.get("/issues", params=params)
        r.raise_for_status()
        agg: dict[str, dict] = {}  # field name -> {"values": [...], "$type": ...}
        for issue in (r.json() or []):
            for cf in issue.get("customFields") or []:
                name = cf.get("name")
                val = cf.get("value")
                vname = val.get("name") if isinstance(val, dict) else None
                if not name or not vname:
                    continue
                slot = agg.setdefault(name, {"values": [], "$type": cf.get("$type")})
                if vname not in slot["values"]:
                    slot["values"].append(vname)
        return [{"field": n, "values": s["values"], "$type": s["$type"]}
                for n, s in agg.items()]
