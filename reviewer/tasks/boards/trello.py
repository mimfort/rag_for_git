"""Провайдер доски Trello (REST API v1).

Особенности Trello, определяющие устройство адаптера:

* аутентификация — query-параметры ``key``/``token`` на каждом запросе, не заголовок
  (``RestBoardBase(params=...)``);
* у карточек НЕТ надёжного server-side фильтра по дате изменения, поэтому ``iter_raw``
  вычитывает доску целиком и сортирует по ``dateLastActivity``; инкрементальность даёт
  watermark ``SyncService`` по ``RawTask.timestamp``;
* роль статуса играет список (list) доски: «закрыть задачу» = перенести карточку
  в done-список (``PUT /cards/{id}`` с ``idList``);
* канонический ключ синтезирован: ``<key_prefix>-<idShort>``; нативные ``id``/``shortLink``
  остаются в ``RawTask.board_id`` и в ``aliases`` нормализованной задачи;
* ``desc`` карточки — plain text, который Trello рендерит как подмножество markdown,
  поэтому описание отдаётся и записывается без конвертации разметки.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from reviewer.tasks.boards.attachments import (
    _registrable_domain,
    attachment_supported,
    fetch_attachment,
    host_allowed,
)
from reviewer.tasks.boards.base import RawTask, project_prefix
from reviewer.tasks.boards.errors import BoardProviderError
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

DEFAULT_API_BASE = "https://api.trello.com/1"
_MAX_PAGE = 1000  # жёсткий предел выдачи Trello на один запрос
_BOARD_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")
_SHORT_KEY_RE = re.compile(r"^(?P<prefix>[A-Za-z][A-Za-z0-9_]*)-(?P<number>\d+)$")
_PR_LABEL = "PR: "


def _build_provider(context: ProviderBuildContext) -> TrelloBoard:
    options = context.options
    return TrelloBoard(
        api_key=context.credentials["TRELLO_API_KEY"],
        api_token=context.credentials["TRELLO_API_TOKEN"],
        api_base=context.credentials.get("TRELLO_API_BASE") or DEFAULT_API_BASE,
        board_id=str(options.get("board_id") or ""),
        key_prefix=str(options.get("key_prefix") or ""),
        key_pattern=context.key_pattern,
        url_template=context.url_template,
        attachment_max_bytes=context.attachment_max_bytes,
        attachment_timeout=context.attachment_timeout,
        attachment_store_chars=context.attachment_store_chars,
    )


def provider_spec() -> BoardProviderSpec:
    """Immutable registry spec Trello: key+token в query, доска и префикс — опции."""
    return BoardProviderSpec(
        board_type="trello",
        factory=_build_provider,
        credential_fields=(
            CredentialFieldSpec(
                env="TRELLO_API_KEY",
                label="Trello API key",
                secret=True,
            ),
            CredentialFieldSpec(
                env="TRELLO_API_TOKEN",
                label="Trello API token",
                secret=True,
                aliases=("TRELLO_TOKEN",),
            ),
            CredentialFieldSpec(
                env="TRELLO_API_BASE",
                label="Trello API base URL",
                required=False,
                default=DEFAULT_API_BASE,
            ),
        ),
        setup=ProviderSetupSpec(
            label="Trello",
            help_url="https://trello.com/apps/admin",
            help_text=(
                "Создайте Power-Up на https://trello.com/apps/admin, на вкладке "
                "«API Key» сгенерируйте key, затем по ссылке «Token» рядом с ним "
                "выдайте токен своей учётной записи. Пара key+token даёт полный "
                "доступ к аккаунту Trello — храните оба значения как секрет."
            ),
        ),
        option_fields=(
            ProviderOptionSpec(
                key="board_id",
                label="Trello board id",
                required_for=("sync", "create", "finish"),
            ),
            ProviderOptionSpec(
                key="key_prefix",
                label="Префикс ключа задачи",
                required_for=("sync",),
            ),
        ),
        default_api_base=DEFAULT_API_BASE,
        create_target_label="Список создания",
        done_target_label="Список завершения",
    )


def _normalize_api_base(value: str, *, secrets: tuple[str, ...]) -> str:
    """Fail-closed https-origin API Trello: без userinfo, query и fragment."""
    try:
        parsed = urlsplit((value or "").strip() or DEFAULT_API_BASE)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        hostname, port, parsed = "", None, urlsplit("")
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
            "Trello API base URL must be an https origin without credentials.",
            hint=f"Используйте {DEFAULT_API_BASE}.",
            secrets=secrets,
        )
    suffix = f":{port}" if port and port != 443 else ""
    return f"https://{hostname}{suffix}{parsed.path.rstrip('/')}"


def _timestamp(value: object) -> int:
    """``dateLastActivity`` Trello (ISO 8601, UTC) → epoch ms; мусор → 0."""
    if not isinstance(value, str) or not value:
        return 0
    text = f"{value[:-1]}+00:00" if value[-1:] in {"Z", "z"} else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _rate_limit_hint(status: int, headers: Mapping[str, str]) -> float | None:
    """Пауза из ``x-rate-limit-*-interval-ms``; не 429 → None (обычный retry/backoff)."""
    if status != 429:
        return None
    for name in (
        "x-rate-limit-api-token-interval-ms",
        "x-rate-limit-api-key-interval-ms",
    ):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            return max(0.0, float(raw) / 1000.0)
        except (TypeError, ValueError):
            continue
    return 1.0


class _AttachmentDownloader:
    """httpx-совместимый мини-клиент скачивания вложения Trello.

    Файл вложения отдаётся только по заголовку
    ``Authorization: OAuth oauth_consumer_key="…", oauth_token="…"`` — пары
    ``key``/``token`` в query-строке для download-эндпоинта недостаточно.
    """

    def __init__(self, client: httpx.Client, api_key: str, api_token: str) -> None:
        self._client = client
        self._header = f'OAuth oauth_consumer_key="{api_key}", oauth_token="{api_token}"'

    def get(self, url: str, timeout: float) -> httpx.Response:
        return self._client.get(url, timeout=timeout, headers={"Authorization": self._header})


class TrelloBoard(RestBoardBase):
    """Адаптер Trello поверх общего REST-скелета досок."""

    board_type = "trello"
    CARD_FIELDS = (
        "id,idShort,name,desc,dateLastActivity,idList,idBoard,shortLink,shortUrl,closed"
    )
    LIST_FIELDS = "id,name"
    ATTACHMENT_FIELDS = "id,name,url,mimeType,bytes,isUpload"

    def __init__(
        self,
        *,
        api_key: str,
        api_token: str,
        api_base: str = DEFAULT_API_BASE,
        board_id: str = "",
        key_prefix: str = "",
        key_pattern: str = "",
        url_template: str = "",
        attachment_max_bytes: int = 10 * 1024 * 1024,
        attachment_timeout: float = 10.0,
        attachment_store_chars: int = 200000,
        page_size: int = _MAX_PAGE,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        secrets = tuple(value for value in (api_token, api_key) if value)
        base = _normalize_api_base(api_base, secrets=secrets)
        super().__init__(
            base_url=base,
            secrets=secrets,
            key_pattern=key_pattern,
            url_template=url_template,
            headers={"Accept": "application/json"},
            params={"key": api_key, "token": api_token},
            transport=transport,
            sleeper=sleeper,
            rate_limit_hint=_rate_limit_hint,
        )
        self._board_option = (board_id or "").strip()
        self._key_prefix = (key_prefix or "").strip()
        self._page_size = max(1, min(int(page_size), _MAX_PAGE))
        self._att_domains = (_registrable_domain(urlsplit(base).hostname or ""),)
        self._att_max_bytes = attachment_max_bytes
        self._att_timeout = attachment_timeout
        self._att_store_chars = attachment_store_chars
        self._downloader = _AttachmentDownloader(self._client, api_key, api_token)
        self._lists_cache: dict[str, list[dict]] = {}

    # --- конфигурация ---------------------------------------------------------

    def _board_id(self, scope: str | None = None) -> str:
        """Id доски: опция ``board_id``; иначе project-скоуп, если он сам похож на id."""
        if self._board_option:
            return self._board_option
        if scope and _BOARD_ID_RE.match(scope):
            return scope
        raise BoardProviderError(
            "configuration",
            "Trello board_id option is required.",
            hint="Укажите board_id в task_board.options целевой ветки.",
            secrets=tuple(self.secrets),
        )

    def _card_key(self, card: Mapping[str, Any]) -> str:
        """Канонический ключ ``<key_prefix>-<idShort>``."""
        if not self._key_prefix:
            raise BoardProviderError(
                "configuration",
                "Trello key_prefix option is required to build task keys.",
                hint="Укажите key_prefix в task_board.options целевой ветки.",
                secrets=tuple(self.secrets),
            )
        short = card.get("idShort")
        if short is None:
            raise BoardProviderError(
                "unsupported",
                "Trello card response did not include idShort.",
                secrets=tuple(self.secrets),
            )
        return f"{self._key_prefix}-{short}"

    # --- списки доски ---------------------------------------------------------

    def _lists(self, board_id: str) -> list[dict]:
        """Списки доски (id + name) с кэшом на время жизни провайдера."""
        cached = self._lists_cache.get(board_id)
        if cached is not None:
            return cached
        payload = self._read(
            "GET",
            f"/boards/{quote(board_id, safe='')}/lists",
            params={"fields": self.LIST_FIELDS},
        )
        rows = [
            {"id": str(item.get("id") or ""), "name": str(item.get("name") or "")}
            for item in (payload or [])
            if isinstance(item, Mapping) and item.get("id")
        ]
        self._lists_cache[board_id] = rows
        return rows

    @staticmethod
    def _resolve_list(lists: list[dict], target: str) -> tuple[dict | None, str | None]:
        """Список по точному id, затем по однозначному имени."""
        exact = next((item for item in lists if item["id"] == target), None)
        if exact is not None:
            return exact, None
        by_name = [item for item in lists if item["name"] == target]
        if len(by_name) == 1:
            return by_name[0], None
        if len(by_name) > 1:
            return None, f"список Trello {target!r} неоднозначен"
        return None, f"список Trello {target!r} не найден"

    # --- чтение ---------------------------------------------------------------

    def _raw_from_card(self, card: Mapping[str, Any], statuses: Mapping[str, str]) -> RawTask:
        key = self._card_key(card)
        card_id = str(card.get("id") or "")
        return RawTask(
            key=key,
            project_code=key,
            title=str(card.get("name") or ""),
            description=str(card.get("desc") or ""),
            status=statuses.get(str(card.get("idList") or "")),
            subtask_ids=[],
            timestamp=_timestamp(card.get("dateLastActivity")),
            links=[],
            attachments=[],
            board_id=card_id,
            provider_data={
                "id_short": card.get("idShort"),
                "id_list": str(card.get("idList") or ""),
                "id_board": str(card.get("idBoard") or ""),
                "short_link": str(card.get("shortLink") or ""),
                "short_url": str(card.get("shortUrl") or ""),
                "closed": bool(card.get("closed")),
            },
        )

    def _iter_cards(self, board_id: str) -> Iterator[dict]:
        """Ленивый обход карточек доски: ``limit`` + курсор ``before`` по id карточки.

        ``before`` официально принимает id карточки (Trello выводит из него дату
        создания), поэтому курсором берётся МИНИМАЛЬНЫЙ id страницы: порядок выдачи
        листинга Trello не специфицирован, а минимум гарантирует, что следующая
        страница строго старше всего уже прочитанного. Повторы (известная особенность
        листинга) отбрасываются по id, что заодно исключает зацикливание.
        """
        path = f"/boards/{quote(board_id, safe='')}/cards"
        seen: set[str] = set()
        fresh = 0

        def fetch(cursor: str | None) -> Any:
            params: dict[str, str] = {
                "fields": self.CARD_FIELDS,
                "limit": str(self._page_size),
            }
            if cursor:
                params["before"] = cursor
            return self._read("GET", path, params=params) or []

        def items(payload: Any) -> list:
            nonlocal fresh
            rows = [
                item
                for item in (payload or [])
                if isinstance(item, Mapping) and item.get("id") and item["id"] not in seen
            ]
            seen.update(str(item["id"]) for item in rows)
            fresh = len(rows)
            return rows

        def next_cursor(payload: Any) -> str | None:
            ids = [
                str(item["id"])
                for item in (payload or [])
                if isinstance(item, Mapping) and item.get("id")
            ]
            if not fresh or len(ids) < self._page_size:
                return None
            return min(ids)

        return paginate_cursor(fetch, items=items, next_cursor=next_cursor)

    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        """Карточки доски, отсортированные по свежести активности (свежие первыми).

        ``board`` — project-скоуп синка; сама доска задаётся опцией ``board_id``
        (project принимается как id, только если сам выглядит как id Trello).
        Server-side фильтра по дате изменения у Trello нет, поэтому доска читается
        целиком, а ``limit`` режет уже отсортированную выдачу — дорогой ``normalize``
        достаётся самым свежим карточкам.
        """
        board_id = self._board_id(board)
        statuses = {item["id"]: item["name"] for item in self._lists(board_id)}
        rows = [self._raw_from_card(card, statuses) for card in self._iter_cards(board_id)]
        rows.sort(key=lambda row: (-row.timestamp, row.key))
        yield from (rows if limit is None else rows[: max(limit, 0)])

    def _card_by_key(self, key: str) -> dict | None:
        """Карточка по каноническому ключу без перебора доски.

        ``<prefix>-<idShort>`` резолвится вложенным ресурсом
        ``GET /boards/{boardId}/cards/{idShort}``; нативный ``id``/``shortLink`` —
        напрямую через ``GET /cards/{id}``. 404 → None.
        """
        match = _SHORT_KEY_RE.match(key or "")
        if match:
            path = (
                f"/boards/{quote(self._board_id(), safe='')}"
                f"/cards/{quote(match.group('number'), safe='')}"
            )
        else:
            path = f"/cards/{quote(key or '', safe='')}"
        try:
            payload = self._read("GET", path, params={"fields": self.CARD_FIELDS})
        except BoardProviderError as error:
            if error.category == "not_found":
                return None
            raise
        return payload if isinstance(payload, Mapping) else None

    def fetch_one(self, key: str) -> RawTask | None:
        card = self._card_by_key(key)
        if card is None:
            return None
        board_id = str(card.get("idBoard") or "") or self._board_id()
        statuses = {item["id"]: item["name"] for item in self._lists(board_id)}
        return self._raw_from_card(card, statuses)

    # --- discovery ------------------------------------------------------------

    def validate_connection(self, project: str | None = None) -> dict:
        """Проверить identity и доступность доски, не раскрывая key/token."""
        identity = self._read("GET", "/members/me", params={"fields": "id,username,fullName"}) or {}
        capabilities = {"read": True, "create": False, "done": False}
        warnings: list[str] = []
        board_id = ""
        try:
            board_id = self._board_id(project)
        except BoardProviderError as error:
            warnings.append(error.message)
        if board_id:
            board = self._read(
                "GET",
                f"/boards/{quote(board_id, safe='')}",
                params={"fields": "id,name,closed"},
            ) or {}
            if board.get("closed"):
                warnings.append("доска Trello закрыта (архив) — запись может быть недоступна")
            lists = self._lists(board_id)
            capabilities["create"] = bool(lists)
            capabilities["done"] = bool(lists)
            if not lists:
                warnings.append("у доски Trello нет списков — создание и закрытие невозможны")
        if project and self._key_prefix and project != self._key_prefix:
            warnings.append(
                f"project {project!r} не совпадает с key_prefix {self._key_prefix!r}"
            )
        return {
            "status": "ok",
            "identity": {
                "id": identity.get("id"),
                "username": identity.get("username"),
                "full_name": identity.get("fullName"),
            },
            "project": project,
            "capabilities": capabilities,
            "warnings": warnings,
        }

    def list_targets(self, project: str | None) -> dict:
        """Списки доски как единые create/done-цели."""
        warnings: list[str] = []
        lists: list[dict] = []
        try:
            lists = self._lists(self._board_id(project))
        except BoardProviderError as error:
            warnings.append(error.message)
        if not lists and not warnings:
            warnings.append("у доски Trello нет списков")
        return {
            "targets": [
                {"id": item["id"], "label": item["name"], "purposes": ["create", "done"]}
                for item in lists
            ],
            "options": [],
            "warnings": warnings,
        }

    # --- нормализация ---------------------------------------------------------

    def _checklists(self, card_id: str) -> tuple[list[str], list[dict], list[str]]:
        """Пункты чеклистов карточки → (criteria, links типа ``subtask``, warnings)."""
        try:
            payload = self._read(
                "GET",
                f"/cards/{quote(card_id, safe='')}/checklists",
                params={"fields": "id,name", "checkItem_fields": "name,state"},
            ) or []
        except BoardProviderError as error:
            log.warning("trello: чеклисты карточки %s недоступны: %s", card_id, error.category)
            return [], [], [f"чеклисты карточки недоступны: {error.category}"]
        criteria: list[str] = []
        links: list[dict] = []
        for checklist in payload:
            if not isinstance(checklist, Mapping):
                continue
            group = str(checklist.get("name") or "")
            for item in checklist.get("checkItems") or []:
                if not isinstance(item, Mapping):
                    continue
                name = str(item.get("name") or "")
                if not name:
                    continue
                criteria.append(name)
                links.append(
                    {
                        "type": "subtask",
                        "key": str(item.get("id") or name),
                        "title": name,
                        "group": group,
                        "state": str(item.get("state") or ""),
                    }
                )
        return criteria, links, []

    def _attachments(self, card_id: str) -> tuple[list[dict], list[str]]:
        """Вложения карточки: метаданные + текст, если файл доступен и парсится."""
        try:
            payload = self._read(
                "GET",
                f"/cards/{quote(card_id, safe='')}/attachments",
                params={"fields": self.ATTACHMENT_FIELDS},
            ) or []
        except BoardProviderError as error:
            log.warning("trello: вложения карточки %s недоступны: %s", card_id, error.category)
            return [], [f"вложения карточки недоступны: {error.category}"]
        result: list[dict] = []
        warnings: list[str] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or item.get("fileName") or "")
            url = str(item.get("url") or "")
            size = item.get("bytes")
            mime = item.get("mimeType") or None
            meta = {"name": name, "mime_type": mime, "size": size, "content_text": None}
            reason = self._attachment_skip_reason(name, mime, size, url)
            if reason:
                result.append(meta)
                warnings.append(f"вложение {name!r}: {reason}")
                continue
            fetched = fetch_attachment(
                self._downloader,
                name=name,
                mime=mime,
                size=size,
                url=url,
                timeout=self._att_timeout,
                max_bytes=self._att_max_bytes,
                store_chars=self._att_store_chars,
            )
            result.append(fetched)
            if fetched["content_text"] is None:
                warnings.append(f"вложение {name!r}: содержимое недоступно")
        return result, warnings

    def _attachment_skip_reason(
        self,
        name: str,
        mime: object,
        size: object,
        url: str,
    ) -> str | None:
        """Причина не качать файл вложения (иначе None)."""
        if not url:
            return "нет ссылки на файл"
        if not host_allowed(url, self._att_domains):
            return "ссылка вне домена Trello — файл не запрашивается"
        if isinstance(size, int) and size > self._att_max_bytes:
            return "размер превышает лимит"
        if not attachment_supported(name, mime if isinstance(mime, str) else None):
            return "формат не поддерживается"
        return None

    def _brief(self, raw: RawTask, *, with_details: bool) -> dict:
        provider_data = raw.provider_data
        warnings: list[str] = []
        criteria: list[str] = []
        links: list[dict] = []
        attachments: list[dict] = []
        if with_details:
            criteria, links, checklist_warnings = self._checklists(raw.board_id)
            attachments, attachment_warnings = self._attachments(raw.board_id)
            warnings.extend(checklist_warnings)
            warnings.extend(attachment_warnings)
        aliases = [
            value
            for value in (raw.board_id, str(provider_data.get("short_link") or ""))
            if value and value != raw.key
        ]
        covered = {raw.key, *aliases, *(link["key"] for link in links)}
        if self._key_pattern:
            for match in re.finditer(self._key_pattern, raw.description or ""):
                code = match.group(0)
                if code not in covered:
                    links.append({"type": "related", "key": code, "title": ""})
                    covered.add(code)
        return {
            "key": raw.key,
            "aliases": aliases,
            "title": raw.title,
            "description": raw.description,
            "criteria": criteria,
            "status": raw.status,
            "url": str(provider_data.get("short_url") or "") or self._task_url(raw.key) or None,
            "links": links,
            "project": project_prefix(raw.project_code or raw.key),
            "attachments": attachments,
            "warnings": warnings,
        }

    def normalize(self, raw: RawTask) -> dict:
        """RawTask → TaskBrief: ``desc`` Trello уже markdown, конвертация не нужна."""
        return self._brief(raw, with_details=True)

    def normalize_meta(self, raw: RawTask) -> dict:
        """Дешёвый TaskBrief без I/O: чеклисты и вложения не резолвятся."""
        return self._brief(raw, with_details=False)

    # --- запись ---------------------------------------------------------------

    def create(
        self,
        doc_md: str,
        *,
        title: str,
        target: str | None,
        project: str | None,
    ) -> dict:
        """Создать карточку: ``POST /cards`` с ``idList`` целевого списка.

        Цель — список доски (по id или однозначному имени); не найдена → первый
        список доски и причина в ``warnings``.
        """
        board_id = self._board_id(project)
        lists = self._lists(board_id)
        if not lists:
            raise BoardProviderError(
                "configuration",
                "Trello board has no lists to create a card in.",
                hint="Создайте хотя бы один список на доске.",
                secrets=tuple(self.secrets),
            )
        warnings: list[str] = []
        chosen: dict | None = None
        if target:
            chosen, warning = self._resolve_list(lists, target)
            if warning:
                warnings.append(f"{warning} — карточка создана в {lists[0]['name']!r}")
        chosen = chosen or lists[0]
        created = self._write(
            "POST",
            "/cards",
            json={
                "idList": chosen["id"],
                "name": title,
                "desc": doc_md,
                "pos": "bottom",
            },
        ) or {}
        card_id = str(created.get("id") or "")
        if not card_id:
            raise BoardProviderError(
                "unsupported",
                "Trello create response did not include a card id.",
                secrets=tuple(self.secrets),
            )
        try:
            key = self._card_key(created)
        except BoardProviderError as error:
            if error.category != "unsupported":
                raise
            warnings.append("idShort карточки не вернулся — ключом стал нативный id")
            key = str(created.get("shortLink") or card_id)
        return {
            "key": key,
            "url": str(created.get("shortUrl") or "") or self._task_url(key) or None,
            "board_id": card_id,
            "target_resolved": chosen["name"],
            "warnings": warnings,
        }

    def finish(
        self,
        key: str,
        pr_url: str,
        *,
        note: str | None = None,
        mark_done: bool = True,
        target: str | None = None,
    ) -> dict:
        """Закрыть задачу: перенести карточку в done-список + дописать PR-ссылку в ``desc``.

        Одно ``PUT /cards/{id}`` двигает ``dateLastActivity`` ровно один раз, поэтому
        следующий синк переиндексирует задачу. Идемпотентность — по наличию URL PR
        в описании (маркер — строка ``PR: <url>``; HTML-комментарий не годится:
        Trello показал бы его в описании как текст) и по текущему списку карточки.
        """
        card = self._card_by_key(key)
        if card is None:
            raise BoardProviderError(
                "not_found",
                "Trello card was not found for the requested task key.",
                hint="Проверьте key_prefix и board_id доски.",
                secrets=tuple(self.secrets),
            )
        card_id = str(card.get("id") or "")
        desc = str(card.get("desc") or "")
        current_list = str(card.get("idList") or "")
        warnings: list[str] = []
        payload: dict[str, Any] = {}

        link_present = bool(pr_url) and pr_url in desc
        if pr_url and not link_present:
            block = f"{_PR_LABEL}{pr_url}"
            if note:
                block = f"{block}\n{note}"
            payload["desc"] = f"{desc}\n\n{block}" if desc else block

        target_list: dict | None = None
        already_at_target = False
        if mark_done:
            if not target:
                warnings.append(
                    "done-цель Trello не задана — список завершения не угадывается"
                )
            else:
                board_id = str(card.get("idBoard") or "") or self._board_id()
                target_list, warning = self._resolve_list(self._lists(board_id), target)
                if warning:
                    warnings.append(f"{warning} — карточка не перенесена")
                elif target_list is not None and target_list["id"] == current_list:
                    already_at_target = True
                elif target_list is not None:
                    payload["idList"] = target_list["id"]

        pr_link_added = "desc" in payload
        done_set = "idList" in payload
        if payload:
            try:
                self._write("PUT", f"/cards/{quote(card_id, safe='')}", json=payload)
            except BoardProviderError as error:
                warnings.append(f"обновление карточки Trello не удалось: {error.category}")
                pr_link_added = False
                done_set = False
        link_satisfied = not pr_url or link_present or pr_link_added
        done_satisfied = not mark_done or already_at_target or done_set
        return {
            "key": key,
            "board_id": card_id,
            "done_set": done_set,
            "pr_link_added": pr_link_added,
            "already_closed": (
                not pr_link_added and not done_set and link_satisfied and done_satisfied
            ),
            "warnings": warnings,
        }
