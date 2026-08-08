"""Безопасный интерактивный setup credentials для board providers."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from reviewer.config.provider_access import render_provider_access
from reviewer.tasks.boards.errors import (
    BoardProviderError,
    sanitize_provider_text,
)
from reviewer.tasks.boards.reporting import sanitize_validation_report
from reviewer.tasks.boards.registry import BoardProviderSpec, ProviderBuildContext

_YOUGILE_API_BASE = "https://yougile.com/api-v2"


@dataclass(frozen=True)
class SetupChoice:
    value: str
    label: str


class SetupIO(Protocol):
    dry_run: bool
    non_interactive: bool

    def confirm(self, text: str, default: bool = False) -> bool: ...

    def prompt(self, text: str, *, secret: bool = False, default: str = "") -> str: ...

    def choose(self, text: str, choices: Sequence[SetupChoice]) -> str: ...

    def open_url(self, url: str) -> None: ...


class ClickSetupIO:
    """SetupIO поверх Click; браузер открывается только из явной confirm-ветки."""

    def __init__(self, *, dry_run: bool = False, non_interactive: bool = False) -> None:
        self.dry_run = dry_run
        self.non_interactive = non_interactive

    def confirm(self, text: str, default: bool = False) -> bool:
        import click

        return click.confirm(text, default=default)

    def prompt(self, text: str, *, secret: bool = False, default: str = "") -> str:
        import click

        return click.prompt(
            text,
            default=default,
            hide_input=secret,
            show_default=not secret,
        )

    def choose(self, text: str, choices: Sequence[SetupChoice]) -> str:
        import click

        rendered = [f"{choice.label} [{choice.value}]" for choice in choices]
        selected = click.prompt(text, type=click.Choice(rendered))
        return choices[rendered.index(selected)].value

    def open_url(self, url: str) -> None:
        import click

        click.launch(url)

    @staticmethod
    def echo(text: str) -> None:
        import click

        click.echo(text)


def _echo(io: SetupIO, text: str) -> None:
    echo = getattr(io, "echo", None)
    if callable(echo):
        echo(text)


def _manual_yougile_key(io: SetupIO, api_base: str) -> dict[str, str]:
    key = io.prompt("YouGile API key", secret=True).strip()
    if not key:
        raise BoardProviderError(
            "configuration",
            "YouGile API key is required.",
            hint="Create a key with the official REST API console and try again.",
        )
    return {"YOUGILE_API_KEY": key, "YOUGILE_API_BASE": api_base}


def _yougile_api_base(value: str) -> str:
    raw = (value or _YOUGILE_API_BASE).strip().rstrip("/")
    parsed = urlsplit("")
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        hostname = None
        port = None
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.rstrip("/").endswith("/api-v2")
    ):
        raise BoardProviderError(
            "configuration",
            "YouGile API base must be a credential-free HTTPS URL ending in /api-v2.",
            hint="Use https://yougile.com/api-v2 or the HTTPS API URL of your installation.",
        )
    safe_hostname = f"[{hostname}]" if ":" in hostname else hostname
    authority = safe_hostname if port is None else f"{safe_hostname}:{port}"
    return f"https://{authority}{parsed.path.rstrip('/')}"


def _yougile_response(response: httpx.Response) -> object:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = {}
    if 200 <= response.status_code < 300:
        return payload
    error = payload.get("error", "") if isinstance(payload, Mapping) else ""
    if "allowonlyopenid" in str(error).lower():
        raise BoardProviderError(
            "unsupported",
            "YouGile password authentication is disabled by allowOnlyOpenId.",
            hint=(
                "Use a ready API key created by an API-capable account; "
                "YouGile does not document an exchange for REST API keys."
            ),
        )
    category = "authentication" if response.status_code in {401, 403} else "unsupported"
    raise BoardProviderError(
        category,
        f"YouGile API key acquisition failed with status {response.status_code}.",
        hint="Check the login and password or enter an existing API key.",
    )


def acquire_yougile_key(io: SetupIO, *, client: httpx.Client | None = None) -> dict[str, str]:
    """Получить YouGile key официальным companies→key flow или вручную.

    Login/password используются только в локальных переменных этого hook.
    Password не возвращается, не логируется и очищается в ``finally``.
    """
    if io.dry_run or io.non_interactive:
        return {}
    api_base = io.prompt(
        "YouGile API base URL",
        default=_YOUGILE_API_BASE,
    )
    api_base = _yougile_api_base(api_base)
    if not io.confirm("Получить YouGile API key автоматически?", default=True):
        return _manual_yougile_key(io, api_base)

    login = io.prompt("YouGile login", secret=True).strip()
    password = io.prompt("YouGile password", secret=True)
    own_client = client is None
    active_client = client
    try:
        if active_client is None:
            try:
                active_client = httpx.Client(base_url=api_base, timeout=30.0)
            except Exception:
                raise BoardProviderError(
                    "configuration",
                    "YouGile API client initialization failed.",
                    hint="Check the HTTPS API base URL or enter an existing API key.",
                    secrets=(password, login),
                ) from None
        try:
            companies_response = active_client.post(
                "/auth/companies",
                json={"login": login, "password": password},
            )
            companies_payload = _yougile_response(companies_response)
            companies = (
                companies_payload.get("content", [])
                if isinstance(companies_payload, Mapping)
                else []
            )
            choices = [
                SetupChoice(
                    str(company.get("id", "")),
                    str(company.get("name") or company.get("title") or company.get("id") or ""),
                )
                for company in companies
                if isinstance(company, Mapping) and company.get("id")
            ]
            if not choices:
                raise BoardProviderError(
                    "not_found",
                    "No YouGile companies are available for this account.",
                    hint="Check account access or enter an existing API key.",
                )
            company_id = io.choose("Выберите компанию YouGile", choices)
            key_response = active_client.post(
                "/auth/keys",
                json={
                    "login": login,
                    "password": password,
                    "companyId": company_id,
                },
            )
            key_payload = _yougile_response(key_response)
            key = key_payload.get("key", "") if isinstance(key_payload, Mapping) else ""
            if not key:
                raise BoardProviderError(
                    "unsupported",
                    "YouGile did not return an API key.",
                    hint="Enter an existing API key from the official REST API console.",
                )
            return {"YOUGILE_API_KEY": str(key), "YOUGILE_API_BASE": api_base}
        except httpx.RequestError:
            raise BoardProviderError(
                "transient",
                "YouGile API key acquisition request failed.",
                hint="Check the connection or enter an existing API key.",
                retryable=True,
            ) from None
        except BoardProviderError as error:
            _echo(io, f"{error.message} {error.hint}".strip())
            if io.confirm("Ввести готовый YouGile API key вручную?", default=True):
                return _manual_yougile_key(io, api_base)
            raise
    finally:
        password = ""
        if own_client and active_client is not None:
            active_client.close()


def _setup_url(spec: BoardProviderSpec, values: Mapping[str, str]) -> str:
    builder = spec.setup.help_url_builder
    return builder(values) if builder is not None else spec.setup.help_url


def _prompt_credentials(fields, io: SetupIO) -> dict[str, str]:
    return {
        field.env: io.prompt(
            field.label,
            secret=field.secret,
            default=field.default,
        ).strip()
        for field in fields
    }


def configure_board_provider(spec: BoardProviderSpec, io: SetupIO) -> dict[str, str]:
    """Запросить canonical credentials, проверить provider и вернуть только env values."""
    if io.dry_run or io.non_interactive:
        return {}

    _echo(
        io,
        render_provider_access(
            label=spec.setup.label,
            help_text=spec.setup.help_text,
            help_url=spec.setup.help_url,
            access=spec.setup.access,
        ),
    )
    if spec.setup.help_url_builder is None and io.confirm(
        f"Открыть официальную инструкцию {spec.setup.help_url}?",
        default=False,
    ):
        io.open_url(spec.setup.help_url)

    if spec.setup.acquisition is not None:
        acquired = spec.setup.acquisition(io)
    elif spec.setup.help_url_builder is not None:
        acquired = _prompt_credentials(
            [field for field in spec.credential_fields if not field.secret],
            io,
        )
        url = _setup_url(spec, acquired)
        if io.confirm(f"Открыть предложенную страницу Account Security {url}?", default=False):
            io.open_url(url)
        acquired.update(
            _prompt_credentials(
                [field for field in spec.credential_fields if field.secret],
                io,
            )
        )
    else:
        acquired = _prompt_credentials(spec.credential_fields, io)
    declared = {field.env for field in spec.credential_fields}
    values = {
        field.env: acquired.get(field.env, "") or field.default
        for field in spec.credential_fields
    }
    if set(acquired) - declared:
        raise BoardProviderError(
            "configuration",
            "Provider setup returned undeclared credential fields.",
        )
    missing = [field.env for field in spec.credential_fields if field.required and not values[field.env]]
    if missing:
        raise BoardProviderError(
            "configuration",
            "Required board credentials are missing.",
            hint="Fill all required provider fields.",
        )

    project = io.prompt("Project key for validation (optional)", default="").strip() or None
    secrets = frozenset(
        values[field.env]
        for field in spec.credential_fields
        if field.secret and values[field.env]
    )
    context = ProviderBuildContext(
        credentials=values,
        options={},
        key_pattern="",
        url_template="",
        attachment_max_bytes=10 * 1024 * 1024,
        attachment_timeout=10.0,
        attachment_store_chars=200000,
    )
    try:
        provider = spec.factory(context)
    except BoardProviderError as error:
        raise BoardProviderError(
            error.category,
            error.message,
            hint=error.hint,
            retryable=error.retryable,
            secrets=secrets,
        ) from None
    except Exception as error:
        raise BoardProviderError(
            "configuration",
            "Board provider initialization failed.",
            hint=sanitize_provider_text(error, secrets),
            secrets=secrets,
        ) from None
    try:
        validation = provider.validate_connection(project)
        safe_validation = sanitize_validation_report(validation, secrets)
        _echo(io, json.dumps(safe_validation, ensure_ascii=False, sort_keys=True))
    except BoardProviderError as error:
        raise BoardProviderError(
            error.category,
            error.message,
            hint=error.hint,
            retryable=error.retryable,
            secrets=secrets,
        ) from None
    except Exception:
        raise BoardProviderError(
            "unsupported",
            "Board provider validation failed.",
            hint="Check the provider credentials and project.",
            secrets=secrets,
        ) from None
    finally:
        with suppress(Exception):
            provider.close()
    return values
