import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from reviewer.mcp.schemas import FindingIn

def normalize_message(message: str) -> str:
    """Нормализовать текст находки для dedup/fingerprint: устранить незначимые
    различия формулировки (регистр, пробелы, пунктуация), чтобы near-duplicate
    схлопывались и повторный прогон не плодил дубли."""
    s = message.casefold()              # юникод-корректный lower
    s = re.sub(r"[^\w\s]", " ", s)      # пунктуация → пробел (\w юникодный: кириллица/латиница/цифры/_)
    s = re.sub(r"\s+", " ", s).strip()  # схлопнуть пробелы + тримминг
    return s


@dataclass
class PullRequest:
    number: int
    base_sha: str
    head_sha: str
    base_ref: str            # напр. "main"
    title: str
    body: str
    draft: bool = False
    head_ref: str | None = None   # имя head-ветки PR; источник ключа задачи (None если недоступно)

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
    code_quote: str | None = None   # дословная цитата строки (для fuzzy snap)
    centrality: float = 0.0         # центральность символа (входящие CALLS); tie-breaker сортировки (PRI-129)

    def fingerprint(self) -> str:
        import hashlib
        key = f"{self.file}|{self.line}|{self.side}|{self.category}|{normalize_message(self.message)}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @classmethod
    def from_in(cls, fi: "FindingIn") -> "Finding":
        """Построить внутренний Finding из валидированного FindingIn (PRI-156).

        Дакт-тайпинг по атрибутам: без runtime-импорта FindingIn (vcs не зависит
        от mcp). centrality стартует с 0.0 (проставляется графом в publish_review).
        """
        fix = fi.fix
        return cls(
            category=fi.category,
            severity=fi.severity,
            file=fi.file,
            line=fi.line,
            side=fi.side,
            message=fi.message,
            suggestion=fi.suggestion,
            confidence=fi.confidence,
            fix_start=fix.start_line if fix else None,
            fix_end=fix.end_line if fix else None,
            replacement=fix.replacement if fix else None,
            code_quote=fi.code_quote,
        )

class VCSProvider(Protocol):
    def get_pull_request(self, number: int) -> PullRequest: ...
    def update_pull_request_body(self, number: int, body: str) -> None: ...
    def get_changed_files(self, number: int) -> list[ChangedFile]: ...
    def get_file_at_ref(self, path: str, ref: str) -> str | None: ...
    def list_existing_fingerprints(self, number: int) -> set[str]: ...
    def publish_review(self, number: int, head_sha: str, summary: str,
                       comments: list[InlineComment]) -> None: ...
    def compare_files(self, base_sha: str, head_sha: str) -> list[ChangedFile]: ...
    def close(self) -> None: ...
