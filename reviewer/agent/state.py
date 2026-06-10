from __future__ import annotations
import operator
from dataclasses import dataclass
from typing import Annotated, Protocol
from typing_extensions import NotRequired, TypedDict

from reviewer.config.settings import Settings
from reviewer.vcs.base import Finding, InlineComment, VCSProvider


class RetrieverLike(Protocol):
    def retrieve(self, query, changed_node_ids, overlay_ref, changed_paths,
                 top_k=15, candidates=50): ...


class GraphLike(Protocol):
    def expand(self, node_ids: list[str], hops: int = 2) -> set[str]: ...
    def find_symbol(self, name: str) -> list[str]: ...
    def callers(self, node_ids: list[str]) -> set[str]: ...


class PolicyLike(Protocol):
    def gate(self, finding) -> bool: ...
    @property
    def max_comments(self) -> int: ...


class SynthesizerLike(Protocol):
    def synthesize(self, findings: list[Finding], deps: Deps) -> list[Finding]: ...


class UsageLike(Protocol):
    def add(self, stage: str, message) -> None: ...
    def report(self) -> str: ...
    @property
    def total_tokens(self) -> int: ...


class TraceLike(Protocol):
    def record_prompt(self, stage: str, unit: str, text: str) -> None: ...
    def record_llm_call(self, stage: str, unit: str, message) -> None: ...
    def record_tool_call(self, stage: str, unit: str, name: str,
                         args: dict, result) -> None: ...


class VerdictsLike(Protocol):
    def log_verdict(self, finding: Finding, is_real: bool,
                    source: str = "agentic") -> None: ...
    def log_published(self, finding: Finding, inline: bool) -> None: ...


@dataclass
class ReviewUnit:
    path: str
    node_ids: list[str]
    changed_text: str
    new_source: str = ""        # полная новая версия файла (для точных fix-диапазонов)

@dataclass
class Deps:
    vcs: VCSProvider | None
    retriever: RetrieverLike | None
    graph: GraphLike | None
    policy: PolicyLike | None
    analyzer: UnitAnalyzer
    verifier: Verifier
    pr_number: int
    head_sha: str
    overlay_ref: str
    changed_paths: list[str]
    patches: dict[str, str | None]
    suggestions_mode: str = "apply"   # apply | text
    pr_title: str = ""
    pr_body: str = ""
    changed_status: dict | None = None
    synthesizer: SynthesizerLike | None = None
    sources: dict[str, str] | None = None
    usage: UsageLike | None = None
    trace: TraceLike | None = None
    verdicts: VerdictsLike | None = None
    skipped_paths: list[str] | None = None
    tool_cache: dict | None = None
    settings: Settings | None = None

class UnitAnalyzer(Protocol):
    def analyze(self, unit: ReviewUnit, deps: "Deps") -> list[Finding]: ...

class Verifier(Protocol):
    def verify(self, findings: list[Finding], deps: "Deps") -> list[Finding]: ...

class ReviewState(TypedDict):
    review_units: list[ReviewUnit]
    findings: Annotated[list[Finding], operator.add]
    failed_units: Annotated[list[str], operator.add]
    verified: list[Finding]
    summary: str
    inline_comments: list[InlineComment]
    published: NotRequired[bool | None]
