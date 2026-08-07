# tests/mcp/test_resolve_repo_branch.py
"""_resolve_repo_branch валидирует ветку по per-repo домашним слоям, не REVIEW_BRANCHES."""
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService


class Comp:
    graph = None


def _mcp_service(csv="main"):
    s = Settings(_env_file=None, review_branches=csv, review_session_persist=False)
    return MCPReviewService(s, Comp())


def test_session_less_branch_validated_per_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "rag-reviewer" / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("repository:\n  index_branches: [dev]\n", encoding="utf-8")

    mcp_service = _mcp_service()
    note = mcp_service._resolve_repo_branch("o/r", "main")
    assert isinstance(note, str)
    assert "main" in note
    assert mcp_service._resolve_repo_branch("o/r", None) == ("o/r", "dev")


def test_session_less_branch_falls_back_to_env_without_home_layer(tmp_path, monkeypatch):
    # Без домашних файлов поведение остаётся прежним (env REVIEW_BRANCHES).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    mcp_service = _mcp_service(csv="main,master")
    assert mcp_service._resolve_repo_branch("o/r", "master") == ("o/r", "master")
    note = mcp_service._resolve_repo_branch("o/r", "unknown")
    assert isinstance(note, str)
    assert "unknown" in note
