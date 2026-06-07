from __future__ import annotations
import operator
from dataclasses import dataclass
from typing import Annotated, Protocol
from typing_extensions import TypedDict

from reviewer.vcs.base import Finding, InlineComment

@dataclass
class ReviewUnit:
    path: str
    node_ids: list[str]
    changed_text: str
    new_source: str = ""        # полная новая версия файла (для точных fix-диапазонов)

@dataclass
class Deps:
    vcs: object
    retriever: object
    graph: object
    policy: object
    analyzer: "UnitAnalyzer"
    verifier: "Verifier"
    pr_number: int
    head_sha: str
    overlay_ref: str
    changed_paths: list[str]
    patches: dict[str, str | None]
    suggestions_mode: str = "apply"   # apply | text
    pr_title: str = ""
    pr_body: str = ""
    changed_status: dict | None = None
    synthesizer: object = None        # LLMSynthesizer (Task 6); None = узел выключен

class UnitAnalyzer(Protocol):
    def analyze(self, unit: ReviewUnit, deps: "Deps") -> list[Finding]: ...

class Verifier(Protocol):
    def verify(self, findings: list[Finding], deps: "Deps") -> list[Finding]: ...

class ReviewState(TypedDict):
    review_units: list[ReviewUnit]
    findings: Annotated[list[Finding], operator.add]
    verified: list[Finding]
    summary: str
    inline_comments: list[InlineComment]
