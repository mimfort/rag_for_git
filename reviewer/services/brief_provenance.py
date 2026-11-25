"""Локальная сверка ссылок сохранённого брифа по объектам Git."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile

from reviewer.metrics.brief_quality.briefs import (
    RELEVANT_HEADER,
    TEST_HEADER,
    parse_rendered_citations,
)
from reviewer.services.repo_id import derive_repo_from_remote, normalize_repo

_MARKER = "<!-- reviewer:brief-provenance "
_END = " -->"
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
Anchor = tuple[str, str, int]


class GitCheckout:
    def __init__(self, root: str) -> None:
        self.root = root
        self.env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0"}
        self.blob = lru_cache(maxsize=1024)(self._blob)
        self.lines = lru_cache(maxsize=64)(self._lines)

    def git(self, *args: str) -> bytes | None:
        try:
            result = subprocess.run(
                ["git", "--no-replace-objects", "--literal-pathspecs", "-C", self.root, *args],
                capture_output=True, check=False, env=self.env, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout if result.returncode == 0 else None

    def _object_id(self, ref: str) -> str | None:
        oid = (self.git("rev-parse", "--verify", "--end-of-options", ref) or b"").strip()
        result = oid.decode("ascii", errors="replace")
        return result if _OID.fullmatch(result) else None

    def commit(self, ref: str) -> str | None:
        return self._object_id(f"{ref}^{{commit}}")

    def _blob(self, sha: str, path: str) -> str | None:
        return self._object_id(f"{sha}:{path}")

    def _lines(self, blob: str) -> list[bytes] | None:
        content = self.git("cat-file", "blob", blob)
        return content.splitlines() if content is not None and b"\0" not in content else None

    def detected_repo(self) -> str | None:
        origin = (self.git("remote", "get-url", "origin") or b"").decode(errors="replace")
        return derive_repo_from_remote(origin)

    def dirty_paths(self, paths: list[str]) -> set[str] | None:
        if not paths:
            return set()
        raw = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all",
                       "--no-renames", "--", *paths)
        if raw is None:
            return None
        return {os.fsdecode(entry[3:]) for entry in raw.split(b"\0") if entry}


def _safe_path(path: str) -> bool:
    return not any(part in {"", ".", ".."} for part in path.split("/")) and "\0" not in path


def _anchors(text: str) -> list[Anchor]:
    return [
        (section, path, line)
        for section, header in (("code", RELEVANT_HEADER), ("test_exemplars", TEST_HEADER))
        for path, line in (parse_rendered_citations(text, header) or ())
    ]


def _anchor_digest(anchors: list[Anchor]) -> str:
    content = json.dumps(anchors, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(content.encode("ascii")).hexdigest()


def _unavailable(reason: str) -> dict:
    return {"status": "unavailable", "reason": reason}


def _markers(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(_MARKER)]


def capture(
    git: GitCheckout, text: str, *, repo: str, branch: str, indexed_sha: str,
) -> tuple[dict, str]:
    """Записать один маркер. Секции с цитатами остаются побайтово неизменными."""
    try:
        repo = normalize_repo(repo)
        anchors = _anchors(text)
    except ValueError:
        return _unavailable("invalid_brief_or_repo"), text
    if not anchors:
        return _unavailable("unverified_brief"), text
    if (detected := git.detected_repo()) and detected != repo:
        return _unavailable("repository_mismatch"), text
    if not branch or not _OID.fullmatch(indexed_sha) or git.commit(indexed_sha) != indexed_sha:
        return _unavailable("source_unavailable"), text
    prior = _markers(text)
    if len(prior) > 1 or (prior and not prior[0].endswith(_END)):
        return _unavailable("invalid_provenance"), text

    lines_by_path = defaultdict(list)
    for _, path, line in anchors:
        lines_by_path[path].append(line)
    blobs = {}
    for path, lines in lines_by_path.items():
        blob = git.blob(indexed_sha, path) if _safe_path(path) else None
        if blob and (source := git.lines(blob)) is not None and max(lines) <= len(source):
            blobs[path] = blob

    metadata = {"repo": repo, "branch": branch, "indexed_sha": indexed_sha,
                "blobs": blobs, "anchors_sha256": _anchor_digest(anchors)}
    # Исключить HTML-комментарий внутри комментария и преждевременный закрывающий маркер.
    encoded = json.dumps(metadata, sort_keys=True, ensure_ascii=True).replace("--", "\\u002d\\u002d")
    marker = f"{_MARKER}{encoded}{_END}"
    updated = text.replace(prior[0], marker, 1) if prior else f"{marker}\n{text}"
    return {
        "status": "captured" if blobs else "unavailable",
        "blobs": len(blobs),
        "missing_paths": sorted(lines_by_path.keys() - blobs.keys()),
    }, updated if blobs else text


def _unique_context_position(
    rows: list[bytes], positions: list[int], window: list[bytes], offset: int,
) -> int | None:
    matches = [position for position in positions if position >= offset
               and rows[position - offset:position - offset + len(window)] == window]
    return matches[0] if len(matches) == 1 else None


def _find_repeated_anchor(
    source: list[bytes], target: list[bytes], index: int,
    source_positions: list[int], target_positions: list[int],
) -> int | None:
    for radius in (1, 2):
        start = max(0, index - radius)
        window = source[start:index + radius + 1]
        if not any(row.strip() and row != source[index] for row in window):
            continue
        offset = index - start
        if _unique_context_position(source, source_positions, window, offset) != index:
            continue
        position = _unique_context_position(target, target_positions, window, offset)
        if position is not None:
            return position
    return None


def _find_anchor(source: list[bytes], target: list[bytes], line: int) -> dict:
    """Сдвиг требует единственного совпадения строки или её окружения в обоих снимках."""
    if line > len(source):
        return {"status": "source_unavailable"}
    index = line - 1
    original = source[index]
    target_positions = [i for i, row in enumerate(target) if row == original]
    if not original.strip():
        return {"status": "unchanged" if index in target_positions else "changed"}
    if not target_positions:
        return {"status": "changed"}
    source_positions = [i for i, row in enumerate(source) if row == original]
    if len(source_positions) == len(target_positions) == 1:
        position = target_positions[0]
    else:
        position = _find_repeated_anchor(source, target, index, source_positions, target_positions)
    if position is None:
        return {"status": "ambiguous"}
    if position == index:
        return {"status": "unchanged"}
    return {"status": "moved", "suggested_line": position + 1}


def compare_citations(
    git: GitCheckout, anchors: Iterable[Anchor], blobs: dict[str, str],
    source_sha: str, target_sha: str, dirty: set[str],
) -> list[dict]:
    """Сверить пути для двух закреплённых снимков. Git-чтения кэшируются по пути и блобу."""
    results = []
    for section, path, line in anchors:
        source_blob = blobs.get(path)
        row = {"section": section, "path": path, "line": line}
        if path in dirty:
            verdict = {"status": "working_tree_changed"}
        elif not source_blob or not _safe_path(path) or git.blob(source_sha, path) != source_blob:
            verdict = {"status": "source_unavailable"}
        elif not (target_blob := git.blob(target_sha, path)):
            verdict = {"status": "path_missing"}
        elif source_blob == target_blob:
            verdict = {"status": "unchanged"}
        else:
            source, target = git.lines(source_blob), git.lines(target_blob)
            if source is None:
                verdict = {"status": "source_unavailable"}
            elif target is None:
                verdict = {"status": "target_unavailable"}
            else:
                verdict = _find_anchor(source, target, line)
        results.append({**row, **verdict})
    return results


def revalidate(git: GitCheckout, text: str) -> dict:
    markers = _markers(text)
    if not markers:
        return _unavailable("legacy_brief")
    if len(markers) != 1 or not markers[0].endswith(_END):
        return _unavailable("invalid_provenance")
    try:
        metadata = json.loads(markers[0].removeprefix(_MARKER).removesuffix(_END))
        repo = normalize_repo(metadata["repo"])
        source = metadata["indexed_sha"]
        branch = metadata["branch"]
        blobs = metadata["blobs"]
        digest = metadata["anchors_sha256"]
        anchors = _anchors(text)
        if (not isinstance(branch, str) or not branch or not _OID.fullmatch(source)
            or not all(_OID.fullmatch(blob) for blob in blobs.values())):
            return _unavailable("invalid_provenance")
    except (AttributeError, KeyError, TypeError, ValueError):
        return _unavailable("invalid_provenance")
    if _anchor_digest(anchors) != digest:
        return _unavailable("provenance_mismatch")
    if (detected := git.detected_repo()) and detected != repo:
        return _unavailable("repository_mismatch")
    if git.commit(source) != source:
        return _unavailable("source_unavailable")
    paths = list(dict.fromkeys(path for _, path, _ in anchors if _safe_path(path)))
    target = git.commit("HEAD")
    dirty = git.dirty_paths(paths)
    if target is None or dirty is None or git.commit("HEAD") != target:
        return _unavailable("checkout_unavailable")
    return {
        "status": "checked", "repo": repo, "branch": branch, "source_sha": source,
        "target_sha": target, "citations": compare_citations(git, anchors, blobs, source, target, dirty),
    }


def replace_brief(path: Path, expected: str, updated: str) -> None:
    """Заменить бриф атомарно, сохранив права и правки, сделанные во время проверки Git."""
    with NamedTemporaryFile(dir=path.parent, delete=False) as staging:
        staged = Path(staging.name)
        try:
            staging.write(updated.encode("utf-8"))
            staging.close()
            staged.chmod(path.stat().st_mode & 0o777)
            if path.read_bytes() != expected.encode("utf-8"):
                raise OSError("Бриф изменился во время чтения Git")
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)
