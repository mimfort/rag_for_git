from dataclasses import dataclass
from types import SimpleNamespace

from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.tasks.boards.base import RawTask
from reviewer.tasks.boards.registry import (
    BoardProviderRegistry,
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderOptionSpec,
    ProviderSetupSpec,
)
from reviewer.tasks.sync import SyncService


class _TaskService:
    def __init__(self):
        self.indexed = []

    def index_task(self, task):
        self.indexed.append(task)
        return {"key": task["key"], "embedded": True}

    def index_batch(self, tasks):
        self.indexed.extend(tasks)
        return [{"key": task["key"], "embedded": True, "warnings": []} for task in tasks]

    def refresh_meta_batch(self, tasks):
        return {"meta_refreshed": len(tasks), "warnings": []}

    def purge_orphaned_tasks(self, active_keys, *, keep_with_prs=True, project=None):
        return {
            "deleted_store": 0,
            "deleted_graph": 0,
            "protected_prs": 0,
            "warnings": [],
        }


class _MetaStore:
    def __init__(self):
        self.values = {}

    def get_index_meta(self, repo, ref):
        return self.values.get((repo, ref))

    def set_index_meta(self, repo, ref, value):
        self.values[(repo, ref)] = value


@dataclass
class _State:
    contexts: list[ProviderBuildContext]
    providers: list["_Provider"]


class _Provider:
    board_type = "fake"

    def __init__(self, context, state, *, secret_warning=False, fail_targets=False):
        self.context = context
        self.state = state
        self.closed = False
        self.secret_warning = secret_warning
        self.fail_targets = fail_targets
        state.providers.append(self)

    def validate_connection(self, project=None):
        return {"status": "ok", "identity": {}, "project": project,
                "capabilities": [], "warnings": []}

    def iter_raw(self, board, limit):
        yield self.fetch_one("FAKE-1")

    def normalize(self, raw):
        return {"key": raw.key, "title": raw.title, "description": raw.description,
                "status": raw.status, "project": "FAKE", "aliases": [],
                "criteria": [], "url": None, "links": [], "attachments": []}

    def normalize_meta(self, raw):
        return self.normalize(raw)

    def fetch_one(self, key):
        return RawTask(
            key=key,
            project_code=key,
            title="Fake task",
            description="body",
            status="Open",
            subtask_ids=[],
            timestamp=1,
        )

    def list_targets(self, project):
        if self.fail_targets:
            raise RuntimeError(
                f"upstream failure {self.context.credentials['FAKE_TOKEN']}"
            )
        warnings = (
            [f"warning {self.context.credentials['FAKE_TOKEN']}"]
            if self.secret_warning
            else []
        )
        return {
            "targets": [{"id": "done", "label": "Done", "purposes": ["create", "done"]}],
            "options": [],
            "warnings": warnings,
        }

    def create(self, doc_md, *, title, target, project):
        return {"key": "FAKE-1", "url": "https://fake/FAKE-1",
                "target_resolved": target, "warnings": []}

    def finish(self, key, pr_url, *, note=None, mark_done=True, target=None):
        return {"key": key, "done_set": True, "pr_link_added": True,
                "already_closed": False, "warnings": []}

    def close(self):
        self.closed = True


def _service(*, secret_warning=False, fail_targets=False):
    state = _State([], [])

    def factory(context):
        state.contexts.append(context)
        return _Provider(
            context,
            state,
            secret_warning=secret_warning,
            fail_targets=fail_targets,
        )

    spec = BoardProviderSpec(
        board_type="fake",
        factory=factory,
        credential_fields=(
            CredentialFieldSpec("FAKE_TOKEN", "Token", secret=True),
        ),
        option_fields=(ProviderOptionSpec("lane", "Lane"),),
        setup=ProviderSetupSpec("Fake", "https://fake/help", "Configure."),
    )
    registry = BoardProviderRegistry([spec])
    credentials = ProviderCredentialSource(values={"FAKE_TOKEN": "runtime-secret"})
    tasks = _TaskService()
    meta = _MetaStore()
    service = MCPReviewService.__new__(MCPReviewService)
    service.settings = Settings(_env_file=None)
    service.components = SimpleNamespace(
        task_service=tasks,
        store=meta,
        sync_service=SyncService([], tasks, meta),
    )
    service._board_registry = registry
    service._board_credentials = credentials
    return service, state, tasks


def test_fake_provider_runs_full_mcp_lifecycle_without_production_registration():
    service, state, tasks = _service()
    options = {"lane": "Backend"}

    targets = service.get_board_targets("fake", "FAKE", options)
    created = service.create_task(
        "Title",
        board_type="fake",
        project="FAKE",
        target="done",
        provider_options=options,
    )
    finished = service.finish_task(
        "FAKE-1",
        "https://github.test/pr/1",
        board_type="fake",
        target="done",
        provider_options=options,
    )
    synced = service.sync_board(
        board="FAKE",
        board_type="fake",
        provider_options=options,
    )

    assert targets["targets"][0]["id"] == "done"
    assert created["reindexed"] is True
    assert finished["reindexed"] is True
    assert synced["enumerated"] == 1
    assert len(tasks.indexed) == 3
    assert all(context.options == options for context in state.contexts)
    assert len(state.providers) == 4
    assert all(provider.closed for provider in state.providers)


def test_mcp_boundary_sanitizes_provider_warnings_and_errors():
    service, _, _ = _service(secret_warning=True)
    result = service.get_board_targets("fake")
    assert "runtime-secret" not in repr(result)
    assert "[REDACTED]" in repr(result)

    failing, state, _ = _service(fail_targets=True)
    result = failing.get_board_targets("fake")
    assert result["status"] == "error"
    assert "runtime-secret" not in repr(result)
    assert state.providers[0].closed is True
