import inspect
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from reviewer.config.layers import ResolutionMeta
from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.config.task_board import TaskSyncFilter
from reviewer.mcp.service import MCPReviewService
from reviewer.policy.policy import ReviewPolicy
from reviewer.tasks.boards.registry import (
    BoardProviderRegistry,
    CredentialFieldSpec,
)
from tests.mcp.test_board_provider_extensibility import _service


class _Svc(MCPReviewService):
    """Лёгкий deploy-wide service без scoped provider resolution."""

    def __init__(self, sync_service):
        self.settings = Settings(_env_file=None)
        self.components = SimpleNamespace(sync_service=sync_service)
        self._board_registry = BoardProviderRegistry()
        self._board_credentials = ProviderCredentialSource(values={})


def test_sync_board_no_provider_returns_generic_error():
    out = _Svc(None).sync_board()
    assert out == {
        "status": "error",
        "reason": "task board REST is not configured",
    }


def test_deploy_wide_sync_delegates_without_provider_specific_options():
    class FakeSync:
        def __init__(self):
            self.called_with = None

        def run(self, board=None, limit=None, purge_orphaned=False,
                keep_with_prs=True, board_type=None, force_renormalize=False, *,
                sync_filter=None, filter_source=None):
            self.called_with = (
                board,
                limit,
                purge_orphaned,
                keep_with_prs,
                board_type,
                force_renormalize,
                sync_filter,
                filter_source,
            )
            return {"enumerated": 3, "changed": 1, "warnings": []}

    fake = FakeSync()
    out = _Svc(fake).sync_board(board="B", limit=5)
    assert out["enumerated"] == 3 and out["changed"] == 1
    assert fake.called_with == ("B", 5, False, True, None, False, None, None)


def test_scoped_sync_builds_provider_with_immutable_options():
    service, state, _ = _service()

    out = service.sync_board(
        board="FAKE",
        board_type="fake",
        provider_options={"lane": "Backend"},
        force_renormalize=True,
    )

    assert out["enumerated"] == 1
    assert state.contexts[0].options == {"lane": "Backend"}
    assert state.providers[0].closed is True


def test_provider_options_without_type_rejected_when_multiple_are_configured():
    service, _, _ = _service()
    first = service._board_registry.get("fake")
    second = replace(
        first,
        board_type="second",
        credential_fields=(
            CredentialFieldSpec("SECOND_TOKEN", "Second token", secret=True),
        ),
    )
    service._board_registry = BoardProviderRegistry([first, second])
    service._board_credentials = ProviderCredentialSource(
        values={"FAKE_TOKEN": "one", "SECOND_TOKEN": "two"}
    )

    out = service.sync_board(provider_options={"lane": "Backend"})

    assert out["status"] == "error"
    assert "board_type is required" in out["reason"]


def test_sync_board_threads_force_renormalize_deploy_wide():
    class FakeSync:
        def run(self, **kwargs):
            self.called_with = kwargs["force_renormalize"]
            return {"enumerated": 1, "warnings": []}

    fake = FakeSync()
    _Svc(fake).sync_board(force_renormalize=True)
    assert fake.called_with is True


def test_sync_board_failsoft_on_exception():
    class Boom:
        def run(self, **kwargs):
            raise RuntimeError("kaboom")

    out = _Svc(Boom()).sync_board()
    assert out["status"] == "error"
    assert "kaboom" in out["reason"]


def test_sync_board_signature_preserves_prefix_and_adds_keyword_only_repo_filter_args():
    parameters = list(inspect.signature(MCPReviewService.sync_board).parameters.values())

    assert [parameter.name for parameter in parameters[:8]] == [
        "self",
        "board",
        "limit",
        "purge_orphaned",
        "keep_with_prs",
        "board_type",
        "provider_options",
        "force_renormalize",
    ]
    assert [parameter.name for parameter in parameters[8:]] == [
        "repo",
        "branch",
        "sync_filter",
        "status_field",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in parameters[8:]
    )


def test_repo_sync_resolves_policy_and_threads_typed_filter_separately():
    service, state, _ = _service()
    resolved = []

    def resolve_policy(repo, branch):
        resolved.append((repo, branch))
        return (
            ReviewPolicy(
                task_board={
                    "type": "fake",
                    "project": "FAKE",
                    "options": {"lane": "Backend"},
                    "sync_filter": {"include_archived": False},
                },
                task_board_warnings=["legacy task_board field migrated"],
            ),
            ResolutionMeta(
                sources={"task_board": "home:repos/owner/repo.yml"},
                shadowed={},
                warnings=(),
            ),
        )

    service._resolve_policy = resolve_policy

    out = service.sync_board(repo="Owner/Repo", branch="main")

    assert resolved == [("owner/repo", "main")]
    assert out["filter_source"] == "home:repos/owner/repo.yml"
    assert "legacy task_board field migrated" in out["warnings"]
    assert state.contexts[0].options == {"lane": "Backend"}
    assert state.providers[0].sync_calls == [
        ("FAKE", TaskSyncFilter(include_archived=False))
    ]
    assert state.providers[0].closed is True


@pytest.mark.parametrize(
    "mixed",
    [
        {"board": "FAKE"},
        {"board_type": "fake"},
        {"provider_options": {}},
        {"sync_filter": {}},
        {"status_field": "Stage"},
    ],
    ids=("board", "board-type", "empty-options", "empty-filter", "legacy-status"),
)
def test_repo_sync_rejects_explicit_board_selection_without_io(mixed):
    class NoIoSync:
        def run(self, **kwargs):
            raise AssertionError("deploy-wide sync must not run")

    service = _Svc(NoIoSync())
    service._resolve_policy = lambda *args: (_ for _ in ()).throw(
        AssertionError("policy resolution must not run")
    )

    out = service.sync_board(repo="owner/repo", **mixed)

    assert out["status"] == "error"
    assert out["category"] == "configuration"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"branch": "main"},
        {"repo": ""},
        {"repo": "   "},
    ],
    ids=("branch-without-repo", "empty-repo", "blank-repo"),
)
def test_repo_mode_selector_validation_happens_without_io(kwargs):
    class NoIoSync:
        def run(self, **run_kwargs):
            raise AssertionError("deploy-wide sync must not run")

    service = _Svc(NoIoSync())
    service._resolve_policy = lambda *args: (_ for _ in ()).throw(
        AssertionError("policy resolution must not run")
    )

    out = service.sync_board(**kwargs)

    assert out["status"] == "error"
    assert out["category"] == "configuration"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"repo": "not-a-repo"},
        {"repo": "owner/repo", "branch": "untracked"},
    ],
    ids=("invalid-repo", "untracked-branch"),
)
def test_repo_resolution_errors_are_structured_configuration_errors(kwargs):
    service, state, _ = _service()
    service._resolve_policy = lambda *args: (_ for _ in ()).throw(
        AssertionError("policy resolution must not run")
    )

    out = service.sync_board(**kwargs)

    assert out["status"] == "error"
    assert out["category"] == "configuration"
    assert state.providers == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"repo": "runtime-secret"},
        {"repo": "owner/repo", "branch": "feature-runtime-secret"},
    ],
    ids=("invalid-repo", "untracked-branch"),
)
def test_repo_resolution_errors_sanitize_configured_secrets(kwargs):
    service, state, _ = _service()
    service._resolve_policy = lambda *args: (_ for _ in ()).throw(
        AssertionError("policy resolution must not run")
    )

    out = service.sync_board(**kwargs)

    assert out["status"] == "error"
    assert out["category"] == "configuration"
    assert "runtime-secret" not in repr(out)
    assert "[REDACTED]" in out["reason"]
    assert state.providers == []


@pytest.mark.parametrize("failure", ["exception", "warning"])
def test_repo_policy_resolution_failure_never_falls_back_to_board_runtime(failure):
    service, state, _ = _service()
    if failure == "exception":
        service._resolve_policy = lambda *args: (_ for _ in ()).throw(
            RuntimeError("policy failed")
        )
    else:
        service._resolve_policy = lambda *args: (
            ReviewPolicy(task_board={"type": "fake", "project": "FAKE"}),
            ResolutionMeta({}, {}, ("home policy was skipped",)),
        )

    out = service.sync_board(repo="owner/repo")

    assert out["status"] == "error"
    assert out["category"] == "configuration"
    assert state.providers == []


def test_repo_policy_exception_includes_sanitized_diagnostic_detail():
    service, state, _ = _service()
    service._resolve_policy = lambda *args: (_ for _ in ()).throw(
        RuntimeError("policy read failed for runtime-secret")
    )

    out = service.sync_board(repo="owner/repo")

    assert out["status"] == "error"
    assert out["category"] == "configuration"
    assert "policy read failed for [REDACTED]" in out["reason"]
    assert "runtime-secret" not in repr(out)
    assert state.providers == []


def test_repo_policy_warnings_include_all_sanitized_diagnostic_details():
    service, state, _ = _service()
    service._resolve_policy = lambda *args: (
        ReviewPolicy(task_board={"type": "fake", "project": "FAKE"}),
        ResolutionMeta(
            {},
            {},
            (
                "global policy skipped",
                "repo policy failed for runtime-secret",
            ),
        ),
    )

    out = service.sync_board(repo="owner/repo")

    assert out["status"] == "error"
    assert out["category"] == "configuration"
    assert "global policy skipped" in out["reason"]
    assert "repo policy failed for [REDACTED]" in out["reason"]
    assert "runtime-secret" not in repr(out)
    assert state.providers == []


@pytest.mark.parametrize("diagnostic_kind", ["exception", "warnings"])
def test_repo_policy_diagnostics_sanitize_board_and_vcs_credentials(
    diagnostic_kind,
    caplog,
):
    service, state, _ = _service()
    service.settings = Settings(
        _env_file=None,
        github_token="github-vcs-secret",
        gitlab_token="gitlab-vcs-secret",
    )
    diagnostic = (
        "policy transport failed: board=runtime-secret "
        "github=github-vcs-secret gitlab=gitlab-vcs-secret"
    )
    if diagnostic_kind == "exception":
        service._resolve_policy = lambda *args: (_ for _ in ()).throw(
            RuntimeError(diagnostic)
        )
    else:
        service._resolve_policy = lambda *args: (
            ReviewPolicy(task_board={"type": "fake", "project": "FAKE"}),
            ResolutionMeta({}, {}, ("home layer skipped", diagnostic)),
        )

    with caplog.at_level(logging.WARNING, logger="reviewer.mcp.service"):
        out = service.sync_board(repo="owner/repo")

    assert out["status"] == "error"
    assert out["category"] == "configuration"
    assert (
        "policy transport failed: board=[REDACTED] "
        "github=[REDACTED] gitlab=[REDACTED]"
    ) in out["reason"]
    for secret in ("runtime-secret", "github-vcs-secret", "gitlab-vcs-secret"):
        assert secret not in repr(out)
        assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text
    if diagnostic_kind == "warnings":
        assert "home layer skipped" in out["reason"]
    assert state.providers == []


def test_repo_policy_can_disable_board_with_exact_legacy_error():
    service, state, _ = _service()
    service._resolve_policy = lambda *args: (
        ReviewPolicy(task_board=None),
        ResolutionMeta({}, {}, ()),
    )

    out = service.sync_board(repo="owner/repo")

    assert out == {
        "status": "error",
        "reason": "task board REST is not configured",
    }
    assert state.providers == []


def test_repo_sync_without_branch_uses_primary_branch():
    service, state, _ = _service()
    service.settings = Settings(_env_file=None, review_branches="trunk,main")
    resolved = []

    def resolve_policy(repo, branch):
        resolved.append((repo, branch))
        return (
            ReviewPolicy(task_board={"type": "fake", "project": "FAKE"}),
            ResolutionMeta({"task_board": ".review.yml"}, {}, ()),
        )

    service._resolve_policy = resolve_policy

    service.sync_board(repo="owner/repo")

    assert resolved == [("owner/repo", "trunk")]
    assert state.providers[0].closed is True


def test_repo_sync_rejects_tuple_board_type_before_provider_io():
    service, state, _ = _service()
    service._resolve_policy = lambda *args: (
        ReviewPolicy(task_board={"type": ("fake", "second"), "project": "FAKE"}),
        ResolutionMeta({"task_board": "env"}, {}, ()),
    )

    out = service.sync_board(repo="owner/repo")

    assert out["status"] == "error"
    assert out["category"] == "configuration"
    assert state.providers == []


def test_repo_sync_requires_explicit_type_with_single_configured_provider():
    service, state, _ = _service()
    service._resolve_policy = lambda *args: (
        ReviewPolicy(task_board={"project": "FAKE", "options": {}}),
        ResolutionMeta({"task_board": ".review.yml"}, {}, ()),
    )

    out = service.sync_board(repo="owner/repo")

    assert out["status"] == "error"
    assert out["category"] == "configuration"
    assert "task_board.type" in out["reason"]
    assert state.contexts == []
    assert state.providers == []


def test_repo_sync_without_type_rejects_ambiguous_configured_providers_safely():
    service, state, _ = _service()
    first = service._board_registry.get("fake")
    second = replace(
        first,
        board_type="second",
        credential_fields=(
            CredentialFieldSpec("SECOND_TOKEN", "Second token", secret=True),
        ),
    )
    service._board_registry = BoardProviderRegistry([first, second])
    service._board_credentials = ProviderCredentialSource(
        values={"FAKE_TOKEN": "one", "SECOND_TOKEN": "two"}
    )
    service._resolve_policy = lambda *args: (
        ReviewPolicy(task_board={"project": "FAKE"}),
        ResolutionMeta({"task_board": "env"}, {}, ()),
    )

    out = service.sync_board(repo="owner/repo")

    assert out["status"] == "error"
    assert out["category"] == "configuration"
    assert "task_board.type" in out["reason"]
    assert state.providers == []


def test_explicit_sync_filter_preserves_positional_prefix_and_is_typed():
    class FakeSync:
        def run(self, **kwargs):
            self.called_with = kwargs
            return {"enumerated": 0, "warnings": []}

    fake = FakeSync()

    _Svc(fake).sync_board(
        "FAKE",
        5,
        True,
        False,
        None,
        None,
        True,
        sync_filter={"max_age_days": 30},
    )

    assert fake.called_with == {
        "board": "FAKE",
        "limit": 5,
        "purge_orphaned": True,
        "keep_with_prs": False,
        "force_renormalize": True,
        "sync_filter": TaskSyncFilter(max_age_days=30),
        "filter_source": "explicit",
    }


def test_explicit_mode_preserves_hidden_status_field_migration():
    service, state, _ = _service()

    out = service.sync_board(
        board="FAKE",
        board_type="fake",
        status_field="Stage",
    )

    assert state.contexts[0].options == {"status_field": "Stage"}
    assert "legacy status_field migrated to provider_options.status_field" in out["warnings"]
    assert state.providers[0].closed is True


def test_invalid_explicit_sync_filter_returns_configuration_error_without_io():
    class NoIoSync:
        def run(self, **kwargs):
            raise AssertionError("sync must not run")

    out = _Svc(NoIoSync()).sync_board(
        sync_filter={"max_age_days": 0},
    )

    assert out["status"] == "error"
    assert out["category"] == "configuration"
    assert "max_age_days" in out["reason"]
