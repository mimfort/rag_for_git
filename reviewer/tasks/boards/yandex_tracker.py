"""Провайдер доски Yandex Tracker (REST API v3) поверх общего ``RestBoardBase``.

Факты API сверены с официальной документацией (см. отчёт задачи PRI-217):

- base URL — ``https://api.tracker.yandex.net``, актуальная версия пути — ``/v3``
  (issues, links, attachments, transitions, statuses, myself);
- авторизация — ``Authorization: OAuth <OAuth-токен>`` либо ``Authorization: Bearer <IAM-токен>``
  (IAM — только для организаций Yandex Cloud), плюс **обязательный** заголовок организации:
  ``X-Org-ID`` (Яндекс 360 для бизнеса) или ``X-Cloud-Org-ID`` (Yandex Cloud Organization);
- листинг — ``POST /v3/issues/_search`` с телом-фильтром и постраничным выводом
  ``page``/``perPage``; ``expand=attachments`` отдаёт вложения сразу в выдаче.
  Ограничение: постраничный режим документирован для выдачи **до 10000 задач**, для
  больших очередей нужен scroll-курсор (``scrollType``/``X-Scroll-Id``) — он здесь
  не реализован, потому что синк перечисляет очередь целиком (purge требует полного
  списка ключей), и очередь на 10000+ задач упрётся в лимит API;
- описание — YFM (Yandex Flavored Markdown), поэтому нормализуется через ``yfm.yfm_to_md``,
  а на запись передаётся с ``markupType: "md"``;
- закрытие задачи — **переход** (``GET/POST /v3/issues/{key}/transitions[/{id}/_execute]``),
  дискавери целей — ``GET /v3/statuses`` (queue-независимый список статусов организации).

Ключ задачи нативный (``QUEUE-123``), поэтому ``key_prefix`` не нужен: ``project_prefix``
достаёт префикс очереди прямо из ключа.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from reviewer.tasks.boards.attachments import _registrable_domain, fetch_attachment, host_allowed
from reviewer.tasks.boards.base import RawTask, TaskListing, TaskListingStats, project_prefix
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
from reviewer.tasks.boards.yfm import yfm_to_md

log = logging.getLogger(__name__)

_PAGE = 100
_API_BASE = "https://api.tracker.yandex.net/v3"
_WEB_BASE = "https://tracker.yandex.ru"
_HELP_URL = "https://yandex.ru/support/tracker/ru/api-ref/access"

# Схемы Authorization: OAuth-токен Яндекс ID либо IAM-токен Yandex Cloud (Bearer).
_AUTH_SCHEMES = {"oauth": "OAuth", "bearer": "Bearer", "iam": "Bearer"}

# Смещение времени Трекера приходит без двоеточия («+0000») — нормализуем под fromisoformat.
_OFFSET_RE = re.compile(r"([+-]\d{2})(\d{2})$")


def _build_provider(context: ProviderBuildContext) -> YandexTrackerBoard:
    """Собрать провайдера из registry-контекста (креды из env + immutable options)."""
    return YandexTrackerBoard(
        token=context.credentials["YANDEX_TRACKER_TOKEN"],
        api_base=context.credentials.get("YANDEX_TRACKER_API_BASE") or _API_BASE,
        org_id=context.credentials.get("YANDEX_TRACKER_ORG_ID", ""),
        cloud_org_id=context.credentials.get("YANDEX_TRACKER_CLOUD_ORG_ID", ""),
        auth_scheme=context.credentials.get("YANDEX_TRACKER_AUTH_SCHEME") or "OAuth",
        queue=str(context.options.get("queue") or ""),
        key_pattern=context.key_pattern,
        url_template=context.url_template,
        attachment_max_bytes=context.attachment_max_bytes,
        attachment_timeout=context.attachment_timeout,
        attachment_store_chars=context.attachment_store_chars,
    )


def provider_spec() -> BoardProviderSpec:
    """Immutable registry spec Yandex Tracker.

    Идентификатор организации объявлен **двумя** non-secret полями: ровно одно из них
    обязательно, и выбор поля задаёт заголовок (``X-Org-ID`` против ``X-Cloud-Org-ID``).
    Одного поля не хватило бы: по значению ID тип организации не определить, а неверный
    заголовок Трекер отвергает как ошибку авторизации.
    """
    return BoardProviderSpec(
        board_type="yandex_tracker",
        factory=_build_provider,
        credential_fields=(
            CredentialFieldSpec(
                env="YANDEX_TRACKER_TOKEN",
                label="Yandex Tracker OAuth или IAM token",
                secret=True,
            ),
            CredentialFieldSpec(
                env="YANDEX_TRACKER_API_BASE",
                label="Yandex Tracker API base URL",
                required=False,
                default=_API_BASE,
            ),
            CredentialFieldSpec(
                env="YANDEX_TRACKER_ORG_ID",
                label="ID организации Яндекс 360 (заголовок X-Org-ID)",
                required=False,
            ),
            CredentialFieldSpec(
                env="YANDEX_TRACKER_CLOUD_ORG_ID",
                label="ID организации Yandex Cloud (заголовок X-Cloud-Org-ID)",
                required=False,
            ),
            CredentialFieldSpec(
                env="YANDEX_TRACKER_AUTH_SCHEME",
                label="Схема Authorization: OAuth или Bearer (IAM)",
                required=False,
                default="OAuth",
            ),
        ),
        option_fields=(
            ProviderOptionSpec(
                key="queue",
                label="Очередь (ключ, например TREK)",
                required_for=("sync", "create", "finish"),
            ),
        ),
        setup=ProviderSetupSpec(
            label="Yandex Tracker",
            help_url=_HELP_URL,
            help_text=(
                "Создайте на https://oauth.yandex.ru приложение «Для доступа к API», "
                "выдайте права tracker:read/tracker:write и получите OAuth-токен по ссылке "
                "https://oauth.yandex.ru/authorize?response_type=token&client_id=<id>. "
                "ID организации — Администрирование → Организации; для Яндекс 360 задайте "
                "YANDEX_TRACKER_ORG_ID, для Yandex Cloud — YANDEX_TRACKER_CLOUD_ORG_ID "
                "(там же можно вместо OAuth использовать IAM-токен со схемой Bearer)."
            ),
        ),
        default_api_base=_API_BASE,
        create_target_label="Статус создания",
        done_target_label="Статус завершения",
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
            "Yandex Tracker API base must be an HTTPS URL without credentials or query.",
            hint="Use https://api.tracker.yandex.net/v3.",
            secrets=secrets,
        )
    return f"https://{hostname}{parsed.path.rstrip('/')}"


def _auth_header(scheme: str, token: str, *, secrets: tuple[str, ...]) -> str:
    """``Authorization`` по схеме: OAuth-токен Яндекс ID либо IAM-токен (Bearer)."""
    resolved = _AUTH_SCHEMES.get((scheme or "").strip().lower())
    if not resolved:
        raise BoardProviderError(
            "configuration",
            "Yandex Tracker auth scheme must be OAuth or Bearer.",
            hint="Set YANDEX_TRACKER_AUTH_SCHEME to OAuth (Yandex ID) or Bearer (Cloud IAM).",
            secrets=secrets,
        )
    return f"{resolved} {token}"


def _org_header(org_id: str, cloud_org_id: str, *, secrets: tuple[str, ...]) -> dict[str, str]:
    """Ровно один заголовок организации: X-Org-ID (Яндекс 360) или X-Cloud-Org-ID (Cloud)."""
    org = (org_id or "").strip()
    cloud = (cloud_org_id or "").strip()
    if bool(org) == bool(cloud):
        raise BoardProviderError(
            "configuration",
            "Yandex Tracker needs exactly one organization id header.",
            hint=(
                "Set YANDEX_TRACKER_ORG_ID for a Yandex 360 organization or "
                "YANDEX_TRACKER_CLOUD_ORG_ID for a Yandex Cloud organization, not both."
            ),
            secrets=secrets,
        )
    return {"X-Org-ID": org} if org else {"X-Cloud-Org-ID": cloud}


def _timestamp(value: object) -> int | None:
    """``updatedAt`` Трекера → epoch ms; неразбираемое значение остаётся неизвестным."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    match = _OFFSET_RE.search(text)
    if match:
        text = f"{text[: match.start()]}{match.group(1)}:{match.group(2)}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _ref(value: object) -> dict[str, str]:
    """Ссылочный объект Трекера (status/queue) → плоский {id, key, display}."""
    payload = value if isinstance(value, Mapping) else {}
    return {
        "id": str(payload.get("id") or ""),
        "key": str(payload.get("key") or ""),
        "display": str(payload.get("display") or payload.get("name") or ""),
    }


def _attachments_of(issue: Mapping[str, Any]) -> list[dict]:
    """Метаданные вложений из ``expand=attachments``; запись без ``content`` пропускается."""
    result: list[dict] = []
    for item in issue.get("attachments") or []:
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("content") or "")
        if not url:
            continue
        result.append(
            {
                "name": str(item.get("name") or ""),
                "mime": item.get("mimetype"),
                "size": item.get("size"),
                "url": url,
            }
        )
    return result


def _issue_to_raw(issue: Mapping[str, Any]) -> RawTask:
    """JSON задачи Трекера → RawTask. Чистая: без I/O. Описание конвертируется YFM → markdown."""
    key = str(issue.get("key") or "")
    status = _ref(issue.get("status"))
    queue = _ref(issue.get("queue"))
    return RawTask(
        key=key,
        project_code=key,
        title=str(issue.get("summary") or ""),
        description=yfm_to_md(str(issue.get("description") or "")),
        status=status["display"] or status["key"] or None,
        subtask_ids=[],
        timestamp=_timestamp(issue.get("updatedAt")),
        links=[],
        attachments=_attachments_of(issue),
        board_id=str(issue.get("id") or key),
        archived=None,
        terminal=None,
        provider_data={"queue": queue, "status": status},
    )


class YandexTrackerBoard(RestBoardBase):
    """Адаптер Yandex Tracker: чтение через ``_search``, закрытие через переходы."""

    board_type = "yandex_tracker"

    def __init__(
        self,
        *,
        token: str,
        api_base: str = _API_BASE,
        org_id: str = "",
        cloud_org_id: str = "",
        auth_scheme: str = "OAuth",
        queue: str = "",
        key_pattern: str = "",
        url_template: str = "",
        attachment_max_bytes: int = 10 * 1024 * 1024,
        attachment_timeout: float = 10.0,
        attachment_store_chars: int = 200000,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        secrets = (token,)
        base = _normalize_api_base(api_base, secrets=secrets)
        headers = {
            "Authorization": _auth_header(auth_scheme, token, secrets=secrets),
            "Accept": "application/json",
            **_org_header(org_id, cloud_org_id, secrets=secrets),
        }
        super().__init__(
            base_url=base,
            secrets=secrets,
            key_pattern=key_pattern,
            url_template=url_template,
            headers=headers,
            transport=transport,
            sleeper=sleeper,
        )
        self._queue = (queue or "").strip()
        host = urlsplit(base).netloc.split("@")[-1].split(":")[0]
        self._att_domains = (_registrable_domain(host),)
        self._att_max_bytes = attachment_max_bytes
        self._att_timeout = attachment_timeout
        self._att_store_chars = attachment_store_chars

    # --- очередь и ссылки ---

    def _queue_for(self, board: str | None) -> str:
        """Очередь запроса: явный project/board приоритетнее option ``queue``."""
        return (board or "").strip() or self._queue

    def _web_url(self, key: str) -> str | None:
        """Ссылка на задачу: шаблон из настроек, иначе веб-интерфейс Трекера."""
        if not key:
            return None
        return self._task_url(key) or f"{_WEB_BASE}/{key}"

    # --- проверка окружения ---

    def validate_connection(self, project: str | None = None) -> dict:
        """Проверить токен и заголовок организации (``/myself``) и доступность очереди."""
        identity = self._read("GET", "/myself") or {}
        queue = self._queue_for(project)
        warnings: list[str] = []
        if queue:
            self._read("GET", f"/queues/{quote(queue, safe='')}")
        else:
            warnings.append(
                "очередь Yandex Tracker не проверена — права на создание неизвестны"
            )
        return {
            "status": "ok",
            "identity": {
                "id": str(identity.get("uid") or identity.get("id") or ""),
                "login": identity.get("login"),
                "display": identity.get("display"),
            },
            "project": queue or None,
            "capabilities": {"read": True, "create": bool(queue), "transition": True},
            "warnings": warnings,
        }

    # --- чтение ---

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
        """Все задачи очереди страницами ``page``/``perPage``, отсортированные по updatedAt."""
        queue = self._queue_for(board)
        if not queue:
            raise BoardProviderError(
                "configuration",
                "Yandex Tracker queue is required to list issues.",
                hint="Set task_board.project or the queue provider option.",
                secrets=tuple(self.secrets),
            )

        def fetch(page: int, per_page: int) -> Any:
            return self._read(
                "POST",
                "/issues/_search",
                params={"expand": "attachments", "page": page, "perPage": per_page},
                json={"filter": {"queue": queue}, "order": "+updatedAt"},
            ) or []

        count = 0
        if limit is not None and count >= limit:
            return
        for issue in paginate_page(fetch, page_size=_PAGE):
            if not isinstance(issue, Mapping):
                continue
            yield _issue_to_raw(issue)
            count += 1
            if limit is not None and count >= limit:
                return

    def fetch_one(self, key: str) -> RawTask | None:
        """Одна задача по ключу тем же маппером; сбой/404 → None (fail-soft write-through)."""
        try:
            issue = self._read(
                "GET",
                f"/issues/{quote(key, safe='')}",
                params={"expand": "attachments"},
            )
        except BoardProviderError as error:
            log.warning("yandex_tracker: fetch_one(%s) не удался (%s)", key, error.category)
            return None
        if not isinstance(issue, Mapping) or not issue.get("key"):
            return None
        return _issue_to_raw(issue)

    # --- нормализация ---

    def _links_of(self, key: str) -> tuple[list[dict], list[str]]:
        """Связи задачи: ``direction=outward`` + тип ``subtask`` → подзадача, ``inward`` → родитель.

        Fail-soft: недоступные связи не ломают нормализацию, а уходят в warnings.
        """
        try:
            payload = self._read("GET", f"/issues/{quote(key, safe='')}/links") or []
        except BoardProviderError as error:
            return [], [f"связи задачи недоступны: {error.category}"]
        result: list[dict] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            linked = item.get("object") if isinstance(item.get("object"), Mapping) else {}
            linked_key = str(linked.get("key") or "")
            if not linked_key:
                continue
            relation = _ref(item.get("type"))["id"].lower()
            outward = str(item.get("direction") or "").lower() == "outward"
            if relation == "subtask":
                kind = "subtask" if outward else "parent"
            else:
                kind = "related"
            result.append(
                {"type": kind, "key": linked_key, "title": str(linked.get("display") or "")}
            )
        return result, []

    def _attachment_briefs(self, raw: RawTask) -> tuple[list[dict], list[str]]:
        """Скачать вложения тем же авторизованным клиентом; чужой хост — пропуск с warning."""
        briefs: list[dict] = []
        warnings: list[str] = []
        for attachment in raw.attachments:
            name = str(attachment.get("name") or "")
            url = str(attachment.get("url") or "")
            if not host_allowed(url, self._att_domains):
                warnings.append(f"вложение {name!r}: origin не разрешён")
                continue
            size = attachment.get("size")
            if isinstance(size, int) and size > self._att_max_bytes:
                warnings.append(f"вложение {name!r}: размер превышает лимит")
                continue
            result = fetch_attachment(
                self._client,
                name=name,
                mime=attachment.get("mime"),
                size=size,
                url=url,
                timeout=self._att_timeout,
                max_bytes=self._att_max_bytes,
                store_chars=self._att_store_chars,
            )
            briefs.append(result)
            if result["content_text"] is None:
                warnings.append(f"вложение {name!r}: содержимое недоступно")
        return briefs, warnings

    def _brief(self, raw: RawTask, *, with_details: bool) -> dict:
        """RawTask → TaskBrief. ``with_details=False`` — ноль HTTP (self-healing meta-refresh)."""
        warnings: list[str] = []
        links: list[dict] = list(raw.links)
        criteria: list[str] = []
        attachments: list[dict] = []
        if with_details:
            fetched, link_warnings = self._links_of(raw.key)
            links.extend(fetched)
            warnings.extend(link_warnings)
            criteria = [
                link["title"] or link["key"] for link in fetched if link["type"] == "subtask"
            ]
            attachments, attachment_warnings = self._attachment_briefs(raw)
            warnings.extend(attachment_warnings)
        covered = {raw.key, *(link["key"] for link in links)}
        if self._key_pattern:
            for match in re.finditer(self._key_pattern, raw.description or ""):
                code = match.group(0)
                if code in covered:
                    continue
                covered.add(code)
                links.append({"type": "related", "key": code, "title": ""})
        return {
            "key": raw.key,
            "aliases": [],
            "title": raw.title,
            "description": raw.description,
            "criteria": criteria,
            "status": raw.status,
            "url": self._web_url(raw.key),
            "links": links,
            "project": project_prefix(raw.key),
            "attachments": attachments,
            "warnings": warnings,
        }

    def normalize(self, raw: RawTask) -> dict:
        return self._brief(raw, with_details=True)

    def normalize_meta(self, raw: RawTask) -> dict:
        return self._brief(raw, with_details=False)

    # --- discovery целей ---

    def _queue_choices(self) -> tuple[list[dict], list[str]]:
        """Очереди для подсказки option ``queue``; недоступны — fail-soft с warning."""
        try:
            payload = self._read("GET", "/queues") or []
        except BoardProviderError as error:
            return [], [f"список очередей недоступен: {error.category}"]
        choices: list[dict] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("key") or "")
            if key:
                choices.append({"id": key, "label": str(item.get("name") or key)})
        return choices, []

    def list_targets(self, project: str | None) -> dict:
        """Статусы организации как create/done цели + выбор очереди в options.

        Queue-специфичного списка статусов в API Трекера нет: workflow отдаётся только
        как связка «тип задачи → workflow», без статусов. Поэтому цели — статусы
        организации (``GET /v3/statuses``), а фактическая применимость статуса к задаче
        проверяется при закрытии по списку доступных переходов.
        """
        statuses = self._read("GET", "/statuses") or []
        targets = [
            {
                "id": str(status["key"]),
                "label": str(status.get("name") or status["key"]),
                "purposes": ["create", "done"],
            }
            for status in statuses
            if isinstance(status, Mapping) and status.get("key")
        ]
        choices, warnings = self._queue_choices()
        if not self._queue_for(project):
            warnings.append("очередь не выбрана — задайте task_board.project или option queue")
        return {
            "targets": targets,
            "options": [
                {
                    "key": "queue",
                    "label": "Очередь (ключ, например TREK)",
                    "required_for": ["sync", "create", "finish"],
                    "choices": choices,
                }
            ],
            "warnings": warnings,
        }

    # --- переходы и запись ---

    def _apply_transition(self, key: str, target: str) -> tuple[str | None, list[str]]:
        """Перевести задачу в статус ``target`` доступным переходом (структурный REST).

        Цель матчится по ``to.key``, ``to.id`` и ``to.display``, поэтому работают и
        ключ статуса из ``list_targets``, и его человекочитаемое имя. Возвращает
        (канонический ключ применённого статуса | None, warnings) — fail-soft.
        """
        safe_key = quote(key, safe="")
        try:
            transitions = self._read("GET", f"/issues/{safe_key}/transitions") or []
        except BoardProviderError as error:
            return None, [f"переходы задачи недоступны: {error.category}"]
        match: Mapping[str, Any] | None = None
        for item in transitions:
            if not isinstance(item, Mapping):
                continue
            status = _ref(item.get("to"))
            if target in {status["key"], status["id"], status["display"]}:
                match = item
                break
        if match is None:
            return None, [
                f"цель {target!r} недоступна среди переходов задачи — статус не изменён"
            ]
        transition_id = str(match.get("id") or "")
        try:
            self._write(
                "POST",
                f"/issues/{safe_key}/transitions/{quote(transition_id, safe='')}/_execute",
                json={},
            )
        except BoardProviderError as error:
            return None, [f"переход в {target!r} не выполнен: {error.category}"]
        status = _ref(match.get("to"))
        return status["key"] or status["id"] or target, []

    def create(
        self,
        doc_md: str,
        *,
        title: str,
        target: str | None,
        project: str | None,
    ) -> dict:
        """Создать задачу в очереди и, если запрошена цель, сразу перевести её переходом.

        Описание уходит как есть с ``markupType: "md"`` — канонический markdown задачи
        совпадает с YFM в достаточной части, а Трекер иначе трактует разметку.
        """
        queue = self._queue_for(project)
        if not queue:
            raise BoardProviderError(
                "configuration",
                "Yandex Tracker queue is required to create an issue.",
                hint="Set task_board.project or the queue provider option.",
                secrets=tuple(self.secrets),
            )
        created = self._write(
            "POST",
            "/issues",
            json={
                "queue": {"key": queue},
                "summary": title,
                "description": doc_md,
                "markupType": "md",
            },
        ) or {}
        key = str(created.get("key") or "")
        if not key:
            raise BoardProviderError(
                "unsupported",
                "Yandex Tracker create response did not include an issue key.",
                hint="Check the queue permissions and required queue fields.",
                secrets=tuple(self.secrets),
            )
        status = _ref(created.get("status"))
        target_resolved: str | None = status["key"] or status["display"] or None
        warnings: list[str] = []
        if target:
            resolved, transition_warnings = self._apply_transition(key, target)
            warnings.extend(transition_warnings)
            if resolved:
                target_resolved = resolved
        return {
            "key": key,
            "url": self._web_url(key),
            "board_id": str(created.get("id") or key),
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
        """Идемпотентно дописать PR-ссылку в описание и перевести задачу в done-цель.

        Идемпотентность PR-ссылки — по наличию самого URL в описании (скрытый
        html-маркер в YFM отображался бы как текст). Правка описания и переход
        независимы: сбой одного не отменяет другой, оба fail-soft через warnings.
        Цель закрытия не угадывается: без ``target`` статус не меняется.
        """
        safe_key = quote(key, safe="")
        issue = self._read("GET", f"/issues/{safe_key}") or {}
        description = str(issue.get("description") or "")
        status = _ref(issue.get("status"))

        link_present = bool(pr_url and pr_url in description)
        pr_link_added = False
        warnings: list[str] = []
        if pr_url and not link_present:
            block = f"PR: {pr_url}" + (f"\n\n{note}" if note else "")
            updated = f"{description}\n\n{block}" if description else block
            try:
                self._write(
                    "PATCH",
                    f"/issues/{safe_key}",
                    json={"description": updated, "markupType": "md"},
                )
                pr_link_added = True
            except BoardProviderError as error:
                warnings.append(f"описание задачи не обновлено: {error.category}")

        already_at_target = bool(
            target and target in {status["key"], status["id"], status["display"]}
        )
        done_set = False
        if mark_done and not already_at_target:
            if not target:
                warnings.append(
                    "цель закрытия не задана — статус Yandex Tracker не угадывается"
                )
            else:
                resolved, transition_warnings = self._apply_transition(key, target)
                done_set = resolved is not None
                warnings.extend(transition_warnings)

        link_satisfied = not pr_url or link_present or pr_link_added
        done_satisfied = not mark_done or already_at_target or done_set
        return {
            "key": key,
            "board_id": str(issue.get("id") or key),
            "done_set": done_set,
            "pr_link_added": pr_link_added,
            "already_closed": (
                not pr_link_added and not done_set and link_satisfied and done_satisfied
            ),
            "warnings": warnings,
        }
