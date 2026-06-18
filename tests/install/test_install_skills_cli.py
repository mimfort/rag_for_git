from click.testing import CliRunner

from reviewer import install as inst
from reviewer.entrypoints.cli import cli


def _tar(members):
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_install_skills_cli_writes_stamp(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "_home", lambda: tmp_path)
    tar = _tar({"r/plugin/skills/sync-tasks/SKILL.md": b"# s"})
    monkeypatch.setattr(inst, "fetch_skills_archive", lambda url=inst.SKILLS_TARBALL: (tar, '"etagX"'))
    res = CliRunner().invoke(cli, ["install-skills", "kimi"])
    assert res.exit_code == 0, res.output
    dest = inst.CLIENTS["kimi"].skills_fn("Linux")
    stamp = inst.read_skills_stamp(dest)
    assert stamp is not None and stamp["source_etag"] == '"etagX"'
