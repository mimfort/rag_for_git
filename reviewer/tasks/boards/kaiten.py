"""Провайдер доски Kaiten (kaiten.ru): REST API компании на её собственном субдомене.

Base URL индивидуален для каждой компании — ``https://<company>.kaiten.ru/api/latest``
(алиас актуальной версии; путь ``/api/v1`` тоже принимается), поэтому он приходит
обязательным non-secret credential ``KAITEN_BASE_URL``. Аутентификация —
``Authorization: Bearer <token>`` (ключ выпускается в профиле пользователя).

Особенности доски (сверено с developers.kaiten.ru):
- listing ``GET /cards`` НЕ отдаёт ``description`` по умолчанию — оно запрашивается
  параметром ``additional_card_fields=description``; пагинация — ``limit``/``offset``
  (limit max 100);
- ``description`` — нативный markdown (Kaiten поддерживает markdown-разметку описания),
  конвертация не нужна;
- чеклисты и файлы карточки приходят инлайн в ``GET /cards/{id}``, но не в listing,
  поэтому ``normalize`` при необходимости дочитывает карточку (fail-soft);
- статус — колонка доски; машинно-читаемый ``type`` колонки: 1 — очередь,
  2 — в работе, 3 — готово (done-цель определяется без угадывания локализованных
  названий);
- ключ синтезируется как ``<key_prefix>-<card id>``; нативный числовой id уходит в
  ``RawTask.board_id`` и в ``aliases`` нормализованной задачи.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
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
from reviewer.tasks.boards.base import RawTask, project_prefix
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.pagination import paginate_offset
from reviewer.tasks.boards.registry import (
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderOptionSpec,
    ProviderSetupSpec,
)
from reviewer.tasks.boards.restbase import RestBoardBase

log = logging.getLogger(__name__)

_PAGE = 100                      # limit листинга карточек: максимум по документации
_API_SUFFIX = "/api/latest"      # алиас актуальной версии API компании
_DOCS_URL = "https://developers.kaiten.ru/"
_TOKEN_PATH = "/profile/api-key"
_DEFAULT_PREFIX = "KTN"

# Маркер идемпотентности PR-ссылки в markdown-описании карточки: повторный finish его видит.
_PR_MARKER = "<!-- reviewer:pr-link -->"

_NUMBER_RE = re.compile(r"(\d+)\s*$")

# Числовые enum-коды Kaiten: колонка (`type`) и карточка (`state`) используют один набор.
_COLUMN_QUEUE, _COLUMN_IN_PROGRESS, _COLUMN_DONE = 1, 2, 3
_STATE_NAMES = {
    _COLUMN_QUEUE: "queued",
    _COLUMN_IN_PROGRESS: "in_progress",
    _COLUMN_DONE: "done",
}


def _build_provider(context: ProviderBuildContext) -> KaitenBoard:
    """Собрать провайдера из registry-контекста (креды + immutable options)."""
    return KaitenBoard(
        token=context.credentials["KAITEN_API_TOKEN"],
        base_url=context.credentials["KAITEN_BASE_URL"],
        board_id=str(context.options.get("board_id") or ""),
        space_id=str(context.options.get("space_id") or ""),
        key_prefix=str(context.options.get("key_prefix") or ""),
        key_pattern=context.key_pattern,
        url_template=context.url_template,
        attachment_max_bytes=context.attachment_max_bytes,
        attachment_timeout=context.attachment_timeout,
        attachment_store_chars=context.attachment_store_chars,
    )


def _api_key_url(credentials: Mapping[str, str]) -> str:
    """Страница выпуска API-ключа на субдомене компании; кривой base URL → общая дока."""
    try:
        api_base = _normalize_api_base(credentials.get("KAITEN_BASE_URL", ""), secrets=())
    except BoardProviderError:
        return _DOCS_URL
    return _web_base(api_base) + _TOKEN_PATH


def provider_spec() -> BoardProviderSpec:
    """Immutable registry spec Kaiten.

    ``default_api_base`` пуст осознанно: единого базового URL у Kaiten нет —
    он состоит из субдомена компании, поэтому ``KAITEN_BASE_URL`` обязателен.
    """
    return BoardProviderSpec(
        board_type="kaiten",
        factory=_build_provider,
        credential_fields=(
            CredentialFieldSpec(
                env="KAITEN_BASE_URL",
                label="Kaiten API base URL (https://<company>.kaiten.ru)",
            ),
            CredentialFieldSpec(
                env="KAITEN_API_TOKEN",
                label="Kaiten API token",
                secret=True,
            ),
        ),
        option_fields=(
            ProviderOptionSpec(
                key="board_id",
                label="Доска Kaiten (числовой id)",
                required_for=("sync", "create", "finish"),
            ),
            ProviderOptionSpec(
                key="key_prefix",
                label="Префикс ключа задачи",
                required_for=("sync",),
            ),
            ProviderOptionSpec(
                key="space_id",
                label="Пространство Kaiten (числовой id)",
            ),
        ),
        setup=ProviderSetupSpec(
            label="Kaiten",
            help_url=_DOCS_URL,
            help_text=(
                "Укажите адрес компании вида https://<company>.kaiten.ru и создайте "
                "API-ключ в профиле пользователя (раздел API key → CREATE API KEY). "
                "Ключ постоянный: его можно отозвать и выпустить заново."
            ),
            help_url_builder=_api_key_url,
        ),
        default_api_base="",
        create_target_label="Колонка создания",
        done_target_label="Колонка завершения",
    )


def _normalize_api_base(value: str, *, secrets: tuple[str, ...]) -> str:
    """Fail-closed HTTPS base URL Kaiten; суффикс ``/api/latest`` дописывается при отсутствии."""
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
            "Kaiten base URL must be an HTTPS company URL without credentials or query.",
            hint="Use https://<company>.kaiten.ru (the /api/latest suffix is optional).",
            secrets=secrets,
        )
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/")
    if "/api" not in path:
        path = f"{path}{_API_SUFFIX}"
    return f"https://{hostname}{port}{path}"


def _web_base(api_base: str) -> str:
    """Web-origin компании из API base: путь до сегмента ``/api`` (self-hosted subpath цел)."""
    parsed = urlsplit(api_base)
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path
    index = path.find("/api")
    return f"https://{(parsed.hostname or '').lower()}{port}{path[:index] if index >= 0 else path}"


def _epoch_ms(value: object) -> int:
    """ISO 8601 → epoch ms в UTC; нераспознанное значение → 0 (без падения)."""
    if not isinstance(value, str) or not value.strip():
        return 0
    text = value.strip()
    text = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _int_or_none(value: object) -> int | None:
    """Числовой id Kaiten (int или строка цифр) → int; иначе None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _card_id(key: str) -> int:
    """Числовой id карточки из ключа задачи (``KTN-7``, ``7``); иначе configuration-ошибка."""
    match = _NUMBER_RE.search(str(key or ""))
    if match is None:
        raise BoardProviderError(
            "configuration",
            "Kaiten task key must end with the card id.",
            hint="Use the synthesized key <prefix>-<card id>.",
        )
    return int(match.group(1))


def _children(card: Mapping[str, Any]) -> list[dict]:
    """Дочерние карточки (реальные подзадачи) из инлайнового ``children``."""
    result: list[dict] = []
    for item in card.get("children") or []:
        if not isinstance(item, Mapping):
            continue
        child_id = _int_or_none(item.get("id"))
        if child_id is None:
            continue
        result.append({"id": child_id, "title": str(item.get("title") or "")})
    return result


def _sort_key(item: Mapping[str, Any]) -> tuple[int, int]:
    """Порядок элемента доски: ``sort_order``, затем id (стабильный тай-брейк)."""
    return (_int_or_none(item.get("sort_order")) or 0, _int_or_none(item.get("id")) or 0)


def _checklists(card: Mapping[str, Any]) -> list[dict]:
    """Чеклисты карточки: имя + элементы (``text``/``checked``) в порядке ``sort_order``."""
    result: list[dict] = []
    for checklist in card.get("checklists") or []:
        if not isinstance(checklist, Mapping) or checklist.get("deleted"):
            continue
        raw_items = [
            item
            for item in (checklist.get("items") or [])
            if isinstance(item, Mapping) and not item.get("deleted")
        ]
        items = [
            {"text": str(item.get("text") or ""), "checked": bool(item.get("checked"))}
            for item in sorted(raw_items, key=_sort_key)
            if str(item.get("text") or "")
        ]
        result.append({"name": str(checklist.get("name") or ""), "items": items})
    return result


def _files(card: Mapping[str, Any]) -> list[dict]:
    """Вложения карточки из инлайнового ``files``: метаданные без скачивания."""
    result: list[dict] = []
    for item in card.get("files") or []:
        if not isinstance(item, Mapping) or item.get("deleted"):
            continue
        url = str(item.get("url") or "")
        name = str(item.get("name") or "")
        if not url or not name:
            continue
        result.append({"name": name, "mime": item.get("mime_type"),
                       "size": _int_or_none(item.get("size")), "url": url})
    return result


def _flatten_columns(payload: object) -> list[dict]:
    """Колонки доски вместе с подколонками в порядке доски.

    ``sort_order`` подколонки задан относительно своего родителя, поэтому сортировка
    делается по каждому уровню отдельно, а обход — в глубину (родитель → его подколонки).
    """
    result: list[dict] = []

    def walk(items: object) -> None:
        level = [item for item in (items or []) if isinstance(item, Mapping)]  # type: ignore
        level.sort(key=_sort_key)
        for item in level:
            column_id = _int_or_none(item.get("id"))
            if column_id is None:
                continue
            result.append({
                "id": column_id,
                "title": str(item.get("title") or ""),
                "type": _int_or_none(item.get("type")),
                "sort_order": _int_or_none(item.get("sort_order")) or 0,
            })
            walk(item.get("subcolumns"))

    walk(payload if isinstance(payload, list) else [])
    return result


class KaitenBoard(RestBoardBase):
    """Адаптер Kaiten поверх общего безопасного REST-скелета."""

    board_type = "kaiten"

    def __init__(
        self,
        *,
        token: str,
        base_url: str,
        board_id: str = "",
        space_id: str = "",
        key_prefix: str = "",
        key_pattern: str = "",
        url_template: str = "",
        attachment_max_bytes: int = 10 * 1024 * 1024,
        attachment_timeout: float = 10.0,
        attachment_store_chars: int = 200000,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_base = _normalize_api_base(base_url, secrets=(token,))
        self._web_base = _web_base(self._api_base)
        self._board_id = (board_id or "").strip()
        self._space_id = (space_id or "").strip()
        self._prefix = (key_prefix or "").strip() or _DEFAULT_PREFIX
        self._att_max_bytes = attachment_max_bytes
        self._att_timeout = attachment_timeout
        self._att_store_chars = attachment_store_chars
        self._att_domains = (
            _registrable_domain(urlsplit(self._api_base).hostname or ""),
        )
        self._columns_cache: dict[str, list[dict]] = {}
        super().__init__(
            base_url=self._api_base,
            secrets=(token,),
            key_pattern=key_pattern,
            url_template=url_template,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            transport=transport,
            sleeper=sleeper,
        )

    # --- разрешение доски и колонок ---

    def _resolve_board(self, value: str | None) -> str:
        """Доска: числовой аргумент project/board, иначе настроенный option ``board_id``."""
        candidate = (value or "").strip()
        return candidate if candidate.isdigit() else self._board_id

    def _require_board(self, value: str | None) -> str:
        """Числовой id доски; пустой или нечисловой option → configuration-ошибка."""
        board = self._resolve_board(value)
        if not board.isdigit():
            raise BoardProviderError(
                "configuration",
                "Kaiten board id is required and must be numeric.",
                hint="Set the provider option board_id to the numeric board id.",
                secrets=tuple(self.secrets),
            )
        return board

    def _read_columns(self, board: str) -> list[dict]:
        """Колонки доски строгим чтением (ошибка доступа пробрасывается)."""
        return _flatten_columns(self._read("GET", f"/boards/{board}/columns"))

    def _columns(self, board: str) -> list[dict]:
        """Кэшированные колонки доски; fail-soft: недоступны → пустой список (один раз)."""
        if not board:
            return []
        if board not in self._columns_cache:
            try:
                self._columns_cache[board] = self._read_columns(board)
            except BoardProviderError as error:
                log.warning("kaiten: колонки доски %s недоступны (%s)", board, error.category)
                self._columns_cache[board] = []
        return self._columns_cache[board]

    def _column_by_id(self, board: str, column_id: int | None) -> dict | None:
        if column_id is None:
            return None
        return next((c for c in self._columns(board) if c["id"] == column_id), None)

    @staticmethod
    def _resolve_target(columns: list[dict], target: str) -> tuple[dict | None, list[str]]:
        """Колонка по точному id, иначе по точному title; неоднозначный label не выбирается."""
        by_id = [column for column in columns if str(column["id"]) == str(target)]
        if by_id:
            return by_id[0], []
        by_label = [column for column in columns if column["title"] == target]
        if len(by_label) == 1:
            return by_label[0], []
        if len(by_label) > 1:
            return None, [f"колонка {target!r} неоднозначна — цель не применена"]
        return None, [f"колонка {target!r} не найдена на доске"]

    @staticmethod
    def _done_column(columns: list[dict]) -> dict | None:
        """Первая колонка с машинным ``type == 3`` (done) — локализованные названия не угадываем."""
        return next((column for column in columns if column["type"] == _COLUMN_DONE), None)

    # --- диагностика и discovery ---

    def validate_connection(self, project: str | None = None) -> dict:
        """Проверить identity и доступ к доске без возврата credentials."""
        identity = self._read("GET", "/users/current") or {}
        board = self._resolve_board(project)
        warnings: list[str] = []
        writable = False
        if board:
            columns = self._read_columns(board)
            self._columns_cache[board] = columns
            writable = True
            if self._done_column(columns) is None:
                warnings.append(
                    f"на доске {board} нет колонки типа done — цель завершения нужно задать явно"
                )
        else:
            warnings.append(
                "board_id не настроен: создание и завершение задач Kaiten недоступны"
            )
        return {
            "status": "ok",
            "identity": {
                "id": identity.get("id"),
                "name": identity.get("full_name") or identity.get("username"),
                "username": identity.get("username"),
            },
            "project": project,
            "capabilities": {
                "read": True,
                "create": writable,
                "finish": writable,
                "attachments": True,
            },
            "warnings": warnings,
        }

    def list_targets(self, project: str | None) -> dict:
        """Нормализованные create/done targets — колонки доски (``type``: 1/2/3)."""
        board = self._resolve_board(project)
        warnings: list[str] = []
        if not board:
            return {"targets": [], "options": [],
                    "warnings": ["board_id не настроен — колонки доски неизвестны"]}
        columns = self._columns(board)
        if not columns:
            warnings.append(f"колонки доски {board} недоступны")
        elif self._done_column(columns) is None:
            warnings.append(f"на доске {board} нет колонки типа done")
        return {
            "targets": [
                {
                    "id": str(column["id"]),
                    "label": column["title"],
                    "purposes": (
                        ["create", "done"] if column["type"] == _COLUMN_DONE else ["create"]
                    ),
                    "kind": _STATE_NAMES.get(column["type"] or 0, "unknown"),
                }
                for column in columns
            ],
            "options": [],
            "warnings": warnings,
        }

    # --- чтение ---

    def _raw_from_card(self, card: Mapping[str, Any]) -> RawTask:
        """Карточка Kaiten → RawTask. Единый маппер для listing и точечного чтения.

        ``status`` — title колонки, если её удалось разрешить (кэш колонок доски);
        иначе имя машинного ``state`` карточки. Детали (чеклисты/файлы) приходят
        только в точечном ответе, поэтому фиксируется флаг ``detailed``.
        """
        card_id = _int_or_none(card.get("id")) or 0
        key = f"{self._prefix}-{card_id}"
        board = str(_int_or_none(card.get("board_id")) or "")
        column_id = _int_or_none(card.get("column_id"))
        column = self._column_by_id(board, column_id)
        state = _int_or_none(card.get("state"))
        column_type = column.get("type") if column else None
        children = _children(card)
        return RawTask(
            key=key,
            project_code=key,
            title=str(card.get("title") or ""),
            description=str(card.get("description") or ""),
            status=(column or {}).get("title") or _STATE_NAMES.get(state or 0),
            subtask_ids=[f"{self._prefix}-{child['id']}" for child in children],
            timestamp=_epoch_ms(card.get("updated")),
            links=[
                {"type": "subtask", "key": f"{self._prefix}-{child['id']}",
                 "title": child["title"]}
                for child in children
            ],
            attachments=_files(card),
            board_id=str(card_id),
            completed=(column_type == _COLUMN_DONE) if column_type else state == _COLUMN_DONE,
            provider_data={
                "card_id": card_id,
                "board_id": board,
                "column_id": column_id,
                "column_title": (column or {}).get("title"),
                "lane_id": _int_or_none(card.get("lane_id")),
                "space_id": _int_or_none(card.get("space_id")),
                "state": state,
                "condition": _int_or_none(card.get("condition")),
                "children": children,
                "checklists": _checklists(card),
                "detailed": "checklists" in card or "files" in card,
            },
        )

    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        """Ленивый обход всех страниц ``GET /cards`` (limit/offset), свежие — первыми."""
        board_id = self._require_board(board)
        self._columns(board_id)  # прогреть кэш колонок один раз до маппинга строк

        def fetch(offset: int, size: int) -> Any:
            params = {
                "board_id": board_id,
                "additional_card_fields": "description",
                "order_by": "updated",
                "order_direction": "desc",
                "limit": size,
                "offset": offset,
            }
            if self._space_id:
                params["space_id"] = self._space_id
            return self._read("GET", "/cards", params=params) or []

        count = 0
        for card in paginate_offset(fetch, page_size=_PAGE):
            if not isinstance(card, Mapping):
                continue
            yield self._raw_from_card(card)
            count += 1
            if limit is not None and count >= limit:
                return

    def fetch_one(self, key: str) -> RawTask | None:
        """Один RawTask по ключу задачи; 404/кривой ключ → None (write-through пропускается)."""
        try:
            card = self._read("GET", f"/cards/{_card_id(key)}")
        except BoardProviderError as error:
            if error.category in {"not_found", "configuration"}:
                return None
            raise
        return self._raw_from_card(card) if isinstance(card, Mapping) else None

    # --- нормализация ---

    def _attachments(self, files: list[dict], warnings: list[str]) -> list[dict]:
        """Вложения карточки: метаданные всегда, текст — только с домена доски и по лимитам.

        Файл может лежать во внешнем хранилище (Google Drive, Яндекс Диск и т.п.), поэтому
        off-host URL не запрашивается вовсе — иначе Authorization доски ушёл бы третьей стороне.
        """
        result: list[dict] = []
        for item in files:
            name = item["name"]
            mime = item.get("mime")
            size = item.get("size")
            meta = {"name": name, "mime_type": mime, "size": size, "content_text": None}
            if not host_allowed(item["url"], self._att_domains):
                warnings.append(f"вложение {name!r}: домен вне доски — только метаданные")
                result.append(meta)
                continue
            if isinstance(size, int) and size > self._att_max_bytes:
                warnings.append(f"вложение {name!r}: размер превышает лимит")
                result.append(meta)
                continue
            if not attachment_supported(name, mime):
                warnings.append(f"вложение {name!r}: формат не поддерживается")
                result.append(meta)
                continue
            fetched = fetch_attachment(
                self._client,
                name=name,
                mime=mime,
                size=size,
                url=item["url"],
                timeout=self._att_timeout,
                max_bytes=self._att_max_bytes,
                store_chars=self._att_store_chars,
            )
            result.append(fetched)
            if fetched["content_text"] is None:
                warnings.append(f"вложение {name!r}: содержимое недоступно")
        return result

    def _brief(self, raw: RawTask, *, with_details: bool,
               warnings: list[str] | None = None) -> dict:
        """RawTask → TaskBrief. ``with_details=False`` — без I/O (criteria/attachments пусты)."""
        result_warnings = list(warnings or [])
        links = [dict(link) for link in raw.links]
        covered = {raw.key, *(link.get("key", "") for link in links)}
        if self._key_pattern:
            for match in re.finditer(self._key_pattern, raw.description or ""):
                code = match.group(0)
                if code not in covered:
                    links.append({"type": "related", "key": code, "title": ""})
                    covered.add(code)
        criteria: list[str] = []
        if with_details:
            criteria = [
                f"[x] {item['text']}" if item["checked"] else f"[ ] {item['text']}"
                for checklist in raw.provider_data.get("checklists") or []
                for item in checklist["items"]
            ]
        card_id = raw.board_id
        return {
            "key": raw.key,
            "aliases": [card_id] if card_id and card_id != raw.key else [],
            "title": raw.title,
            "description": raw.description,
            "criteria": criteria,
            "status": raw.status,
            "url": self._card_url(card_id),
            "links": links,
            "project": project_prefix(raw.key),
            "attachments": (
                self._attachments(raw.attachments, result_warnings) if with_details else []
            ),
            "warnings": result_warnings,
        }

    def normalize(self, raw: RawTask) -> dict:
        """Полный TaskBrief: чеклисты/файлы дочитываются точечным GET, если их не было в listing."""
        source, warnings = raw, []
        if not raw.provider_data.get("detailed"):
            try:
                card = self._read("GET", f"/cards/{raw.provider_data.get('card_id') or 0}")
                if isinstance(card, Mapping):
                    source = self._raw_from_card(card)
            except BoardProviderError as error:
                log.warning("kaiten: детали карточки %s недоступны (%s)", raw.key, error.category)
                warnings.append(f"детали карточки недоступны: {error.category}")
        return self._brief(source, with_details=True, warnings=warnings)

    def normalize_meta(self, raw: RawTask) -> dict:
        """Дешёвый TaskBrief без I/O (PRI-207): плоские поля и ссылки на дочерние карточки."""
        return self._brief(raw, with_details=False)

    # --- запись ---

    def _card_url(self, card_id: str) -> str | None:
        """Ссылка на карточку: шаблон из настроек, иначе короткая форма ``<web base>/<id>``."""
        return self._task_url(card_id) or (f"{self._web_base}/{card_id}" if card_id else None)

    def create(self, doc_md: str, *, title: str, target: str | None,
               project: str | None) -> dict:
        """Создать карточку: ``POST /cards`` с описанием в нативном markdown.

        Колонка ищется по точному id, затем по точному title; не найдена → карточка
        создаётся в дефолтной колонке доски (Kaiten ставит первую), причина — в warnings.
        ``target_resolved`` — фактическая колонка из ответа, а не эхо запроса.
        """
        board = self._require_board(project)
        columns = self._columns(board)
        warnings: list[str] = []
        column: dict | None = None
        if target:
            column, warnings = self._resolve_target(columns, target)
        payload: dict[str, Any] = {
            "title": title,
            "board_id": int(board),
            "description": doc_md,
        }
        if column is not None:
            payload["column_id"] = column["id"]
        created = self._write("POST", "/cards", json=payload) or {}
        card_id = _int_or_none(created.get("id"))
        if card_id is None:
            raise BoardProviderError(
                "unsupported",
                "Kaiten create response did not include a card id.",
                hint="Check the board id and API base URL.",
                secrets=tuple(self.secrets),
            )
        resolved_id = _int_or_none(created.get("column_id"))
        resolved = self._column_by_id(board, resolved_id) or column
        return {
            "key": f"{self._prefix}-{card_id}",
            "url": self._card_url(str(card_id)),
            "board_id": str(card_id),
            "target_resolved": (
                (resolved or {}).get("title") or (str(resolved_id) if resolved_id else None)
            ),
            "warnings": warnings,
        }

    def _pr_block(self, pr_url: str, note: str | None) -> str:
        """Markdown-блок с PR-ссылкой и скрытым маркером идемпотентности."""
        block = f"\n\n{_PR_MARKER}\nPR: {pr_url}"
        return f"{block}\n\n{note.strip()}" if note and note.strip() else block

    def finish(self, key: str, pr_url: str, *, note: str | None = None,
               mark_done: bool = True, target: str | None = None) -> dict:
        """Закрыть карточку: перенос в done-колонку + идемпотентная PR-ссылка в описание.

        Один ``PATCH /cards/{id}`` меняет описание и колонку сразу — правка двигает
        ``updated`` карточки, поэтому следующий инкрементальный синк её переиндексирует.
        Done-цель — колонка: явный ``target`` (id или title) либо первая колонка с
        машинным ``type == 3``; локализованные названия не угадываются.
        """
        card_id = _card_id(key)
        card = self._read("GET", f"/cards/{card_id}") or {}
        description = str(card.get("description") or "")
        board = str(_int_or_none(card.get("board_id")) or "") or self._board_id
        current_column = _int_or_none(card.get("column_id"))

        payload: dict[str, Any] = {}
        warnings: list[str] = []
        link_present = bool(pr_url) and (_PR_MARKER in description or pr_url in description)
        pr_link_added = False
        if pr_url and not link_present:
            payload["description"] = description + self._pr_block(pr_url, note)
            pr_link_added = True

        done_set = False
        already_at_target = False
        if mark_done:
            columns = self._columns(board)
            if target:
                column, target_warnings = self._resolve_target(columns, target)
                warnings.extend(target_warnings)
            else:
                column = self._done_column(columns)
                if column is None:
                    warnings.append(
                        "колонка типа done (type=3) на доске не найдена — "
                        "укажите цель завершения явно"
                    )
                else:
                    warnings.append(
                        f"цель завершения выбрана автоматически по type=3: {column['title']!r}"
                    )
            if column is not None and current_column == column["id"]:
                already_at_target = True
            elif column is not None:
                payload["column_id"] = column["id"]
                done_set = True

        if payload:
            self._write("PATCH", f"/cards/{card_id}", json=payload)
        link_satisfied = not pr_url or link_present or pr_link_added
        done_satisfied = not mark_done or already_at_target or done_set
        return {
            "key": key,
            "board_id": str(card_id),
            "done_set": done_set,
            "pr_link_added": pr_link_added,
            "already_closed": (
                not pr_link_added and not done_set and link_satisfied and done_satisfied
            ),
            "warnings": warnings,
        }
