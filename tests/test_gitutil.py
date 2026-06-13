import subprocess, pathlib
from reviewer.gitutil import changed_files, file_at_ref, remote_url

def _run(*a, cwd): subprocess.run(a, cwd=cwd, check=True, capture_output=True)

def test_changed_files_and_file_at_ref(tmp_path):
    r = tmp_path
    _run("git","init","-q", cwd=r)
    _run("git","config","user.email","t@t","--local", cwd=r)
    _run("git","config","user.name","t","--local", cwd=r)
    (r/"a.py").write_text("x=1\n")
    _run("git","add","-A", cwd=r); _run("git","commit","-qm","c1", cwd=r)
    base = subprocess.run(["git","rev-parse","HEAD"],cwd=r,capture_output=True,text=True).stdout.strip()
    (r/"a.py").write_text("x=2\n"); (r/"b.py").write_text("y=1\n")
    _run("git","add","-A", cwd=r); _run("git","commit","-qm","c2", cwd=r)
    assert set(changed_files(str(r), base, "HEAD")) == {"a.py", "b.py"}
    assert file_at_ref(str(r), "a.py", base) == "x=1\n"


def test_remote_url_returns_origin(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    assert remote_url(str(repo)) is None
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/owner/name.git"], check=True, capture_output=True)
    assert "github.com" in (remote_url(str(repo)) or "")
