import json

from click.testing import CliRunner

from reviewer.entrypoints.cli import cli


def test_branches_shown_even_when_policy_part_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    path = tmp_path / "rag-reviewer" / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "repository:\n  primary_branch: dev\n  index_branches: [dev, main]\n",
        encoding="utf-8",
    )

    def boom(*args, **kwargs):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(
        "reviewer.services.review_service.ReviewService._create_vcs_provider", boom
    )
    result = CliRunner().invoke(cli, ["config", "show", "--repo", "o/r", "--json"])
    payload = json.loads(result.output)
    assert payload["branches"] == {
        "primary": "dev",
        "index": ["dev", "main"],
        "source": "home:repos/o/r.yml",
    }
    assert "policy_error" in payload
    # Branch-секция печатается без падения, но код возврата обязан остаться
    # ненулевым — иначе `config show; echo $?` теряет сигнал о сломанном конфиге.
    assert result.exit_code != 0


def test_policy_error_does_not_echo_raw_exception_text(tmp_path, monkeypatch):
    """Important 4: `policy_error` для произвольного исключения печатает только
    тип, а не str(exc) — иначе секреты из VCS-клиента утекли бы в вывод CLI."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    secret = "do-not-echo-token-xyz"

    def boom(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(
        "reviewer.services.review_service.ReviewService._create_vcs_provider", boom
    )
    result = CliRunner().invoke(cli, ["config", "show", "--repo", "o/r", "--json"])
    payload = json.loads(result.output)
    assert payload["policy_error"] == "RuntimeError"
    assert secret not in result.output


def test_config_show_exits_zero_when_everything_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    path = tmp_path / "rag-reviewer" / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("repository:\n  index_branches: [dev]\n", encoding="utf-8")

    def fake_provider(self, owner, name):
        class _V:
            def get_file_at_ref(self, _path, ref):
                return "max_comments: 5\n"

            def close(self):
                return None

        return _V()

    monkeypatch.setattr(
        "reviewer.services.review_service.ReviewService._create_vcs_provider",
        fake_provider,
    )
    result = CliRunner().invoke(cli, ["config", "show", "--repo", "o/r", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "policy_error" not in payload
    assert payload["branches"]["primary"] == "dev"


def test_branch_used_for_policy_ref_comes_from_home_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    path = tmp_path / "rag-reviewer" / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text("repository:\n  index_branches: [dev]\n", encoding="utf-8")
    seen = []

    def fake_provider(self, owner, name):
        class _V:
            def get_file_at_ref(self, _path, ref):
                seen.append(ref)
                return "max_comments: 5\n"

            def close(self):
                return None

        return _V()

    monkeypatch.setattr(
        "reviewer.services.review_service.ReviewService._create_vcs_provider",
        fake_provider,
    )
    CliRunner().invoke(cli, ["config", "show", "--repo", "o/r", "--json"])
    assert seen == ["dev"]
