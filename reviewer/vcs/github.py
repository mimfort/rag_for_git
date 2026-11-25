from __future__ import annotations
import base64

import httpx

from reviewer.vcs.base import PullRequest, ChangedFile, InlineComment
from reviewer.vcs._http import _RetryTransport, _FP  # noqa: F401  (реэкспорт для тестов)


_COMPARE_FILE_LIMIT = 300
_COMPARE_FILE_STATUSES = (
    "added", "removed", "modified", "renamed", "copied", "changed", "unchanged",
)


class GitHubComparisonError(RuntimeError):
    """GitHub compare не подтвердил полный инкрементальный diff."""


def _to_changed_file(file: dict) -> ChangedFile:
    status = file["status"]
    return ChangedFile(
        path=file["filename"],
        status=status,
        patch=file.get("patch"),
        previous_path=file.get("previous_filename") if status == "renamed" else None,
    )


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
            head_ref=d.get("head", {}).get("ref"),
        )

    def update_pull_request_body(self, number: int, body: str) -> None:
        """Заменить тело PR (обратный линк задачи из finish_task)."""
        self._c.patch(
            f"{self._base()}/pulls/{number}", json={"body": body}
        ).raise_for_status()

    def get_changed_files(self, number: int) -> list[ChangedFile]:
        files, page = [], 1
        while True:
            r = self._c.get(
                f"{self._base()}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            ).raise_for_status()
            batch = r.json()
            files += [_to_changed_file(f) for f in batch]
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
        """Полный список изменённых файлов для подтверждённого сравнения GitHub."""
        r = self._c.get(f"{self._base()}/compare/{base_sha}...{head_sha}").raise_for_status()
        try:
            data = r.json()
        except ValueError as exc:
            raise GitHubComparisonError("GitHub compare вернул некорректный JSON") from exc

        if not isinstance(data, dict):
            raise GitHubComparisonError("GitHub compare вернул некорректный объект")
        status = data.get("status")
        if status in ("behind", "diverged"):
            raise GitHubComparisonError(f"GitHub compare вернул статус {status}")
        if status not in ("ahead", "identical"):
            raise GitHubComparisonError("GitHub compare не подтвердил статус сравнения")

        ahead_by = data.get("ahead_by")
        behind_by = data.get("behind_by")
        if type(ahead_by) is not int or type(behind_by) is not int or behind_by != 0:
            raise GitHubComparisonError("GitHub compare вернул некорректные счётчики коммитов")
        if status == "ahead" and ahead_by <= 0:
            raise GitHubComparisonError("GitHub compare вернул некорректные счётчики коммитов")

        files = data.get("files")
        if not isinstance(files, list):
            raise GitHubComparisonError("GitHub compare не вернул список файлов")
        # GitHub ограничивает весь список 300 записями. Достижение лимита
        # не позволяет подтвердить, что API вернул все изменённые файлы.
        if len(files) >= _COMPARE_FILE_LIMIT:
            raise GitHubComparisonError(f"GitHub compare достиг лимита {_COMPARE_FILE_LIMIT} файлов")
        if status == "identical" and (ahead_by != 0 or files):
            raise GitHubComparisonError("GitHub compare вернул противоречивый статус identical")

        for file in files:
            if (
                not isinstance(file, dict)
                or not isinstance(file.get("filename"), str)
                or not file["filename"]
                or file.get("status") not in _COMPARE_FILE_STATUSES
            ):
                raise GitHubComparisonError("GitHub compare вернул некорректный путь или тип файла")
            for field in ("patch", "previous_filename"):
                value = file.get(field)
                if value is not None and not isinstance(value, str):
                    raise GitHubComparisonError(f"GitHub compare вернул некорректное поле {field}")
        return [_to_changed_file(file) for file in files]

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
