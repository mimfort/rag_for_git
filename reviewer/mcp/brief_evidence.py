"""Short-lived evidence for verifying solve-task citations across MCP clients."""

from __future__ import annotations

import logging
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from html import escape
from pathlib import Path
from typing import NamedTuple

from pydantic import TypeAdapter, ValidationError

from reviewer.mcp.schemas import BriefCitationIn, BriefReferenceIn
from reviewer.metrics.brief_quality import briefs

log = logging.getLogger(__name__)

_HEADERS = {"code": briefs.RELEVANT_HEADER, "test_exemplars": briefs.TEST_HEADER}
_ROLE_LABELS = {"edit": "Править: ", "read": "Прочитать: "}
_HEADER = re.compile(r"^// (?P<node_id>.+) \((?P<path>.+):(?P<first>\d+)-(?P<last>\d+)\)$")
_NUMBERED = re.compile(r"^\s*(\d+) \| (.*)$")
_BACKTICKS = re.compile(r"`+")
_TRUNCATED = "[...truncated]"
_GIT_EVIDENCE_PREFIX = "git1."  # Точка отсутствует в ранее выдаваемых token_urlsafe ID.
_MAX_DB_BYTES = 16 * 1024 * 1024
BriefSections = dict[str, dict[str, set[int]]]
BriefAliases = dict[str, dict[str, tuple[str, set[int]]]]


@dataclass(frozen=True)
class _StoredEvidence:
    sections: BriefSections
    aliases: BriefAliases = field(default_factory=dict)


_STORED_EVIDENCE = TypeAdapter(_StoredEvidence | BriefSections)
_SCHEMA = """CREATE TABLE IF NOT EXISTS brief_evidence (
    id_hash BLOB NOT NULL UNIQUE, expires_at REAL NOT NULL, sections BLOB NOT NULL
)"""


class _VisibleExcerpt(NamedTuple):
    node_id: str
    path: str
    first: int
    rows: list[tuple[int, str]]


def _visible_excerpts(text: str) -> list[_VisibleExcerpt]:
    """Идентификаторы и полностью показанные строки фрагментов."""
    lines = text.split("\n")
    if _TRUNCATED in lines:
        # as_context may cut the preceding row and append notes after the marker.
        lines = lines[:max(0, lines.index(_TRUNCATED) - 1)]
    blocks: list[_VisibleExcerpt] = []
    rows: list[tuple[int, str]] | None = None
    first = last = 0
    for line in lines:
        if header := _HEADER.fullmatch(line):
            first, last = int(header["first"]), int(header["last"])
            rows = []
            blocks.append(_VisibleExcerpt(header["node_id"], header["path"], first, rows))
            continue
        if rows is not None and (row := _NUMBERED.match(line)):
            number = int(row[1])
            if first <= number <= last:
                rows.append((number, row[2]))
        else:
            rows = None
    return [block for block in blocks if block.rows]


def _visible_blocks(text: str) -> list[tuple[str, int, list[tuple[int, str]]]]:
    """Пути, начальные номера и полностью показанные строки фрагментов."""
    return [(block.path, block.first, block.rows) for block in _visible_excerpts(text)]


def _visible_lines(text: str) -> dict[str, set[int]]:
    """Index actual numbered rows. A header may advertise more than was rendered."""
    visible: dict[str, set[int]] = {}
    for _, path, _, rows in _visible_excerpts(text):
        visible.setdefault(path, set()).update(number for number, _ in rows)
    return visible


def _is_single_line(text: str) -> bool:
    return text.splitlines() == [text] and "\x00" not in text


def _render_reference(reference: BriefReferenceIn) -> str:
    label = f"{reference.path}:{reference.line}"
    if not _is_single_line(label):
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
    """Ограниченное локальное хранилище выданных координат без текста кода."""

    def __init__(
        self, *, limit: int = 1024, ttl_seconds: int = 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._limit = limit
        self._ttl = ttl_seconds
        self._clock = clock
        self._state_home = Path(os.environ.get("XDG_STATE_HOME", ""))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        state_home = self._state_home
        if not state_home.is_absolute():
            state_home = Path.home() / ".local" / "state"
        path = state_home / "rag-reviewer" / "brief-evidence" / "entries.sqlite3"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with closing(sqlite3.connect(path, timeout=1.0)) as db, db:
            db.execute(_SCHEMA)
            yield db

    def save(self, payload: Mapping[str, object]) -> str | None:
        sections: dict[str, dict[str, set[int]]] = {}
        for name in _HEADERS:
            text = payload.get(name)
            if isinstance(text, str) and (visible := _visible_lines(text)):
                sections[name] = visible
        return self.save_sections(sections)

    def save_sections(self, sections: BriefSections, *, aliases: BriefAliases | None = None) -> str | None:
        if not sections or self._limit <= 0:
            return None
        key = _GIT_EVIDENCE_PREFIX + secrets.token_urlsafe(16)
        now = self._clock()
        try:
            with self._connect() as db:
                page_size, = db.execute("PRAGMA page_size").fetchone()
                db.execute(f"PRAGMA max_page_count = {_MAX_DB_BYTES // page_size}")
                db.execute("PRAGMA secure_delete = ON")
                db.execute("DELETE FROM brief_evidence WHERE expires_at <= ?", (now,))
                stored = _StoredEvidence(sections, aliases) if aliases else sections
                db.execute(
                    "INSERT INTO brief_evidence (id_hash, expires_at, sections) VALUES (?, ?, ?)",
                    (sha256(key.encode()).digest(), now + self._ttl, _STORED_EVIDENCE.dump_json(stored)),
                )
                db.execute("""
                    DELETE FROM brief_evidence WHERE rowid IN (
                        SELECT rowid FROM brief_evidence ORDER BY rowid DESC LIMIT -1 OFFSET ?
                    )
                """, (self._limit,))
        except (OSError, RuntimeError, sqlite3.Error) as error:
            log.warning("Не удалось сохранить данные проверки брифа (%s)", type(error).__name__)
            return None
        return key

    def verify(self, key: str, citations: Sequence[BriefCitationIn]) -> dict:
        return self._verify(key, citations, resolve_aliases=False)

    def _verify(
        self, key: str, citations: Sequence[BriefCitationIn], *, resolve_aliases: bool,
    ) -> dict:
        if not key.startswith(_GIT_EVIDENCE_PREFIX) or (stored := self._load(key)) is None:
            return {"status": "evidence_unavailable", "citations": []}
        checked = []
        for citation in citations:
            path = citation.path
            paths = stored.sections.get(citation.section)
            if resolve_aliases and paths is not None and path not in paths:
                match = stored.aliases.get(citation.section, {}).get(path)
                if match and citation.line in match[1]:
                    path = match[0]
            if paths is None:
                status = "section_unavailable"
            elif path not in paths:
                status = "path_unseen"
            elif citation.line not in paths[path]:
                status = "outside_excerpt"
            else:
                status = "verified"
            checked.append({**citation.model_dump(), "path": path, "status": status})
        return {"status": "ok", "citations": checked}

    def _load(self, key: str) -> _StoredEvidence | None:
        try:
            with self._connect() as db:
                entry = db.execute(
                    "SELECT sections FROM brief_evidence WHERE id_hash = ? AND expires_at > ?",
                    (sha256(key.encode()).digest(), self._clock()),
                ).fetchone()
            if not entry:
                return None
            stored = _STORED_EVIDENCE.validate_json(entry[0])
            return _StoredEvidence(stored) if isinstance(stored, dict) else stored
        except (OSError, RuntimeError, sqlite3.Error, UnicodeEncodeError, ValidationError) as error:
            log.warning("Не удалось загрузить данные проверки брифа (%s)", type(error).__name__)
            return None

    def verify_references(self, evidence_id: str, references: list[BriefReferenceIn]) -> dict:
        checked = self._verify(evidence_id, references, resolve_aliases=True)
        if checked["status"] != "ok":
            return checked
        renderable = []
        malformed = False
        for reference, verdict in zip(references, checked["citations"], strict=True):
            if verdict["status"] != "verified":
                continue
            if verdict["path"] != reference.path:
                reference = reference.model_copy(update={"path": verdict["path"]})
            if not _is_single_line(reference.path):
                verdict["status"] = "invalid_path"
            elif not reference.reason.strip() or not _is_single_line(reference.reason):
                verdict["status"] = "invalid_reason"
            else:
                renderable.append(reference)
                continue
            malformed = True

        failure = {**checked, "render_status": "invalid_format", "markdown": None}
        if malformed and not renderable:
            return failure
        try:
            markdown = _render_references(renderable)
        except ValueError:
            return failure
        return {**checked, "render_status": "ok" if renderable else "empty", "markdown": markdown}
