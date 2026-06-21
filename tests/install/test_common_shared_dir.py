import io
import tarfile

from reviewer import install as inst


def _tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_common_dir_extracted_and_hashed(tmp_path):
    # _common не имеет SKILL.md — проверяем, что обход подкаталогов его не теряет.
    tar = _tar({
        "r/plugin/skills/review-pr/SKILL.md": b"# review",
        "r/plugin/skills/_common/findings-schema.md": b"# schema",
    })
    names = inst.extract_skills(tar, tmp_path)
    assert "_common" in names
    assert (tmp_path / "_common" / "findings-schema.md").is_file()

    hashes = inst._skill_file_hashes(tmp_path)
    assert "_common" in hashes
    assert hashes["_common"].startswith("sha256:")


def test_common_registered_in_skill_names():
    assert "_common" in inst.SKILL_NAMES
