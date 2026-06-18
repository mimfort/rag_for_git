from click.testing import CliRunner
from reviewer import install as inst
from reviewer.entrypoints.cli import cli


def test_read_env_parses_key_value(tmp_path):
    f = tmp_path / ".env"
    f.write_text("VOYAGE_API_KEY=sk-abc\nGITHUB_TOKEN=ghp-xyz\n", encoding="utf-8")
    result = inst.read_env(f)
    assert result == {"VOYAGE_API_KEY": "sk-abc", "GITHUB_TOKEN": "ghp-xyz"}


def test_read_env_skips_comments_and_empty(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# комментарий\n\nFOO=bar\n", encoding="utf-8")
    result = inst.read_env(f)
    assert result == {"FOO": "bar"}
    assert len(result) == 1


def test_read_env_missing_file(tmp_path):
    result = inst.read_env(tmp_path / "nonexistent.env")
    assert result == {}


def test_read_env_value_with_equals(tmp_path):
    f = tmp_path / ".env"
    f.write_text("PG_DSN=postgresql://u:p@localhost:5433/db\n", encoding="utf-8")
    result = inst.read_env(f)
    assert result["PG_DSN"] == "postgresql://u:p@localhost:5433/db"


def test_render_env_contains_wizard_keys():
    values = {
        "VOYAGE_API_KEY": "sk-test",
        "GITHUB_TOKEN": "",
        "PG_DSN": "postgresql://reviewer:reviewer@localhost:5433/reviewer",
        "NEO4J_URI": "neo4j://localhost:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "reviewerpass",
        "DEFAULT_REPO": "",
        "REVIEW_BRANCHES": "main,master",
        "TASK_BOARD_TYPE": "",
        "TASK_BOARD_MCP": "",
        "TASK_BOARD_KEY_PATTERN": "",
        "TASK_BOARD_URL_TEMPLATE": "",
    }
    result = inst.render_env(values, extra={})
    assert "VOYAGE_API_KEY=sk-test" in result
    assert "PG_DSN=postgresql://reviewer:reviewer@localhost:5433/reviewer" in result


def test_render_env_extra_keys_preserved():
    values = {
        "VOYAGE_API_KEY": "sk-test",
        "GITHUB_TOKEN": "",
        "PG_DSN": "postgresql://reviewer:reviewer@localhost:5433/reviewer",
        "NEO4J_URI": "neo4j://localhost:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "reviewerpass",
        "DEFAULT_REPO": "",
        "REVIEW_BRANCHES": "main,master",
        "TASK_BOARD_TYPE": "",
        "TASK_BOARD_MCP": "",
        "TASK_BOARD_KEY_PATTERN": "",
        "TASK_BOARD_URL_TEMPLATE": "",
    }
    extra = {"REVIEW_MAX_COMMENTS": "30", "REVIEW_HISTORY": "true"}
    result = inst.render_env(values, extra=extra)
    assert "REVIEW_MAX_COMMENTS=30" in result
    assert "REVIEW_HISTORY=true" in result
    assert "Прочие настройки" in result


def test_render_env_no_extra_no_extra_block():
    values = {f.key: f.default for g in inst.WIZARD_GROUPS for f in g.fields}
    result = inst.render_env(values, extra={})
    assert "Прочие настройки" not in result


def test_prompt_groups_yes_uses_current_values():
    current = {"VOYAGE_API_KEY": "sk-existing", "GITHUB_TOKEN": "ghp-existing"}
    result = inst.prompt_groups(inst.WIZARD_GROUPS, current=current, yes=True)
    assert result["VOYAGE_API_KEY"] == "sk-existing"
    assert result["GITHUB_TOKEN"] == "ghp-existing"


def test_prompt_groups_yes_uses_field_default_when_no_current():
    result = inst.prompt_groups(inst.WIZARD_GROUPS, current={}, yes=True)
    assert result["PG_DSN"] == "postgresql://reviewer:reviewer@localhost:5433/reviewer"
    assert result["REVIEW_BRANCHES"] == "main,master"
    assert result["VOYAGE_API_KEY"] == ""


def test_prompt_groups_yes_skips_optional_groups():
    # При yes=True опциональные группы сохраняют current или default — не вызывают confirm
    current = {"TASK_BOARD_TYPE": "yougile"}
    result = inst.prompt_groups(inst.WIZARD_GROUPS, current=current, yes=True)
    assert result["TASK_BOARD_TYPE"] == "yougile"
    # Остальные поля доски — пустые (default)
    assert result["TASK_BOARD_MCP"] == ""


def test_init_yes_creates_env_file(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    assert dest.exists()
    content = dest.read_text(encoding="utf-8")
    assert "VOYAGE_API_KEY=" in content
    assert "PG_DSN=" in content


def test_init_yes_preserves_existing_secret(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    dest.write_text("VOYAGE_API_KEY=sk-existing\n", encoding="utf-8")
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    content = dest.read_text(encoding="utf-8")
    assert "VOYAGE_API_KEY=sk-existing" in content


def test_init_yes_preserves_extra_keys(tmp_path, monkeypatch):
    dest = tmp_path / ".env"
    dest.write_text("VOYAGE_API_KEY=sk-x\nREVIEW_MAX_COMMENTS=42\n", encoding="utf-8")
    monkeypatch.setattr("reviewer.install.default_env_path", lambda: dest)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    content = dest.read_text(encoding="utf-8")
    assert "REVIEW_MAX_COMMENTS=42" in content
