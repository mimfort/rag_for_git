"""Публикация issue в репозиторий инструмента и поиск дублей (PRI-239).

Отдельный минимальный клиент, а не расширение ``VCSProvider``: тот контракт описывает
ревью PR (диффы, inline-комментарии, фингерпринты), issue-канал к нему отношения не
имеет, и добавление методов в протокол заставило бы каждый адаптер платформы их
реализовывать ради чужой задачи.

Любой сбой — не исключение наружу, а исход ``fallback``: сессия не должна ломаться
из-за того, что не удалось отправить отчёт о баге.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from reviewer.bugreport.render import MARKER, TARGET_REPO, BugReport, prefilled_url
from reviewer.vcs._http import _RetryTransport

log = logging.getLogger(__name__)

#: Порог схожести заголовков, при котором открытая issue считается тем же дефектом.
_TITLE_OVERLAP = 0.6


@dataclass(frozen=True)
class PublishResult:
    """Исход публикации: три различимых состояния вместо одного bool.

    ``published`` — issue создана; ``commented`` — дополнен найденный дубль;
    ``fallback`` — опубликовать не удалось, пользователю отдан готовый markdown и
    ссылка на форму. Ни одно из состояний не считается сбоем сессии.
    """

    status: str
    url: str = ""
    fallback_url: str = ""
    duplicate_url: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "issue_url": self.url,
            "fallback_url": self.fallback_url,
            "duplicate_url": self.duplicate_url,
            "reason": self.reason,
        }


class IssueClient:
    """Минимальный GitHub issue-клиент поверх того же retry-транспорта, что и VCS."""

    def __init__(
        self,
        token: str,
        *,
        repo: str = TARGET_REPO,
        client: httpx.Client | None = None,
        retry_attempts: int = 3,
        retry_backoff_base: float = 1.0,
    ) -> None:
        self.repo = repo
        self._token = token
        if client is None:
            client = httpx.Client(
                base_url="https://api.github.com",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Accept": "application/vnd.github+json",
                },
                timeout=30,
                transport=_RetryTransport(
                    httpx.HTTPTransport(),
                    attempts=retry_attempts,
                    backoff_base=retry_backoff_base,
                ),
            )
        self._c = client

    def close(self) -> None:
        self._c.close()

    def list_open_issues(self, limit: int = 50) -> list[dict]:
        response = self._c.get(
            f"/repos/{self.repo}/issues",
            params={"state": "open", "per_page": min(limit, 100)},
        )
        response.raise_for_status()
        # /issues отдаёт и pull request'ы — они не дубли репорта.
        return [item for item in response.json() if "pull_request" not in item]

    def create_issue(self, title: str, body: str, labels: tuple[str, ...] = ()) -> dict:
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        response = self._c.post(f"/repos/{self.repo}/issues", json=payload)
        response.raise_for_status()
        return response.json()

    def comment(self, number: int, body: str) -> dict:
        response = self._c.post(
            f"/repos/{self.repo}/issues/{number}/comments", json={"body": body})
        response.raise_for_status()
        return response.json()


def find_duplicate(issues: list[dict], report: BugReport) -> dict | None:
    """Найти открытую issue про тот же дефект: сначала по сигнатуре, потом по заголовку.

    Сигнатура точна, но появилась только с этим каналом — у issue, заведённых руками,
    её нет, поэтому есть и второй, нестрогий проход по пересечению слов заголовка.
    """
    for issue in issues:
        body = str(issue.get("body") or "")
        if MARKER in body and report.signature and report.signature in body:
            return issue
    target = _title_words(report.title)
    if not target:
        return None
    for issue in issues:
        words = _title_words(str(issue.get("title") or ""))
        if not words:
            continue
        overlap = len(target & words) / len(target)
        if overlap >= _TITLE_OVERLAP:
            return issue
    return None


def _title_words(title: str) -> set[str]:
    return {word for word in title.lower().replace("[", " ").replace("]", " ").split()
            if len(word) > 3}


def publish_report(
    report: BugReport,
    *,
    token: str,
    repo: str = TARGET_REPO,
    client: IssueClient | None = None,
    allow_duplicate_comment: bool = True,
) -> PublishResult:
    """Опубликовать репорт; при любой проблеме вернуть фолбэк, а не исключение."""
    fallback = prefilled_url(report, repo)
    if not token:
        return PublishResult(
            status="fallback",
            fallback_url=fallback,
            reason="нет GitHub-токена: опубликовать от вашего имени нечем",
        )
    owned = client is None
    issue_client = client or IssueClient(token, repo=repo)
    try:
        duplicate = None
        try:
            duplicate = find_duplicate(issue_client.list_open_issues(), report)
        except Exception:      # noqa: BLE001 — поиск дублей необязателен для публикации
            log.warning("поиск дублей issue не удался", exc_info=True)
        if duplicate is not None and allow_duplicate_comment:
            created = issue_client.comment(int(duplicate["number"]), report.body)
            return PublishResult(
                status="commented",
                url=str(created.get("html_url") or duplicate.get("html_url") or ""),
                duplicate_url=str(duplicate.get("html_url") or ""),
            )
        created = issue_client.create_issue(report.title, report.body, report.labels)
        return PublishResult(status="published", url=str(created.get("html_url") or ""))
    except Exception as error:      # noqa: BLE001 — канал обратной связи не ломает сессию
        log.warning("публикация issue не удалась", exc_info=True)
        return PublishResult(
            status="fallback",
            fallback_url=fallback,
            reason=f"публикация не удалась ({type(error).__name__}); "
                   "опубликуйте отчёт вручную по ссылке",
        )
    finally:
        if owned:
            try:
                issue_client.close()
            except Exception:      # noqa: BLE001
                log.warning("не удалось закрыть issue-клиент", exc_info=True)
