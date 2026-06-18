from reviewer import install as inst


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
