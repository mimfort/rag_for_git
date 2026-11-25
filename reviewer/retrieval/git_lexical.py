"""BM25 по текстовым блобам Git для секции code (PRI-298)."""
from __future__ import annotations

import io
import math
import os
import re
import subprocess
from collections import Counter
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import monotonic

from reviewer.index.pathfilter import is_ignored
from reviewer.index.store import Retrieved
from reviewer.retrieval.retriever import _is_test_path

MAX_GIT_FILES = 3
_MAX_TREE_BYTES = 4 * 1024 * 1024
_MAX_BLOB_BYTES = 256 * 1024
_MAX_TOTAL_BLOB_BYTES = 8 * 1024 * 1024
_MAX_BLOBS = 512
_MAX_QUERY_CHARS = 16 * 1024
_GIT_TIMEOUT = 3.0
_CONFIG_SUFFIXES = (".sql", ".yml", ".yaml", ".toml")
_EXCLUDED_PREFIXES = ("eval/", "docs/superpowers/")
_WORD = re.compile(r"[^\W_]{2,}")
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_REGULAR_MODES = {b"100644", b"100755"}
_K1 = 1.2
_B = 0.75


@dataclass(frozen=True)
class _Document:
    path: str
    text: str
    frequencies: dict[str, int]
    length: int


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class _Git:
    """Локальные операции Git с общим сроком выполнения."""

    def __init__(self, repo_path: str | os.PathLike[str]) -> None:
        self.command = ["git", "--no-replace-objects", "-C", os.fspath(repo_path)]
        self.env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""}
        # Эти режимы меняют интерпретацию явно заданного :(top,literal).
        for name in ("GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS",
                     "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"):
            self.env.pop(name, None)
        self.deadline = monotonic() + _GIT_TIMEOUT

    def _remaining(self) -> float:
        return max(0, self.deadline - monotonic())

    def _run(self, *args: str, input_data: bytes | None = None) -> bytes:
        return subprocess.run(
            [*self.command, *args], input=input_data, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=self.env, timeout=self._remaining(), check=True,
        ).stdout

    def resolve(self, ref: str) -> str:
        oid = self._run("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        return oid.decode("ascii").strip()

    def tree(self, sha: str, paths: Sequence[str] = ()) -> bytes:
        """Ограничить объём дерева, не запрашивая блобы из него."""
        if not _SHA.fullmatch(sha):
            raise OSError("Git tree requires a full commit SHA")
        args = [*self.command, "ls-tree", "-z", "--full-tree"]
        args.append("-r")
        args.append(sha)
        if paths:
            args.extend(["--", *(f":(top,literal){path}" for path in paths)])
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              env=self.env) as proc, ThreadPoolExecutor(max_workers=1) as worker:
            stdout = proc.stdout
            assert stdout is not None
            try:
                read = worker.submit(stdout.read, _MAX_TREE_BYTES + 1)
                listing = read.result(timeout=self._remaining())
                if len(listing) > _MAX_TREE_BYTES:
                    raise OSError("Git tree exceeds lexical search bounds")
                if proc.wait(timeout=self._remaining()):
                    raise subprocess.CalledProcessError(proc.returncode, args)
                return listing
            except TimeoutError as exc:
                raise subprocess.TimeoutExpired(args, _GIT_TIMEOUT) from exc
            finally:
                if proc.poll() is None:
                    proc.kill()

    def cat_file(self, mode: str, blobs: Sequence[bytes]) -> bytes:
        return self._run("cat-file", mode, input_data=b"".join(oid + b"\n" for oid in blobs))


def _entries(tree: bytes) -> Iterator[tuple[bytes, bytes, bytes, bytes]]:
    for entry in tree.split(b"\0"):
        if entry:
            meta, _, raw_path = entry.partition(b"\t")
            mode, kind, blob_id = meta.split()
            yield mode, kind, blob_id, raw_path


def _candidate_blobs(tree: bytes, ignore: list[str], wide: bool) -> list[tuple[str, bytes]]:
    picked = []
    for mode, kind, blob_id, raw_path in _entries(tree):
        if mode not in _REGULAR_MODES or kind != b"blob":
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if ("\n" in path or "\r" in path or path.startswith(_EXCLUDED_PREFIXES)
                or _is_test_path(path) or is_ignored(path, ignore)):
            continue
        suffix_path = path.lower()
        is_markdown = suffix_path.endswith(".md") and (wide or path.startswith("plugin/skills/"))
        if not is_markdown and not suffix_path.endswith(_CONFIG_SUFFIXES):
            continue
        picked.append((path, blob_id))
        if len(picked) >= _MAX_BLOBS:
            break
    return picked


def _read_blobs(
    git: _Git, blobs: list[tuple[str, bytes]], *, skip_missing: bool = False,
) -> Iterator[tuple[str, bytes]]:
    if not blobs:
        return
    metadata = git.cat_file("--batch-check", [blob_id for _, blob_id in blobs]).splitlines()
    if len(metadata) != len(blobs):
        raise OSError("Git batch metadata is incomplete")
    picked = []
    remaining = _MAX_TOTAL_BLOB_BYTES
    for (path, blob_id), row in zip(blobs, metadata, strict=True):
        fields = row.split()
        if skip_missing and fields == [blob_id, b"missing"]:
            continue
        if len(fields) != 3 or fields[:2] != [blob_id, b"blob"] or not fields[2].isdigit():
            raise OSError("Git batch blob is missing or has an unexpected type")
        size = int(fields[2])
        if size <= min(_MAX_BLOB_BYTES, remaining):
            picked.append((path, blob_id, size))
            remaining -= size
    if not picked:
        return
    batch = io.BytesIO(git.cat_file("--batch", [blob_id for _, blob_id, _ in picked]))
    for path, blob_id, size in picked:
        if batch.readline().split() != [blob_id, b"blob", str(size).encode()]:
            raise OSError("Git batch blob is missing or has an unexpected size")
        raw = batch.read(size)
        if len(raw) != size or batch.read(1) != b"\n":
            raise OSError("Git batch blob is incomplete")
        yield path, raw


def _read_documents(git: _Git, blobs: list[tuple[str, bytes]], terms: list[str]) -> list[_Document]:
    documents = []
    for path, raw in _read_blobs(git, blobs):
        if not raw or b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        path_words = _words(path)
        words = _words(text)
        counts = Counter(words)
        counts.update(path_words * 2)
        frequencies = {term: counts[term] for term in terms if counts[term]}
        documents.append(_Document(path, text, frequencies, len(words) + 2 * len(path_words)))
    return documents


def _rank(documents: list[_Document]) -> list[tuple[float, _Document]]:
    if not documents:
        return []
    size = len(documents)
    average = sum(doc.length for doc in documents) / size
    document_frequency = Counter(term for doc in documents for term in doc.frequencies)
    idf = {term: math.log1p((size - freq + 0.5) / (freq + 0.5))
           for term, freq in document_frequency.items()}
    ranked = []
    for doc in documents:
        if doc.frequencies:
            normalization = _K1 * (1 - _B + _B * doc.length / average)
            score = sum(idf[term] * frequency * (_K1 + 1) / (frequency + normalization)
                        for term, frequency in doc.frequencies.items())
            ranked.append((score, doc))
    return sorted(ranked, key=lambda hit: (-hit[0], hit[1].path))


def _excerpt(text: str, terms: set[str], chars: int) -> tuple[str, int, int] | None:
    lines = text.replace("\r\n", "\n").removesuffix("\n").split("\n")
    anchor = max(
        (i for i, line in enumerate(lines) if len(line) <= chars and line.strip()),
        key=lambda i: len(terms.intersection(_words(lines[i]))),
        default=None,
    )
    if anchor is None:
        return None
    first = last = anchor
    remaining = chars - len(lines[anchor])
    while remaining > 0:
        before = (first, last)
        if last + 1 < len(lines) and len(lines[last + 1]) + 1 <= remaining:
            last += 1
            remaining -= len(lines[last]) + 1
        if first > 0 and len(lines[first - 1]) + 1 <= remaining:
            first -= 1
            remaining -= len(lines[first]) + 1
        if before == (first, last):
            break
    return "\n".join(lines[first:last + 1]), first + 1, last + 1


def resolve_git_commit(repo_path: str | os.PathLike[str], ref: str) -> str:
    """Закрепить снимок до чтения политики и поиска."""
    return _Git(repo_path).resolve(ref)


def read_git_blob(repo_path: str | os.PathLike[str], sha: str, path: str) -> str | None:
    """Прочитать текстовый файл по SHA. None означает отсутствие пути."""
    git = _Git(repo_path)
    entry = next(_entries(git.tree(sha, [path])), None)
    if entry is None:
        return None
    mode, kind, blob_id, raw_path = entry
    if raw_path != path.encode("utf-8") or mode not in _REGULAR_MODES or kind != b"blob":
        raise OSError("Git path is not a regular blob")
    blob = next(_read_blobs(git, [(path, blob_id)]), None)
    if blob is None or b"\0" in blob[1]:
        raise OSError("Git blob is too large or binary")
    try:
        return blob[1].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OSError("Git blob is not UTF-8 text") from exc


def read_git_blobs(
    repo_path: str | os.PathLike[str], sha: str, paths: Sequence[str],
) -> dict[str, str]:
    """Прочитать конкретные UTF-8 файлы с одним общим лимитом времени и объёма."""
    paths = tuple(dict.fromkeys(paths))[:_MAX_BLOBS]
    if not paths:
        return {}
    git = _Git(repo_path)
    requested = {path.encode("utf-8"): path for path in paths}
    blobs = [(requested[raw_path], blob_id)
             for mode, kind, blob_id, raw_path in _entries(git.tree(sha, paths))
             if raw_path in requested and mode in _REGULAR_MODES and kind == b"blob"]
    text = {}
    for path, raw in _read_blobs(git, blobs, skip_missing=True):
        if b"\0" in raw:
            continue
        try:
            text[path] = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return text


def search_git(
    repo_path: str | os.PathLike[str], ref: str, query: str, *,
    ignore: list[str], limit: int, chars_per_file: int, wide: bool = False,
) -> list[Retrieved]:
    """Найти фрагменты на одном коммите, закреплённом до чтения дерева."""
    if limit <= 0 or chars_per_file <= 0:
        return []
    terms = sorted(set(_words(query[:_MAX_QUERY_CHARS])))
    if not terms:
        return []
    git = _Git(repo_path)
    sha = git.resolve(ref)
    documents = _read_documents(git, _candidate_blobs(git.tree(sha), ignore, wide), terms)
    query_terms = set(terms)
    results = []
    for score, doc in _rank(documents):
        fragment = _excerpt(doc.text, query_terms, chars_per_file)
        if fragment is None:
            continue
        text, start_line, end_line = fragment
        results.append(Retrieved(
            node_id=f"{doc.path}#git-lexical@{sha}", path=doc.path, symbol_fqn="",
            kind="git-lexical", start_line=start_line, end_line=end_line, text=text,
            score=score, bm25_hit=True,
        ))
        if len(results) >= min(limit, MAX_GIT_FILES):
            break
    return results
