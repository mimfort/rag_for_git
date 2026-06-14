import json

import pytest

from reviewer import install as inst


@pytest.fixture
def fake_uvx(monkeypatch):
    """Детерминированный uvx-путь, чтобы команда запуска не зависела от машины."""
    monkeypatch.setattr(inst.shutil, "which",
                        lambda name: "/fake/bin/uvx" if name == "uvx" else None)


def test_launch_command_latest(fake_uvx):
    cmd, args = inst.launch_command("latest")
    assert cmd == "/fake/bin/uvx"
    assert args == ["--from", "rag-reviewer@latest", "reviewer-mcp"]


def test_launch_command_no_pin(fake_uvx):
    _, args = inst.launch_command("")
    assert args == ["--from", "rag-reviewer", "reviewer-mcp"]


def test_launch_command_uv_fallback(monkeypatch):
    monkeypatch.setattr(inst.shutil, "which",
                        lambda name: "/fake/bin/uv" if name == "uv" else None)
    cmd, args = inst.launch_command("latest")
    assert cmd == "/fake/bin/uv"
    assert args == ["tool", "run", "--from", "rag-reviewer@latest", "reviewer-mcp"]


def test_plan_mcpservers_preserves_other(fake_uvx, tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    plan = inst.build_plan(inst.CLIENTS["cursor"], path_override=str(cfg))
    data = json.loads(plan.content)
    assert data["mcpServers"]["other"] == {"command": "x"}        # чужое сохранено
    entry = data["mcpServers"]["reviewer"]
    assert entry["command"] == "/fake/bin/uvx"
    assert entry["args"] == ["--from", "rag-reviewer@latest", "reviewer-mcp"]
    assert plan.already is False


def test_plan_marks_already(fake_uvx, tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"reviewer": {"command": "old"}}}))
    plan = inst.build_plan(inst.CLIENTS["cursor"], path_override=str(cfg))
    assert plan.already is True


def test_plan_vscode_uses_servers_key(fake_uvx, tmp_path):
    cfg = tmp_path / "mcp.json"
    plan = inst.build_plan(inst.CLIENTS["vscode"], path_override=str(cfg))
    data = json.loads(plan.content)
    assert "servers" in data and "mcpServers" not in data
    assert data["servers"]["reviewer"]["command"] == "/fake/bin/uvx"


def test_plan_mimo_array_command(fake_uvx, tmp_path):
    cfg = tmp_path / "mimocode.json"
    plan = inst.build_plan(inst.CLIENTS["mimo"], path_override=str(cfg))
    entry = json.loads(plan.content)["mcp"]["reviewer"]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    assert entry["command"] == ["/fake/bin/uvx", "--from", "rag-reviewer@latest", "reviewer-mcp"]


def test_plan_opencode_no_enabled(fake_uvx, tmp_path):
    cfg = tmp_path / "opencode.json"
    entry = json.loads(inst.build_plan(inst.CLIENTS["opencode"],
                                       path_override=str(cfg)).content)["mcp"]["reviewer"]
    assert entry["type"] == "local"
    assert "enabled" not in entry


def test_plan_codex_toml_append(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[other]\nx = 1\n")
    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))
    assert "[other]" in plan.content                              # чужое сохранено
    assert "[mcp_servers.reviewer]" in plan.content
    assert 'command = "/fake/bin/uvx"' in plan.content
    assert '"rag-reviewer@latest"' in plan.content


def test_plan_codex_idempotent(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[mcp_servers.reviewer]\ncommand = \"old\"\n")
    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))
    assert plan.already is True
    assert plan.content.count("[mcp_servers.reviewer]") == 1      # не дублируем


@pytest.mark.parametrize("system,expected", [
    ("Darwin", "Library/Application Support/Code/User/mcp.json"),
    ("Linux", ".config/Code/User/mcp.json"),
])
def test_vscode_path_per_os(system, expected):
    p = inst.CLIENTS["vscode"].path_fn(system)
    assert str(p).endswith(expected)


def test_vscode_path_windows(monkeypatch):
    monkeypatch.setenv("APPDATA", "C:\\Users\\u\\AppData\\Roaming")
    p = inst.CLIENTS["vscode"].path_fn("Windows")
    assert "Code" in str(p) and str(p).endswith("mcp.json")


def test_no_latest_in_plan(fake_uvx, tmp_path):
    cfg = tmp_path / "mcp.json"
    plan = inst.build_plan(inst.CLIENTS["cursor"], version="", path_override=str(cfg))
    assert "rag-reviewer@latest" not in plan.content
    assert json.loads(plan.content)["mcpServers"]["reviewer"]["args"][1] == "rag-reviewer"


def test_apply_plan_writes_and_backs_up(fake_uvx, tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    plan = inst.build_plan(inst.CLIENTS["cursor"], path_override=str(cfg))
    backup = inst.apply_plan(plan)
    assert backup is not None and backup.exists()
    written = json.loads(cfg.read_text())
    assert "reviewer" in written["mcpServers"] and "other" in written["mcpServers"]


def test_apply_plan_creates_parent(fake_uvx, tmp_path):
    cfg = tmp_path / "nested" / "deep" / "mcp.json"
    plan = inst.build_plan(inst.CLIENTS["cursor"], path_override=str(cfg))
    assert inst.apply_plan(plan) is None                          # нового файла — без бэкапа
    assert cfg.exists()


def test_detect_installed(monkeypatch, tmp_path):
    home = tmp_path
    (home / ".cursor").mkdir()                                    # «установлен» только Cursor
    monkeypatch.setattr(inst, "_home", lambda: home)
    found = {c.key for c in inst.detect_installed("Linux")}
    assert "cursor" in found
    assert "windsurf" not in found


# --------------------------------------------------------------------------- #
# скилы
# --------------------------------------------------------------------------- #
def _make_tarball(members: dict[str, bytes]) -> bytes:
    """Собрать .tar.gz в памяти: {имя_в_архиве: содержимое}."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_extract_skills_basic(tmp_path):
    tar = _make_tarball({
        "rag_for_git-main/plugin/skills/review-pr/SKILL.md": b"# review",
        "rag_for_git-main/plugin/skills/solve-task/SKILL.md": b"# solve",
        "rag_for_git-main/plugin/skills/solve-task/refs/x.md": b"ref",
        "rag_for_git-main/README.md": b"ignored",                 # вне skills — пропуск
    })
    names = inst.extract_skills(tar, tmp_path / "skills")
    assert names == ["review-pr", "solve-task"]
    assert (tmp_path / "skills" / "review-pr" / "SKILL.md").read_text() == "# review"
    assert (tmp_path / "skills" / "solve-task" / "refs" / "x.md").read_text() == "ref"
    assert not (tmp_path / "skills" / "README.md").exists()


def test_extract_skills_path_traversal_guard(tmp_path):
    tar = _make_tarball({
        "x/plugin/skills/../../../evil.md": b"pwned",
        "x/plugin/skills/ok/SKILL.md": b"ok",
    })
    names = inst.extract_skills(tar, tmp_path / "skills")
    assert names == ["ok"]
    assert not (tmp_path / "evil.md").exists()


def test_install_skills_uses_client_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "_home", lambda: tmp_path)
    tar = _make_tarball({"r/plugin/skills/review-pr/SKILL.md": b"# r"})
    dest, names = inst.install_skills(inst.CLIENTS["mimo"], system="Linux", tar_bytes=tar)
    assert names == ["review-pr"]
    assert dest == tmp_path / ".config" / "mimocode" / "skills"
    assert (dest / "review-pr" / "SKILL.md").exists()


def test_install_skills_unsupported_client(tmp_path):
    tar = _make_tarball({"r/plugin/skills/review-pr/SKILL.md": b"# r"})
    with pytest.raises(ValueError):
        inst.install_skills(inst.CLIENTS["cursor"], tar_bytes=tar)


def test_skills_capable_clients_have_dirs():
    capable = {c.key for c in inst.CLIENTS.values() if c.skills_fn}
    assert capable == {"gemini", "mimo", "kimi"}
