"""Тесты пересчёта корпуса брифов: чистое ядро (measure_corpus) и CLI-обвязка."""
import json
from unittest.mock import MagicMock, patch

import psycopg
from click.testing import CliRunner

from reviewer.entrypoints.cli import cli
from reviewer.metrics.brief_quality.config import DEFAULT
from reviewer.metrics.brief_quality.corpus import measure_corpus


def test_measure_corpus_writes_row_per_pr(tmp_path):
    """Строка на PR-мерж, а не на задачу: два мержа одной задачи — две строки
    с разными pr_number (а не одна строка, продублированная на оба мержа)."""
    briefs = tmp_path / "docs" / "superpowers" / "briefs"
    briefs.mkdir(parents=True)
    (briefs / "2026-09-01-PRI-1-x.md").write_text(
        "# Brief\n\n## Relevant code\n- `reviewer/app.py` — зачем\n", encoding="utf-8")

    def run_git(args):
        if args[0] == "log":
            return (
                "abc Merge pull request #7 from o/feat/pri-1\n"
                "def Merge pull request #9 from o/feat/pri-1-follow-up\n"
            )
        if args[:2] == ["diff", "--name-status"]:
            return "M\treviewer/app.py\n"
        if args[:2] == ["diff", "--name-only"]:
            # ground_truth.collect() сам сводит truth.changed по всем мержам
            # задачи (тут не используется measure_corpus, но вызывается всегда).
            return "reviewer/app.py\n"
        raise AssertionError(args)

    rows = []

    class _History:
        def record_brief_quality(self, run_id, repo, pr_number, head_sha, measurement):
            rows.append((run_id, repo, pr_number, measurement.status))
            return len(rows)

    summary = measure_corpus(str(tmp_path), "o/r", DEFAULT, run_git, _History())
    assert rows == [(None, "o/r", 7, "measured"), (None, "o/r", 9, "measured")]
    assert summary["measured"] == 2 and summary["briefs"] == 1 and summary["rows"] == 2


def test_measure_corpus_counts_write_failures_separately(tmp_path):
    """Сбой записи (history гасит DB-ошибку и отдаёт None) — отдельный счётчик
    write_failed, а не общий not_recorded: тот же случай, что реально бывает
    при живом Postgres в начале прогона и упавшем на середине."""
    briefs = tmp_path / "docs" / "superpowers" / "briefs"
    briefs.mkdir(parents=True)
    (briefs / "2026-09-01-PRI-1-x.md").write_text(
        "# Brief\n\n## Relevant code\n- `reviewer/app.py` — зачем\n", encoding="utf-8")

    def run_git(args):
        if args[0] == "log":
            return "abc Merge pull request #7 from o/feat/pri-1\n"
        if args[:2] == ["diff", "--name-status"]:
            return "M\treviewer/app.py\n"
        if args[:2] == ["diff", "--name-only"]:
            return "reviewer/app.py\n"
        raise AssertionError(args)

    class _FailingHistory:
        def record_brief_quality(self, run_id, repo, pr_number, head_sha, measurement):
            raise psycopg.OperationalError("connection refused")

    summary = measure_corpus(str(tmp_path), "o/r", DEFAULT, run_git, _FailingHistory())

    assert summary["write_failed"] == 1
    assert summary["rows"] == 1
    assert "not_recorded" not in summary
    assert "measured" not in summary


def test_measure_corpus_skips_brief_without_key(tmp_path):
    briefs = tmp_path / "docs" / "superpowers" / "briefs"
    briefs.mkdir(parents=True)
    (briefs / "2026-09-01-no-key.md").write_text("# Brief\n", encoding="utf-8")
    summary = measure_corpus(str(tmp_path), "o/r", DEFAULT, lambda args: "", None)
    assert summary["skipped_no_key"] == 1


def test_measure_corpus_skips_task_without_pr_merges(tmp_path):
    """Ключ есть, но настоящих PR-мержей нет — считается отдельно от skipped_no_key."""
    briefs = tmp_path / "docs" / "superpowers" / "briefs"
    briefs.mkdir(parents=True)
    (briefs / "2026-09-01-PRI-2-x.md").write_text("# Brief\n", encoding="utf-8")

    def run_git(args):
        if args[0] == "log":
            return "abc merge: dev в feature/pri-2\n"
        raise AssertionError(args)

    summary = measure_corpus(str(tmp_path), "o/r", DEFAULT, run_git, None)
    assert summary["skipped_no_merges"] == 1
    assert summary["briefs"] == 1


# ---------------------------------------------------------------------------
# CLI-обвязка: команда `reviewer measure-briefs`
# ---------------------------------------------------------------------------


@patch("reviewer.entrypoints.cli.ReviewHistory")
@patch("reviewer.entrypoints.cli.measure_corpus")
@patch("reviewer.entrypoints.cli.remote_url")
def test_measure_briefs_command_renders_text_summary(
    mock_remote_url, mock_measure, mock_history_cls, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    mock_remote_url.return_value = "https://github.com/owner/myrepo.git"
    mock_measure.return_value = {
        "briefs": 2, "skipped_no_key": 1, "skipped_no_merges": 0,
        "rows": 1, "measured": 1,
    }
    history = MagicMock()
    mock_history_cls.return_value = history

    result = CliRunner().invoke(cli, ["measure-briefs", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "2" in result.output
    assert "measured" in result.output
    history.close.assert_called_once()
    called_args = mock_measure.call_args[0]
    assert called_args[1] == "owner/myrepo"


@patch("reviewer.entrypoints.cli.ReviewHistory")
@patch("reviewer.entrypoints.cli.measure_corpus")
@patch("reviewer.entrypoints.cli.remote_url")
def test_measure_briefs_json_flag_is_machine_readable(
    mock_remote_url, mock_measure, mock_history_cls, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    mock_remote_url.return_value = "https://github.com/owner/myrepo.git"
    mock_measure.return_value = {
        "briefs": 0, "skipped_no_key": 0, "skipped_no_merges": 0, "rows": 0,
    }
    mock_history_cls.return_value = MagicMock()

    result = CliRunner().invoke(cli, ["measure-briefs", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == mock_measure.return_value


@patch("reviewer.entrypoints.cli.ReviewHistory")
@patch("reviewer.entrypoints.cli.measure_corpus")
@patch("reviewer.entrypoints.cli.remote_url")
def test_measure_briefs_postgres_down_is_a_clean_error(
    mock_remote_url, mock_measure, mock_history_cls, monkeypatch, tmp_path
):
    """Postgres лежит с самого начала — preflight (init_schema) обязан поймать
    это ДО сканирования корпуса, а не полагаться на то, что measure_corpus
    сам бросит исключение (measure_and_record внутри fail-soft и гасит его)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    mock_remote_url.return_value = "https://github.com/owner/myrepo.git"
    history = MagicMock()
    history.init_schema.side_effect = psycopg.OperationalError("connection refused")
    mock_history_cls.return_value = history

    result = CliRunner().invoke(cli, ["measure-briefs", str(tmp_path)])

    assert result.exit_code != 0
    assert "Postgres" in result.output
    history.close.assert_called_once()
    mock_measure.assert_not_called()


@patch("reviewer.entrypoints.cli.ReviewHistory")
@patch("reviewer.entrypoints.cli.measure_corpus")
@patch("reviewer.entrypoints.cli.remote_url")
def test_measure_briefs_reports_write_failures_as_error(
    mock_remote_url, mock_measure, mock_history_cls, monkeypatch, tmp_path
):
    """Postgres умер на середине прогона: preflight его не ловит (был жив на
    старте), поэтому реакция — на явный счётчик write_failed из summary."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    mock_remote_url.return_value = "https://github.com/owner/myrepo.git"
    mock_measure.return_value = {
        "briefs": 1, "skipped_no_key": 0, "skipped_no_merges": 0,
        "rows": 1, "write_failed": 1,
    }
    history = MagicMock()
    mock_history_cls.return_value = history

    result = CliRunner().invoke(cli, ["measure-briefs", str(tmp_path)])

    assert result.exit_code != 0
    assert "write_failed" in result.output or "1" in result.output
    history.close.assert_called_once()


@patch("reviewer.entrypoints.cli.remote_url")
def test_measure_briefs_no_repo_raises(mock_remote_url, monkeypatch, tmp_path):
    monkeypatch.setenv("DEFAULT_REPO", "")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    mock_remote_url.return_value = None

    result = CliRunner().invoke(cli, ["measure-briefs", str(tmp_path)])

    assert result.exit_code != 0
    assert "repo" in result.output.lower()


@patch("reviewer.entrypoints.cli.ReviewHistory")
@patch("reviewer.entrypoints.cli.measure_corpus")
@patch("reviewer.entrypoints.cli.remote_url")
def test_measure_briefs_warns_on_substituted_repo(
    mock_remote_url, mock_measure, mock_history_cls, monkeypatch, tmp_path
):
    """Как migrate-branches: подстановка из DEFAULT_REPO — предупреждение, а не тишина."""
    monkeypatch.setenv("DEFAULT_REPO", "owner/fallback")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    mock_remote_url.return_value = None
    mock_measure.return_value = {
        "briefs": 0, "skipped_no_key": 0, "skipped_no_merges": 0, "rows": 0,
    }
    mock_history_cls.return_value = MagicMock()

    result = CliRunner().invoke(cli, ["measure-briefs", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "DEFAULT_REPO" in result.output
