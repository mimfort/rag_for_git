"""Провайдер доски Weeek (Task Manager, Public API v1).

Weeek — SaaS-only (self-hosted версии нет), поэтому база всегда
``https://api.weeek.net/public/v1``; ``WEEEK_API_BASE`` оставлен как non-secret
credential ради тестов и возможного proxy. Транспорт — общий ``RestBoardBase``:
своей httpx-обвязки адаптер не заводит.

Источник фактов — официальная OpenAPI-схема документации Weeek (SPA
``developers.weeek.net`` грузит её бандлом ``/assets/weeek.yaml-*.js``,
``servers[0].url == https://api.weeek.net/public/v1``) плюс официальный PHP SDK
``github.com/weeekcom/weeek-client-php`` (``src/Client.php``,
``src/Endpoints/TaskManager/Tasks.php``). Ниже — только то, что подтверждено
этими двумя источниками; всё неподтверждённое помечено ``TODO(weeek)``.

Особенности доски:
- пагинация — ``perPage`` + ``offset``, признак следующей страницы — ``hasMore``
  в конверте ответа (``{"success": true, "tasks": [...], "hasMore": bool}``);
- инкрементального фильтра по времени изменения у ``GET /tm/tasks`` нет: среди
  query-параметров нет ничего вида ``updatedSince``, а ``sortBy`` знает только
  ``name|type|priority|duration|overdue|created|date|start``. Поэтому синк читает
  страницы целиком, а watermark считается по полю задачи ``updatedAt``
  (ISO 8601 date-time, обязательное поле схемы);
- человекочитаемого ключа у задачи нет — ключ синтезируется как
  ``<key_prefix>-<id>``, нативный числовой id уходит в ``board_id``/``aliases``;
- описание задачи Weeek хранит в HTML (узкое подмножество редактора), поэтому на
  чтении применяется ``markup.html_to_md``, на записи — ``markup.md_to_html``;
- done-цель — колонка доски; каноническое «закрыто» — флаг ``isCompleted``
  (``POST /tm/tasks/{id}/complete``), у колонок Weeek нет типа «done»;
- вложения задачи приходят метаданными (``name``/``url``/``size``); ссылка
  сервиса ``weeek`` живёт час, поэтому текст извлекается сразу при нормализации.
"""
from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from reviewer.tasks.boards.attachments import (
    _registrable_domain,
    attachment_supported,
    fetch_attachment,
    host_allowed,
)
from reviewer.tasks.boards.base import RawTask, TaskListing, TaskListingStats, project_prefix
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.markup import html_to_md, md_to_html
from reviewer.tasks.boards.pagination import paginate_cursor
from reviewer.config.provider_access import ProviderAccessSpec
from reviewer.tasks.boards.registry import (
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderOptionSpec,
    ProviderSetupSpec,
)
from reviewer.tasks.boards.restbase import RestBoardBase

log = logging.getLogger(__name__)

_API_BASE = "https://api.weeek.net/public/v1"
_HELP_URL = "https://developers.weeek.net/#generating-access-token"
_DEFAULT_PREFIX = "WEEEK"
# perPage: официальная схема не объявляет максимум страницы.
# TODO(weeek): не подтверждено докой — верхняя граница perPage (100 взято по
# сообществу: weeek-mcp объявляет max 100); при 422 достаточно снизить константу.
_PAGE = 100
# Предохранитель на резолв заголовков подзадач: каждая подзадача — отдельный GET.
_SUBTASK_CAP = 50
# Маркер идемпотентности PR-ссылки в HTML-описании задачи.
_PR_MARKER = "<!-- reviewer:pr-link -->"
_NUMBER_RE = re.compile(r"(\d+)\s*$")


def _build_provider(context: ProviderBuildContext) -> WeeekBoard:
    """Собрать провайдера из registry-контекста (креды + immutable options)."""
    return WeeekBoard(
        api_token=context.credentials["WEEEK_API_TOKEN"],
        api_base=context.credentials.get("WEEEK_API_BASE") or _API_BASE,
        project_id=str(context.options.get("project_id") or ""),
        board_id=str(context.options.get("board_id") or ""),
        key_prefix=str(context.options.get("key_prefix") or ""),
        key_pattern=context.key_pattern,
        url_template=context.url_template,
        attachment_max_bytes=context.attachment_max_bytes,
        attachment_timeout=context.attachment_timeout,
        attachment_store_chars=context.attachment_store_chars,
    )


def provider_spec() -> BoardProviderSpec:
    """Immutable registry spec Weeek."""
    return BoardProviderSpec(
        board_type="weeek",
        factory=_build_provider,
        credential_fields=(
            CredentialFieldSpec(
                env="WEEEK_API_TOKEN",
                label="Weeek API token",
                secret=True,
            ),
            CredentialFieldSpec(
                env="WEEEK_API_BASE",
                label="Weeek API base URL",
                required=False,
                default=_API_BASE,
            ),
        ),
        option_fields=(
            ProviderOptionSpec(
                key="project_id",
                label="ID проекта Weeek",
                required_for=("sync", "create", "finish"),
            ),
            ProviderOptionSpec(
                key="board_id",
                label="ID доски Weeek",
                required_for=("sync", "create", "finish"),
            ),
            ProviderOptionSpec(
                key="key_prefix",
                label="Префикс ключа задачи",
                required_for=("sync",),
            ),
        ),
        setup=ProviderSetupSpec(
            label="Weeek",
            help_url=_HELP_URL,
            help_text=(
                "Откройте настройки воркспейса Weeek → раздел API и создайте access "
                "token: все запросы выполняются от имени его создателя, поэтому нужен "
                "аккаунт с доступом к проекту задач."
            ),
            access=ProviderAccessSpec(
                minimum_permissions="token владельца с read/write к project и board",
                read_operations=("workspaces, projects, boards, columns, tasks и attachments",),
                write_operations=("создание, правка и завершение tasks, добавление PR-ссылок",),
                validation="user identity и видимость project",
            ),
        ),
        default_api_base=_API_BASE,
        create_target_label="Колонка создания",
        done_target_label="Колонка завершения",
    )


def _normalize_api_base(value: str, *, secrets: tuple[str, ...]) -> str:
    """Fail-closed HTTPS API base без userinfo/query/fragment; иначе configuration-ошибка."""
    try:
        parsed = urlsplit((value or "").strip())
        hostname = (parsed.hostname or "").lower()
    except (TypeError, ValueError):
        parsed, hostname = urlsplit(""), ""
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise BoardProviderError(
            "configuration",
            "Weeek API base must be an HTTPS URL without credentials or query.",
            hint="Use https://api.weeek.net/public/v1.",
            secrets=secrets,
        )
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{hostname}{port}{parsed.path.rstrip('/')}"


def _epoch_ms(value: object) -> int | None:
    """ISO 8601 → epoch ms; нераспознанное значение остаётся неизвестным."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    text = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _as_int(value: object) -> int | None:
    """Целое из числа/строки; иначе None (id доски приходят и строками)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _location(task: Mapping[str, Any]) -> Mapping[str, Any]:
    """Первая локация задачи (проект/доска/колонка); нет локаций → пустой mapping."""
    locations = task.get("locations")
    if isinstance(locations, list):
        for item in locations:
            if isinstance(item, Mapping):
                return item
    return {}


def _placement(task: Mapping[str, Any]) -> tuple[int | None, int | None, int | None]:
    """(projectId, boardId, boardColumnId): плоские поля задачи, иначе из ``locations``.

    Схема задачи объявляет размещение только через ``locations[]``, но официальный
    пример ответа (``x-examples`` той же схемы) отдаёт ещё и плоские ``projectId``/
    ``boardId``/``boardColumnId`` — читаем оба варианта, чтобы не зависеть от того,
    какую форму отдаёт конкретный воркспейс.
    """
    location = _location(task)
    return (
        _as_int(task.get("projectId")) or _as_int(location.get("projectId")),
        _as_int(task.get("boardId")) or _as_int(location.get("boardId")),
        _as_int(task.get("boardColumnId")) or _as_int(location.get("boardColumnId")),
    )


def _attachments(task: Mapping[str, Any]) -> list[dict]:
    """Метаданные вложений задачи (``attachments[]`` официальной схемы)."""
    result: list[dict] = []
    for item in task.get("attachments") or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        result.append(
            {
                "id": str(item.get("id") or ""),
                "name": name,
                # Weeek не отдаёт mime вложения — формат определяется по расширению.
                "mime": None,
                "size": _as_int(item.get("size")),
                "url": str(item.get("url") or ""),
                "service": str(item.get("service") or ""),
            }
        )
    return result


class WeeekBoard(RestBoardBase):
    """Адаптер Weeek Task Manager поверх общего безопасного REST-скелета."""

    board_type = "weeek"

    def __init__(
        self,
        *,
        api_token: str,
        api_base: str = _API_BASE,
        project_id: str = "",
        board_id: str = "",
        key_prefix: str = "",
        key_pattern: str = "",
        url_template: str = "",
        attachment_max_bytes: int = 10 * 1024 * 1024,
        attachment_timeout: float = 10.0,
        attachment_store_chars: int = 200000,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_base = _normalize_api_base(api_base, secrets=(api_token,))
        self._project_id = _as_int(project_id)
        self._board_id = _as_int(board_id)
        self._key_prefix = (key_prefix or "").strip() or _DEFAULT_PREFIX
        self._att_max_bytes = attachment_max_bytes
        self._att_timeout = attachment_timeout
        self._att_store_chars = attachment_store_chars
        host = (urlsplit(self._api_base).hostname or "").lower()
        self._att_domains = (_registrable_domain(host),)
        self._columns_cache: dict[int, list[dict]] = {}
        super().__init__(
            base_url=self._api_base,
            secrets=(api_token,),
            key_pattern=key_pattern,
            url_template=url_template,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
            },
            transport=transport,
            sleeper=sleeper,
        )

    # --- ключи и скоуп ---

    def _key(self, task_id: int) -> str:
        """Канонический ключ задачи: ``<key_prefix>-<нативный id>``."""
        return f"{self._key_prefix}-{task_id}"

    def _task_id(self, key: str) -> int:
        """Нативный id из ключа (``WEEEK-7``, ``7``); иначе configuration-ошибка."""
        match = _NUMBER_RE.search(str(key or ""))
        if match is None:
            raise BoardProviderError(
                "configuration",
                "Weeek task key must end with the numeric task id.",
                hint="Use the synthesized key <prefix>-<task id>.",
                secrets=tuple(self.secrets),
            )
        return int(match.group(1))

    def _scope(self, value: str | None) -> tuple[dict[str, Any], str]:
        """Скоуп выборки: (query-параметры, префикс-скоуп ключа).

        Числовой ``value`` — это projectId доски (перебивает option), нечисловой —
        префикс синтезированного ключа (как ``PRI`` у YouGile): такие задачи
        отфильтровываются на стороне клиента, потому что нативного проектного кода
        у Weeek нет.
        """
        text = (value or "").strip()
        project_id = _as_int(text) if text else None
        prefix = "" if project_id is not None else text
        params: dict[str, Any] = {}
        resolved_project = project_id if project_id is not None else self._project_id
        if resolved_project is not None:
            params["projectId"] = resolved_project
        if self._board_id is not None:
            params["boardId"] = self._board_id
        return params, prefix

    def validate_connection(self, project: str | None = None) -> dict:
        """Проверить токен (``GET /user/me``) и доступность скоупа задач.

        Токен воркспейс-широкий: он либо валиден, либо нет, поэтому единственный
        обязательный запрос — профиль. Отсутствие project_id/board_id — не ошибка,
        а warning: синк по всему воркспейсу возможен, а create — нет.
        """
        payload = self._read("GET", "/user/me") or {}
        user = payload.get("user") if isinstance(payload.get("user"), Mapping) else {}
        name = " ".join(
            part for part in (user.get("firstName"), user.get("lastName")) if part
        ).strip()
        params, prefix = self._scope(project)
        project_id = params.get("projectId")
        warnings: list[str] = []
        if project_id is None:
            warnings.append(
                "project_id не задан — синк пойдёт по всему воркспейсу, создание задач недоступно"
            )
        if self._board_id is None:
            warnings.append("board_id не задан — колонки и цели create/done недоступны")
        else:
            _columns, column_warnings = self._columns()
            warnings.extend(column_warnings)
        return {
            "status": "ok",
            "identity": {
                "id": user.get("id"),
                "email": user.get("email"),
                "name": name or user.get("email"),
            },
            "project": str(project_id) if project_id is not None else (prefix or None),
            "capabilities": {
                "read": True,
                "create": project_id is not None,
                "finish": True,
            },
            "warnings": warnings,
        }

    # --- колонки доски ---

    def _columns(self, board_id: int | None = None) -> tuple[list[dict], list[str]]:
        """Колонки доски как ``[{id, label}]`` (кэш на инстанс); сбой → warning, не ошибка."""
        target_board = board_id if board_id is not None else self._board_id
        if target_board is None:
            return [], ["доска Weeek не задана — колонки не перечислены"]
        cached = self._columns_cache.get(target_board)
        if cached is not None:
            return cached, []
        try:
            payload = self._read(
                "GET",
                "/tm/board-columns",
                params={"boardId": target_board},
            ) or {}
        except BoardProviderError as error:
            return [], [f"колонки доски недоступны: {error.category}"]
        columns: list[dict] = []
        for item in payload.get("boardColumns") or []:
            column_id = _as_int(item.get("id")) if isinstance(item, Mapping) else None
            if column_id is None:
                continue
            columns.append({"id": column_id, "label": str(item.get("name") or "")})
        self._columns_cache[target_board] = columns
        return columns, []

    def _column_titles(self, board_id: int | None = None) -> dict[int, str]:
        """Индекс id → title колонки для резолва статуса задачи (best-effort)."""
        columns, _warnings = self._columns(board_id)
        return {column["id"]: column["label"] for column in columns}

    def _resolve_column(self, target: str | None) -> tuple[dict | None, list[str]]:
        """Колонка по точному id или однозначному title; иначе (None, warnings).

        Ненайденная/неоднозначная цель — предупреждение, а не ошибка: контракт
        create/finish запрещает падать из-за target.
        """
        requested = (target or "").strip()
        if not requested:
            return None, []
        columns, warnings = self._columns()
        exact = [column for column in columns if str(column["id"]) == requested]
        if exact:
            return exact[0], warnings
        by_label = [column for column in columns if column["label"] == requested]
        if len(by_label) == 1:
            return by_label[0], warnings
        if len(by_label) > 1:
            warnings.append(f"колонка {requested!r} неоднозначна — не применена")
            return None, warnings
        warnings.append(f"колонка {requested!r} не найдена среди колонок доски")
        return None, warnings

    def list_targets(self, project: str | None) -> dict:
        """Колонки доски как единый список целей create/done."""
        columns, warnings = self._columns()
        return {
            "targets": [
                {
                    "id": str(column["id"]),
                    "label": column["label"],
                    "purposes": ["create", "done"],
                }
                for column in columns
            ],
            "options": [
                {
                    "key": "project_id",
                    "label": "ID проекта Weeek",
                    "required_for": ["sync", "create", "finish"],
                    "choices": [],
                },
                {
                    "key": "board_id",
                    "label": "ID доски Weeek",
                    "required_for": ["sync", "create", "finish"],
                    "choices": [],
                },
                {
                    "key": "key_prefix",
                    "label": "Префикс ключа задачи",
                    "required_for": ["sync"],
                    "choices": [],
                },
            ],
            "warnings": warnings,
        }

    # --- чтение ---

    def _raw_from_task(
        self,
        task: Mapping[str, Any],
        *,
        columns: Mapping[int, str],
    ) -> RawTask:
        """Задача Weeek → RawTask. Чистая функция: единый маппер для iter_raw/fetch_one."""
        task_id = _as_int(task.get("id")) or 0
        key = self._key(task_id)
        project_id, board_id, column_id = _placement(task)
        subtask_ids = [
            str(value)
            for value in (_as_int(item) for item in task.get("subTasks") or [])
            if value is not None
        ]
        completed = task.get("isCompleted")
        return RawTask(
            key=key,
            project_code=key,
            title=str(task.get("title") or ""),
            description=str(task.get("description") or ""),
            status=columns.get(column_id) if column_id is not None else None,
            subtask_ids=subtask_ids,
            timestamp=_epoch_ms(task.get("updatedAt")),
            links=[],
            attachments=_attachments(task),
            board_id=str(task_id),
            archived=None,
            terminal=completed if isinstance(completed, bool) else None,
            provider_data={
                "task_id": task_id,
                "project_id": project_id,
                "board_id": board_id,
                "board_column_id": column_id,
                "parent_id": _as_int(task.get("parentId")),
                "is_deleted": bool(task.get("isDeleted")),
                "completed_at": task.get("completedAt"),
                "priority": _as_int(task.get("priority")),
                "type": str(task.get("type") or ""),
            },
        )

    def _iter_tasks(self, params: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
        """Ленивый обход страниц ``GET /tm/tasks`` по offset/perPage, пока ``hasMore``."""
        consumed = 0

        def fetch(cursor: str | None) -> Any:
            return self._read(
                "GET",
                "/tm/tasks",
                params={**params, "perPage": _PAGE, "offset": int(cursor or 0)},
            ) or {}

        def items(payload: Any) -> list:
            nonlocal consumed
            rows = payload.get("tasks") if isinstance(payload, Mapping) else None
            rows = rows if isinstance(rows, list) else []
            consumed += len(rows)
            return [row for row in rows if isinstance(row, Mapping)]

        def next_cursor(payload: Any) -> str | None:
            has_more = isinstance(payload, Mapping) and payload.get("hasMore") is True
            return str(consumed) if has_more and consumed else None

        yield from paginate_cursor(fetch, items=items, next_cursor=next_cursor)

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
        """Все задачи скоупа: серверные фильтры projectId/boardId + префикс-скоуп ключа."""
        params, prefix = self._scope(board)
        count = 0
        if limit is not None and count >= limit:
            return
        columns = self._column_titles()
        for task in self._iter_tasks(params):
            raw = self._raw_from_task(task, columns=columns)
            if prefix and project_prefix(raw.project_code) != prefix:
                continue
            yield raw
            count += 1
            if limit is not None and count >= limit:
                return

    # --- запись ---

    def _require_project(self, project: str | None) -> int:
        """Числовой projectId: аргумент (если он числовой), иначе option; иначе ошибка."""
        text = (project or "").strip()
        resolved = _as_int(text) if text else None
        if resolved is None:
            resolved = self._project_id
        if resolved is None:
            raise BoardProviderError(
                "configuration",
                "Weeek project_id is required to create a task.",
                hint="Set the provider option project_id to the numeric Weeek project id.",
                secrets=tuple(self.secrets),
            )
        return resolved

    def create(
        self,
        doc_md: str,
        *,
        title: str,
        target: str | None,
        project: str | None,
    ) -> dict:
        """Создать задачу из канонического markdown; цель — колонка доски.

        Описание уходит в HTML (транспорт Weeek). Ненайденная колонка не мешает
        созданию: задача уходит в первую колонку доски, причина — в warnings.
        """
        project_id = self._require_project(project)
        if not (title or "").strip():
            raise BoardProviderError(
                "configuration",
                "Weeek task title is required.",
                hint="Pass a non-empty task title.",
                secrets=tuple(self.secrets),
            )
        columns, warnings = self._columns()
        column, target_warnings = self._resolve_column(target)
        if target:
            warnings.extend(warning for warning in target_warnings if warning not in warnings)
        if column is None and columns:
            column = columns[0]
            if target:
                warnings.append(
                    f"задача создана в колонке {column['label']!r}"
                )
        location = {
            "projectId": project_id,
            "boardColumnId": column["id"] if column is not None else None,
        }
        created = self._write(
            "POST",
            "/tm/tasks",
            json={
                "title": title,
                "description": md_to_html(doc_md),
                "locations": [location],
            },
        ) or {}
        task = created.get("task") if isinstance(created.get("task"), Mapping) else {}
        task_id = _as_int(task.get("id"))
        if task_id is None:
            raise BoardProviderError(
                "unsupported",
                "Weeek create response did not include a task id.",
                hint="Check the project and token permissions.",
                secrets=tuple(self.secrets),
            )
        key = self._key(task_id)
        return {
            "key": key,
            "url": self._task_url(key) or None,
            "board_id": str(task_id),
            "target_resolved": column["label"] if column is not None else None,
            "warnings": warnings,
        }

    def _pr_block(self, pr_url: str, note: str | None) -> str:
        """HTML-блок PR-ссылки с маркером идемпотентности; note от пользователя экранируется."""
        safe_url = html.escape(pr_url, quote=True)
        block = f'\n{_PR_MARKER}\n<p>PR: <a href="{safe_url}">{safe_url}</a></p>'
        if (note or "").strip():
            block += f"\n<p>{html.escape(note.strip())}</p>"
        return block

    def finish(
        self,
        key: str,
        pr_url: str,
        *,
        note: str | None = None,
        mark_done: bool = True,
        target: str | None = None,
    ) -> dict:
        """Закрыть задачу Weeek: PR-ссылка в описание + isCompleted + перенос в колонку.

        Каноническое «сделано» в Weeek — флаг ``isCompleted`` (``POST /tm/tasks/{id}/complete``):
        у колонок доски нет типа «done», поэтому по колонке закрытость не определяется.
        Перенос в done-колонку — дополнительный, косметический шаг (поле ``column_moved``).
        Комментариев в Public API нет, поэтому PR-ссылка дописывается в описание;
        идемпотентность — по маркеру ``_PR_MARKER`` (и по самой ссылке для задач,
        отредактированных вручную). Каждая запись двигает ``updatedAt``, поэтому следующий
        синк подхватит задачу.
        """
        task_id = self._task_id(key)
        payload = self._read("GET", f"/tm/tasks/{task_id}") or {}
        task = payload.get("task") if isinstance(payload.get("task"), Mapping) else {}
        description = str(task.get("description") or "")
        completed = bool(task.get("isCompleted"))
        _project_id, _board_id, column_id = _placement(task)
        warnings: list[str] = []

        safe_url = html.escape(pr_url, quote=True)
        link_present = bool(pr_url) and (
            _PR_MARKER in description or pr_url in description or safe_url in description
        )
        pr_link_added = False
        if pr_url and not link_present:
            # TODO(weeek): не подтверждено докой — официальная схема PUT /tm/tasks/{id}
            # не перечисляет description среди полей (перечислены title/parentId/priority/
            # locations/...), хотя сторонние клиенты (npm weeek-mcp, weeek-api) обновляют
            # описание именно этим методом. Если деплой получит 4xx — сбой уйдёт в warnings,
            # ревью не сломается, а адаптеру потребуется endpoint из ответа поддержки.
            try:
                self._write(
                    "PUT",
                    f"/tm/tasks/{task_id}",
                    json={"description": description + self._pr_block(pr_url, note)},
                )
                pr_link_added = True
            except BoardProviderError as error:
                warnings.append(f"описание задачи не обновлено ({error.category})")

        column_moved = False
        target_resolved: str | None = None
        if target:
            column, target_warnings = self._resolve_column(target)
            warnings.extend(warning for warning in target_warnings if warning not in warnings)
            if column is not None:
                target_resolved = column["label"]
                if column["id"] != column_id:
                    try:
                        self._write(
                            "POST",
                            f"/tm/tasks/{task_id}/board-column",
                            json={"boardColumnId": column["id"]},
                        )
                        column_moved = True
                    except BoardProviderError as error:
                        warnings.append(f"колонка задачи не изменена ({error.category})")

        done_set = False
        if mark_done and not completed:
            try:
                self._write("POST", f"/tm/tasks/{task_id}/complete")
                done_set = True
            except BoardProviderError as error:
                warnings.append(f"задача не закрыта ({error.category})")

        link_satisfied = not pr_url or link_present
        done_satisfied = not mark_done or completed
        return {
            "key": key,
            "board_id": str(task_id),
            "done_set": done_set,
            "pr_link_added": pr_link_added,
            "column_moved": column_moved,
            "already_closed": (
                not pr_link_added
                and not done_set
                and not column_moved
                and link_satisfied
                and done_satisfied
            ),
            "target_resolved": target_resolved,
            "warnings": warnings,
        }

    # --- нормализация ---

    def _subtask_links(self, raw: RawTask) -> tuple[list[dict], list[str]]:
        """Подзадачи: ссылка на каждую + заголовок отдельным GET (best-effort, fail-soft)."""
        links: list[dict] = []
        warnings: list[str] = []
        ids = raw.subtask_ids[:_SUBTASK_CAP]
        if len(raw.subtask_ids) > _SUBTASK_CAP:
            warnings.append(
                f"подзадач больше {_SUBTASK_CAP} — заголовки резолвятся не для всех"
            )
        for value in ids:
            task_id = _as_int(value)
            if task_id is None:
                continue
            key = self._key(task_id)
            title = ""
            try:
                payload = self._read("GET", f"/tm/tasks/{task_id}") or {}
            except BoardProviderError as error:
                warnings.append(f"подзадача {key} не резолвится: {error.category}")
            else:
                task = payload.get("task")
                title = str(task.get("title") or "") if isinstance(task, Mapping) else ""
            links.append({"type": "subtask", "key": key, "title": title})
        return links, warnings

    def _attachment_url(self, item: Mapping[str, Any]) -> str:
        """Ссылка на файл: из payload задачи, иначе свежая через ``GET /ws/attachments/{id}``.

        Ссылка сервиса ``weeek`` живёт час (официальная схема), поэтому пустое/устаревшее
        значение имеет смысл переспросить; сбой запроса — не ошибка нормализации.
        """
        url = str(item.get("url") or "")
        if url:
            return url
        attachment_id = str(item.get("id") or "")
        if not attachment_id:
            return ""
        try:
            payload = self._read("GET", f"/ws/attachments/{attachment_id}") or {}
        except BoardProviderError:
            log.warning("weeek: ссылка на вложение %s не обновлена", attachment_id)
            return ""
        data = payload.get("data")
        return str(data.get("url") or "") if isinstance(data, Mapping) else ""

    def _attachment_briefs(self, raw: RawTask) -> tuple[list[dict], list[str]]:
        """Вложения задачи: текст, если файл доступен и парсится; иначе метаданные + warning."""
        result: list[dict] = []
        warnings: list[str] = []
        for item in raw.attachments:
            name = str(item.get("name") or "")
            mime = item.get("mime")
            size = _as_int(item.get("size"))
            url = self._attachment_url(item)
            meta = {
                "name": name,
                "mime_type": mime,
                "size": size,
                "content_text": None,
                "url": url,
            }
            if not url:
                warnings.append(f"вложение {name!r}: ссылка недоступна")
            elif not host_allowed(url, self._att_domains):
                warnings.append(f"вложение {name!r}: origin не разрешён")
            elif size is not None and size > self._att_max_bytes:
                warnings.append(f"вложение {name!r}: размер превышает лимит")
            elif not attachment_supported(name, mime):
                warnings.append(f"вложение {name!r}: формат не поддерживается")
            else:
                fetched = fetch_attachment(
                    self._client,
                    name=name,
                    mime=mime,
                    size=size,
                    url=url,
                    timeout=self._att_timeout,
                    max_bytes=self._att_max_bytes,
                    store_chars=self._att_store_chars,
                )
                meta.update(fetched)
                if meta["content_text"] is None:
                    warnings.append(f"вложение {name!r}: содержимое недоступно")
            result.append(meta)
        return result, warnings

    def _brief(self, raw: RawTask, *, with_details: bool) -> dict:
        """Общее тело normalize/normalize_meta: details=False — без I/O и без деталей."""
        data = raw.provider_data
        links: list[dict] = []
        criteria: list[str] = []
        attachments: list[dict] = []
        warnings: list[str] = []

        parent_id = _as_int(data.get("parent_id"))
        if parent_id is not None:
            links.append({"type": "parent", "key": self._key(parent_id), "title": ""})

        if with_details:
            subtasks, subtask_warnings = self._subtask_links(raw)
            links.extend(subtasks)
            warnings.extend(subtask_warnings)
            criteria.extend(link["title"] for link in subtasks if link["title"])
            attachments, attachment_warnings = self._attachment_briefs(raw)
            warnings.extend(attachment_warnings)
        else:
            for value in raw.subtask_ids:
                task_id = _as_int(value)
                if task_id is not None:
                    links.append(
                        {"type": "subtask", "key": self._key(task_id), "title": ""}
                    )
        links.extend(raw.links)

        description = html_to_md(raw.description)
        covered = {raw.key, *(link["key"] for link in links if link.get("key"))}
        if self._key_pattern:
            for match in re.finditer(self._key_pattern, description):
                code = match.group(0)
                if code not in covered:
                    links.append({"type": "related", "key": code, "title": ""})
                    covered.add(code)

        return {
            "key": raw.key,
            "aliases": [raw.board_id] if raw.board_id else [],
            "title": raw.title,
            "description": description,
            "criteria": criteria,
            "status": "done" if raw.terminal is True else raw.status,
            "url": self._task_url(raw.key) or None,
            "links": links,
            "project": project_prefix(raw.key),
            "attachments": attachments,
            "warnings": warnings,
            "provider_data": {
                "task_id": data.get("task_id"),
                "project_id": data.get("project_id"),
                "board_id": data.get("board_id"),
                "board_column_id": data.get("board_column_id"),
                "priority": data.get("priority"),
                "type": data.get("type"),
            },
        }

    def normalize(self, raw: RawTask) -> dict:
        return self._brief(raw, with_details=True)

    def normalize_meta(self, raw: RawTask) -> dict:
        """Дешёвый TaskBrief без I/O: только плоские поля (PRI-207)."""
        return self._brief(raw, with_details=False)

    def fetch_one(self, key: str) -> RawTask | None:
        """Один RawTask по ключу задачи; 404/сбой/невалидный ключ → None."""
        try:
            task_id = self._task_id(key)
            payload = self._read("GET", f"/tm/tasks/{task_id}")
        except BoardProviderError as error:
            if error.category in {"not_found", "configuration"}:
                return None
            raise
        task = (payload or {}).get("task")
        if not isinstance(task, Mapping):
            return None
        return self._raw_from_task(task, columns=self._column_titles())
