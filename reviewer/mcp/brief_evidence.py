"""Short-lived evidence for verifying solve-task citations across MCP clients."""

from __future__ import annotations

import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from html import escape
from threading import Lock

from reviewer.mcp.schemas import BriefCitationIn, BriefReferenceIn
from reviewer.metrics.brief_quality import briefs

_HEADERS = {"code": briefs.RELEVANT_HEADER, "test_exemplars": briefs.TEST_HEADER}
_ROLE_LABELS = {"edit": "Править: ", "read": "Прочитать: "}
_HEADER = re.compile(r"^// .+ \((?P<path>.+):(?P<first>\d+)-(?P<last>\d+)\)$")
_NUMBERED = re.compile(r"^\s*(\d+) \|")
_BACKTICKS = re.compile(r"`+")
_TRUNCATED = "[...truncated]"


def _visible_lines(text: str) -> dict[str, set[int]]:
    """Index actual numbered rows. A header may advertise more than was rendered."""
    lines = text.split("\n")
    if _TRUNCATED in lines:
        # as_context may cut the preceding row and append notes after the marker.
        lines = lines[:max(0, lines.index(_TRUNCATED) - 1)]
    visible: dict[str, set[int]] = {}
    path = None
    first = last = 0
    for line in lines:
        if header := _HEADER.fullmatch(line):
            path = header["path"]
            first, last = int(header["first"]), int(header["last"])
            continue
        if path is not None and (row := _NUMBERED.match(line)):
            number = int(row[1])
            if first <= number <= last:
                visible.setdefault(path, set()).add(number)
        else:
            path = None
    return visible


def _render_reference(reference: BriefReferenceIn) -> str:
    label = f"{reference.path}:{reference.line}"
    if label.splitlines() != [label] or "\x00" in label:
        raise ValueError("A citation must fit on one line")
    fence = "`" * (1 + max((len(run) for run in _BACKTICKS.findall(label)), default=0))
    # CommonMark removes one padding space on each side, keeping the path intact.
    if label.startswith(("`", " ")):
        label = f" {label} "
    role = _ROLE_LABELS[reference.role] if reference.role else ""
    return f"- {fence}{label}{fence} — {role}{escape(reference.reason.strip(), quote=False)}"


def _render_references(references: list[BriefReferenceIn]) -> str:
    groups = {
        header: [reference for reference in references if reference.section == section]
        for section, header in _HEADERS.items()
    }
    if not groups[briefs.TEST_HEADER]:
        del groups[briefs.TEST_HEADER]
    sections = []
    for header, group in groups.items():
        rows = [header, briefs.VERIFIED_CITATIONS_MARKER]
        if group:
            rows.extend(["", *map(_render_reference, group)])
        sections.append("\n".join(rows))
    markdown = "\n\n".join(sections) + "\n"
    for header, group in groups.items():
        expected = [(reference.path, reference.line) for reference in group]
        if briefs.parse_rendered_citations(markdown, header) != expected:
            raise ValueError("Rendered citations differ from verified references")
    return markdown


class BriefEvidence:
    """Bounded process-local index of what the user received, without source text."""

    def __init__(
        self, *, limit: int = 64, ttl_seconds: int = 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = Lock()
        self._entries: OrderedDict[str, tuple[float, dict[str, dict[str, set[int]]]]] = (
            OrderedDict()
        )

    def save(self, payload: Mapping[str, object]) -> str | None:
        sections: dict[str, dict[str, set[int]]] = {}
        for name in _HEADERS:
            text = payload.get(name)
            if isinstance(text, str) and (visible := _visible_lines(text)):
                sections[name] = visible
        if not sections:
            return None
        key = secrets.token_urlsafe(16)
        with self._lock:
            self._entries[key] = (self._clock() + self._ttl, sections)
            if len(self._entries) > self._limit:
                self._entries.popitem(last=False)
        return key

    def verify(self, key: str, citations: Sequence[BriefCitationIn]) -> dict:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return {"status": "evidence_unavailable", "citations": []}
            expires, sections = entry
            if self._clock() >= expires:
                del self._entries[key]
                return {"status": "evidence_unavailable", "citations": []}
        checked = []
        for citation in citations:
            paths = sections.get(citation.section)
            if paths is None:
                status = "section_unavailable"
            elif citation.path not in paths:
                status = "path_unseen"
            elif citation.line not in paths[citation.path]:
                status = "outside_excerpt"
            else:
                status = "verified"
            checked.append({**citation.model_dump(), "status": status})
        return {"status": "ok", "citations": checked}

    def verify_references(self, evidence_id: str, references: list[BriefReferenceIn]) -> dict:
        checked = self.verify(evidence_id, references)
        if checked["status"] != "ok":
            return checked
        verified = [
            reference for reference, verdict in zip(references, checked["citations"], strict=True)
            if verdict["status"] == "verified"
        ]
        try:
            if any(
                not reason.strip() or reason.splitlines() != [reason] or "\x00" in reason
                for reason in (reference.reason for reference in references)
            ):
                raise ValueError("A reference reason must be a non-empty single line")
            markdown = _render_references(verified)
        except ValueError:
            return {**checked, "render_status": "invalid_format", "markdown": None}
        return {**checked, "render_status": "ok" if verified else "empty", "markdown": markdown}
