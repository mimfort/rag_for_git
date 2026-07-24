from dataclasses import replace
from types import SimpleNamespace

from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
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
                keep_with_prs=True, board_type=None, force_renormalize=False):
            self.called_with = (
                board,
                limit,
                purge_orphaned,
                keep_with_prs,
                board_type,
                force_renormalize,
            )
            return {"enumerated": 3, "changed": 1, "warnings": []}

    fake = FakeSync()
    out = _Svc(fake).sync_board(board="B", limit=5)
    assert out["enumerated"] == 3 and out["changed"] == 1
    assert fake.called_with == ("B", 5, False, True, None, False)


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
