from dataclasses import dataclass
from typing import Literal, Protocol

@dataclass
class PullRequest:
    number: int
    base_sha: str
    head_sha: str
    base_ref: str            # напр. "main"
    title: str
    body: str
    draft: bool = False

@dataclass
class ChangedFile:
    path: str
    status: str              # added/removed/modified/renamed
    patch: str | None        # unified diff; None для слишком больших файлов

@dataclass
class InlineComment:
    path: str
    line: int
    side: Literal["RIGHT", "LEFT"]
    body: str
    start_line: int | None = None
    start_side: Literal["RIGHT", "LEFT"] | None = None

@dataclass
class Finding:
    category: str            # correctness/security/performance/style/...
    severity: Literal["low", "medium", "high", "critical"]
    file: str
    line: int | None
    side: Literal["RIGHT", "LEFT"]
    message: str
    suggestion: str | None
    confidence: float        # 0..1
    # опц. applyable-правка: точная замена непрерывного диапазона строк НОВОЙ версии (RIGHT)
    fix_start: int | None = None
    fix_end: int | None = None
    replacement: str | None = None

    def fingerprint(self) -> str:
        import hashlib
        key = f"{self.file}|{self.line}|{self.side}|{self.category}|{self.message}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

class VCSProvider(Protocol):
    def get_pull_request(self, number: int) -> PullRequest: ...
    def get_changed_files(self, number: int) -> list[ChangedFile]: ...
    def get_file_at_ref(self, path: str, ref: str) -> str | None: ...
    def list_existing_fingerprints(self, number: int) -> set[str]: ...
    def publish_review(self, number: int, head_sha: str, summary: str,
                       comments: list[InlineComment]) -> None: ...
    def compare_files(self, base_sha: str, head_sha: str) -> list[ChangedFile]: ...
    def close(self) -> None: ...
