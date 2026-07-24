"""Провайдер доски YouTrack (JetBrains): REST-клиент (httpx) + нормализация.

REST API: base <instance>/api, заголовок Authorization: Bearer perm:<token>.
Один list-эндпоинт /issues с богатым `fields` отдаёт всё (idReadable, summary,
description, updated, State, links) без доп. запросов, поэтому normalize чистая,
а links резолвятся уже в iter_raw. Порт client-side маппинга задачи в TaskBrief.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from reviewer.tasks.boards.attachments import fetch_attachment, host_allowed, _registrable_domain
from reviewer.tasks.boards.base import RawTask, project_prefix
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.http import BoardHttpClient
from reviewer.tasks.boards.registry import (
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderOptionSpec,
    ProviderSetupSpec,
)

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


def _build_provider(context: ProviderBuildContext) -> YouTrackBoard:
    status_field = context.options.get("status_field") or "State"
    return YouTrackBoard(
        token=context.credentials["YOUTRACK_TOKEN"],
        base_url=context.credentials["YOUTRACK_BASE_URL"],
        key_pattern=context.key_pattern,
        status_field=str(status_field),
        attachment_max_bytes=context.attachment_max_bytes,
        attachment_timeout=context.attachment_timeout,
        attachment_store_chars=context.attachment_store_chars,
    )


def _permanent_token_url(credentials: Mapping[str, str]) -> str:
    base_url = credentials.get("YOUTRACK_BASE_URL", "").rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/api")
    ):
        raise BoardProviderError(
            "configuration",
            "YouTrack base URL must be an HTTPS API URL ending in /api.",
            hint="Use https://<instance>/api.",
        )
    instance_path = parsed.path.removesuffix("/api")
    return f"https://{parsed.netloc}{instance_path}/users/me?tab=accountSecurity"


def provider_spec() -> BoardProviderSpec:
    """Immutable registry spec YouTrack."""
    return BoardProviderSpec(
        board_type="youtrack",
        factory=_build_provider,
        credential_fields=(
            CredentialFieldSpec(
                env="YOUTRACK_BASE_URL",
                label="YouTrack API base URL",
            ),
            CredentialFieldSpec(
                env="YOUTRACK_TOKEN",
                label="YouTrack permanent token",
                secret=True,
            ),
        ),
        option_fields=(
            ProviderOptionSpec(
                key="status_field",
                label="Status field",
                required_for=("sync", "create", "finish"),
            ),
        ),
        setup=ProviderSetupSpec(
            label="YouTrack",
            help_url="https://www.jetbrains.com/help/youtrack/devportal/authentication-with-permanent-token.html",
            help_text=(
                "Создайте permanent token, выберите YouTrack service scope и "
                "сохраните полный token с префиксом perm:. Синтезированная instance "
                "Account Security URL — только удобная ссылка для bundled Hub. "
                "При external Hub откройте Profile в YouTrack и следуйте ссылке "
                "Account Security в профиль Hub."
            ),
            help_url_builder=_permanent_token_url,
        ),
        create_target_label="Статус создания",
        done_target_label="Статус завершения",
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
        self._http = BoardHttpClient(self._client, secrets=(token,))
        self._http_raw = self._client

    def close(self) -> None:
        self._board_http().close()

    def _board_http(self) -> BoardHttpClient:
        """Обёртка JSON-транспорта; lazy-ветка поддерживает тестовые fake clients."""
        if getattr(self, "_http_raw", None) is not self._client:
            self._http = BoardHttpClient(self._client, attempts=1)
            self._http_raw = self._client
        return self._http

    def _read(self, path: str, **kwargs: Any) -> Any:
        return self._board_http().request_json("GET", path, operation="read", **kwargs)

    def _write(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._board_http().request_json(method, path, operation="write", **kwargs)

    def validate_connection(self, project: str | None = None) -> dict:
        """Проверить текущего пользователя и доступ к выбранному проекту."""
        identity = self._read("/users/me", params={"fields": "id,login,name"}) or {}
        project_info = None
        if project:
            projects = self._read(
                "/admin/projects",
                params={"fields": "id,shortName,name", "query": project},
            ) or []
            project_info = next(
                (
                    {"id": item.get("id"), "key": item.get("shortName"), "name": item.get("name")}
                    for item in projects
                    if item.get("shortName") == project
                ),
                None,
            )
            if project_info is None:
                raise BoardProviderError(
                    "not_found",
                    "YouTrack project is not accessible.",
                    hint="Check the project key and token permissions.",
                )
        return {
            "status": "ok",
            "identity": {
                "id": identity.get("id"),
                "login": identity.get("login"),
                "name": identity.get("name"),
            },
            "project": project_info,
            "capabilities": ["sync", "create", "finish", "attachments"],
            "warnings": [],
        }

    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        count = 0
        skip = 0
        while True:
            params: dict = {"fields": _FIELDS, "$top": _PAGE, "$skip": skip}
            if board:
                params["query"] = f"project: {board}"
            page = self._read("/issues", params=params) or []
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
        try:
            self._write(
                "POST",
                f"/issues/{safe_key}",
                json={"customFields": [payload]},
            )
        except BoardProviderError as exc:
            warnings.append(
                f"не удалось установить {self._status_field}={state}: {exc.category}"
            )
            return False, warnings
        return True, warnings

    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, target: str | None = None) -> dict:
        """Закрыть задачу YouTrack: правка описания (PR-ссылка) + структурная смена статуса.

        Первый GET тянет `description` и `customFields` (с типами). PR-ссылка дописывается
        отдельным `POST /issues/{key}` json={"description": …} (идемпотентный append,
        двигает `updated` — watermark синка).

        Смена статуса — **структурное REST-обновление кастом-поля**, а не command-DSL:
        `POST /issues/{key}` json={"customFields": [{"name": <status_field>, "$type": …,
        "value": {"name": <target>, …}}]}. Имя поля берётся из `self._status_field`
        (дефолт «State»), значение — из `target` (дефолт «Fixed»). YouTrack матчит
        значение по имени против существующих элементов бандла. **DSL здесь нет вообще**,
        поэтому инъекция через `target`/`status_field` структурно невозможна (значения
        идут в JSON, не в командную строку), а многословные имена полей («Kanban State»)
        и статусов («Готово к сдаче») работают без экранирования.

        Fail-soft: если поле `status_field` или target не найден — warning,
        `done_set=False`, поле не трогаем; если REST-обновление вернуло ошибку —
        warning, `done_set=False`. PR-ссылка в любом случае уже записана.
        """
        safe_key = quote(key, safe="")
        body = self._read(
            f"/issues/{safe_key}",
            params={"fields": "description,customFields(name,$type,value($type,name))"},
        ) or {}
        desc = body.get("description", "") or ""
        custom_fields = body.get("customFields") or []

        pr_link_added = False
        if pr_url and pr_url not in desc:
            block = f"\n\nPR: {pr_url}" + (f"\n\n{note}" if note else "")
            new_desc = desc + block if desc else block.lstrip("\n")
            self._write("POST", f"/issues/{safe_key}", json={"description": new_desc})
            pr_link_added = True

        warnings: list[str] = []
        done_set = False
        if mark_done:
            resolved = target or "Fixed"
            current = _state_of({"customFields": custom_fields}, self._status_field)
            if current != resolved:
                done_set, status_warnings = self._set_status(
                    safe_key,
                    resolved,
                    custom_fields,
                )
                warnings.extend(status_warnings)

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
        pid = self._resolve_project_id(project)
        if not pid:
            raise ValueError(f"проект {project!r} не найден в YouTrack")

        created = self._write(
            "POST",
            "/issues",
            params={"fields": "idReadable"},
            json={"project": {"id": pid}, "summary": title, "description": doc_md},
        )
        key = (created or {}).get("idReadable") or ""
        warnings: list[str] = []
        target_resolved = None
        if not key:
            # Ответ 200, но без idReadable — задача, вероятно, создана, но её ключ
            # неизвестен: без него нельзя ни выставить target, ни вернуть рабочую
            # ссылку. Тихо не проглатываем — это не полный успех.
            warnings.append(
                "ответ YouTrack не содержит idReadable — задача создана, "
                "но её ключ не определён")
            if target:
                warnings.append(
                    f"target={target!r} не применён: ключ задачи неизвестен")
        elif target:
            safe_key = quote(key, safe="")
            try:
                fields = (
                    self._read(
                        f"/issues/{safe_key}",
                        params={"fields": "customFields(name,$type,value($type,name))"},
                    )
                    or {}
                ).get("customFields") or []
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

    def _resolve_project_id(self, project: str) -> str | None:
        """Внутренний id проекта YouTrack по shortName (query-фильтром на сервере).

        Общий хелпер для `create` и `_admin_status_fields` — оба резолвят один
        и тот же `GET /admin/projects`. None, если проект не найден. Бросает при
        ошибке HTTP — вызывающий сам решает, fail-soft это или нет.
        """
        projects = self._read(
            "/admin/projects",
            params={"fields": "id,shortName", "query": project},
        ) or []
        return next((p["id"] for p in projects if p.get("shortName") == project), None)

    def fetch_one(self, key: str) -> RawTask | None:
        """Один RawTask по idReadable — write-through после finish.

        GET /issues/{key} с богатым `fields` (_FIELDS) → _issue_to_raw. Имя поля
        статуса — self._status_field (per-repo). fail-soft: сбой/пусто → None."""
        try:
            issue = self._read(
                f"/issues/{quote(key, safe='')}",
                params={"fields": _FIELDS},
            )
        except Exception:
            log.warning("youtrack: fetch_one(%s) не удался", key, exc_info=True)
            return None
        if not issue:
            return None
        return _issue_to_raw(issue, self._status_field)

    def _discover_status_fields(self, project: str | None) -> tuple[list[dict], list[str]]:
        """Старые status fields для преобразования в общую discovery-модель."""
        warnings: list[str] = []
        try:
            fields = self._admin_status_fields(project)
            if fields:
                return fields, warnings
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
        return fields, warnings

    def list_targets(self, project: str | None) -> dict:
        """Статусы immutable status_field и доступные status-field options."""
        fields, warnings = self._discover_status_fields(project)
        selected = next((field for field in fields if field["field"] == self._status_field), None)
        values = selected["values"] if selected is not None else []
        if selected is None and fields:
            warnings.append(
                f"поле статуса {self._status_field!r} не найдено — targets недоступны"
            )
        return {
            "targets": [
                {"id": value, "label": value, "purposes": ["create", "done"]}
                for value in values
            ],
            "options": [
                {
                    "key": "status_field",
                    "label": "Status field",
                    "required_for": ["sync", "create", "finish"],
                    "choices": [
                        {"id": field["field"], "label": field["field"]}
                        for field in fields
                    ],
                }
            ],
            "warnings": warnings,
        }

    def _admin_status_fields(self, project: str | None) -> list[dict]:
        """Bundle-поля (state/enum) проекта из admin API + их значения. [] если project пуст
        или проект не найден. Бросает при ошибке HTTP — вызывающий ловит и фолбэкает."""
        if not project:
            return []
        pid = self._resolve_project_id(project)
        if not pid:
            return []
        payload = self._read(
            f"/admin/projects/{quote(str(pid), safe='')}/customFields",
            params={"fields": "field(name),$type,bundle(values(name,$type))"},
        ) or []
        out: list[dict] = []
        for pcf in payload:
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
        payload = self._read("/issues", params=params) or []
        agg: dict[str, dict] = {}  # field name -> {"values": [...], "$type": ...}
        for issue in payload:
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
