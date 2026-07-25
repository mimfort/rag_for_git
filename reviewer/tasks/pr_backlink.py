"""Обратный линк: кликабельная ссылка на задачу в теле PR.

`finish_task` пишет PR-ссылку в задачу; здесь — обратная сторона связи.
Чистые функции без I/O: разбор ссылки на PR/MR и идемпотентная сборка тела.
Сам HTTP-вызов делает VCS-провайдер, оркестрация — MCPReviewService.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Скрытый маркер идемпотентности — по образцу <!-- ai-review:hash --> в комментариях
# ревью: HTML-комментарий не рендерится ни на GitHub, ни на GitLab.
MARKER = "<!-- reviewer:task-link -->"

# Платформу определяет форма пути, а не хост: так self-hosted GitLab работает
# без предварительной индексации репо (repo_vcs может быть пуст).
_GITHUB_RE = re.compile(
    r"^https?://[^/\s]+/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)"
)
_GITLAB_RE = re.compile(
    r"^(?P<root>https?://[^/\s]+)/(?P<path>[^\s?#]+?)/-/merge_requests/(?P<number>\d+)"
)


@dataclass(frozen=True)
class PRTarget:
    """Разобранная ссылка на PR/MR."""
    platform: str    # "github" | "gitlab"
    owner: str       # для GitLab — путь группы любой глубины (team/sub)
    repo: str
    number: int
    base_url: str    # схема+хост; "" для github (провайдер ходит в api.github.com)


def parse_pr_url(url: str) -> PRTarget | None:
    """PRTarget из ссылки на PR/MR; None — если ссылка не распознана.

    Хвосты (/files, ?tab=…, #note_1, завершающий /) игнорируются: регулярки
    не якорят конец строки."""
    if not url:
        return None
    m = _GITHUB_RE.match(url)
    if m:
        return PRTarget("github", m.group("owner"), m.group("repo"),
                        int(m.group("number")), "")
    m = _GITLAB_RE.match(url)
    if m:
        parts = [p for p in m.group("path").split("/") if p]
        if len(parts) < 2:
            return None
        return PRTarget("gitlab", "/".join(parts[:-1]), parts[-1],
                        int(m.group("number")), m.group("root"))
    return None


def apply_backlink(body: str, key: str, task_url: str) -> str | None:
    """Новое тело PR со строкой-ссылкой в начале; None — если писать не надо.

    Идемпотентность двойная: маркер (наша прошлая правка) и сам URL задачи
    (ручная ссылка автора PR — её не дублируем)."""
    body = body or ""
    if MARKER in body or task_url in body:
        return None
    line = f"Задача: [{key}]({task_url})\n{MARKER}"
    return line if not body.strip() else f"{line}\n\n{body}"
