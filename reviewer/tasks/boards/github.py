"""Провайдер доски GitHub Issues (REST API, версия заголовком X-GitHub-Api-Version).

Поддерживаются GitHub Cloud (``https://api.github.com``) и GitHub Enterprise Server
(``https://<host>/api/v3``) — оба через один credential ``GITHUB_ISSUES_API_BASE``.
Транспорт — общий ``RestBoardBase``: свои httpx-обвязки адаптеры не заводят.

Особенности доски:
- listing-эндпоинт ``/issues`` возвращает и pull request'ы — записи с полем
  ``pull_request`` отфильтровываются, иначе PR попадут в корпус задач;
- ``body`` — нативный GFM markdown, конвертация не нужна;
- нативных вложений у issue нет: файлы живут markdown-ссылками, а скачать их через
  API нельзя, поэтому отдаются только метаданные (без ``content_text``);
- ключ синтезируется как ``<key_prefix>-<issue number>``; нативный номер и node_id
  уходят в ``board_id``/``aliases``.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

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

_PAGE = 100
_API_VERSION = "2022-11-28"
_CLOUD_API_BASE = "https://api.github.com"
_CLOUD_WEB_BASE = "https://github.com"
_TOKEN_PATH = "/settings/personal-access-tokens/new"

# Маркер идемпотентности PR-ссылки в теле issue: повторный finish его видит и не дублирует.
_PR_MARKER = "<!-- reviewer:pr-link -->"

_NUMBER_RE = re.compile(r"(\d+)\s*$")
# Слаг репозитория попадает прямо в путь запроса, поэтому форма проверяется строго:
# только допустимые GitHub-символы и ровно два сегмента (без «..» и обхода пути).
_REPO_RE = re.compile(r"^(?!\.{1,2}/)[A-Za-z0-9._-]{1,100}/(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}$")
_TASK_ITEM_RE = re.compile(r"^\s*[-*+]\s+\[( |x|X)\]\s+(.*\S)\s*$")
_ISSUE_REF_RE = re.compile(r"(?:^|\s)#(\d+)\b|/issues/(\d+)\b")
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\((https?://[^)\s]+)\)")
_ATTACHMENT_HOSTS = ("user-images.githubusercontent.com", "objects.githubusercontent.com")
_ATTACHMENT_PATHS = ("/user-attachments/", "/files/", "/assets/")
_LABEL_KIND = "label"
_MILESTONE_KIND = "milestone"
_STATE_REASONS = {"completed", "not_planned"}


def _build_provider(context: ProviderBuildContext) -> GitHubIssuesBoard:
    """Собрать провайдера из registry-контекста (креды + immutable options)."""
    return GitHubIssuesBoard(
        token=context.credentials["GITHUB_ISSUES_TOKEN"],
        api_base=context.credentials.get("GITHUB_ISSUES_API_BASE") or _CLOUD_API_BASE,
        repo=str(context.options.get("repo") or ""),
        key_prefix=str(context.options.get("key_prefix") or ""),
        key_pattern=context.key_pattern,
        url_template=context.url_template,
    )


def _token_url(credentials: Mapping[str, str]) -> str:
    """Страница выпуска fine-grained токена рядом с настроенным API base (в т.ч. GHES)."""
    api_base = credentials.get("GITHUB_ISSUES_API_BASE") or _CLOUD_API_BASE
    try:
        return _web_base(_normalize_api_base(api_base, secrets=())) + _TOKEN_PATH
    except BoardProviderError:
        return _CLOUD_WEB_BASE + _TOKEN_PATH


def provider_spec() -> BoardProviderSpec:
    """Immutable registry spec GitHub Issues.

    ``GITHUB_TOKEN`` намеренно НЕ объявлен алиасом токена доски: этот env есть в любом
    деплое (им работает ревью PR), и алиас автоматически включил бы доску ``github``
    в ``configured_types`` без всякого явного решения оператора.
    """
    return BoardProviderSpec(
        board_type="github",
        factory=_build_provider,
        credential_fields=(
            CredentialFieldSpec(
                env="GITHUB_ISSUES_TOKEN",
                label="GitHub personal access token (issues: read and write)",
                secret=True,
            ),
            CredentialFieldSpec(
                env="GITHUB_ISSUES_API_BASE",
                label="GitHub REST API base URL",
                required=False,
                default=_CLOUD_API_BASE,
            ),
        ),
        option_fields=(
            ProviderOptionSpec(
                key="repo",
                label="Репозиторий (owner/name)",
                required_for=("sync", "create", "finish"),
            ),
            ProviderOptionSpec(
                key="key_prefix",
                label="Префикс ключа задачи",
                required_for=("sync",),
            ),
        ),
        setup=ProviderSetupSpec(
            label="GitHub Issues",
            help_url=_CLOUD_WEB_BASE + _TOKEN_PATH,
            help_text=(
                "Создайте fine-grained personal access token с доступом к нужному "
                "репозиторию и правом Issues: Read and write (classic token — scope repo). "
                "Токен показывается один раз, сохраните его сразу."
            ),
            help_url_builder=_token_url,
        ),
        default_api_base=_CLOUD_API_BASE,
        create_target_label="Метка или milestone создания",
        done_target_label="Метка или milestone закрытия",
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
            "GitHub API base must be an HTTPS URL without credentials or query.",
            hint="Use https://api.github.com or https://<host>/api/v3.",
            secrets=secrets,
        )
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{hostname}{port}{parsed.path.rstrip('/')}"


def _web_base(api_base: str) -> str:
    """Web-origin доски из API base: api.github.com → github.com, GHES — без /api/v3."""
    parsed = urlsplit(api_base)
    port = f":{parsed.port}" if parsed.port else ""
    host = (parsed.hostname or "").lower()
    if host == "api.github.com":
        return _CLOUD_WEB_BASE
    return f"https://{host}{port}{parsed.path.rstrip('/').removesuffix('/api/v3')}"


def _epoch_ms(value: object) -> int:
    """ISO 8601 UTC-aware → epoch ms; нераспознанное значение → 0 (без падения)."""
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


def _rate_limit_hint(status: int, headers: Mapping[str, str]) -> float | None:
    """403/429 с исчерпанным лимитом GitHub → секунды ожидания (retryable rate_limit).

    Обычный 403 (нет прав) хука не касается и остаётся permission-ошибкой.
    """
    if status not in {403, 429}:
        return None
    remaining = _header(headers, "X-RateLimit-Remaining")
    retry_after = _header(headers, "Retry-After")
    if remaining is not None and remaining.strip() == "0":
        reset = _header(headers, "X-RateLimit-Reset")
        try:
            return max(0.0, float(reset) - time.time()) if reset else 1.0
        except (TypeError, ValueError):
            return 1.0
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            pass
        try:
            return max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
        except (TypeError, ValueError, IndexError, OverflowError):
            return 1.0
    return 1.0 if status == 429 else None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Значение заголовка без учёта регистра (httpx.Headers и обычный dict)."""
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    return value


def _derive_prefix(repo: str) -> str:
    """Префикс ключа из имени репозитория: ``acme/widgets`` → ``WIDGETS`` (fallback)."""
    name = (repo or "").split("/")[-1]
    letters = "".join(ch for ch in name if ch.isascii() and ch.isalnum()).upper()
    return letters if letters[:1].isalpha() else f"GH{letters}"


def _issue_number(key: str) -> int:
    """Номер issue из ключа задачи (``PRI-7``, ``#7``, ``7``); иначе configuration-ошибка."""
    match = _NUMBER_RE.search(str(key or ""))
    if match is None:
        raise BoardProviderError(
            "configuration",
            "GitHub task key must end with the issue number.",
            hint="Use the synthesized key <prefix>-<issue number>.",
        )
    return int(match.group(1))


def _label_names(issue: Mapping[str, Any]) -> list[str]:
    """Имена меток issue: API отдаёт как объекты, так и plain-строки."""
    names: list[str] = []
    for item in issue.get("labels") or []:
        name = item.get("name") if isinstance(item, Mapping) else item
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _task_items(body: str) -> list[dict]:
    """Markdown task list (``- [ ] ...``) → чеклист: текст, отметка, ссылка на issue."""
    items: list[dict] = []
    for line in (body or "").splitlines():
        match = _TASK_ITEM_RE.match(line)
        if match is None:
            continue
        text = match.group(2)
        reference = _ISSUE_REF_RE.search(text)
        number = next((g for g in (reference.groups() if reference else ()) if g), None)
        items.append(
            {
                "title": text,
                "checked": match.group(1).lower() == "x",
                "number": int(number) if number else None,
            }
        )
    return items


def _attachment_name(text: str, url: str) -> str:
    """Имя вложения: последний сегмент пути с расширением, иначе текст markdown-ссылки."""
    segment = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    if "." in segment:
        return segment
    return text.strip() or segment


def _attachments(body: str, *, web_base: str) -> list[dict]:
    """Вложения issue из markdown-ссылок на хосты вложений GitHub (только метаданные)."""
    allowed_host = (urlsplit(web_base).hostname or "").lower()
    result: list[dict] = []
    seen: set[str] = set()
    for match in _MD_LINK_RE.finditer(body or ""):
        url = match.group(2)
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        is_asset_host = host in _ATTACHMENT_HOSTS
        is_repo_asset = host == allowed_host and any(
            part in parsed.path for part in _ATTACHMENT_PATHS
        )
        if not (is_asset_host or is_repo_asset) or url in seen:
            continue
        seen.add(url)
        result.append({"name": _attachment_name(match.group(1), url), "url": url})
    return result


def _body_with_pr_link(body: str, pr_url: str, note: str | None) -> str:
    """Тело issue с дописанным блоком PR-ссылки (маркер даёт идемпотентность)."""
    block = f"{_PR_MARKER}\nPR: {pr_url}"
    if (note or "").strip():
        block = f"{block}\n\n{note.strip()}"
    return f"{body.rstrip()}\n\n{block}" if body.strip() else block


class GitHubIssuesBoard(RestBoardBase):
    """Адаптер GitHub Issues поверх общего безопасного REST-скелета."""

    board_type = "github"

    def __init__(
        self,
        *,
        token: str,
        api_base: str = _CLOUD_API_BASE,
        repo: str = "",
        key_prefix: str = "",
        key_pattern: str = "",
        url_template: str = "",
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_base = _normalize_api_base(api_base, secrets=(token,))
        self._web_base = _web_base(self._api_base)
        self._repo = (repo or "").strip().strip("/")
        self._key_prefix = (key_prefix or "").strip()
        super().__init__(
            base_url=self._api_base,
            secrets=(token,),
            key_pattern=key_pattern,
            url_template=url_template,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION,
            },
            transport=transport,
            sleeper=sleeper,
            rate_limit_hint=_rate_limit_hint,
        )

    # --- разрешение репозитория и ключей ---

    def _resolve_repo(self, value: str | None) -> str:
        """Слаг ``owner/name``: аргумент project/board, если он в этой форме, иначе option."""
        for candidate in ((value or "").strip().strip("/"), self._repo):
            if _REPO_RE.match(candidate):
                return candidate
        return ""

    def _require_repo(self, value: str | None) -> str:
        """Валидный слаг репозитория или configuration-ошибка (слаг идёт в путь запроса)."""
        repo = self._resolve_repo(value)
        if not _REPO_RE.match(repo):
            raise BoardProviderError(
                "configuration",
                "GitHub repository must be a valid owner/name slug.",
                hint="Set the provider option repo to owner/name.",
                secrets=tuple(self.secrets),
            )
        return repo

    def _prefix_for(self, repo: str) -> str:
        return self._key_prefix or _derive_prefix(repo)

    # --- проверка подключения ---

    def validate_connection(self, project: str | None = None) -> dict:
        """Проверить токен (``GET /user``) и доступ к репозиторию задач."""
        identity = self._read("GET", "/user") or {}
        repo = self._resolve_repo(project)
        capabilities = {"read": False, "create": False, "finish": False}
        warnings: list[str] = []
        if not repo:
            warnings.append(
                "репозиторий не задан — права на создание и закрытие задач неизвестны"
            )
        else:
            info = self._read("GET", f"/repos/{repo}") or {}
            permissions = info.get("permissions") or {}
            writable = any(permissions.get(name) for name in ("push", "maintain", "admin"))
            issues_on = bool(info.get("has_issues"))
            capabilities = {
                "read": True,
                "create": issues_on,
                "finish": issues_on and writable,
            }
            if not issues_on:
                warnings.append(f"issues отключены в репозитории {repo}")
            if not writable:
                warnings.append(f"нет write-доступа к {repo} — закрытие задач недоступно")
        return {
            "status": "ok",
            "identity": {
                "login": identity.get("login"),
                "id": identity.get("id"),
                "name": identity.get("name"),
            },
            "project": repo or None,
            "capabilities": capabilities,
            "warnings": warnings,
        }

    # --- чтение ---

    def _raw_from_issue(self, issue: Mapping[str, Any], *, repo: str) -> RawTask:
        """Issue JSON → RawTask. Чистая функция: без I/O (единый маппер для iter/fetch)."""
        number = int(issue.get("number") or 0)
        key = f"{self._prefix_for(repo)}-{number}"
        body = str(issue.get("body") or "")
        items = _task_items(body)
        milestone = issue.get("milestone") if isinstance(issue.get("milestone"), Mapping) else {}
        return RawTask(
            key=key,
            project_code=key,
            title=str(issue.get("title") or ""),
            description=body,
            status=str(issue.get("state") or "") or None,
            subtask_ids=[str(item["number"]) for item in items if item["number"]],
            timestamp=_epoch_ms(issue.get("updated_at")),
            links=[],
            attachments=_attachments(body, web_base=self._web_base),
            board_id=str(number),
            completed=str(issue.get("state") or "").lower() == "closed",
            provider_data={
                "repo": repo,
                "number": number,
                "node_id": str(issue.get("node_id") or ""),
                "html_url": str(issue.get("html_url") or ""),
                "state_reason": issue.get("state_reason"),
                "labels": _label_names(issue),
                "milestone": {
                    "number": milestone.get("number"),
                    "title": milestone.get("title"),
                },
                "checklist": items,
                "sub_issues_total": int(
                    (issue.get("sub_issues_summary") or {}).get("total") or 0
                ),
            },
        )

    def iter_raw(self, board: str | None, limit: int | None) -> Iterable[RawTask]:
        """Ленивый обход всех страниц ``/issues`` (page+per_page); PR отфильтрованы."""
        repo = self._require_repo(board)

        def fetch(number: int, size: int) -> Any:
            return self._read(
                "GET",
                f"/repos/{repo}/issues",
                params={
                    "state": "all",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": size,
                    "page": number,
                },
            ) or []

        count = 0
        for issue in paginate_page(fetch, page_size=_PAGE):
            # /issues отдаёт и pull request'ы — в корпус задач они попадать не должны.
            if not isinstance(issue, Mapping) or issue.get("pull_request") is not None:
                continue
            yield self._raw_from_issue(issue, repo=repo)
            count += 1
            if limit is not None and count >= limit:
                return

    # --- discovery целей ---

    def _paged(self, path: str, params: Mapping[str, Any]) -> list[dict]:
        """Все страницы простого listing-эндпоинта (labels/milestones)."""
        def fetch(number: int, size: int) -> Any:
            return self._read(
                "GET",
                path,
                params={**params, "per_page": size, "page": number},
            ) or []

        return [item for item in paginate_page(fetch, page_size=_PAGE) if isinstance(item, Mapping)]

    def _label_targets(self, repo: str) -> tuple[list[dict], list[str]]:
        try:
            payload = self._paged(f"/repos/{repo}/labels", {})
        except BoardProviderError as error:
            return [], [f"метки репозитория недоступны: {error.category}"]
        return [
            {
                "id": f"{_LABEL_KIND}:{item['name']}",
                "label": str(item["name"]),
                "purposes": ["create", "done"],
                "kind": _LABEL_KIND,
                "value": str(item["name"]),
            }
            for item in payload
            if item.get("name")
        ], []

    def _milestone_targets(self, repo: str) -> tuple[list[dict], list[str]]:
        try:
            payload = self._paged(f"/repos/{repo}/milestones", {"state": "all"})
        except BoardProviderError as error:
            return [], [f"milestone'ы репозитория недоступны: {error.category}"]
        return [
            {
                "id": f"{_MILESTONE_KIND}:{int(item['number'])}",
                "label": str(item.get("title") or item["number"]),
                "purposes": ["create", "done"],
                "kind": _MILESTONE_KIND,
                "value": int(item["number"]),
            }
            for item in payload
            if item.get("number")
        ], []

    def _discover_targets(
        self,
        repo: str,
        *,
        kinds: tuple[str, ...],
    ) -> tuple[list[dict], list[str]]:
        """Метки и milestone'ы в общей форме targets; недоступный вид — warning, не ошибка."""
        targets: list[dict] = []
        warnings: list[str] = []
        for kind, loader in (
            (_LABEL_KIND, self._label_targets),
            (_MILESTONE_KIND, self._milestone_targets),
        ):
            if kind not in kinds:
                continue
            found, kind_warnings = loader(repo)
            targets.extend(found)
            warnings.extend(kind_warnings)
        return targets, warnings

    def list_targets(self, project: str | None) -> dict:
        """Метки и milestone'ы репозитория как единый список целей create/done."""
        repo = self._resolve_repo(project)
        targets: list[dict] = []
        warnings: list[str] = []
        if repo:
            targets, warnings = self._discover_targets(
                repo,
                kinds=(_LABEL_KIND, _MILESTONE_KIND),
            )
        else:
            warnings.append(
                "репозиторий не задан — метки и milestone'ы не перечислены"
            )
        return {
            "targets": targets,
            "options": [
                {
                    "key": "repo",
                    "label": "Репозиторий (owner/name)",
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

    def _resolve_target(self, repo: str, target: str | None) -> tuple[dict | None, list[str]]:
        """Цель по id (``label:bug``/``milestone:3``) или по человекочитаемой метке.

        Ненайденная/неоднозначная цель — предупреждение и ``None``: вызывающий работает
        дальше в дефолтном месте (контракт create/finish запрещает падать из-за target).
        """
        requested = (target or "").strip()
        if not requested:
            return None, []
        kind, _, _rest = requested.partition(":")
        kinds = (kind,) if kind in {_LABEL_KIND, _MILESTONE_KIND} else (
            _LABEL_KIND,
            _MILESTONE_KIND,
        )
        targets, warnings = self._discover_targets(repo, kinds=kinds)
        exact = [item for item in targets if requested in {item["id"], item["label"]}]
        if exact:
            return exact[0], warnings
        folded = requested.casefold()
        loose = [
            item
            for item in targets
            if folded in {item["id"].casefold(), item["label"].casefold()}
        ]
        if len(loose) == 1:
            return loose[0], warnings
        if len(loose) > 1:
            warnings.append(f"цель {requested!r} неоднозначна — не применена")
            return None, warnings
        warnings.append(f"цель {requested!r} не найдена среди меток и milestone'ов")
        return None, warnings

    # --- запись ---

    def create(
        self,
        doc_md: str,
        *,
        title: str,
        target: str | None,
        project: str | None,
    ) -> dict:
        """Создать issue из канонического markdown; цель — метка или milestone.

        Тело уходит как есть: GitHub хранит описание в GFM markdown. Ненайденная цель
        не мешает созданию — issue уходит без метки/milestone, причина в warnings.
        """
        repo = self._require_repo(project)
        if not (title or "").strip():
            raise BoardProviderError(
                "configuration",
                "GitHub issue title is required.",
                hint="Pass a non-empty task title.",
                secrets=tuple(self.secrets),
            )
        resolved, warnings = self._resolve_target(repo, target)
        payload: dict[str, Any] = {"title": title, "body": doc_md}
        if resolved is not None:
            if resolved["kind"] == _LABEL_KIND:
                payload["labels"] = [resolved["value"]]
            else:
                payload["milestone"] = resolved["value"]
        created = self._write("POST", f"/repos/{repo}/issues", json=payload) or {}
        number = created.get("number")
        if not number:
            raise BoardProviderError(
                "unsupported",
                "GitHub create response did not include an issue number.",
                hint="Check the repository and token permissions.",
                secrets=tuple(self.secrets),
            )
        return {
            "key": f"{self._prefix_for(repo)}-{int(number)}",
            "url": str(created.get("html_url") or f"{self._web_base}/{repo}/issues/{number}"),
            "board_id": str(number),
            "target_resolved": resolved["id"] if resolved is not None else None,
            "warnings": warnings,
        }

    def _done_payload(
        self,
        repo: str,
        issue: Mapping[str, Any],
        target: str | None,
    ) -> tuple[dict[str, Any], str | None, str, list[str]]:
        """Часть PATCH-тела, применяющая цель закрытия: метка, milestone или state_reason."""
        requested = (target or "").strip()
        if not requested:
            return {}, None, "completed", []
        if requested.casefold() in _STATE_REASONS:
            return {}, requested.casefold(), requested.casefold(), []
        resolved, warnings = self._resolve_target(repo, requested)
        if resolved is None:
            return {}, None, "completed", warnings
        payload: dict[str, Any] = {}
        if resolved["kind"] == _LABEL_KIND:
            names = _label_names(issue)
            if resolved["value"] not in names:
                payload["labels"] = [*names, resolved["value"]]
        else:
            current = issue.get("milestone")
            milestone = current if isinstance(current, Mapping) else {}
            if milestone.get("number") != resolved["value"]:
                payload["milestone"] = resolved["value"]
        return payload, resolved["id"], "completed", warnings

    def finish(
        self,
        key: str,
        pr_url: str,
        *,
        note: str | None = None,
        mark_done: bool = True,
        target: str | None = None,
    ) -> dict:
        """Закрыть issue и идемпотентно дописать PR-ссылку в тело — одним PATCH.

        Повторный вызов видит маркер ссылки и ``state="closed"``, ничего не пишет и
        возвращает ``already_closed=True``. Правка двигает ``updated_at``, поэтому
        инкрементальный синк переиндексирует задачу.
        """
        repo = self._require_repo(None)
        number = _issue_number(key)
        issue = self._read("GET", f"/repos/{repo}/issues/{number}") or {}
        body = str(issue.get("body") or "")
        state = str(issue.get("state") or "").lower()
        payload: dict[str, Any] = {}
        warnings: list[str] = []

        link_present = bool(pr_url) and (_PR_MARKER in body or pr_url in body)
        if pr_url and not link_present:
            payload["body"] = _body_with_pr_link(body, pr_url, note)

        target_resolved: str | None = None
        if mark_done:
            done_payload, target_resolved, reason, done_warnings = self._done_payload(
                repo,
                issue,
                target,
            )
            payload.update(done_payload)
            warnings.extend(done_warnings)
            if state != "closed":
                payload["state"] = "closed"
                payload["state_reason"] = reason

        pr_link_added = False
        done_set = False
        if payload:
            try:
                self._write("PATCH", f"/repos/{repo}/issues/{number}", json=payload)
            except BoardProviderError as error:
                warnings.append(f"обновление issue не удалось: {error.category}")
            else:
                pr_link_added = "body" in payload
                done_set = bool({"state", "labels", "milestone"} & set(payload))

        link_satisfied = not pr_url or link_present or pr_link_added
        done_satisfied = not mark_done or state == "closed" or done_set
        return {
            "key": key,
            "board_id": str(number),
            "done_set": done_set,
            "pr_link_added": pr_link_added,
            "already_closed": (
                not pr_link_added and not done_set and link_satisfied and done_satisfied
            ),
            "target_resolved": target_resolved,
            "warnings": warnings,
        }

    # --- нормализация ---

    def _sub_issues(self, raw: RawTask) -> tuple[list[dict], list[str]]:
        """Нативные sub-issues задачи (запрос только если summary их обещает), fail-soft."""
        data = raw.provider_data
        if not data.get("sub_issues_total"):
            return [], []
        repo = str(data.get("repo") or "")
        number = data.get("number")
        try:
            payload = self._read(
                "GET",
                f"/repos/{repo}/issues/{number}/sub_issues",
                params={"per_page": _PAGE},
            ) or []
        except BoardProviderError as error:
            return [], [f"sub-issues недоступны: {error.category}"]
        prefix = self._prefix_for(repo)
        result: list[dict] = []
        for item in payload:
            if not isinstance(item, Mapping) or not item.get("number"):
                continue
            result.append(
                {
                    "type": "subtask",
                    "key": f"{prefix}-{int(item['number'])}",
                    "title": str(item.get("title") or ""),
                }
            )
        return result, []

    def _issue_url(self, raw: RawTask) -> str | None:
        """Ссылка на issue: html_url из payload, иначе восстановленная из web-origin."""
        data = raw.provider_data
        html_url = str(data.get("html_url") or "")
        if html_url:
            return html_url
        repo = str(data.get("repo") or "")
        number = data.get("number")
        if repo and number:
            return f"{self._web_base}/{repo}/issues/{number}"
        return self._task_url(raw.key) or None

    def _brief(self, raw: RawTask, *, with_details: bool) -> dict:
        """Общее тело normalize/normalize_meta: details=False — без I/O и без деталей."""
        data = raw.provider_data
        checklist = list(data.get("checklist") or [])
        prefix = self._prefix_for(str(data.get("repo") or ""))
        warnings: list[str] = []
        links: list[dict] = []
        criteria: list[str] = []
        attachments: list[dict] = []

        if with_details:
            sub_issues, sub_warnings = self._sub_issues(raw)
            warnings.extend(sub_warnings)
            links.extend(sub_issues)
            criteria.extend(item["title"] for item in checklist)
            criteria.extend(link["title"] for link in sub_issues if link["title"])
            for item in checklist:
                number = item.get("number")
                links.append(
                    {
                        "type": "subtask",
                        "key": f"{prefix}-{number}" if number else "",
                        "title": item["title"],
                    }
                )
            for attachment in raw.attachments:
                attachments.append(
                    {
                        "name": attachment["name"],
                        "mime_type": None,
                        "size": None,
                        "content_text": None,
                        "url": attachment["url"],
                    }
                )
                warnings.append(
                    f"вложение {attachment['name']!r}: GitHub не отдаёт файлы issue "
                    "через API — сохранены только метаданные"
                )
            links.extend(raw.links)
            covered = {raw.key, *(link["key"] for link in links if link["key"])}
            if self._key_pattern:
                for match in re.finditer(self._key_pattern, raw.description or ""):
                    code = match.group(0)
                    if code not in covered:
                        links.append({"type": "related", "key": code, "title": ""})
                        covered.add(code)

        return {
            "key": raw.key,
            "aliases": [
                value
                for value in (
                    f"{data.get('repo')}#{data.get('number')}" if data.get("repo") else "",
                    str(data.get("node_id") or ""),
                )
                if value
            ],
            "title": raw.title,
            "description": raw.description,
            "criteria": criteria,
            "status": raw.status,
            "url": self._issue_url(raw),
            "links": links,
            "project": project_prefix(raw.key),
            "attachments": attachments,
            "warnings": warnings,
            "provider_data": {
                "number": data.get("number"),
                "labels": list(data.get("labels") or []),
                "milestone": data.get("milestone") or {},
                "state_reason": data.get("state_reason"),
            },
        }

    def normalize(self, raw: RawTask) -> dict:
        return self._brief(raw, with_details=True)

    def normalize_meta(self, raw: RawTask) -> dict:
        """Дешёвый TaskBrief без I/O: только плоские поля (PRI-207)."""
        return self._brief(raw, with_details=False)

    def fetch_one(self, key: str) -> RawTask | None:
        """Один RawTask по ключу задачи; 404/сбой → None (write-through пропускается)."""
        repo = self._resolve_repo(None)
        if not repo:
            return None
        try:
            number = _issue_number(key)
            issue = self._read("GET", f"/repos/{repo}/issues/{number}")
        except BoardProviderError as error:
            if error.category in {"not_found", "configuration"}:
                return None
            raise
        if not isinstance(issue, Mapping) or issue.get("pull_request") is not None:
            return None
        return self._raw_from_issue(issue, repo=repo)
