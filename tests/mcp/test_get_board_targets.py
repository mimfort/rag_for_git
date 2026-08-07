from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.tasks.boards.base import NativeSubtaskIdentity, TaskListing
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.registry import (
    BoardProviderRegistry,
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderSetupSpec,
)


class _Provider:
    board_type = "fake"

    def __init__(self, targets):
        self.targets = targets
        self.project = "UNSET"
        self.closed = False

    def validate_connection(self, project=None):
        return {}

    def iter_raw(self, board, limit, *, sync_filter=None, now_ms=None):
        return TaskListing(rows=())

    def normalize(self, raw):
        return {}

    def normalize_meta(self, raw):
        return {}

    def fetch_one(self, key):
        return None

    def list_targets(self, project):
        self.project = project
        return self.targets

    def create(self, doc_md, *, title, target, project):
        return {}

    def finish(self, key, pr_url, *, note=None, mark_done=True, target=None):
        return {}

    def close(self):
        self.closed = True


class _NativeSubtaskProvider(_Provider):
    def reconcile_native_subtasks(self, source_board_id, markers):
        return []

    def create_native_subtask(self, doc_md, *, title, source_column_id, marker):
        return NativeSubtaskIdentity("child-id", "FAKE-2", title)

    def replace_native_subtasks(self, parent_task_id, subtask_ids):
        return None


class _Svc(MCPReviewService):
    def __init__(self, configured, provider=None, capabilities=frozenset()):
        provider = provider or _Provider(
            {"targets": [], "options": [], "warnings": []}
        )
        self.settings = Settings(_env_file=None)
        specs = []
        values = {}
        for board_type in configured:
            def factory(context: ProviderBuildContext, type_=board_type):
                provider.board_type = type_
                return provider

            env = f"{board_type.upper()}_TOKEN"
            specs.append(BoardProviderSpec(
                board_type=board_type,
                factory=factory,
                credential_fields=(CredentialFieldSpec(env, "Token", secret=True),),
                setup=ProviderSetupSpec(board_type, "https://fake/help", "Configure."),
                capabilities=capabilities,
            ))
            values[env] = "secret"
        self._board_registry = BoardProviderRegistry(specs)
        self._board_credentials = ProviderCredentialSource(values=values)


def test_get_board_targets_single_board_threads_project():
    prov = _Provider({
        "targets": [{"id": "c1", "label": "Готово", "purposes": ["done"]}],
        "options": [],
        "warnings": [],
    })
    out = _Svc(["fake"], prov).get_board_targets(project="PRI")
    assert out["board_type"] == "fake"
    assert out["project"] == "PRI"
    assert out["capabilities"] == []
    assert out["targets"][0]["label"] == "Готово"
    assert prov.project == "PRI"
    assert prov.closed is True
    # креды наружу не отдаются
    assert "api_key" not in out and "token" not in out


def test_get_board_targets_ambiguous_requires_type():
    out = _Svc(["first", "second"]).get_board_targets()
    assert out["status"] == "error"
    assert "board_type" in out["reason"]


def test_get_board_targets_not_configured():
    out = _Svc([]).get_board_targets(board_type="fake")
    assert out["status"] == "error"


def test_get_board_targets_explicit_type():
    prov = _Provider({
        "targets": [{"id": "done", "label": "Готово", "purposes": ["done"]}],
        "options": [],
        "warnings": [],
    })
    out = _Svc(["first", "second"], prov).get_board_targets(
        board_type="second",
        project="TES",
    )
    assert out["board_type"] == "second"
    assert out["targets"][0]["id"] == "done"
    assert prov.project == "TES"


def test_get_board_targets_failsoft():
    class Boom(_Provider):
        def __init__(self):
            super().__init__({})

        def list_targets(self, project):
            raise RuntimeError("kaboom")

    provider = Boom()
    out = _Svc(["fake"], provider).get_board_targets()
    assert out["status"] == "error"
    assert "kaboom" in out["reason"]
    assert provider.closed is True


def test_get_board_targets_preserves_safe_structured_provider_error():
    secret = "upstream-secret"

    class Boom(_Provider):
        def __init__(self):
            super().__init__({})

        def list_targets(self, project):
            raise BoardProviderError(
                "rate_limit",
                f"Jira throttled {secret}",
                hint=f"retry after rotating {secret}",
                retryable=True,
                secrets=(secret,),
            )

    out = _Svc(["fake"], Boom()).get_board_targets()

    assert out == {
        "status": "error",
        "reason": "Jira throttled [REDACTED]",
        "category": "rate_limit",
        "hint": "retry after rotating [REDACTED]",
        "retryable": True,
    }
    assert secret not in repr(out)


def test_get_board_targets_uses_authoritative_sorted_registry_metadata():
    provider = _NativeSubtaskProvider({
        "board_type": "spoofed",
        "project": "spoofed",
        "capabilities": ["spoofed"],
        "targets": [],
        "options": [],
        "warnings": [],
    })

    out = _Svc(
        ["fake"],
        provider,
        capabilities=frozenset({"native_subtasks"}),
    ).get_board_targets(project="PRI")

    assert out["board_type"] == "fake"
    assert out["project"] == "PRI"
    assert out["capabilities"] == ["native_subtasks"]
