"""Guardrail: скилл sync-tasks — тонкий repo-mode триггер sync_board.

После PRI-140 enumerate/normalize/index выполняются на сервере; скилл лишь зовёт
sync_board и печатает summary. Никакого LLM-обхода доски и поштучной индексации.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugin" / "skills" / "sync-tasks" / "SKILL.md"
OLD_REF = ROOT / "plugin" / "skills" / "sync-tasks" / "references" / "sync-tasks-yougile.md"


def test_skill_is_thin_trigger_of_sync_board():
    text = SKILL.read_text(encoding="utf-8")
    assert "sync_board(" in text                 # вызывает серверный тул
    assert "server-side" in text                 # явно: работа на сервере
    # LLM больше не индексирует поштучно и не вызывает index_tasks_batch из скилла
    assert "index_tasks_batch" not in text
    assert "index_task(" not in text


def test_legacy_enumeration_reference_removed():
    assert not OLD_REF.exists()


def _repo_mode_call(text: str) -> str:
    match = re.search(r"```(?:text)?\n\s*(sync_board\(.*?\))\n\s*```", text, re.DOTALL)
    assert match, "skill must contain one fenced sync_board repo-mode call"
    return match.group(1)


def test_skill_calls_only_repo_mode_with_operation_flags():
    text = SKILL.read_text(encoding="utf-8")
    call = _repo_mode_call(text)

    assert re.findall(r"\b([a-z_]+)=", call) == [
        "repo",
        "branch",
        "limit",
        "purge_orphaned",
        "keep_with_prs",
        "force_renormalize",
    ]
    assert "repo=<canonical owner/name or group/.../name>" in call
    assert "branch=<explicit tracked branch or null>" in call
    for forbidden in ("board=", "board_type=", "provider_options=", "sync_filter="):
        assert forbidden not in call


def test_skill_resolves_repo_and_branch_without_reconstructing_policy():
    text = SKILL.read_text(encoding="utf-8")
    lowered = " ".join(text.lower().split())

    assert "canonical lowercase repository id" in lowered
    assert "git -c <path> remote get-url origin" in lowered
    assert "strip a trailing `.git`" in lowered
    assert "preserve every path segment after the host or scp colon" in lowered
    assert "last two path segments" not in lowered
    assert "explicitly supplied or selected" in lowered
    assert "tracked branches" in lowered
    assert "branch=null" in lowered
    assert "server uses its primary tracked branch" in lowered
    assert "never infer the branch from the current worktree branch" in lowered
    for forbidden in (".review.yml", "get_board_config", "get_board_targets"):
        assert forbidden not in text


def test_skill_documents_nested_https_ssh_and_scp_repo_parsing():
    text = " ".join(SKILL.read_text(encoding="utf-8").split()).casefold()

    examples = (
        "`https://gitlab.example.com/group/sub/repo.git` → `group/sub/repo`",
        "`ssh://git@gitlab.example.com/group/sub/repo.git` → `group/sub/repo`",
        "`git@gitlab.example.com:group/sub/repo.git` → `group/sub/repo`",
    )
    for example in examples:
        assert example in text


def test_skill_does_not_retry_policy_errors_unfiltered():
    text = SKILL.read_text(encoding="utf-8").lower()

    assert "configuration or policy error" in text
    assert "do not retry" in text
    assert "unfiltered explicit mode" in text


def test_skill_shows_by_board_breakdown():
    text = SKILL.read_text(encoding="utf-8")
    assert "by_board" in text          # обрабатывает per-board breakdown


def test_skill_reports_complete_retention_and_purge_summary_in_russian():
    text = SKILL.read_text(encoding="utf-8")

    assert "Reply in Russian" in text
    for field in (
        "eligible",
        "filtered_by_age",
        "filtered_archived",
        "age_unknown",
        "archive_unknown",
        "filter_applied",
        "filter_fingerprint",
        "filter_source",
        "by_board",
        "purge",
        "warnings",
    ):
        assert field in text, field
