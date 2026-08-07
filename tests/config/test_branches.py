import pytest

from reviewer.config.branches import RepoBranches, resolve_repo_branches
from reviewer.config.layers import HomePolicyError
from reviewer.config.settings import Settings


def _settings(monkeypatch, branches="main"):
    monkeypatch.setenv("REVIEW_BRANCHES", branches)
    return Settings(_env_file=None)


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_per_repo_layer_wins_over_global_and_env(tmp_path, monkeypatch):
    _write(tmp_path, "review.yml", "repository:\n  index_branches: [main]\n")
    _write(
        tmp_path,
        "repos/o/r.yml",
        "repository:\n  primary_branch: dev\n  index_branches: [dev, main]\n",
    )
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch, "trunk"), config_root=tmp_path
    )
    assert result == RepoBranches(
        primary="dev", index=("dev", "main"), source="home:repos/o/r.yml", warnings=()
    )


def test_global_layer_used_when_no_per_repo_file(tmp_path, monkeypatch):
    _write(tmp_path, "review.yml", "repository:\n  index_branches: [release, main]\n")
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch, "trunk"), config_root=tmp_path
    )
    assert result.primary == "release"
    assert result.index == ("release", "main")
    assert result.source == "home:review.yml"


def test_env_fallback_when_no_home_files(tmp_path, monkeypatch):
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch, "dev,main"), config_root=tmp_path
    )
    assert result.primary == "dev"
    assert result.index == ("dev", "main")
    assert result.source == "env"


def test_default_main_when_env_empty(tmp_path, monkeypatch):
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch, ""), config_root=tmp_path
    )
    assert result == RepoBranches(
        primary="main", index=("main",), source="default", warnings=()
    )


def test_block_replaced_whole_not_merged_per_key(tmp_path, monkeypatch):
    _write(tmp_path, "review.yml", "repository:\n  index_branches: [main, release]\n")
    _write(tmp_path, "repos/o/r.yml", "repository:\n  primary_branch: dev\n")
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch), config_root=tmp_path
    )
    assert result.index == ("dev",)
    assert result.primary == "dev"


def test_primary_defaults_to_first_index_branch(tmp_path, monkeypatch):
    _write(tmp_path, "repos/o/r.yml", "repository:\n  index_branches: [dev, main]\n")
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch), config_root=tmp_path
    )
    assert result.primary == "dev"


def test_primary_outside_index_is_config_error(tmp_path, monkeypatch):
    _write(
        tmp_path,
        "repos/o/r.yml",
        "repository:\n  primary_branch: qa\n  index_branches: [dev, main]\n",
    )
    with pytest.raises(HomePolicyError) as exc:
        resolve_repo_branches(
            "o/r", settings=_settings(monkeypatch), config_root=tmp_path
        )
    assert "qa" in str(exc.value)
    assert "repos/o/r.yml" in str(exc.value)


@pytest.mark.parametrize(
    "body",
    [
        "repository:\n  index_branches: []\n",
        "repository:\n  index_branches: [dev, dev]\n",
        "repository:\n  index_branches: [dev, 7]\n",
        "repository:\n  index_branches: dev\n",
        "repository:\n  index_branches: ['']\n",
        "repository:\n  primary_branch: 7\n  index_branches: [dev]\n",
        "repository: []\n",
    ],
)
def test_invalid_block_is_config_error(tmp_path, monkeypatch, body):
    _write(tmp_path, "repos/o/r.yml", body)
    with pytest.raises(HomePolicyError):
        resolve_repo_branches(
            "o/r", settings=_settings(monkeypatch), config_root=tmp_path
        )


def test_broken_yaml_does_not_silently_fall_back_to_env(tmp_path, monkeypatch):
    _write(tmp_path, "repos/o/r.yml", "repository: [unclosed\n")
    with pytest.raises(Exception):
        resolve_repo_branches(
            "o/r", settings=_settings(monkeypatch, "dev"), config_root=tmp_path
        )


def test_unknown_subkeys_are_preserved_not_rejected(tmp_path, monkeypatch):
    _write(
        tmp_path,
        "repos/o/r.yml",
        "repository:\n  index_branches: [dev]\n  future_key: whatever\n",
    )
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch), config_root=tmp_path
    )
    assert result.index == ("dev",)


def test_missing_repository_block_falls_through_to_next_layer(tmp_path, monkeypatch):
    _write(tmp_path, "repos/o/r.yml", "max_comments: 10\n")
    result = resolve_repo_branches(
        "o/r", settings=_settings(monkeypatch, "dev"), config_root=tmp_path
    )
    assert result.source == "env"
    assert result.index == ("dev",)


def test_owner_subgroups_and_dotted_names(tmp_path, monkeypatch):
    _write(tmp_path, "repos/group/sub/r.yml", "repository:\n  index_branches: [a]\n")
    _write(tmp_path, "repos/o/r.io.yml", "repository:\n  index_branches: [b]\n")
    nested = resolve_repo_branches(
        "group/sub/r", settings=_settings(monkeypatch), config_root=tmp_path
    )
    dotted = resolve_repo_branches(
        "o/r.io", settings=_settings(monkeypatch), config_root=tmp_path
    )
    assert nested.index == ("a",)
    assert dotted.index == ("b",)


def test_credential_key_inside_repository_block_is_rejected(tmp_path, monkeypatch):
    _write(
        tmp_path,
        "repos/o/r.yml",
        "repository:\n  index_branches: [dev]\n  github_token: ghp_secret\n",
    )
    with pytest.raises(Exception) as exc:
        resolve_repo_branches(
            "o/r", settings=_settings(monkeypatch), config_root=tmp_path
        )
    assert "ghp_secret" not in str(exc.value)
