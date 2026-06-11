from __future__ import annotations
import base64
import re
import time

import httpx

from reviewer.vcs.base import PullRequest, ChangedFile, InlineComment

_FP = re.compile(r"<!-- ai-review:([0-9a-f]+) -->")
_RETRY_CODES = {429, 502, 503, 504}


class _RetryTransport:
    """Обёртка над httpx-транспортом с retry по статусам 429/502/503/504."""

    def __init__(
        self,
        wrapped,
        *,
        attempts: int = 3,
        backoff_base: float = 1.0,
        max_wait: float = 8.0,
        _sleep=time.sleep,
    ):
        self._wrapped = wrapped
        self._attempts = attempts
        self._backoff_base = backoff_base
        self._max_wait = max_wait
        self._sleep = _sleep

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(self._attempts):
            response = self._wrapped.handle_request(request)
            if response.status_code not in _RETRY_CODES:
                return response
            if attempt < self._attempts - 1:
                retry_after = response.headers.get("retry-after")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = self._backoff_base * (2 ** attempt)
                else:
                    wait = self._backoff_base * (2 ** attempt)
                # max(0, …): невалидный/отрицательный Retry-After (напр. "-1")
                # не должен уводить sleep в минус — time.sleep(<0) бросает ValueError.
                self._sleep(max(0.0, min(wait, self._max_wait)))
        # Исчерпаны попытки — возвращаем последний ответ (raise_for_status сделает своё дело)
        assert response is not None
        return response

    def close(self) -> None:
        self._wrapped.close()

    def __enter__(self):
        self._wrapped.__enter__()
        return self

    def __exit__(self, *args):
        return self._wrapped.__exit__(*args)


class GitHubProvider:
    def __init__(
        self,
        owner: str,
        repo: str,
        token: str,
        client: httpx.Client | None = None,
        *,
        retry_attempts: int = 3,
        retry_backoff_base: float = 1.0,
    ):
        self.owner, self.repo = owner, repo
        if client is None:
            transport = _RetryTransport(
                httpx.HTTPTransport(),
                attempts=retry_attempts,
                backoff_base=retry_backoff_base,
            )
            client = httpx.Client(
                base_url="https://api.github.com",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Accept": "application/vnd.github+json",
                },
                timeout=30,
                transport=transport,
            )
        self._c = client

    def close(self) -> None:
        self._c.close()

    def _base(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"

    def get_pull_request(self, number: int) -> PullRequest:
        d = self._c.get(f"{self._base()}/pulls/{number}").raise_for_status().json()
        return PullRequest(
            number=number,
            base_sha=d["base"]["sha"],
            head_sha=d["head"]["sha"],
            base_ref=d["base"]["ref"],
            title=d.get("title", ""),
            body=d.get("body") or "",
            draft=bool(d.get("draft", False)),
        )

    def get_changed_files(self, number: int) -> list[ChangedFile]:
        files, page = [], 1
        while True:
            r = self._c.get(
                f"{self._base()}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            ).raise_for_status()
            batch = r.json()
            files += [ChangedFile(f["filename"], f["status"], f.get("patch")) for f in batch]
            if len(batch) < 100:
                break
            page += 1
        return files

    def get_file_at_ref(self, path: str, ref: str) -> str | None:
        r = self._c.get(f"{self._base()}/contents/{path}", params={"ref": ref})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return base64.b64decode(r.json()["content"]).decode("utf-8", "replace")

    def list_existing_fingerprints(self, number: int) -> set[str]:
        fps, page = set(), 1
        while True:
            r = self._c.get(
                f"{self._base()}/pulls/{number}/comments",
                params={"per_page": 100, "page": page},
            ).raise_for_status()
            batch = r.json()
            for cm in batch:
                fps.update(_FP.findall(cm.get("body", "")))
            if len(batch) < 100:
                break
            page += 1
        return fps

    def compare_files(self, base_sha: str, head_sha: str) -> list[ChangedFile]:
        """Изменённые файлы между двумя коммитами (GitHub compare).

        Внимание: API отдаёт максимум 300 файлов — для синка base-индекса после
        обычного продвижения ветки этого достаточно.

        404 (например, force-push, sha недоступен) пробрасывается как есть —
        вызывающий обработает.
        """
        r = self._c.get(f"{self._base()}/compare/{base_sha}...{head_sha}").raise_for_status()
        files = r.json().get("files", [])
        return [ChangedFile(f["filename"], f["status"], f.get("patch")) for f in files]

    def publish_review(
        self,
        number: int,
        head_sha: str,
        summary: str,
        comments: list[InlineComment],
    ) -> None:
        payload_comments = []
        for c in comments:
            item = {"path": c.path, "line": c.line, "side": c.side, "body": c.body}
            if c.start_line is not None:
                item["start_line"] = c.start_line
                item["start_side"] = c.start_side or c.side
            payload_comments.append(item)
        self._c.post(
            f"{self._base()}/pulls/{number}/reviews",
            json={
                "commit_id": head_sha,
                "body": summary,
                "event": "COMMENT",
                "comments": payload_comments,
            },
        ).raise_for_status()
