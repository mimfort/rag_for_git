"""Провайдер доски ClickUp (REST API v2) поверх общего REST-скелета.

Аутентификация — personal API token в заголовке ``Authorization`` **без** префикса
``Bearer`` (OAuth намеренно не поддерживается: он требует loopback-редиректа, а
плагин должен работать в headless CLI на любой ОС).

Листинг — ``GET /list/{list_id}/task`` с 0-индексной страничной пагинацией
(до 100 задач на страницу). Описание задачи ClickUp хранит plain-текстом, поэтому
запрос всегда идёт с ``include_markdown_description=true`` и markdown берётся из
``markdown_description``; при его отсутствии описание отдаётся как есть с warning.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
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
from reviewer.tasks.boards.pagination import paginate_page
from reviewer.tasks.boards.registry import (
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderOptionSpec,
    ProviderSetupSpec,
)
from reviewer.tasks.boards.restbase import RestBoardBase

log = logging.getLogger(__name__)

API_BASE = "https://api.clickup.com/api/v2"
_PAGE = 100  # жёсткий лимит ClickUp на страницу Get Tasks
_APP_TASK_URL = "https://app.clickup.com/t/{task_id}"
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _build_provider(context: ProviderBuildContext) -> ClickUpBoard:
    options = context.options
    return ClickUpBoard(
        token=context.credentials["CLICKUP_API_TOKEN"],
        base_url=str(context.credentials.get("CLICKUP_API_BASE") or API_BASE),
        list_id=str(options.get("list_id") or ""),
        key_prefix=str(options.get("key_prefix") or ""),
        team_id=str(options.get("team_id") or ""),
        key_pattern=context.key_pattern,
        url_template=context.url_template,
        attachment_max_bytes=context.attachment_max_bytes,
        attachment_timeout=context.attachment_timeout,
        attachment_store_chars=context.attachment_store_chars,
    )


def provider_spec() -> BoardProviderSpec:
    """Immutable registry spec ClickUp (регистрацию делает реестр, не адаптер)."""
    return BoardProviderSpec(
        board_type="clickup",
        factory=_build_provider,
        credential_fields=(
            CredentialFieldSpec(
                env="CLICKUP_API_TOKEN",
                label="ClickUp personal API token",
                secret=True,
            ),
            CredentialFieldSpec(
                env="CLICKUP_API_BASE",
                label="ClickUp API base URL",
                secret=False,
                required=False,
                default=API_BASE,
            ),
        ),
        option_fields=(
            ProviderOptionSpec(
                key="list_id",
                label="ID списка ClickUp",
                required_for=("sync", "create", "finish"),
            ),
            ProviderOptionSpec(
                key="key_prefix",
                label="Префикс канонического ключа задачи",
                required_for=("sync",),
            ),
            ProviderOptionSpec(
                key="team_id",
                label="ID workspace (нужен только для custom task ids)",
            ),
        ),
        setup=ProviderSetupSpec(
            label="ClickUp",
            help_url="https://developer.clickup.com/docs/authentication",
            help_text=(
                "Откройте в ClickUp аватар → Settings → Apps и в разделе API Token "
                "нажмите Generate, затем Copy: personal token начинается с pk_ и не "
                "истекает. Он передаётся в заголовке Authorization без префикса Bearer."
            ),
        ),
        default_api_base=API_BASE,
        create_target_label="Статус списка",
        done_target_label="Статус завершения",
    )


def _rate_limit_hint(status: int, headers: Mapping[str, str]) -> float | None:
    """Секунды ожидания для 429 ClickUp: Retry-After либо X-RateLimit-Reset (epoch s)."""
    if status != 429:
        return None
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    reset = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
    try:
        return max(0.0, float(reset) - datetime.now(UTC).timestamp())
    except (TypeError, ValueError):
        return 0.0


def _normalize_api_base(value: str, *, secrets: tuple[str, ...]) -> str:
    """Fail-closed HTTPS-origin API ClickUp до создания authenticated client."""
    raw = (value or "").strip() or API_BASE
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
    except (TypeError, ValueError):
        parsed, hostname = urlsplit(""), None
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
            "ClickUp API base must be an HTTPS URL without credentials.",
            hint="Use https://api.clickup.com/api/v2.",
            secrets=secrets,
        )
    return raw.rstrip("/")


def _epoch_ms(value: object) -> int:
    """ClickUp отдаёт date_updated строкой epoch ms; ISO-строка тоже парсится как UTC."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return int(value)
    if not isinstance(value, str):
        return 0
    text = value.strip()
    if text.isdigit():
        return int(text)
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _to_base36(number: int) -> str:
    """Целое → нативный alphanumeric id ClickUp (обратная операция к int(id, 36))."""
    if number <= 0:
        return "0"
    digits: list[str] = []
    while number:
        number, rest = divmod(number, 36)
        digits.append(_B36[rest])
    return "".join(reversed(digits))


def _from_base36(value: str) -> int | None:
    """Нативный id ClickUp → целое; не-base36 строка → None."""
    text = (value or "").strip().lower()
    if not text or any(char not in _B36 for char in text):
        return None
    return int(text, 36)


def _synth_suffix(native: str) -> str:
    """Стабильный числовой суффикс ключа из нативного id ClickUp.

    Нативный id — base36-строка, поэтому основной путь обратим (``_to_base36``).
    Экзотический id вне base36 деградирует в детерминированный digest-номер.
    """
    number = _from_base36(native)
    if number is not None:
        return str(number)
    digest = hashlib.sha1(native.encode("utf-8"), usedforsecurity=False).hexdigest()
    return str(int(digest[:12], 16))


def _int_or_none(value: object) -> int | None:
    """ClickUp иногда отдаёт size строкой — приводим к int, иначе None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _same_status(left: object, right: object) -> bool:
    """Имена статусов ClickUp сравниваются без учёта регистра и краевых пробелов."""
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _attachments_of(task: Mapping) -> list[dict]:
    """Метаданные вложений из объекта задачи (ClickUp кладёт их инлайн)."""
    result: list[dict] = []
    for item in task.get("attachments") or []:
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url") or "")
        if not url:
            continue
        result.append(
            {
                "name": str(item.get("title") or ""),
                "mime": item.get("mimetype") or item.get("mime_type"),
                "size": _int_or_none(item.get("size")),
                "url": url,
            }
        )
    return result


class ClickUpBoard(RestBoardBase):
    """Адаптер ClickUp API v2 поверх общего безопасного REST-скелета."""

    board_type = "clickup"

    def __init__(
        self,
        *,
        token: str,
        base_url: str = API_BASE,
        list_id: str = "",
        key_prefix: str = "",
        team_id: str = "",
        key_pattern: str = "",
        url_template: str = "",
        updated_since_ms: int = 0,
        attachment_max_bytes: int = 10 * 1024 * 1024,
        attachment_timeout: float = 10.0,
        attachment_store_chars: int = 200000,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        clean_token = (token or "").strip()
        if not clean_token:
            raise BoardProviderError(
                "configuration",
                "ClickUp personal API token is required.",
                hint="Set CLICKUP_API_TOKEN to a token starting with pk_.",
            )
        base = _normalize_api_base(base_url, secrets=(clean_token,))
        self._list_id = str(list_id or "").strip()
        self._key_prefix = str(key_prefix or "").strip()
        self._team_id = str(team_id or "").strip()
        self._updated_since_ms = max(0, int(updated_since_ms or 0))
        self._att_domains = (
            _registrable_domain(urlsplit(base).netloc.split("@")[-1].split(":")[0]),
        )
        self._att_max_bytes = attachment_max_bytes
        self._att_timeout = attachment_timeout
        self._att_store_chars = attachment_store_chars
        super().__init__(
            base_url=base,
            secrets=(clean_token,),
            key_pattern=key_pattern,
            url_template=url_template,
            headers={"Authorization": clean_token, "Accept": "application/json"},
            transport=transport,
            sleeper=sleeper,
            rate_limit_hint=_rate_limit_hint,
        )
        # Вложения лежат на CDN ClickUp по неугадываемому URL: отдельный клиент
        # без заголовка Authorization, чтобы personal token не уходил на другой хост.
        self._att_client = httpx.Client(timeout=attachment_timeout, transport=transport)

    def close(self) -> None:
        """Закрыть оба клиента (общий транспорт закрывается один раз)."""
        try:
            self._att_client.close()
        finally:
            super().close()

    # --- ключи и идентификаторы -------------------------------------------------

    def _key_of(self, task: Mapping) -> str:
        """Канонический ключ: custom_id доски, иначе ``<key_prefix>-<число>``."""
        custom = str(task.get("custom_id") or "").strip()
        if custom:
            return custom
        native = str(task.get("id") or "")
        if not native:
            return ""
        if not self._key_prefix:
            return native
        return f"{self._key_prefix}-{_synth_suffix(native)}"

    def _lookup_candidates(self, key: str) -> list[tuple[str, bool]]:
        """Пары (id для пути, custom_task_ids) в порядке проверки для чтения по ключу."""
        result: list[tuple[str, bool]] = []
        prefix = f"{self._key_prefix}-"
        if self._key_prefix and key.startswith(prefix):
            suffix = key[len(prefix) :]
            if suffix.isdigit():
                result.append((_to_base36(int(suffix)), False))
        if self._team_id:
            result.append((key, True))
        if (key, False) not in result:
            result.append((key, False))
        return result

    def _resolve_list_id(self, project: str | None) -> str:
        """Числовой board-аргумент трактуется как list_id, иначе берётся опция."""
        candidate = str(project or "").strip()
        if candidate.isdigit():
            return candidate
        return self._list_id

    # --- маппинг задачи ---------------------------------------------------------

    def _description_of(self, task: Mapping, warnings: list[str]) -> str:
        """Markdown-описание: markdown_description, иначе plain description + warning."""
        markdown = task.get("markdown_description")
        if isinstance(markdown, str) and markdown.strip():
            return markdown
        plain = task.get("description")
        text = plain if isinstance(plain, str) else ""
        if text.strip():
            warnings.append(
                "ClickUp не вернул markdown_description — описание взято из plain description"
            )
        return text

    def _raw_from_task(self, task: Mapping, *, children: Iterable[Mapping] = ()) -> RawTask:
        """Объект задачи ClickUp → RawTask. Чистая: без I/O.

        ``children`` — подзадачи, найденные по полю ``parent`` в той же странице
        листинга; ответ Get Task с ``include_subtasks=true`` кладёт их в ``subtasks``
        сам, и тогда используется он (общий маппер для iter_raw и fetch_one).
        """
        explicit = task.get("subtasks")
        kids = [
            item
            for item in (explicit if isinstance(explicit, list) else list(children))
            if isinstance(item, Mapping)
        ]
        warnings: list[str] = []
        description = self._description_of(task, warnings)
        status = _mapping(task.get("status"))
        key = self._key_of(task)
        return RawTask(
            key=key,
            project_code=key,
            title=str(task.get("name") or ""),
            description=description,
            status=str(status["status"]) if status.get("status") else None,
            subtask_ids=[str(child.get("id") or "") for child in kids],
            timestamp=_epoch_ms(task.get("date_updated")),
            links=[],
            attachments=_attachments_of(task),
            board_id=str(task.get("id") or ""),
            completed=str(status.get("type") or "").strip().casefold() == "closed",
            provider_data={
                "warnings": warnings,
                "subtasks": [
                    {"key": self._key_of(child), "title": str(child.get("name") or "")}
                    for child in kids
                ],
                "parent": str(task.get("parent") or ""),
                "url": str(task.get("url") or ""),
                "custom_id": str(task.get("custom_id") or ""),
                "status_type": str(status.get("type") or ""),
                "list_id": str(_mapping(task.get("list")).get("id") or ""),
            },
        )

    def _page_rows(self, payload: object) -> list[RawTask]:
        """Страница Get Tasks → RawTask с подзадачами, сгруппированными по ``parent``."""
        tasks = _mapping(payload).get("tasks")
        if not isinstance(tasks, list):
            return []
        rows = [item for item in tasks if isinstance(item, Mapping)]
        children: dict[str, list[Mapping]] = {}
        for task in rows:
            parent = str(task.get("parent") or "")
            if parent:
                children.setdefault(parent, []).append(task)
        return [
            self._raw_from_task(task, children=children.get(str(task.get("id") or ""), ()))
            for task in rows
        ]

    # --- чтение -----------------------------------------------------------------

    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        list_id = self._resolve_list_id(board)
        if not list_id:
            raise BoardProviderError(
                "configuration",
                "ClickUp list_id is required to enumerate tasks.",
                hint="Set the list_id provider option in .review.yml.",
                secrets=self.secrets,
            )
        path = f"/list/{quote(list_id, safe='')}/task"
        finished = {"last": False}

        def fetch(number: int, _size: int) -> object:
            # last_page на предыдущей странице → следующего запроса не делаем вовсе,
            # а пустая «короткая» страница штатно останавливает paginate_page.
            if finished["last"]:
                return {"tasks": []}
            params: dict[str, object] = {
                "page": number,
                "subtasks": "true",
                "include_closed": "true",
                "include_markdown_description": "true",
                "order_by": "updated",
            }
            if self._updated_since_ms > 0:
                params["date_updated_gt"] = self._updated_since_ms
            payload = self._read("GET", path, params=params) or {}
            finished["last"] = bool(_mapping(payload).get("last_page"))
            return payload

        count = 0
        for raw in paginate_page(fetch, page_size=_PAGE, items=self._page_rows, start=0):
            yield raw
            count += 1
            if limit is not None and count >= limit:
                return

    def _fetch_task_json(self, key: str) -> Mapping | None:
        """Объект задачи по каноническому ключу; ни один кандидат не подошёл → None."""
        for task_id, custom in self._lookup_candidates(key):
            params: dict[str, object] = {
                "include_subtasks": "true",
                "include_markdown_description": "true",
            }
            if custom:
                params["custom_task_ids"] = "true"
                params["team_id"] = self._team_id
            try:
                payload = self._read(
                    "GET",
                    f"/task/{quote(task_id, safe='')}",
                    params=params,
                )
            except BoardProviderError as error:
                if error.category in {"not_found", "unsupported"}:
                    continue
                raise
            task = _mapping(payload)
            if task.get("id") and self._key_of(task) == key:
                return task
        return None

    def fetch_one(self, key: str) -> RawTask | None:
        try:
            task = self._fetch_task_json(key)
        except BoardProviderError as error:
            log.warning("clickup: fetch_one не удался (%s)", error.category)
            return None
        return self._raw_from_task(task) if task is not None else None

    # --- нормализация -----------------------------------------------------------

    def _brief_url(self, raw: RawTask) -> str:
        """Ссылка на задачу: собственный url ClickUp → шаблон настроек → app-URL."""
        native_url = str(raw.provider_data.get("url") or "")
        if native_url:
            return native_url
        templated = self._task_url(raw.key)
        if templated:
            return templated
        return _APP_TASK_URL.format(task_id=raw.board_id) if raw.board_id else ""

    def _fetch_attachments(self, raw: RawTask, warnings: list[str]) -> list[dict]:
        """Скачать вложения задачи с лимитами; недоступный текст → метаданные + warning."""
        result: list[dict] = []
        for attachment in raw.attachments:
            name = attachment["name"]
            url = attachment["url"]
            size = attachment.get("size")
            mime = attachment.get("mime")
            if not host_allowed(url, self._att_domains):
                warnings.append(f"вложение {name!r}: домен не разрешён")
                continue
            if isinstance(size, int) and size > self._att_max_bytes:
                warnings.append(f"вложение {name!r}: размер превышает лимит")
                continue
            if not attachment_supported(name, mime):
                warnings.append(f"вложение {name!r}: формат не поддерживается")
                continue
            fetched = fetch_attachment(
                self._att_client,
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
                warnings.append(
                    f"вложение {name!r}: содержимое недоступно "
                    "(возможно, включены Private Attachment Links)"
                )
        return result

    def _brief(self, raw: RawTask, *, with_details: bool) -> dict:
        provider_data = raw.provider_data
        warnings = list(provider_data.get("warnings") or [])
        subtasks = provider_data.get("subtasks") or []
        links: list[dict] = [
            {"type": "subtask", "key": item["key"], "title": item.get("title") or ""}
            for item in subtasks
            if item.get("key")
        ]
        links.extend(raw.links)
        covered = {raw.key, *(item["key"] for item in links)}
        if self._key_pattern:
            for match in re.finditer(self._key_pattern, raw.description or ""):
                code = match.group(0)
                if code not in covered:
                    links.append({"type": "related", "key": code, "title": ""})
                    covered.add(code)
        aliases = [
            value
            for value in (raw.board_id, str(provider_data.get("custom_id") or ""))
            if value and value != raw.key
        ]
        return {
            "key": raw.key,
            "aliases": aliases,
            "title": raw.title,
            "description": raw.description,
            "criteria": (
                [item.get("title") or item["key"] for item in subtasks] if with_details else []
            ),
            "status": raw.status,
            "url": self._brief_url(raw) or None,
            "links": links,
            "project": project_prefix(raw.key),
            "attachments": self._fetch_attachments(raw, warnings) if with_details else [],
            "warnings": warnings,
        }

    def normalize(self, raw: RawTask) -> dict:
        return self._brief(raw, with_details=True)

    def normalize_meta(self, raw: RawTask) -> dict:
        """Дешёвый TaskBrief без I/O: подзадачи и вложения не резолвятся (PRI-207)."""
        return self._brief(raw, with_details=False)

    # --- discovery целей --------------------------------------------------------

    def _list_statuses(self, list_id: str) -> list[Mapping]:
        """Статус-объекты списка ClickUp (роль целей создания и завершения)."""
        payload = _mapping(self._read("GET", f"/list/{quote(list_id, safe='')}"))
        statuses = payload.get("statuses")
        if not isinstance(statuses, list):
            return []
        return [item for item in statuses if isinstance(item, Mapping) and item.get("status")]

    def _status_names(self, list_id: str) -> list[str]:
        return [str(item["status"]) for item in self._list_statuses(list_id)]

    def list_targets(self, project: str | None) -> dict:
        """Статусы списка в общей discovery-модели: id == label == имя статуса."""
        warnings: list[str] = []
        statuses: list[Mapping] = []
        list_id = self._resolve_list_id(project)
        if not list_id:
            warnings.append("ClickUp list_id не задан — цели статусов недоступны")
        else:
            statuses = self._list_statuses(list_id)
            if not statuses:
                warnings.append("список ClickUp не вернул statuses — цели недоступны")
        return {
            "targets": [
                {
                    "id": str(item["status"]),
                    "label": str(item["status"]),
                    "purposes": ["create", "done"],
                    "type": str(item.get("type") or ""),
                }
                for item in statuses
            ],
            "options": [
                {
                    "key": option.key,
                    "label": option.label,
                    "required_for": list(option.required_for),
                    "choices": [],
                }
                for option in provider_spec().option_fields
            ],
            "warnings": warnings,
        }

    def validate_connection(self, project: str | None = None) -> dict:
        """Проверить токен и доступность списка, не тратя write-квоту."""
        payload = _mapping(self._read("GET", "/user"))
        user = _mapping(payload.get("user")) or payload
        warnings: list[str] = []
        capabilities = {"read": True, "create": False, "finish": False}
        list_id = self._resolve_list_id(project)
        if not list_id:
            warnings.append("ClickUp list_id не задан — create и finish недоступны")
        else:
            statuses = self._status_names(list_id)
            capabilities["create"] = bool(statuses)
            capabilities["finish"] = bool(statuses)
            if not statuses:
                warnings.append("список ClickUp не вернул statuses — цели недоступны")
        if not self._key_prefix:
            warnings.append("key_prefix не задан — ключами задач станут нативные id ClickUp")
        return {
            "status": "ok",
            "identity": {"id": user.get("id"), "username": user.get("username")},
            "project": project,
            "capabilities": capabilities,
            "warnings": warnings,
        }

    # --- запись -----------------------------------------------------------------

    def _resolve_status(self, list_id: str, target: str) -> tuple[str | None, list[str]]:
        """Точное имя статуса списка по запрошенному target; не найден → (None, warning)."""
        try:
            names = self._status_names(list_id)
        except BoardProviderError as error:
            return None, [f"статусы списка ClickUp недоступны: {error.category}"]
        exact = next((name for name in names if name == target), None)
        if exact is not None:
            return exact, []
        insensitive = [name for name in names if _same_status(name, target)]
        if len(insensitive) == 1:
            return insensitive[0], []
        if len(insensitive) > 1:
            return None, [f"цель ClickUp {target!r} неоднозначна — статус не применён"]
        return None, [f"цель ClickUp {target!r} отсутствует в списке — применён статус по умолчанию"]

    def create(
        self,
        doc_md: str,
        *,
        title: str,
        target: str | None,
        project: str | None,
    ) -> dict:
        """Создать задачу из канонического markdown (ClickUp принимает markdown_content)."""
        list_id = self._resolve_list_id(project)
        if not list_id:
            raise BoardProviderError(
                "configuration",
                "ClickUp list_id is required to create a task.",
                hint="Set the list_id provider option in .review.yml.",
                secrets=self.secrets,
            )
        if not (title or "").strip():
            raise BoardProviderError(
                "configuration",
                "ClickUp task name is required to create a task.",
                secrets=self.secrets,
            )
        warnings: list[str] = []
        body: dict[str, object] = {"name": title, "markdown_content": doc_md}
        if target:
            resolved, target_warnings = self._resolve_status(list_id, target)
            warnings.extend(target_warnings)
            if resolved:
                body["status"] = resolved
        created = _mapping(
            self._write("POST", f"/list/{quote(list_id, safe='')}/task", json=body)
        )
        native = str(created.get("id") or "")
        if not native:
            raise BoardProviderError(
                "unsupported",
                "ClickUp create response did not include a task id.",
                secrets=self.secrets,
            )
        status = _mapping(created.get("status"))
        target_resolved = str(status.get("status") or "") or str(body.get("status") or "") or None
        return {
            "key": self._key_of(created),
            "url": str(created.get("url") or "") or _APP_TASK_URL.format(task_id=native),
            "board_id": native,
            "target_resolved": target_resolved,
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
        """Дописать PR-ссылку в описание и перевести задачу в done-цель (независимо).

        Идемпотентность — по самой ссылке: URL уже в markdown-описании → описание не
        трогаем; статус уже равен target → статус не трогаем. Обе правки двигают
        ``date_updated``, поэтому инкрементальный синк подхватит задачу.
        """
        task = self._fetch_task_json(key)
        if task is None:
            raise BoardProviderError(
                "not_found",
                "ClickUp task was not found.",
                hint="Check the task key, key_prefix and list configuration.",
                secrets=self.secrets,
            )
        native = str(task.get("id") or "")
        path = f"/task/{quote(native, safe='')}"
        warnings: list[str] = []
        description = self._description_of(task, warnings)
        link_present = bool(pr_url) and pr_url in description
        pr_link_added = False
        if pr_url and not link_present:
            block = f"\n\nPR: {pr_url}" + (f"\n\n{note}" if note else "")
            updated = f"{description}{block}" if description else block.lstrip("\n")
            try:
                self._write("PUT", path, json={"markdown_content": updated})
                pr_link_added = True
            except BoardProviderError as error:
                warnings.append(f"не удалось дописать PR-ссылку в описание: {error.category}")

        current = str(_mapping(task.get("status")).get("status") or "")
        already_at_target = bool(target) and _same_status(current, target)
        done_set = False
        if mark_done and not already_at_target:
            if not target:
                warnings.append(
                    "ClickUp done target не задан — статус не угадывается и не меняется"
                )
            else:
                try:
                    self._write("PUT", path, json={"status": target})
                    done_set = True
                except BoardProviderError as error:
                    warnings.append(
                        f"не удалось выставить статус {target!r}: {error.category}"
                    )

        link_satisfied = not pr_url or link_present or pr_link_added
        done_satisfied = not mark_done or already_at_target or done_set
        return {
            "key": key,
            "board_id": native,
            "done_set": done_set,
            "pr_link_added": pr_link_added,
            "already_closed": (
                not pr_link_added and not done_set and link_satisfied and done_satisfied
            ),
            "warnings": warnings,
        }
