"""Извлечение ссылок на GitHub PR из произвольного текста (description задачи).

Используется при sync-tasks для авто-линковки (:Task)-[:IMPLEMENTED_BY]->(:PR):
PR-ссылки живут в description многих задач, но без парсинга остаются текстом.
"""
from __future__ import annotations

import re

from reviewer.tasks.graph import PRRef

# https://github.com/<owner>/<repo>/pull/<number>
_PR_URL_RE = re.compile(
    r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)",
    re.IGNORECASE,
)


def extract_pr_refs(text: str) -> list[PRRef]:
    """PRRef из всех GitHub-PR-URL в тексте. Дедуп по (repo, number), sha=''."""
    if not text:
        return []
    seen: set[tuple[str, int]] = set()
    refs: list[PRRef] = []
    for m in _PR_URL_RE.finditer(text):
        owner, repo, num = m.group(1), m.group(2), int(m.group(3))
        full = f"{owner}/{repo}"
        sig = (full, num)
        if sig in seen:
            continue
        seen.add(sig)
        refs.append(PRRef(repo=full, number=num, url=m.group(0), sha=""))
    return refs
