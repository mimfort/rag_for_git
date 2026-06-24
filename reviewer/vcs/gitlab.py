from __future__ import annotations
import base64
from urllib.parse import quote

import httpx

from reviewer.vcs.base import PullRequest, ChangedFile, InlineComment
from reviewer.vcs._http import _RetryTransport, _FP


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
        # Сводка — обычный нот MR (у GitLab нет объекта «review»).
        self._c.post(
            f"{self._mr(number)}/notes", json={"body": summary}
        ).raise_for_status()
        if not comments:
            return
        # Inline-комментарии — отдельные discussions с позицией. Тройку SHA
        # берём из diff_refs MR (head_sha из аргумента может расходиться).
        d = self._c.get(self._mr(number)).raise_for_status().json()
        refs = d.get("diff_refs") or {}
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
                position["new_line"] = c.line
            else:
                position["old_line"] = c.line
            self._c.post(
                f"{self._mr(number)}/discussions",
                json={"body": c.body, "position": position},
            ).raise_for_status()
