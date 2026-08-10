import json

from click.testing import CliRunner

from reviewer.entrypoints.cli import cli


def test_home_layers_survive_unavailable_vcs(tmp_path, monkeypatch):
    """PRI-234, критерий 1: недоступный VCS больше не обнуляет вывод политики."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    path = tmp_path / "rag-reviewer" / "repos" / "o" / "r.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "repository:\n  primary_branch: dev\n  index_branches: [dev, main]\n"
        "max_comments: 3\n",
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
    assert "policy_error" not in payload
    # Домашний слой применён, хотя коммиченный недоступен.
    assert payload["effective"]["max_comments"] == 3
    assert payload["sources"]["max_comments"] == "home:repos/o/r.yml"
    assert payload["skipped"] == [
        {
            "layer": ".review.yml",
            "repo": "o/r",
            "ref": "dev",
            "category": "unavailable",
            "transport": "unknown",
            "http_status": None,
        }
    ]
    # Политика неполная — сигнал внешним скриптам сохраняется.
    assert result.exit_code != 0


def test_skipped_layer_does_not_echo_raw_exception_text(tmp_path, monkeypatch):
    """Диагностик пропущенного слоя структурный: ни str(exc), ни URL/токен."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    # Реальный env var перекрывает значение из локального .env разработчика
    # (если тот определяет TASK_BOARD_URL_TEMPLATE) — иначе посторонний URL
    # из чужого поля эффективной policy ложно провалил бы проверку "нет URL".
    monkeypatch.setenv("TASK_BOARD_URL_TEMPLATE", "")
    secret = "do-not-echo-token-xyz"

    def boom(*args, **kwargs):
        raise RuntimeError(f"https://api.example/x?token={secret}")

    monkeypatch.setattr(
        "reviewer.services.review_service.ReviewService._create_vcs_provider", boom
    )
    result = CliRunner().invoke(cli, ["config", "show", "--repo", "o/r", "--json"])
    payload = json.loads(result.output)

    assert payload["skipped"][0]["layer"] == ".review.yml"
    assert payload["skipped"][0]["category"] == "unavailable"
    assert secret not in result.output
    assert "https://" not in result.output


def test_config_show_text_output_prints_effective_and_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    path = tmp_path / "rag-reviewer" / "review.yml"
    path.parent.mkdir(parents=True)
    path.write_text("max_comments: 3\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(
        "reviewer.services.review_service.ReviewService._create_vcs_provider", boom
    )
    result = CliRunner().invoke(cli, ["config", "show", "--repo", "o/r"])

    assert "branches:" in result.output
    assert "max_comments: 3" in result.output
    assert "skipped: .review.yml" in result.output
    assert "category=unavailable" in result.output


def test_skipped_home_credential_layer_also_sets_exit_code(tmp_path, monkeypatch):
    """Осознанное изменение: credential-ключ в home-слое теперь даёт код 1.

    Раньше он был warning с кодом 0, но это то же событие — слой не применён.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("REVIEW_BRANCHES", "main")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    path = tmp_path / "rag-reviewer" / "review.yml"
    path.parent.mkdir(parents=True)
    path.write_text("github_token: t\n", encoding="utf-8")

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
    payload = json.loads(result.output)

    assert payload["skipped"][0]["layer"] == "home:review.yml"
    assert payload["skipped"][0]["category"] == "credential"
    assert payload["effective"]["max_comments"] == 5
    assert result.exit_code != 0
