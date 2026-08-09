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
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from reviewer.tasks.boards.attachments import _registrable_domain, fetch_attachment, host_allowed
from reviewer.tasks.boards.base import (
    NativeSubtaskIdentity,
    RawTask,
    ReconciledNativeSubtask,
    TaskListing,
    project_prefix,
)
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.http import BoardHttpClient
from reviewer.tasks.boards.markup import html_to_md, md_to_html
from reviewer.config.provider_access import ProviderAccessSpec
from reviewer.tasks.boards.registry import (
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderSetupSpec,
)
from reviewer.tasks.boards.setup import acquire_yougile_key
from reviewer.tasks.subtasks import SUBTASK_MARKER_RE

if TYPE_CHECKING:
    from reviewer.config.task_board import TaskSyncFilter

log = logging.getLogger(__name__)

_PAGE = 1000
_CANONICAL_UNAVAILABLE_WARNING = (
    "канонический код созданной подзадачи недоступен — вернули внутренний id"
)
_ENRICHMENT_UNCONFIRMED_WARNING = (
    "enrichment созданной подзадачи не подтверждён transport id — использован id из POST"
)

# Структурного списка файлов YouGile в API не отдаёт. Файл попадает в задачу двумя путями:
#  1) прикреплённый к задаче — HTML-ссылкой <a href="…/user-data/…"> в description
#     (реальная модель, подтверждена на живой доске — PRI-196/PRI-197);
#  2) прикреплённый в чате — маркером /root/#file:<url> в text/textHtml сообщения
#     (POST /upload-file → {url}; см. send_task_file).
_FILE_MARKER = re.compile(r"/root/#file:(\S+)")
_HREF = re.compile(r"""href=["']([^"']+)["']""")
_USER_DATA = "/user-data/"  # путь хранилища загруженных файлов YouGile


def _optional_timestamp(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _build_provider(context: ProviderBuildContext) -> YougileBoard:
    return YougileBoard(
        api_key=context.credentials["YOUGILE_API_KEY"],
        api_base=context.credentials["YOUGILE_API_BASE"],
        key_pattern=context.key_pattern,
        url_template=context.url_template,
        attachment_max_bytes=context.attachment_max_bytes,
        attachment_timeout=context.attachment_timeout,
        attachment_store_chars=context.attachment_store_chars,
    )


def provider_spec() -> BoardProviderSpec:
    """Immutable registry spec YouGile с legacy credential aliases."""
    return BoardProviderSpec(
        board_type="yougile",
        factory=_build_provider,
        credential_fields=(
            CredentialFieldSpec(
                env="YOUGILE_API_KEY",
                label="YouGile API key",
                secret=True,
                aliases=("TASK_BOARD_API_KEY",),
            ),
            CredentialFieldSpec(
                env="YOUGILE_API_BASE",
                label="YouGile API base URL",
                required=False,
                default="https://yougile.com/api-v2",
                aliases=("TASK_BOARD_API_BASE",),
            ),
        ),
        setup=ProviderSetupSpec(
            label="YouGile",
            help_url="https://ru.yougile.com/api-v2",
            help_text=(
                "Получите API key автоматически или используйте официальный ручной flow. "
                "Admin role не обязателен: нужен API-capable аккаунт с доступом к компании. "
                "При allowOnlyOpenId используйте готовый key такого аккаунта."
            ),
            access=ProviderAccessSpec(
                minimum_permissions=(
                    "доступ API-capable аккаунта к компании и чтению/записи задач; "
                    "admin role не обязателен"
                ),
                read_operations=(
                    "компании, доски, колонки, задачи, чаты и вложения",
                ),
                write_operations=(
                    "создание, обновление и завершение задач и нативных подзадач",
                ),
                validation="identity и видимость проекта",
            ),
            acquisition=acquire_yougile_key,
        ),
        default_api_base="https://yougile.com/api-v2",
        create_target_label="Колонка создания",
        done_target_label="Колонка завершения",
        capabilities=frozenset({"native_subtasks"}),
    )


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


def _yougile_code(value: object) -> str:
    """Вернуть только строковый код YouGile вида PREFIX-N."""
    code = value.strip() if isinstance(value, str) else ""
    return code if project_prefix(code) else ""


class _VisibleTextParser(HTMLParser):
    """Текстовые узлы HTML без атрибутов, комментариев и script/style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._hidden_depth = 0
        self._malformed = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden_depth:
            return
        if "<" in data:
            self._malformed = True
            return
        self._parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._hidden_depth:
            self._parts.append(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        if not self._hidden_depth:
            self._parts.append(html.unescape(f"&#{name};"))

    def text(self) -> str:
        return "" if self._malformed else "".join(self._parts)


def _visible_description_text(description: object) -> str:
    if not isinstance(description, str):
        return ""
    parser = _VisibleTextParser()
    try:
        parser.feed(description)
        parser.close()
    except Exception:
        log.warning("yougile: не удалось разобрать видимый текст описания", exc_info=True)
        return ""
    return parser.text()


def normalize_yougile(
    raw: RawTask,
    key_pattern: str,
    url_template: str,
    subtask_titles: dict[str, str] | None = None,
    attachments: list[dict] | None = None,
    subtask_refs: dict[str, dict] | None = None,
) -> dict:
    """RawTask → TaskBrief dict. Чистая: без I/O (titles подзадач инжектятся).

    description конвертируется из HTML доски в markdown (PRI-213): стор и LLM
    видят чистый текст, а не <br />, &gt; и <div> транспорта YouGile.
    """
    subtask_titles = subtask_titles or {}
    subtask_refs = subtask_refs or {}
    key = raw.key
    aliases = [raw.project_code] if raw.project_code and raw.project_code != key else []

    links: list[dict] = []
    covered: set[str] = {key, *aliases}
    for sid in raw.subtask_ids:
        ref = subtask_refs.get(sid) or {}
        canonical_key = str(ref.get("key") or ref.get("common_key") or sid)
        link = {"type": "subtask", "key": canonical_key}
        title = ref.get("title") or subtask_titles.get(sid)
        if title:
            link["title"] = title
        links.append(link)
        ref_aliases = list(ref.get("aliases") or [])
        project_alias = ref.get("project_alias") or ref.get("project_code")
        if project_alias:
            ref_aliases.append(project_alias)
        covered.update({sid, canonical_key, *ref_aliases})

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
        "description": SUBTASK_MARKER_RE.sub("", html_to_md(raw.description)),
        "criteria": [],
        "status": "done" if raw.terminal is True else raw.status,
        "url": url,
        "links": links,
        "project": project_prefix(raw.project_code or key),
        "attachments": attachments or [],
        "provider_data": dict(raw.provider_data),
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
        self._http = BoardHttpClient(self._client, secrets=(api_key,))
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
        """Проверить identity и минимальный read-доступ без возврата credentials."""
        companies = self._get_all("/companies")
        projects = self._get_all("/projects")
        company = companies[0] if companies else {}
        project_info = None
        if project:
            selected = next(
                (
                    item
                    for item in projects
                    if project
                    in {
                        item.get("id"),
                        item.get("key"),
                        item.get("code"),
                        item.get("title"),
                        item.get("name"),
                    }
                ),
                None,
            )
            if selected is None:
                raise BoardProviderError(
                    "not_found",
                    "YouGile project is not accessible.",
                    hint="Check the project identifier and API key permissions.",
                )
            label = (
                selected.get("key")
                or selected.get("code")
                or selected.get("title")
                or selected.get("name")
            )
            project_info = {
                "id": selected.get("id"),
                "key": label,
                "name": selected.get("name") or selected.get("title") or label,
            }
        return {
            "status": "ok",
            "identity": {
                "id": company.get("id"),
                "name": company.get("name") or company.get("title"),
            },
            "project": project_info,
            "capabilities": {"read": True},
            "warnings": [],
        }

    def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        """Пагинированный GET listing-эндпоинта (yougile: {content, paging})."""
        out: list[dict] = []
        offset = 0
        while True:
            p = dict(params or {})
            p.update({"limit": _PAGE, "offset": offset})
            content = (self._read(path, params=p) or {}).get("content", [])
            out.extend(content)
            if len(content) < _PAGE:
                break
            offset += len(content)
        return out

    def _iter_raw_rows(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        count = 0
        if limit is not None and count >= limit:
            return
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
                            timestamp=_optional_timestamp(t.get("timestamp")),
                            board_id=t["id"],
                            archived=None,
                            terminal=_optional_bool(t.get("completed")),
                            provider_data={
                                "source_board_id": brd["id"],
                                "source_column_id": col_id,
                            },
                        )
                        count += 1
                        if limit is not None and count >= limit:
                            return

    def iter_raw(
        self,
        board: str | None,
        limit: int | None,
        *,
        sync_filter: TaskSyncFilter | None = None,
        now_ms: int | None = None,
    ) -> TaskListing:
        if limit == 0:
            return TaskListing(rows=iter(()))
        return TaskListing(rows=self._iter_raw_rows(board, limit))

    def _chat_texts(self, task_uuid: str) -> list[str]:
        """text + textHtml всех сообщений чата задачи (best-effort, fail-soft → []).

        chatId == внутренний UUID задачи. Пустой/недоступный чат → [].
        """
        if not task_uuid:
            return []
        try:
            msgs = (self._read(f"/chats/{task_uuid}/messages") or {}).get("content", []) or []
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
        subtask_refs: dict[str, dict] = {}
        for sid in raw.subtask_ids:
            try:
                st = self._read(f"/tasks/{sid}") or {}
                project_alias = st.get("idTaskProject")
                subtask_refs[sid] = {
                    "key": st.get("idTaskCommon") or sid,
                    "title": st.get("title") or "",
                    "aliases": [project_alias] if project_alias else [],
                }
            except Exception:
                log.warning("yougile: не резолвится подзадача %s", sid, exc_info=True)
        attachments = self._collect_attachments(raw.description, raw.board_id)
        return normalize_yougile(
            raw,
            self._key_pattern,
            self._url_template,
            attachments=attachments,
            subtask_refs=subtask_refs,
        )

    def normalize_meta(self, raw: RawTask) -> dict:
        """Дешёвая нормализация без I/O (PRI-207): чистый normalize_yougile без
        резолва подзадач/вложений. Плоские поля (project/url/status) корректны."""
        return normalize_yougile(raw, self._key_pattern, self._url_template)

    def fetch_one(self, key: str) -> RawTask | None:
        """Один RawTask по ключу (проектный/компанийный код) — write-through после finish.

        GET /tasks/{key} (тот же вызов, что и в finish); title колонки резолвится
        best-effort через GET /columns/{columnId}. Только достоверный 404 → None;
        остальные ошибки чтения пробрасываются вызывающему коду."""
        try:
            t = self._read(f"/tasks/{quote(key, safe='')}") or {}
        except BoardProviderError as error:
            if error.category == "not_found":
                return None
            raise
        status = None
        column: dict = {}
        col_id = t.get("columnId")
        if col_id:
            try:
                column = self._read(f"/columns/{quote(str(col_id), safe='')}") or {}
                status = column.get("title")
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
            timestamp=_optional_timestamp(t.get("timestamp")),
            board_id=t.get("id") or key,
            archived=None,
            terminal=_optional_bool(t.get("completed")),
            provider_data={
                "source_board_id": column.get("boardId"),
                "source_column_id": col_id,
            },
        )

    def _native_subtask_identity(
        self,
        task: dict,
        *,
        fallback_id: str = "",
        fallback_title: str = "",
        warnings: tuple[str, ...] = (),
    ) -> NativeSubtaskIdentity:
        returned_id = task.get("id")
        board_id = returned_id.strip() if isinstance(returned_id, str) else fallback_id
        board_id = board_id or fallback_id
        common_key = _yougile_code(task.get("idTaskCommon"))
        key = common_key or board_id
        project_alias = _yougile_code(task.get("idTaskProject"))
        aliases = (project_alias,) if project_alias and project_alias != key else ()
        url_code = project_alias or common_key
        url = (
            self._url_template.replace("{code}", url_code)
            if self._url_template and url_code
            else None
        )
        identity_warnings = list(warnings)
        if not common_key and _CANONICAL_UNAVAILABLE_WARNING not in identity_warnings:
            identity_warnings.append(_CANONICAL_UNAVAILABLE_WARNING)
        returned_title = task.get("title")
        identity_title = (
            returned_title.strip()
            if isinstance(returned_title, str) and returned_title.strip()
            else fallback_title
        )
        return NativeSubtaskIdentity(
            board_id=board_id,
            key=key,
            title=identity_title,
            aliases=aliases,
            url=url,
            warnings=tuple(identity_warnings),
        )

    def create_native_subtask(
        self,
        doc_md: str,
        *,
        title: str,
        source_column_id: str,
        marker: str,
    ) -> NativeSubtaskIdentity:
        description = md_to_html(doc_md) + f"<p><small>{html.escape(marker)}</small></p>"
        created = self._write(
            "POST",
            "/tasks",
            json={
                "title": title,
                "columnId": source_column_id,
                "description": description,
            },
        )
        created_id = (created or {}).get("id")
        uuid = created_id.strip() if isinstance(created_id, str) else ""
        if not uuid:
            raise BoardProviderError(
                "unsupported",
                "YouGile create response did not include a task id.",
                hint="Check the YouGile API response format.",
            )
        try:
            task = self._read(f"/tasks/{quote(uuid, safe='')}") or {}
        except Exception:
            log.warning("yougile: созданная подзадача %s не дочитана", uuid, exc_info=True)
            return self._native_subtask_identity({}, fallback_id=uuid, fallback_title=title)
        enriched_id = task.get("id") if isinstance(task, dict) else None
        if (
            not isinstance(enriched_id, str)
            or not enriched_id.strip()
            or enriched_id != uuid
        ):
            return self._native_subtask_identity(
                {},
                fallback_id=uuid,
                fallback_title=title,
                warnings=(_ENRICHMENT_UNCONFIRMED_WARNING,),
            )
        return self._native_subtask_identity(task, fallback_id=uuid, fallback_title=title)

    def replace_native_subtasks(self, parent_task_id: str, subtask_ids: list[str]) -> None:
        self._write(
            "PUT",
            f"/tasks/{quote(str(parent_task_id), safe='')}",
            json={"subtasks": subtask_ids},
        )

    def reconcile_native_subtasks(
        self,
        source_board_id: str,
        markers: frozenset[str],
    ) -> list[ReconciledNativeSubtask]:
        if not markers:
            return []
        found: list[ReconciledNativeSubtask] = []
        columns = self._get_all("/columns", {"boardId": source_board_id})
        for column in columns:
            for task in self._get_all("/tasks", {"columnId": column["id"]}):
                transport_id = task.get("id")
                if not isinstance(transport_id, str) or not transport_id.strip():
                    continue
                matched = markers.intersection(
                    SUBTASK_MARKER_RE.findall(
                        _visible_description_text(task.get("description"))
                    )
                )
                if not matched:
                    continue
                identity = self._native_subtask_identity(task)
                found.extend(
                    ReconciledNativeSubtask(marker=marker, identity=identity)
                    for marker in sorted(matched)
                )
        return found

    def _resolve_column(
        self,
        current_col_id: str,
        target: str,
    ) -> tuple[dict | None, str | None]:
        """Колонка по exact id или exact label на той же доске.

        GET /columns/{cur} → boardId; GET /columns?boardId=… → match по title.
        Неоднозначный label не выбирается."""
        try:
            board_id = (
                self._read(f"/columns/{quote(str(current_col_id), safe='')}") or {}
            ).get("boardId")
            if not board_id:
                return None, f"не удалось определить доску колонки {target!r}"
            columns = self._get_all("/columns", {"boardId": board_id})
            exact_id = next((col for col in columns if col.get("id") == target), None)
            if exact_id is not None:
                return exact_id, None
            by_label = [col for col in columns if col.get("title") == target]
            if len(by_label) == 1:
                return by_label[0], None
            if len(by_label) > 1:
                return None, f"колонка {target!r} неоднозначна — задача не перенесена"
        except Exception:
            log.warning("yougile: резолв колонки '%s' не удался", target, exc_info=True)
            return None, f"не удалось разрешить колонку {target!r}"
        return None, f"колонка {target!r} не найдена — задача не перенесена"

    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, target: str | None = None) -> dict:
        """Закрыть задачу YouGile: completed:true + PR-ссылка в описание (идемпотентно)
        + опциональный перенос в target-колонку.

        GET /tasks/{key} резолвит проектный/компанийный код в объект (+ uuid, columnId).
        PUT обновляет задачу — двигает её timestamp (watermark синка)."""
        task = self._read(f"/tasks/{quote(key, safe='')}") or {}
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
        if target and cur_col:
            target_column, warning = self._resolve_column(cur_col, target)
            if warning:
                warnings.append(warning)
            elif target_column is not None and target_column.get("id") != cur_col:
                payload["columnId"] = target_column["id"]
                column_moved = True

        done_set = False
        if mark_done and not completed:
            payload["completed"] = True
            done_set = True

        if payload:
            self._write("PUT", f"/tasks/{quote(str(uuid), safe='')}", json=payload)
        return {"key": key, "board_id": uuid, "done_set": done_set,
                "pr_link_added": pr_link_added, "column_moved": column_moved,
                "already_closed": not payload, "warnings": warnings}

    def _columns_of_project(self, project: str | None) -> tuple[list[dict], list[str]]:
        """Колонки досок проекта: ([{title, id, board_id, board_title}], warnings).

        project — код-префикс задач (напр. PRI): доска включается, если на ней есть
        хоть одна задача проекта. Пустой project → все доски. fail-soft: ошибка →
        ([], warnings). Общая основа для discovery done-цели и для create.
        """
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
            log.warning("yougile: обход колонок не удался", exc_info=True)
            warnings.append("не удалось перечислить колонки доски")
        kept = boards if not project else [b for b in boards if b["board_id"] in hosts]
        columns = [{"title": col["title"], "id": col["id"],
                    "board_id": b["board_id"], "board_title": b["board_title"]}
                   for b in kept for col in b["columns"]]
        if project and not hosts and not warnings:
            warnings.append(f"колонки для проекта {project!r} не найдены")
        return columns, warnings

    def list_targets(self, project: str | None) -> dict:
        """Нормализованные create/done targets — колонки досок проекта."""
        columns, warnings = self._columns_of_project(project)
        return {
            "targets": [
                {
                    "id": column["id"],
                    "label": column["title"],
                    "purposes": ["create", "done"],
                }
                for column in columns
            ],
            "options": [],
            "warnings": warnings,
        }

    def create(self, doc_md: str, *, title: str, target: str | None,
               project: str | None) -> dict:
        """Создать задачу YouGile: POST /tasks + резолв проектного кода вторым GET.

        Описание конвертируется в HTML (транспорт YouGile). Колонка ищется по
        точному title среди колонок досок проекта; не найдена → первая колонка
        + warning. POST возвращает только внутренний uuid, поэтому ключ вида
        PRI-N дочитывается GET /tasks/{uuid} (fail-soft: остаётся uuid).
        """
        columns, warnings = self._columns_of_project(project)
        if not columns:
            raise RuntimeError(f"колонки доски для проекта {project!r} не найдены")
        col = None
        if target:
            col = next((c for c in columns if c["id"] == target), None)
            if col is None:
                by_label = [c for c in columns if c["title"] == target]
                if len(by_label) == 1:
                    col = by_label[0]
                elif len(by_label) > 1:
                    warnings.append(
                        f"колонка {target!r} неоднозначна — "
                        f"задача создана в {columns[0]['title']!r}"
                    )
                else:
                    warnings.append(
                        f"колонка {target!r} не найдена — "
                        f"задача создана в {columns[0]['title']!r}"
                    )
        col = col or columns[0]

        created = self._write(
            "POST",
            "/tasks",
            json={"title": title, "columnId": col["id"], "description": md_to_html(doc_md)},
        )
        uuid = str((created or {}).get("id") or "")
        key = uuid
        try:
            payload = self._read(f"/tasks/{quote(uuid, safe='')}") or {}
            key = payload.get("idTaskProject") or uuid
            if not payload.get("idTaskProject"):
                warnings.append(
                    "проектный код задачи не резолвится — ответ без idTaskProject, "
                    "вернули внутренний id")
        except Exception:
            log.warning("yougile: проектный код задачи %s не резолвится", uuid, exc_info=True)
            warnings.append("проектный код задачи не резолвится — вернули внутренний id")
        url = self._url_template.replace("{code}", key) if self._url_template else None
        return {"key": key, "url": url, "board_id": uuid,
                "target_resolved": col["title"], "warnings": warnings}
