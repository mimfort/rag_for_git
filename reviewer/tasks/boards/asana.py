"""Провайдер доски Asana (REST API 1.0).

Транспорт — общий ``RestBoardBase`` (httpx + retry + вымарывание секретов),
пагинация — ``pagination.paginate_cursor`` по opaque-курсору ``next_page.offset``.
Описание задачи Asana хранит в собственном подмножестве HTML (``html_notes``),
поэтому на чтении оно конвертируется в markdown (``markup.html_to_md``), а на
записи markdown собирается обратно в Asana-HTML (``_md_to_asana_html``).
"""
from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from reviewer.tasks.boards.attachments import attachment_supported, fetch_attachment
from reviewer.tasks.boards.base import RawTask, TaskListing, TaskListingStats, project_prefix
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.markup import html_to_md, md_to_html
from reviewer.tasks.boards.pagination import paginate_cursor
from reviewer.tasks.boards.registry import (
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderOptionSpec,
    ProviderSetupSpec,
)
from reviewer.tasks.boards.restbase import RestBoardBase

log = logging.getLogger(__name__)

_PAGE = 100                      # Asana: limit 1..100
_API_BASE = "https://app.asana.com/api/1.0"


def _data(payload: Any) -> list:
    """Строки страницы Asana: ключ ``data``; чужая форма → пустой список."""
    rows = (payload or {}).get("data")
    return list(rows) if isinstance(rows, list) else []


def _next_offset(payload: Any) -> str | None:
    """Opaque-курсор следующей страницы (``next_page.offset``); его нельзя конструировать."""
    next_page = (payload or {}).get("next_page")
    if not isinstance(next_page, Mapping):
        return None
    offset = next_page.get("offset")
    return str(offset) if offset else None


def _timestamp(value: object) -> int | None:
    """ISO 8601 ``modified_at`` → epoch ms; мусор/пусто остаются неизвестными."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _build_provider(context: ProviderBuildContext) -> AsanaBoard:
    """Собрать адаптер из registry-контекста: креды, опции, attachment-лимиты."""
    return AsanaBoard(
        access_token=context.credentials["ASANA_ACCESS_TOKEN"],
        api_base=context.credentials.get("ASANA_API_BASE", ""),
        project_gid=str(context.options.get("project_gid") or ""),
        key_prefix=str(context.options.get("key_prefix") or ""),
        key_pattern=context.key_pattern,
        url_template=context.url_template,
        attachment_max_bytes=context.attachment_max_bytes,
        attachment_timeout=context.attachment_timeout,
        attachment_store_chars=context.attachment_store_chars,
    )


def provider_spec() -> BoardProviderSpec:
    """Immutable registry spec Asana (подключение в реестр — фаза C)."""
    return BoardProviderSpec(
        board_type="asana",
        factory=_build_provider,
        credential_fields=(
            CredentialFieldSpec(
                env="ASANA_ACCESS_TOKEN",
                label="Asana personal access token",
                secret=True,
            ),
            CredentialFieldSpec(
                env="ASANA_API_BASE",
                label="Asana API base URL",
                required=False,
                default=_API_BASE,
            ),
        ),
        setup=ProviderSetupSpec(
            label="Asana",
            help_url="https://app.asana.com/0/my-apps",
            help_text=(
                "Создайте personal access token в Asana: My Settings → Apps → "
                "Manage developer apps (app.asana.com/0/my-apps) → Create new token. "
                "Токен показывается один раз — сохраните его сразу."
            ),
        ),
        option_fields=(
            ProviderOptionSpec(
                key="project_gid",
                label="Asana project gid",
                required_for=("sync", "create", "finish"),
            ),
            ProviderOptionSpec(
                key="key_prefix",
                label="Префикс ключа задачи",
                required_for=("sync",),
            ),
        ),
        default_api_base=_API_BASE,
        create_target_label="Секция создания",
        done_target_label="Секция завершения",
    )


def _md_to_asana_html(md: str) -> str:
    """Канонический markdown → Asana-HTML для ``html_notes``.

    Asana принимает только своё подмножество тегов с обязательным корнем ``<body>``:
    нет ``<p>`` и ``<br/>`` (абзацы разделяются переводами строк), из заголовков —
    только ``<h1>``/``<h2>``, ``<code>`` не вкладывается в ``<pre>``. Общий
    ``md_to_html`` даёт базовый HTML (включая экранирование), здесь он приводится
    к этому подмножеству.
    """
    body = md_to_html(md or "")
    body = body.replace("<pre><code>", "<pre>").replace("</code></pre>", "</pre>")
    body = body.replace("<h3>", "<h2>").replace("</h3>", "</h2>")
    body = body.replace("<p>", "").replace("</p>", "\n\n")
    return f"<body>{body.strip()}</body>"


def _section_of(task: Mapping[str, Any], project_gid: str) -> dict:
    """Секция задачи в нужном проекте из ``memberships``; иначе первая секция."""
    fallback: dict = {}
    for membership in task.get("memberships") or []:
        if not isinstance(membership, Mapping):
            continue
        section = membership.get("section")
        if not isinstance(section, Mapping):
            continue
        entry = {"gid": str(section.get("gid") or ""), "name": section.get("name") or ""}
        project = membership.get("project")
        gid = str((project or {}).get("gid") or "") if isinstance(project, Mapping) else ""
        if project_gid and gid == project_gid:
            return entry
        fallback = fallback or entry
    return fallback


class AsanaBoard(RestBoardBase):
    """Адаптер Asana поверх общего REST-скелета."""

    board_type = "asana"
    _FIELDS = (
        "gid,name,html_notes,notes,completed,modified_at,permalink_url,num_subtasks,"
        "memberships.project.gid,memberships.section.gid,memberships.section.name"
    )

    def __init__(
        self,
        *,
        access_token: str,
        api_base: str = "",
        project_gid: str = "",
        key_prefix: str = "",
        key_pattern: str = "",
        url_template: str = "",
        attachment_max_bytes: int = 10 * 1024 * 1024,
        attachment_timeout: float = 10.0,
        attachment_store_chars: int = 200000,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(
            base_url=(api_base or _API_BASE).rstrip("/"),
            secrets=(access_token,),
            key_pattern=key_pattern,
            url_template=url_template,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            transport=transport,
            sleeper=sleeper,
        )
        self._project_gid = (project_gid or "").strip()
        self._key_prefix = (key_prefix or "").strip()
        self._att_max_bytes = attachment_max_bytes
        self._att_timeout = attachment_timeout
        self._att_store_chars = attachment_store_chars
        # Отдельный клиент БЕЗ Authorization: download_url вложения — подписанный
        # URL на чужом хосте (S3/CDN), туда PAT уходить не должен.
        self._files = httpx.Client(timeout=attachment_timeout, transport=transport)

    def close(self) -> None:
        """Закрыть оба клиента: основной API и безавторизационный для файлов."""
        try:
            self._files.close()
        finally:
            super().close()

    # --- проверка окружения ----------------------------------------------

    def validate_connection(self, project: str | None = None) -> dict:
        """Identity токена и видимость проекта; секреты в результат не попадают."""
        identity = (self._read("GET", "/users/me") or {}).get("data") or {}
        warnings: list[str] = []
        project_info: dict | None = None
        project_gid = ""
        try:
            project_gid = self._resolve_project_gid(project)
        except BoardProviderError:
            warnings.append(
                "Asana project_gid is not configured; sync, create and finish are unavailable."
            )
        if project_gid:
            payload = (
                self._read(
                    "GET",
                    f"/projects/{quote(project_gid, safe='')}",
                    params={"opt_fields": "gid,name"},
                ) or {}
            ).get("data") or {}
            project_info = {
                "gid": str(payload.get("gid") or project_gid),
                "name": str(payload.get("name") or ""),
            }
        if not self._key_prefix:
            warnings.append(
                "Asana key_prefix is not configured; task keys cannot be synchronized."
            )
        return {
            "status": "ok",
            "identity": {
                "gid": str(identity.get("gid") or ""),
                "name": str(identity.get("name") or ""),
            },
            "project": project_info,
            "capabilities": {
                "sync": bool(project_gid and self._key_prefix),
                "create": bool(project_gid),
                "finish": bool(project_gid),
                "attachments": True,
            },
            "warnings": warnings,
        }

    # --- helpers ---------------------------------------------------------

    def _resolve_project_gid(self, board: str | None) -> str:
        """gid проекта: числовой аргумент вызова имеет приоритет над опцией."""
        candidate = (board or "").strip()
        if candidate.isdigit():
            return candidate
        if self._project_gid:
            return self._project_gid
        raise BoardProviderError(
            "configuration",
            "Asana project_gid is required for this operation.",
            hint="Set the project_gid provider option to the Asana project gid.",
            secrets=self._secrets,
        )

    def _key_for(self, gid: str) -> str:
        """Канонический ключ задачи: ``<key_prefix>-<gid>``; без префикса — сам gid."""
        return f"{self._key_prefix}-{gid}" if self._key_prefix else gid

    def _gid_of(self, key: str) -> str:
        """gid из канонического ключа; ``""`` если ключ не содержит числового gid."""
        tail = (key or "").rsplit("-", 1)[-1].strip()
        return tail if tail.isdigit() else ""

    def _raw_from_task(self, task: Mapping[str, Any], project_gid: str) -> RawTask:
        """Единый маппер listing/single ответа Asana в RawTask."""
        gid = str(task.get("gid") or "")
        key = self._key_for(gid)
        section = _section_of(task, project_gid)
        html_notes = task.get("html_notes")
        notes = task.get("notes")
        description = html_notes if isinstance(html_notes, str) and html_notes else notes
        completed = task.get("completed")
        return RawTask(
            key=key,
            project_code=key,
            title=str(task.get("name") or ""),
            description=str(description or ""),
            status=section.get("name") or None,
            subtask_ids=[],
            timestamp=_timestamp(task.get("modified_at")),
            board_id=gid,
            archived=None,
            terminal=completed if isinstance(completed, bool) else None,
            provider_data={
                "gid": gid,
                "project_gid": project_gid,
                "permalink_url": str(task.get("permalink_url") or ""),
                "num_subtasks": int(task.get("num_subtasks") or 0),
                "section": section,
                "html": bool(isinstance(html_notes, str) and html_notes),
            },
        )

    # --- чтение ----------------------------------------------------------

    def iter_raw(
        self,
        board: str | None,
        limit: int | None,
        *,
        sync_filter=None,
        now_ms=None,
    ) -> TaskListing:
        if limit == 0:
            return TaskListing(rows=iter(()))
        return TaskListing(
            rows=self._iter_raw_rows(board, limit),
            stats=TaskListingStats(),
        )

    def _iter_raw_rows(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        """Все задачи проекта: opaque-курсор ``next_page.offset``, страница 100."""
        project_gid = self._resolve_project_gid(board)

        def fetch(cursor: str | None) -> Any:
            params: dict[str, str] = {
                "project": project_gid,
                "limit": str(_PAGE),
                "opt_fields": self._FIELDS,
            }
            if cursor:
                params["offset"] = cursor
            return self._read("GET", "/tasks", params=params)

        count = 0
        if limit is not None and count >= limit:
            return
        for task in paginate_cursor(fetch, items=_data, next_cursor=_next_offset):
            yield self._raw_from_task(task, project_gid)
            count += 1
            if limit is not None and count >= limit:
                return

    def fetch_one(self, key: str) -> RawTask | None:
        """Одна задача по каноническому ключу; 404/сбой/битый ключ → None."""
        gid = self._gid_of(key)
        if not gid:
            return None
        try:
            payload = self._read(
                "GET",
                f"/tasks/{quote(gid, safe='')}",
                params={"opt_fields": self._FIELDS},
            ) or {}
        except BoardProviderError as error:
            log.warning("asana: fetch_one(%s) не удался (%s)", key, error.category)
            return None
        task = payload.get("data")
        if not isinstance(task, Mapping):
            return None
        return self._raw_from_task(task, self._resolve_project_gid(None))

    def _paginated(self, path: str, params: Mapping[str, str]) -> list[dict]:
        """Полный обход курсорного listing-эндпоинта Asana (``data`` + ``next_page``)."""

        def fetch(cursor: str | None) -> Any:
            query = dict(params)
            if cursor:
                query["offset"] = cursor
            return self._read("GET", path, params=query)

        return list(paginate_cursor(fetch, items=_data, next_cursor=_next_offset))

    # --- нормализация ----------------------------------------------------

    def _subtasks(self, gid: str) -> tuple[list[dict], list[str]]:
        """Подзадачи задачи (``criteria``/``links``); сбой fail-soft → warning."""
        try:
            rows = self._paginated(
                f"/tasks/{quote(gid, safe='')}/subtasks",
                {"limit": str(_PAGE), "opt_fields": "gid,name,completed"},
            )
        except BoardProviderError as error:
            log.warning("asana: подзадачи %s недоступны (%s)", gid, error.category)
            return [], [f"подзадачи задачи {gid} недоступны: {error.category}"]
        return (
            [
                {"gid": str(row.get("gid") or ""), "name": str(row.get("name") or "")}
                for row in rows
                if row.get("gid")
            ],
            [],
        )

    def _attachment_skip_reason(self, name: str, url: str, size: object) -> str | None:
        """Причина не качать вложение; None — можно скачивать."""
        parsed = urlsplit(url) if url else None
        if parsed is None or parsed.scheme != "https" or parsed.username or parsed.password:
            return f"вложение {name!r}: ссылка на скачивание недоступна"
        if isinstance(size, int) and size > self._att_max_bytes:
            return f"вложение {name!r}: размер превышает лимит"
        if not attachment_supported(name, None):
            return f"вложение {name!r}: формат не поддерживается"
        return None

    def _attachments(self, gid: str) -> tuple[list[dict], list[str]]:
        """Вложения задачи: метаданные всегда, текст — если файл скачиваем.

        ``download_url`` — короткоживущая подписанная ссылка на сторонний хост,
        поэтому качаем её безавторизационным клиентом: PAT туда не уходит.
        """
        try:
            rows = self._paginated(
                "/attachments",
                {
                    "parent": gid,
                    "limit": str(_PAGE),
                    "opt_fields": "gid,name,size,download_url,permanent_url,resource_subtype",
                },
            )
        except BoardProviderError as error:
            log.warning("asana: вложения %s недоступны (%s)", gid, error.category)
            return [], [f"вложения задачи {gid} недоступны: {error.category}"]

        out: list[dict] = []
        warnings: list[str] = []
        for row in rows:
            name = str(row.get("name") or "")
            url = str(row.get("download_url") or "")
            size = row.get("size")
            reason = self._attachment_skip_reason(name, url, size)
            if reason is not None:
                out.append(
                    {"name": name, "mime_type": None, "size": size, "content_text": None}
                )
                warnings.append(reason)
                continue
            result = fetch_attachment(
                self._files,
                name=name,
                mime=None,
                size=size,
                url=url,
                timeout=self._att_timeout,
                max_bytes=self._att_max_bytes,
                store_chars=self._att_store_chars,
            )
            out.append(result)
            if result["content_text"] is None:
                warnings.append(f"вложение {name!r}: содержимое недоступно")
        return out, warnings

    def _brief(self, raw: RawTask, *, with_details: bool) -> dict:
        provider_data = raw.provider_data
        gid = raw.board_id or str(provider_data.get("gid") or "")
        description = (
            html_to_md(raw.description) if provider_data.get("html") else (raw.description or "")
        )

        criteria: list[str] = []
        links: list[dict] = []
        warnings: list[str] = []
        if with_details and int(provider_data.get("num_subtasks") or 0) > 0:
            subtasks, subtask_warnings = self._subtasks(gid)
            warnings.extend(subtask_warnings)
            for item in subtasks:
                key = self._key_for(item["gid"])
                criteria.append(item["name"] or key)
                links.append({"type": "subtask", "key": key, "title": item["name"]})
        links.extend(raw.links)

        covered = {raw.key, gid, *(link["key"] for link in links)}
        if self._key_pattern:
            for match in re.finditer(self._key_pattern, description):
                key = match.group(0)
                if key not in covered:
                    links.append({"type": "related", "key": key, "title": ""})
                    covered.add(key)

        attachments: list[dict] = []
        if with_details:
            attachments, attachment_warnings = self._attachments(gid)
            warnings.extend(attachment_warnings)

        return {
            "key": raw.key,
            "aliases": [gid] if gid and gid != raw.key else [],
            "title": raw.title,
            "description": description,
            "criteria": criteria,
            "status": "done" if raw.terminal is True else raw.status,
            "url": str(provider_data.get("permalink_url") or "") or self._task_url(raw.key),
            "links": links,
            "project": project_prefix(raw.project_code or raw.key),
            "attachments": attachments,
            "warnings": warnings,
        }

    def normalize(self, raw: RawTask) -> dict:
        """Полный TaskBrief: подзадачи и вложения дочитываются из API."""
        return self._brief(raw, with_details=True)

    def normalize_meta(self, raw: RawTask) -> dict:
        """Дешёвый TaskBrief без I/O: только плоские поля RawTask (PRI-207)."""
        return self._brief(raw, with_details=False)

    # --- цели размещения (секции проекта) --------------------------------

    def _sections(self, project_gid: str) -> list[dict]:
        """Секции проекта: ``[{gid, name}]`` в порядке доски."""
        rows = self._paginated(
            f"/projects/{quote(project_gid, safe='')}/sections",
            {"limit": str(_PAGE), "opt_fields": "gid,name"},
        )
        return [
            {"gid": str(row.get("gid") or ""), "name": str(row.get("name") or "")}
            for row in rows
            if row.get("gid")
        ]

    @staticmethod
    def _resolve_section(sections: list[dict], target: str) -> tuple[dict | None, str | None]:
        """Секция по точному gid или по точному имени; неоднозначное имя не выбирается."""
        by_gid = next((item for item in sections if item["gid"] == target), None)
        if by_gid is not None:
            return by_gid, None
        by_name = [item for item in sections if item["name"] == target]
        if len(by_name) == 1:
            return by_name[0], None
        if len(by_name) > 1:
            return None, f"секция {target!r} неоднозначна — не применена"
        return None, f"секция {target!r} не найдена — не применена"

    def list_targets(self, project: str | None) -> dict:
        """Секции проекта как create/done-цели; недоступность — warning, не исключение."""
        warnings: list[str] = []
        sections: list[dict] = []
        try:
            sections = self._sections(self._resolve_project_gid(project))
        except BoardProviderError as error:
            log.warning("asana: секции проекта недоступны (%s)", error.category)
            warnings.append(f"секции проекта Asana недоступны: {error.category}")
        return {
            "targets": [
                {"id": item["gid"], "label": item["name"], "purposes": ["create", "done"]}
                for item in sections
            ],
            "options": [],
            "warnings": warnings,
        }

    def _resolve_target_section(
        self,
        project_gid: str,
        target: str | None,
    ) -> tuple[dict | None, list[str]]:
        """Секция под запрошенный target; недоступность/промах — warning, не исключение."""
        if not target:
            return None, []
        try:
            sections = self._sections(project_gid)
        except BoardProviderError as error:
            log.warning("asana: секции проекта недоступны (%s)", error.category)
            return None, [f"секции проекта Asana недоступны: {error.category}"]
        section, warning = self._resolve_section(sections, target)
        return section, [warning] if warning else []

    # --- запись ----------------------------------------------------------

    def create(
        self,
        doc_md: str,
        *,
        title: str,
        target: str | None,
        project: str | None,
    ) -> dict:
        """Создать задачу в проекте; не найденная секция → дефолтное место + warning."""
        project_gid = self._resolve_project_gid(project)
        section, warnings = self._resolve_target_section(project_gid, target)
        data: dict[str, Any] = {
            "name": title,
            "html_notes": _md_to_asana_html(doc_md),
            "projects": [project_gid],
        }
        if section is not None:
            data["memberships"] = [{"project": project_gid, "section": section["gid"]}]
        created = (self._write("POST", "/tasks", json={"data": data}) or {}).get("data") or {}
        gid = str(created.get("gid") or "")
        if not gid:
            raise BoardProviderError(
                "unsupported",
                "Asana create response did not include a task gid.",
                hint="Check the Asana API response for POST /tasks.",
                secrets=self._secrets,
            )
        key = self._key_for(gid)
        placed = _section_of(created, project_gid)
        return {
            "key": key,
            "url": str(created.get("permalink_url") or "") or self._task_url(key),
            "board_id": gid,
            "target_resolved": (
                section["gid"] if section is not None else (placed.get("gid") or None)
            ),
            "warnings": warnings,
        }

    def _require_gid(self, key: str) -> str:
        gid = self._gid_of(key)
        if not gid:
            raise BoardProviderError(
                "configuration",
                "Asana task key does not contain a numeric task gid.",
                hint="Use the canonical key <key_prefix>-<gid> or the raw Asana gid.",
                secrets=self._secrets,
            )
        return gid

    @staticmethod
    def _notes_with_link(task: Mapping[str, Any], pr_url: str, note: str | None) -> str:
        """html_notes с дописанным блоком PR-ссылки (валидный Asana-HTML).

        Asana не принимает ``<br/>``, поэтому строки разделяются переводами строк.
        URL и note экранируются: note приходит от пользователя.
        """
        html_notes = task.get("html_notes")
        if isinstance(html_notes, str) and html_notes:
            inner = html_notes.strip()
            if inner.startswith("<body>") and inner.endswith("</body>"):
                inner = inner[len("<body>"): -len("</body>")]
        else:
            inner = html.escape(str(task.get("notes") or ""))
        safe_url = html.escape(pr_url, quote=True)
        block = f'\n\nPR: <a href="{safe_url}">{safe_url}</a>'
        if note:
            block += f"\n{html.escape(note)}"
        return f"<body>{inner}{block}</body>"

    @staticmethod
    def _has_link(task: Mapping[str, Any], pr_url: str) -> bool:
        """Есть ли PR-ссылка в описании (идемпотентность по самому URL, без маркера)."""
        safe_url = html.escape(pr_url, quote=True)
        for field in ("html_notes", "notes"):
            value = task.get(field)
            if isinstance(value, str) and (safe_url in value or pr_url in value):
                return True
        return False

    def _move_to_section(self, gid: str, section: dict) -> tuple[bool, list[str]]:
        """Перенести задачу в секцию; сбой fail-soft → warning."""
        try:
            self._write(
                "POST",
                f"/sections/{quote(section['gid'], safe='')}/addTask",
                json={"data": {"task": gid}},
            )
        except BoardProviderError as error:
            log.warning("asana: перенос в секцию %s не удался (%s)", section["gid"],
                        error.category)
            return False, [f"перенос в секцию {section['name']!r} не удался: {error.category}"]
        return True, []

    def finish(
        self,
        key: str,
        pr_url: str,
        *,
        note: str | None = None,
        mark_done: bool = True,
        target: str | None = None,
    ) -> dict:
        """Закрыть задачу: ``completed=true`` + идемпотентная PR-ссылка (+ перенос в секцию).

        Каноническое закрытие в Asana — булево поле ``completed``; секция (``target``)
        — дополнительное размещение на доске, а не сам факт закрытия. Правки описания
        и статуса выполняются независимо, поэтому отказ одной не блокирует другую.
        """
        gid = self._require_gid(key)
        task = (
            self._read(
                "GET",
                f"/tasks/{quote(gid, safe='')}",
                params={"opt_fields": self._FIELDS},
            ) or {}
        ).get("data") or {}
        warnings: list[str] = []

        link_present = bool(pr_url) and self._has_link(task, pr_url)
        pr_link_added = False
        if pr_url and not link_present:
            try:
                self._write(
                    "PUT",
                    f"/tasks/{quote(gid, safe='')}",
                    json={"data": {"html_notes": self._notes_with_link(task, pr_url, note)}},
                )
                pr_link_added = True
            except BoardProviderError as error:
                log.warning("asana: обновление html_notes %s не удалось (%s)", gid,
                            error.category)
                warnings.append(f"описание задачи не обновлено: {error.category}")

        completed = bool(task.get("completed"))
        done_set = False
        if mark_done and not completed:
            self._write(
                "PUT",
                f"/tasks/{quote(gid, safe='')}",
                json={"data": {"completed": True}},
            )
            done_set = True

        section_moved = False
        if target:
            project_gid = self._resolve_project_gid(None)
            section, section_warnings = self._resolve_target_section(project_gid, target)
            warnings.extend(section_warnings)
            current = _section_of(task, project_gid).get("gid") or ""
            if section is not None and section["gid"] != current:
                section_moved, move_warnings = self._move_to_section(gid, section)
                warnings.extend(move_warnings)

        link_satisfied = not pr_url or link_present or pr_link_added
        done_satisfied = not mark_done or completed or done_set
        return {
            "key": key,
            "board_id": gid,
            "done_set": done_set,
            "pr_link_added": pr_link_added,
            "already_closed": (
                not pr_link_added
                and not done_set
                and not section_moved
                and link_satisfied
                and done_satisfied
            ),
            "section_moved": section_moved,
            "warnings": warnings,
        }
