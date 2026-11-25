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
from hashlib import sha256
from html import escape
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from reviewer.mcp.schemas import BriefCitationIn, BriefReferenceIn
from reviewer.metrics.brief_quality import briefs

log = logging.getLogger(__name__)

_HEADERS = {"code": briefs.RELEVANT_HEADER, "test_exemplars": briefs.TEST_HEADER}
_ROLE_LABELS = {"edit": "Править: ", "read": "Прочитать: "}
_HEADER = re.compile(r"^// .+ \((?P<path>.+):(?P<first>\d+)-(?P<last>\d+)\)$")
_NUMBERED = re.compile(r"^\s*(\d+) \| (.*)$")
_BACKTICKS = re.compile(r"`+")
_TRUNCATED = "[...truncated]"
_GIT_EVIDENCE_PREFIX = "git1."  # Точка отсутствует в ранее выдаваемых token_urlsafe ID.
_MAX_DB_BYTES = 16 * 1024 * 1024
_SECTIONS = TypeAdapter(dict[str, dict[str, set[int]]])
_SCHEMA = """CREATE TABLE IF NOT EXISTS brief_evidence (
    id_hash BLOB NOT NULL UNIQUE, expires_at REAL NOT NULL, sections BLOB NOT NULL
)"""


def _visible_blocks(text: str) -> list[tuple[str, int, list[tuple[int, str]]]]:
    """Пути, начальные номера и полностью показанные строки фрагментов."""
    lines = text.split("\n")
    if _TRUNCATED in lines:
        # as_context may cut the preceding row and append notes after the marker.
        lines = lines[:max(0, lines.index(_TRUNCATED) - 1)]
    blocks: list[tuple[str, int, list[tuple[int, str]]]] = []
    rows: list[tuple[int, str]] | None = None
    first = last = 0
    for line in lines:
        if header := _HEADER.fullmatch(line):
            first, last = int(header["first"]), int(header["last"])
            rows = []
            blocks.append((header["path"], first, rows))
            continue
        if rows is not None and (row := _NUMBERED.match(line)):
            number = int(row[1])
            if first <= number <= last:
                rows.append((number, row[2]))
        else:
            rows = None
    return [block for block in blocks if block[2]]


def _visible_lines(text: str) -> dict[str, set[int]]:
    """Index actual numbered rows. A header may advertise more than was rendered."""
    visible: dict[str, set[int]] = {}
    for path, _, rows in _visible_blocks(text):
        visible.setdefault(path, set()).update(number for number, _ in rows)
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

    def save_sections(self, sections: dict[str, dict[str, set[int]]]) -> str | None:
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
                db.execute(
                    "INSERT INTO brief_evidence (id_hash, expires_at, sections) VALUES (?, ?, ?)",
                    (sha256(key.encode()).digest(), now + self._ttl, _SECTIONS.dump_json(sections)),
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
        if not key.startswith(_GIT_EVIDENCE_PREFIX) or (sections := self._load(key)) is None:
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

    def _load(self, key: str) -> dict[str, dict[str, set[int]]] | None:
        try:
            with self._connect() as db:
                entry = db.execute(
                    "SELECT sections FROM brief_evidence WHERE id_hash = ? AND expires_at > ?",
                    (sha256(key.encode()).digest(), self._clock()),
                ).fetchone()
            return _SECTIONS.validate_json(entry[0]) if entry else None
        except (OSError, RuntimeError, sqlite3.Error, UnicodeEncodeError, ValidationError) as error:
            log.warning("Не удалось загрузить данные проверки брифа (%s)", type(error).__name__)
            return None

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
