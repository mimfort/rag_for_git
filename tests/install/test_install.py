import json
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

import reviewer.entrypoints.cli as cli_module
from reviewer import install as inst
from reviewer.entrypoints.cli import cli


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


@pytest.fixture
def fake_uvx(monkeypatch):
    """Детерминированный uvx-путь, чтобы команда запуска не зависела от машины."""
    path = str(Path("/fake/bin/uvx").resolve())
    monkeypatch.setattr(inst.shutil, "which",
                        lambda name: path if name == "uvx" else None)
    return path


def test_launch_command_latest(fake_uvx):
    cmd, args = inst.launch_command("latest")
    assert cmd == fake_uvx
    assert args == ["--from", "rag-reviewer@latest", "reviewer-mcp"]


def test_launch_command_no_pin(fake_uvx):
    _, args = inst.launch_command("")
    assert args == ["--from", "rag-reviewer", "reviewer-mcp"]


def test_launch_command_uv_fallback(monkeypatch):
    path = str(Path("/fake/bin/uv").resolve())
    monkeypatch.setattr(inst.shutil, "which",
                        lambda name: path if name == "uv" else None)
    cmd, args = inst.launch_command("latest")
    assert cmd == path
    assert args == ["tool", "run", "--from", "rag-reviewer@latest", "reviewer-mcp"]


def test_launch_command_makes_relative_which_result_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        inst.shutil,
        "which",
        lambda name: "bin/uvx" if name == "uvx" else None,
    )

    command, _ = inst.launch_command("latest")

    assert command == str((tmp_path / "bin" / "uvx").resolve())


def test_plan_mcpservers_preserves_other(fake_uvx, tmp_path):
    cfg = tmp_path / "mcp.json"
    _write_text(cfg, json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    plan = inst.build_plan(inst.CLIENTS["cursor"], path_override=str(cfg))
    data = json.loads(plan.content)
    assert data["mcpServers"]["other"] == {"command": "x"}        # чужое сохранено
    entry = data["mcpServers"]["reviewer"]
    assert entry["command"] == fake_uvx
    assert entry["args"] == ["--from", "rag-reviewer@latest", "reviewer-mcp"]
    assert plan.already is False


def test_plan_marks_already(fake_uvx, tmp_path):
    cfg = tmp_path / "mcp.json"
    _write_text(cfg, json.dumps({"mcpServers": {"reviewer": {"command": "old"}}}))
    plan = inst.build_plan(inst.CLIENTS["cursor"], path_override=str(cfg))
    assert plan.already is True


def test_plan_vscode_uses_servers_key(fake_uvx, tmp_path):
    cfg = tmp_path / "mcp.json"
    plan = inst.build_plan(inst.CLIENTS["vscode"], path_override=str(cfg))
    data = json.loads(plan.content)
    assert "servers" in data and "mcpServers" not in data
    assert data["servers"]["reviewer"]["command"] == fake_uvx


def test_plan_mimo_array_command(fake_uvx, tmp_path):
    cfg = tmp_path / "mimocode.json"
    plan = inst.build_plan(inst.CLIENTS["mimo"], path_override=str(cfg))
    entry = json.loads(plan.content)["mcp"]["reviewer"]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    assert entry["command"] == [fake_uvx, "--from", "rag-reviewer@latest", "reviewer-mcp"]


def test_plan_opencode_no_enabled(fake_uvx, tmp_path):
    cfg = tmp_path / "opencode.json"
    entry = json.loads(inst.build_plan(inst.CLIENTS["opencode"],
                                       path_override=str(cfg)).content)["mcp"]["reviewer"]
    assert entry["type"] == "local"
    assert "enabled" not in entry


def test_plan_codex_toml_append(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    _write_text(cfg, "[other]\nx = 1\n")
    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))
    assert "[other]" in plan.content                              # чужое сохранено
    assert "[mcp_servers.reviewer]" in plan.content
    assert tomllib.loads(plan.content)["mcp_servers"]["reviewer"]["command"] == fake_uvx
    assert '"rag-reviewer@latest"' in plan.content


def test_render_codex_round_trips_windows_path_and_non_bmp_unicode():
    command = "C:\\Program Files\\😀\\uvx.exe"
    args = ["--from", "rag-reviewer@latest", "reviewer-mcp"]

    parsed = tomllib.loads(inst._render_codex(command, args))

    assert parsed["mcp_servers"]["reviewer"] == {
        "command": command,
        "args": args,
    }


def test_plan_codex_updates_existing_reviewer_and_preserves_other_toml(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    original_other = "# keep\n[other]\npath = 'C:\\\\Program Files\\\\Tool'\n\n"
    _write_text(
        cfg,
        original_other
        + "[mcp_servers.reviewer]\ncommand = \"old\"\nargs = [\"old\"]\n"
        + "[mcp_servers.reviewer.env]\nOLD = \"1\"\n"
        + "[tail]\nvalue = 3\n"
    )
    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))
    assert plan.already is True
    assert plan.content.startswith(original_other)
    assert plan.content.count("[mcp_servers.reviewer]") == 1
    assert tomllib.loads(plan.content)["mcp_servers"]["reviewer"]["command"] == fake_uvx
    assert "[mcp_servers.reviewer.env]" not in plan.content
    assert "[tail]\nvalue = 3\n" in plan.content


def test_plan_codex_ignores_table_like_lines_in_multiline_strings(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    unrelated = (
        '[other]\ntext = """\n[mcp_servers.reviewer]\ncommand = "not a table"\n'
        '[inside]\n"""\n'
    )
    _write_text(
        cfg,
        unrelated
        + '[mcp_servers.reviewer]\ncommand = "old"\n'
        + '[tail]\nvalue = 3\n'
    )

    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))

    assert plan.content.startswith(unrelated)
    assert tomllib.loads(plan.content)["other"]["text"].startswith(
        "[mcp_servers.reviewer]\n"
    )
    assert tomllib.loads(plan.content)["tail"] == {"value": 3}


def test_plan_codex_handles_four_quote_multiline_string_terminator(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    _write_text(
        cfg,
        'values = ["""quoted""""]\n'
        '[mcp_servers.reviewer]\ncommand = "old"\n'
    )

    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))

    parsed = tomllib.loads(plan.content)
    assert parsed["values"] == ['quoted"']
    assert parsed["mcp_servers"]["reviewer"]["command"] == fake_uvx


def test_plan_codex_ignores_brackets_and_table_headers_in_comments(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    unrelated = (
        "[other]\n"
        "value = 1  # unmatched [ must not open an array\n"
        "# [mcp_servers.reviewer]\n"
    )
    _write_text(
        cfg,
        unrelated
        + '[mcp_servers.reviewer]\ncommand = "old"\n'
        + "[tail]\nvalue = 3\n"
    )

    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))

    assert plan.content.startswith(unrelated)
    assert tomllib.loads(plan.content)["tail"] == {"value": 3}


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_plan_codex_does_not_split_unicode_comment_separators(
    fake_uvx, tmp_path, separator
):
    cfg = tmp_path / "config.toml"
    original = (
        f"# retain{separator}[mcp_servers.reviewer]\n"
        "[other]\n"
        "value = 1\n"
    )
    _write_text(cfg, original)

    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))

    parsed = tomllib.loads(plan.content)
    assert plan.already is False
    assert plan.content.startswith(original)
    assert "command" not in parsed
    assert parsed["mcp_servers"]["reviewer"]["command"] == fake_uvx


def test_plan_codex_stops_reviewer_range_at_array_table(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    unrelated = '[[other]]\nname = "keep"\n'
    _write_text(cfg, '[mcp_servers.reviewer]\ncommand = "old"\n' + unrelated)

    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))

    assert plan.content.endswith(unrelated)
    assert tomllib.loads(plan.content)["other"] == [{"name": "keep"}]


def test_plan_codex_rejects_reviewer_array_of_tables(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    _write_text(
        cfg,
        '[[mcp_servers.reviewer]]\ncommand = "old"\n'
        '[[tail]]\nname = "keep"\n'
    )

    with pytest.raises(ValueError, match="массив TOML-таблиц"):
        inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))


def test_plan_codex_ignores_nested_array_lines_as_table_headers(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    _write_text(
        cfg,
        "[mcp_servers.reviewer]\n"
        "values = [\n"
        "  [[1]]\n"
        "]\n"
        "[tail]\n"
        'name = "keep"\n'
    )

    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))

    parsed = tomllib.loads(plan.content)
    assert "values" not in parsed["mcp_servers"]["reviewer"]
    assert parsed["tail"] == {"name": "keep"}


def test_plan_codex_removes_spaced_dotted_reviewer_child(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    _write_text(
        cfg,
        '[mcp_servers.reviewer]\ncommand = "old"\n'
        + '[mcp_servers . reviewer . env]\nOLD = "1"\n'
        + '[tail]\nvalue = 3\n'
    )

    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))

    assert "OLD" not in plan.content
    assert tomllib.loads(plan.content)["mcp_servers"]["reviewer"] == {
        "command": fake_uvx,
        "args": ["--from", "rag-reviewer@latest", "reviewer-mcp"],
    }


def test_plan_codex_replaces_quoted_reviewer_key_paths(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    _write_text(
        cfg,
        '["mcp_servers" . "reviewer"]\ncommand = "old"\n'
        + "['mcp_servers' . 'reviewer' . 'env']\nOLD = \"1\"\n"
        + '[tail]\nvalue = 3\n'
    )

    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))

    assert plan.already is True
    assert "OLD" not in plan.content
    assert tomllib.loads(plan.content)["tail"] == {"value": 3}


def test_plan_codex_rejects_inline_reviewer_table(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    _write_text(cfg, 'mcp_servers = { reviewer = { command = "old" } }\n')
    with pytest.raises(ValueError, match="inline"):
        inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))


@pytest.mark.parametrize("raw", [
    'mcp_servers = { other = { command = "keep" } }\n',
    "mcp_servers = {}\n",
    "mcp_servers = []\n",
    'mcp_servers = "sealed"\n',
])
def test_plan_codex_rejects_incompatible_mcp_servers_parent(fake_uvx, tmp_path, raw):
    cfg = tmp_path / "config.toml"
    _write_text(cfg, raw)

    with pytest.raises(ValueError, match="mcp_servers"):
        inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))


def test_plan_codex_extends_regular_mcp_servers_table(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    _write_text(cfg, '[mcp_servers.other]\ncommand = "keep"\n')

    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))

    parsed = tomllib.loads(plan.content)["mcp_servers"]
    assert parsed["other"] == {"command": "keep"}
    assert parsed["reviewer"]["command"] == fake_uvx


def test_plan_codex_rejects_invalid_toml(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    _write_text(cfg, "[broken\n")
    with pytest.raises(ValueError, match="невалидный TOML"):
        inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))


def test_codex_client_path_honors_codex_home(monkeypatch, tmp_path):
    codex_home = tmp_path / "Codex Home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert inst.CLIENTS["codex"].path_fn("Windows") == codex_home / "config.toml"


def test_launch_command_requires_uvx_or_uv(monkeypatch):
    monkeypatch.setattr(inst.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="uvx/uv не найден"):
        inst.launch_command("latest")


@pytest.mark.parametrize("system,expected", [
    ("Darwin", "Library/Application Support/Code/User/mcp.json"),
    ("Linux", ".config/Code/User/mcp.json"),
])
def test_vscode_path_per_os(system, expected):
    p = inst.CLIENTS["vscode"].path_fn(system)
    assert p.as_posix().endswith(expected)


def test_vscode_path_windows(monkeypatch):
    monkeypatch.setenv("APPDATA", "C:\\Users\\u\\AppData\\Roaming")
    p = inst.CLIENTS["vscode"].path_fn("Windows")
    assert "Code" in p.parts and p.name == "mcp.json"


def test_no_latest_in_plan(fake_uvx, tmp_path):
    cfg = tmp_path / "mcp.json"
    plan = inst.build_plan(inst.CLIENTS["cursor"], version="", path_override=str(cfg))
    assert "rag-reviewer@latest" not in plan.content
    assert json.loads(plan.content)["mcpServers"]["reviewer"]["args"][1] == "rag-reviewer"


def test_apply_plan_writes_and_backs_up(fake_uvx, tmp_path):
    cfg = tmp_path / "mcp.json"
    _write_text(cfg, json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    plan = inst.build_plan(inst.CLIENTS["cursor"], path_override=str(cfg))
    backup = inst.apply_plan(plan)
    assert backup is not None and backup.exists()
    written = json.loads(cfg.read_text())
    assert "reviewer" in written["mcpServers"] and "other" in written["mcpServers"]


def test_apply_plan_codex_preserves_mixed_newlines_and_backup_bytes(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    prefix = b"# keep\r\n[other]\r\nvalue = 1\n\r\n"
    reviewer = b'[mcp_servers.reviewer]\r\ncommand = "old"\n'
    suffix = b'[[tail]]\nname = "keep"\r\n\r\n'
    original = prefix + reviewer + suffix
    cfg.write_bytes(original)

    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))
    backup = inst.apply_plan(plan)
    written = cfg.read_bytes()

    assert backup is not None
    assert backup.read_bytes() == original
    assert written.startswith(prefix)
    assert written.endswith(suffix)
    assert tomllib.loads(written.decode("utf-8"))["tail"] == [{"name": "keep"}]


def test_apply_plan_codex_preserves_trailing_blank_bytes_when_appending(fake_uvx, tmp_path):
    cfg = tmp_path / "config.toml"
    original = b"# keep\r\n[other]\r\nvalue = 1\r\n\r\n\r\n"
    cfg.write_bytes(original)

    plan = inst.build_plan(inst.CLIENTS["codex"], path_override=str(cfg))
    backup = inst.apply_plan(plan)

    assert backup is not None
    assert backup.read_bytes() == original
    assert cfg.read_bytes().startswith(original)


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


def test_install_all_clients_registry_preserves_configs_and_skills(monkeypatch, tmp_path):
    """Каждый конфиговый клиент сохраняет диалект и чужие настройки."""
    home = tmp_path / "home"
    system = "Linux"
    fake_uvx = str((tmp_path / "bin" / "uvx").resolve())
    fake_claude = str((tmp_path / "bin" / "claude").resolve())
    generic = [
        client
        for client in inst.CLIENTS.values()
        if client.scope == "user" and client.key != "codex"
    ]
    top_keys = {
        "mcpServers": "mcpServers",
        "vscode": "servers",
        "mimo": "mcp",
        "opencode": "mcp",
    }
    paths: dict[str, Path] = {}

    monkeypatch.setattr(inst, "_home", lambda: home)
    monkeypatch.setattr(inst.platform, "system", lambda: system)
    for client in generic:
        path = client.path_fn(system)
        paths[client.key] = path
        path.parent.mkdir(parents=True)
        top_key = top_keys[client.dialect]
        if client.dialect == "mimo":
            other = {"type": "local", "command": ["other"], "enabled": False}
        elif client.dialect == "opencode":
            other = {"type": "local", "command": ["other"]}
        else:
            other = {"command": "other"}
        _write_text(
            path,
            json.dumps(
                {"unrelated": {"owner": client.key}, top_key: {"other": other}}
            ),
        )

    def which(name: str) -> str | None:
        if name == "uvx":
            return fake_uvx
        if name == "claude":
            return fake_claude
        return None

    claude_calls: list[dict[str, object]] = []
    codex_calls: list[dict[str, object]] = []
    allowlist_calls: list[dict[str, object]] = []
    fetched: list[None] = []
    skills_tarball = _make_tarball(
        {
            "rag_for_git-main/plugin/skills/review-pr/SKILL.md": b"# review\n",
            "rag_for_git-main/plugin/skills/review-pr/references/info.md": b"info\n",
        }
    )
    monkeypatch.setattr(
        inst,
        "detect_installed",
        lambda: [*generic, inst.CLIENTS["codex"]],
    )
    monkeypatch.setattr(inst.shutil, "which", which)
    monkeypatch.setattr(
        cli_module,
        "_run_claude_target",
        lambda **kwargs: claude_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(cli_module, "_print_claude_result", lambda result: None)
    monkeypatch.setattr(
        cli_module,
        "_apply_claude_allowlist",
        lambda *args, **kwargs: allowlist_calls.append(kwargs),
    )
    monkeypatch.setattr(
        cli_module,
        "_run_codex_target",
        lambda **kwargs: codex_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(cli_module, "_print_codex_result", lambda result: None)

    def fetch_archive():
        fetched.append(None)
        return skills_tarball, '"test-etag"'

    monkeypatch.setattr(inst, "fetch_skills_archive", fetch_archive)

    runner = CliRunner()
    first = runner.invoke(cli, ["install", "--all"])
    assert first.exit_code == 0, first.output
    first_contents = {key: path.read_bytes() for key, path in paths.items()}

    second = runner.invoke(cli, ["install", "--all"])
    assert second.exit_code == 0, second.output
    assert {key: path.read_bytes() for key, path in paths.items()} == first_contents

    assert claude_calls == [{"dry_run": False}, {"dry_run": False}]
    assert allowlist_calls == [{"dry_run": False}, {"dry_run": False}]
    assert codex_calls == [
        {"include_mcp": True, "dry_run": False, "version": "latest", "path_opt": None},
        {"include_mcp": True, "dry_run": False, "version": "latest", "path_opt": None},
    ]
    assert len(fetched) == 2

    expected_args = ["--from", "rag-reviewer@latest", "reviewer-mcp"]
    for client in generic:
        raw = paths[client.key].read_text(encoding="utf-8")
        data = json.loads(raw)
        section = data[top_keys[client.dialect]]
        assert raw.count('"reviewer"') == 1
        assert data["unrelated"] == {"owner": client.key}
        assert section["other"]["command"] == (
            ["other"] if client.dialect in {"mimo", "opencode"} else "other"
        )
        assert list(section).count("reviewer") == 1
        if client.dialect in {"mcpServers", "vscode"}:
            assert section["reviewer"] == {
                "command": fake_uvx,
                "args": expected_args,
            }
        elif client.dialect == "mimo":
            assert section["reviewer"] == {
                "type": "local",
                "command": [fake_uvx, *expected_args],
                "enabled": True,
            }
        else:
            assert section["reviewer"] == {
                "type": "local",
                "command": [fake_uvx, *expected_args],
            }

    cursor_path = paths["cursor"]
    vscode_path = paths["vscode"]
    mimo_path = paths["mimo"]
    opencode_path = paths["opencode"]
    assert json.loads(cursor_path.read_text())["mcpServers"]["reviewer"]["args"][-1] == (
        "reviewer-mcp"
    )
    assert json.loads(vscode_path.read_text())["servers"]["reviewer"]["command"] == (
        fake_uvx
    )
    assert json.loads(mimo_path.read_text())["mcp"]["reviewer"]["enabled"] is True
    assert json.loads(opencode_path.read_text())["mcp"]["reviewer"]["type"] == "local"

    for key in ("gemini", "mimo", "opencode", "kimi"):
        client = inst.CLIENTS[key]
        assert client.skills_fn is not None
        skills_dir = client.skills_fn(system)
        assert (skills_dir / "review-pr" / "SKILL.md").is_file()
        assert inst.read_skills_stamp(skills_dir)["source_etag"] == '"test-etag"'


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
    assert capable == {"gemini", "mimo", "kimi", "opencode"}


# --------------------------------------------------------------------------- #
# allowlist (permissions.allow в .claude/settings.json)
# --------------------------------------------------------------------------- #
def test_allowlist_plan_creates_file(tmp_path):
    cfg = tmp_path / ".claude" / "settings.json"
    plan = inst.build_allowlist_plan(cfg)
    assert plan.created is True and plan.already is False
    data = json.loads(plan.content)
    assert data["permissions"]["allow"] == ["mcp__reviewer__*"]


def test_allowlist_plan_preserves_existing(tmp_path):
    cfg = tmp_path / ".claude" / "settings.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls:*)"], "deny": ["mcp__evil"]},
        "model": "opus",
    }))
    plan = inst.build_allowlist_plan(cfg)
    data = json.loads(plan.content)
    assert data["model"] == "opus"
    assert data["permissions"]["deny"] == ["mcp__evil"]
    assert data["permissions"]["allow"] == ["Bash(ls:*)", "mcp__reviewer__*"]
    assert plan.already is False


def test_allowlist_plan_idempotent(tmp_path):
    cfg = tmp_path / ".claude" / "settings.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"permissions": {"allow": ["mcp__reviewer__*"]}}))
    plan = inst.build_allowlist_plan(cfg)
    assert plan.already is True
    assert json.loads(plan.content)["permissions"]["allow"].count("mcp__reviewer__*") == 1


def test_apply_allowlist_plan_writes_and_backs_up(tmp_path):
    cfg = tmp_path / ".claude" / "settings.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))
    plan = inst.build_allowlist_plan(cfg)
    backup = inst.apply_allowlist_plan(plan)
    assert backup is not None and backup.exists()
    written = json.loads(cfg.read_text())
    assert "mcp__reviewer__*" in written["permissions"]["allow"]


def test_apply_allowlist_plan_no_write_when_unchanged(tmp_path):
    cfg = tmp_path / ".claude" / "settings.json"
    inst.apply_allowlist_plan(inst.build_allowlist_plan(cfg))
    plan2 = inst.build_allowlist_plan(cfg)
    assert plan2.already is True
    assert inst.apply_allowlist_plan(plan2) is None


def test_claude_user_settings_path_is_global():
    # allowlist пишется в ГЛОБАЛЬНЫЙ user-конфиг (~/.claude/settings.json),
    # чтобы правило действовало во всех проектах (вкл. установку плагином).
    p = inst.claude_user_settings_path()
    assert p == inst._home() / ".claude" / "settings.json"


def test_claude_code_is_a_native_global_target(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(inst, "_home", lambda: home)

    client = inst.CLIENTS["claude-code"]

    assert client.scope == "native"
    assert client.dialect == "native"
    assert client.path_fn("Linux") != Path(".mcp.json")
