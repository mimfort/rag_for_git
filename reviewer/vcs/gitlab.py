from __future__ import annotations
import base64
import logging
import re
from urllib.parse import quote

import httpx

from reviewer.vcs.base import PullRequest, ChangedFile, InlineComment
from reviewer.vcs._http import _RetryTransport, _FP

log = logging.getLogger(__name__)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _new_line_map(patch: str | None) -> dict[int, int | None]:
    """new_line → old_line для контекстных строк, new_line → None для добавленных.

    Парсит unified-diff хунки. Нужен GitLab-позиции: комментарий на контекстной
    (неизменённой) строке требует и old_line, и new_line; на добавленной — только
    new_line. Удалённые строки (LEFT) в карту не попадают.
    """
    result: dict[int, int | None] = {}
    if not patch:
        return result
    old_ln = new_ln = 0
    for line in patch.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            old_ln = int(m.group(1))
            new_ln = int(m.group(2))
            continue
        if not line:
            continue
        tag = line[0]
        if tag == "+":
            result[new_ln] = None      # добавленная строка
            new_ln += 1
        elif tag == "-":
            old_ln += 1                # удалённая строка (LEFT) — не в new-карте
        elif tag == "\\":
            continue                   # «\ No newline at end of file»
        else:                          # контекст (' ')
            result[new_ln] = old_ln
            old_ln += 1
            new_ln += 1
    return result


def _file_status(ch: dict) -> str:
    if ch.get("new_file"):
        return "added"
    if ch.get("deleted_file"):
        return "removed"
    if ch.get("renamed_file"):
        return "renamed"
    return "modified"


def _to_changed_file(ch: dict) -> ChangedFile:
    return ChangedFile(
        path=ch.get("new_path") or ch.get("old_path"),
        status=_file_status(ch),
        patch=ch.get("diff") or None,
    )


class GitLabProvider:
    """VCSProvider для GitLab Merge Requests (API v4) поверх httpx.

    `number` — это MR `iid` (per-project счётчик), прямой аналог номера PR.
    Путь проекта `owner/name` URL-энкодится в `:id`.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        token: str,
        *,
        base_url: str = "https://gitlab.com",
        client: httpx.Client | None = None,
        retry_attempts: int = 3,
        retry_backoff_base: float = 1.0,
    ):
        self.owner, self.repo = owner, repo
        self._proj = quote(f"{owner}/{repo}", safe="")
        if client is None:
            transport = _RetryTransport(
                httpx.HTTPTransport(),
                attempts=retry_attempts,
                backoff_base=retry_backoff_base,
            )
            client = httpx.Client(
                base_url=f"{base_url.rstrip('/')}/api/v4",
                headers={"PRIVATE-TOKEN": token},
                timeout=30,
                transport=transport,
            )
        self._c = client

    def close(self) -> None:
        self._c.close()

    def _base(self) -> str:
        return f"/projects/{self._proj}"

    def _mr(self, number: int) -> str:
        return f"{self._base()}/merge_requests/{number}"

    def get_pull_request(self, number: int) -> PullRequest:
        d = self._c.get(self._mr(number)).raise_for_status().json()
        refs = d.get("diff_refs") or {}
        return PullRequest(
            number=number,
            base_sha=refs.get("base_sha", ""),
            head_sha=refs.get("head_sha", ""),
            base_ref=d.get("target_branch", ""),
            title=d.get("title", ""),
            body=d.get("description") or "",
            draft=bool(d.get("draft", d.get("work_in_progress", False))),
            head_ref=d.get("source_branch"),
        )

    def get_changed_files(self, number: int) -> list[ChangedFile]:
        d = self._c.get(f"{self._mr(number)}/changes").raise_for_status().json()
        return [_to_changed_file(ch) for ch in d.get("changes", [])]

    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        r = self._c.get(
            f"{self._base()}/repository/files/{quote(path, safe='')}",
            params={"ref": ref},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return base64.b64decode(r.json()["content"]).decode("utf-8", "replace")

    def list_existing_fingerprints(self, number: int) -> set[str]:
        fps, page = set(), 1
        while True:
            r = self._c.get(
                f"{self._mr(number)}/notes",
                params={"per_page": 100, "page": page},
            ).raise_for_status()
            batch = r.json()
            for note in batch:
                fps.update(_FP.findall(note.get("body", "")))
            if len(batch) < 100:
                break
            page += 1
        return fps

    def compare_files(self, base_sha: str, head_sha: str) -> list[ChangedFile]:
        r = self._c.get(
            f"{self._base()}/repository/compare",
            params={"from": base_sha, "to": head_sha},
        ).raise_for_status()
        return [_to_changed_file(ch) for ch in r.json().get("diffs", [])]

    def publish_review(
        self,
        number: int,
        head_sha: str,
        summary: str,
        comments: list[InlineComment],
    ) -> None:
        # Inline-комментарии — отдельные discussions с позицией. Тройку SHA
        # берём из diff_refs MR (head_sha из аргумента может расходиться).
        if comments:
            d = self._c.get(self._mr(number)).raise_for_status().json()
            refs = d.get("diff_refs") or {}
            # Строим карту new_line → old_line для каждого файла: нужна для корректной
            # GitLab-позиции на контекстных строках (требуют и old_line, и new_line).
            maps: dict[str, dict[int, int | None]] = {
                cf.path: _new_line_map(cf.patch)
                for cf in self.get_changed_files(number)
            }
            for c in comments:
                position = {
                    "position_type": "text",
                    "base_sha": refs.get("base_sha"),
                    "start_sha": refs.get("start_sha"),
                    "head_sha": refs.get("head_sha"),
                    "new_path": c.path,
                    "old_path": c.path,
                }
                # RIGHT → строка новой версии, LEFT → строка старой.
                # Мультистрочные комментарии деградируют в однострочный (на c.line).
                if c.side == "RIGHT":
                    lmap = maps.get(c.path, {})
                    old = lmap.get(c.line, "NOT_FOUND")
                    if old != "NOT_FOUND" and old is not None:
                        # Контекстная (неизменённая) строка — GitLab требует оба поля.
                        position["new_line"] = c.line
                        position["old_line"] = old
                    else:
                        # Добавленная строка или строка не найдена в патче.
                        position["new_line"] = c.line
                else:
                    position["old_line"] = c.line
                try:
                    self._c.post(
                        f"{self._mr(number)}/discussions",
                        json={"body": c.body, "position": position},
                    ).raise_for_status()
                except httpx.HTTPStatusError as exc:
                    log.warning(
                        "Не удалось опубликовать inline-комментарий %s:%d: %s",
                        c.path, c.line, exc,
                    )
        # Сводка — обычный нот MR (у GitLab нет объекта «review»). Публикуется
        # последней, чтобы сбой в цикле комментариев не оставлял её сиротой.
        self._c.post(
            f"{self._mr(number)}/notes", json={"body": summary}
        ).raise_for_status()
