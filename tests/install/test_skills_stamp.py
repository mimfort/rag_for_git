from pathlib import Path

from reviewer import install as inst


def _mk_skill(root: Path, name: str, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = root / name / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def test_skill_file_hashes_deterministic_and_per_skill(tmp_path):
    _mk_skill(tmp_path, "sync-tasks", {"SKILL.md": b"# a", "references/y.md": b"# b"})
    _mk_skill(tmp_path, "solve-task", {"SKILL.md": b"# c"})
    h1 = inst._skill_file_hashes(tmp_path)
    h2 = inst._skill_file_hashes(tmp_path)
    assert h1 == h2                                  # детерминизм
    assert set(h1) == {"sync-tasks", "solve-task"}   # по скилу
    assert all(v.startswith("sha256:") for v in h1.values())


def test_skill_file_hashes_changes_on_edit(tmp_path):
    _mk_skill(tmp_path, "sync-tasks", {"SKILL.md": b"# a"})
    before = inst._skill_file_hashes(tmp_path)["sync-tasks"]
    (tmp_path / "sync-tasks" / "SKILL.md").write_bytes(b"# changed")
    after = inst._skill_file_hashes(tmp_path)["sync-tasks"]
    assert before != after


def test_stamp_roundtrip_and_ignores_stamp_file(tmp_path):
    _mk_skill(tmp_path, "sync-tasks", {"SKILL.md": b"# a"})
    hashes = inst._skill_file_hashes(tmp_path)
    inst.write_skills_stamp(tmp_path, source_url="u", source_etag='"e1"',
                            pkg_version="0.1.8", hashes=hashes)
    stamp = inst.read_skills_stamp(tmp_path)
    assert stamp["source_etag"] == '"e1"'
    assert stamp["pkg_version"] == "0.1.8"
    assert stamp["skills"] == hashes
    # стамп-файл не должен попадать в хэши скилов
    assert inst.STAMP_NAME not in inst._skill_file_hashes(tmp_path)


def test_read_stamp_missing_returns_none(tmp_path):
    assert inst.read_skills_stamp(tmp_path) is None


def test_current_pkg_version_is_str():
    assert isinstance(inst.current_pkg_version(), str)
