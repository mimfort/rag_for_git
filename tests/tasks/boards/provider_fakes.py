"""Полные fake provider/spec для тестов generic registry lifecycle."""
from __future__ import annotations

from reviewer.tasks.boards.base import TaskListing
from reviewer.tasks.boards.registry import (
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderSetupSpec,
)
from tests.provider_access import FAKE_PROVIDER_ACCESS


class FakeBoard:
    board_type = "fake"

    def __init__(self, context: ProviderBuildContext) -> None:
        self.context = context
        self.closed = False

    def validate_connection(self, project=None):
        return {
            "status": "ok",
            "identity": {},
            "project": project,
            "capabilities": [],
            "warnings": [],
        }

    def iter_raw(self, board, limit, *, sync_filter=None, now_ms=None) -> TaskListing:
        return TaskListing(rows=())

    def normalize(self, raw):
        return {}

    def normalize_meta(self, raw):
        return {}

    def fetch_one(self, key):
        return None

    def list_targets(self, project):
        return {"targets": [], "options": [], "warnings": []}

    def create(self, doc_md, *, title, target, project):
        return {}

    def finish(self, key, pr_url, *, note=None, mark_done=True, target=None):
        return {}

    def close(self):
        self.closed = True


def fake_provider_spec(*, factory=None, board_type: str = "fake") -> BoardProviderSpec:
    return BoardProviderSpec(
        board_type=board_type,
        factory=factory or FakeBoard,
        credential_fields=(
            CredentialFieldSpec(env="FAKE_TOKEN", label="Fake token", secret=True),
        ),
        setup=ProviderSetupSpec(
            label="Fake",
            help_url="https://example.test/fake",
            help_text="Configure the fake provider.",
            access=FAKE_PROVIDER_ACCESS,
        ),
    )
