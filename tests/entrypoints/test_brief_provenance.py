"""Сверка ссылок сохранённого брифа на реальных объектах Git."""
from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from reviewer.entrypoints.cli import cli


def git(root, *args):
    return subprocess.check_output(["git", "-C", root, *args], text=True).strip()


def commit(root, message):
    git(root, "add", "-A")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "dev")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.test")
    git(root, "remote", "add", "origin", "https://github.com/owner/repo.git")
    files = {
        "move.py": "move target\nold tail\n",
        "stable.py": "keep this line\n",
        "duplicate.py": "old prefix\nduplicate target\n",
        "changed.py": "edited target\n",
        "missing.py": "removed target\n",
        "dirty.py": "staged target\n",
        "repeat.py": "repeat line\nrepeat line\n",
        "same.py": "same line\nold tail\n",
    }
    for path, text in files.items():
        (root / path).write_text(text)
    return root, commit(root, "source")


def write_brief(path, *, marker=True):
    rows = [
        "## Relevant code", "<!-- reviewer:verified-citations -->" if marker else "",
        "- `move.py:1` — move", "- `stable.py:1` — keep",
        "- `duplicate.py:2` — duplicates", "- `changed.py:1` — edit",
        "- `missing.py:1` — delete", "- `dirty.py:1` — working copy",
        "- `repeat.py:1` — repeated source", "- `same.py:1` — same line",
        "## Test exemplars", "<!-- reviewer:verified-citations -->" if marker else "",
        "- `stable.py:1` — example",
        "## Constraints / open questions", "Other text.",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def invoke(root, path, *args):
    result = CliRunner().invoke(cli, [
        "brief-provenance", str(path), "--path", str(root), *args,
    ])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_capture_and_revalidate_refs(repository, tmp_path):
    root, source = repository
    brief = tmp_path / "brief.md"
    write_brief(brief)
    original = brief.read_bytes()
    captured = invoke(root, brief, "--indexed-sha", source, "--repo", "owner/repo", "--branch", "dev")
    assert captured["status"] == "captured"
    assert captured["blobs"] == 8
    stamped = brief.read_bytes()
    assert stamped.split(b"\n", 1)[1] == original
    assert stamped.count(b"<!-- reviewer:verified-citations -->") == 2
    assert git(root, "rev-parse", f"{source}:move.py").encode() in stamped
    assert invoke(root, brief, "--indexed-sha", source, "--repo", "owner/repo", "--branch", "dev") == captured
    assert brief.read_bytes() == stamped

    (root / "move.py").write_text("inserted\nmove target\nold tail\n")
    (root / "duplicate.py").write_text("new prefix\nchanged\nduplicate target\nduplicate target\n")
    (root / "changed.py").write_text("new content\n")
    (root / "missing.py").unlink()
    (root / "same.py").write_text("same line\nnew tail\n")
    (root / "repeat.py").write_text("inserted\nrepeat line\nother line\n")
    target = commit(root, "target")
    (root / "dirty.py").write_text("changed working tree\n")
    git(root, "add", "dirty.py")

    result = invoke(root, brief)
    assert result["source_sha"] == source
    assert result["target_sha"] == target
    rows = {(row["section"], row["path"]): row for row in result["citations"]}
    assert rows["code", "move.py"]["status"] == "moved"
    assert rows["code", "move.py"]["suggested_line"] == 2
    assert rows["code", "stable.py"]["status"] == "unchanged"
    assert rows["test_exemplars", "stable.py"]["status"] == "unchanged"
    assert rows["code", "duplicate.py"]["status"] == "ambiguous"
    assert rows["code", "repeat.py"]["status"] == "ambiguous"
    assert rows["code", "same.py"]["status"] == "unchanged"
    assert rows["code", "changed.py"]["status"] == "changed"
    assert rows["code", "missing.py"]["status"] == "path_missing"
    assert rows["code", "dirty.py"]["status"] == "working_tree_changed"
    assert all("suggested_line" not in row for row in rows.values() if row["status"] != "moved")
    assert brief.read_bytes() == stamped


@pytest.mark.parametrize("edit,expected", [
    ("swap", [("moved", 7), ("moved", 3)]),
    ("replace", [("ambiguous", None), ("ambiguous", None)]),
    ("append", [("unchanged", None), ("unchanged", None)]),
])
def test_repeated_lines_are_checked_with_surrounding_text(repository, tmp_path, edit, expected):
    root, _ = repository
    original = "def original():\n    # Original default\n    return 1\n\n"
    alternate = "def alternate():\n    # Alternate default\n    return 1\n\n"
    file = root / "calculate.py"
    file.write_text(original + alternate, encoding="utf-8")
    source = commit(root, "two functions with the same return")
    brief = tmp_path / "repeated.md"
    brief.write_text(
        "## Relevant code\n<!-- reviewer:verified-citations -->\n\n"
        "- `calculate.py:3` — Original default\n"
        "- `calculate.py:7` — Alternate default\n",
        encoding="utf-8",
    )
    captured = invoke(root, brief, "--indexed-sha", source, "--repo", "owner/repo", "--branch", "dev")
    assert captured["status"] == "captured"
    saved = brief.read_bytes()

    updated = {
        "swap": alternate + original,
        "replace": original.replace("Original default", "New original comment")
        + alternate.replace("Alternate default", "New alternate comment"),
        "append": original + alternate + "# Unrelated edit at the end\n",
    }[edit]
    file.write_text(updated, encoding="utf-8")
    target = commit(root, edit)

    result = invoke(root, brief)
    assert result["target_sha"] == target
    assert [(r["status"], r.get("suggested_line")) for r in result["citations"]] == expected
    assert brief.read_bytes() == saved


def test_legacy_brief_remains_readable(repository, tmp_path):
    root, source = repository
    brief = tmp_path / "legacy.md"
    write_brief(brief, marker=False)
    original = brief.read_bytes()
    assert invoke(root, brief)["reason"] == "legacy_brief"
    assert invoke(
        root, brief, "--indexed-sha", source, "--repo", "owner/repo", "--branch", "dev",
    )["reason"] == "unverified_brief"
    assert brief.read_bytes() == original


def test_citation_edit_invalidates_provenance(repository, tmp_path):
    root, source = repository
    brief = tmp_path / "brief.md"
    write_brief(brief)
    invoke(root, brief, "--indexed-sha", source, "--repo", "owner/repo", "--branch", "dev")
    brief.write_text(brief.read_text().replace("move.py:1", "move.py:2"))
    assert invoke(root, brief)["reason"] == "provenance_mismatch"


def test_capture_reports_unavailable_source(repository, tmp_path):
    root, _ = repository
    brief = tmp_path / "brief.md"
    write_brief(brief)
    original = brief.read_bytes()
    result = invoke(root, brief, "--indexed-sha", "9" * 40, "--repo", "owner/repo", "--branch", "dev")
    assert result["status"] == "unavailable"
    assert result["reason"] == "source_unavailable"
    assert brief.read_bytes() == original
