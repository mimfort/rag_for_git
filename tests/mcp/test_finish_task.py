from reviewer.config.provider_credentials import ProviderCredentialSource
from reviewer.config.settings import Settings
from reviewer.mcp.service import MCPReviewService
from reviewer.tasks.boards.base import RawTask, TaskListing
from reviewer.tasks.boards.registry import (
    BoardProviderRegistry,
    BoardProviderSpec,
    CredentialFieldSpec,
    ProviderBuildContext,
    ProviderOptionSpec,
    ProviderSetupSpec,
)
from tests.provider_access import FAKE_PROVIDER_ACCESS


class _FakeTaskService:
    def __init__(self):
        self.indexed = []

    def index_task(self, brief):
        self.indexed.append(brief)
        return {"key": brief.get("key"), "embedded": True, "warnings": []}


class _Provider:
    board_type = "fake"

    def __init__(self, raw=None):
        self.finished = None
        self.closed = False
        self.fetched = None
        self._raw = raw if raw is not None else RawTask(
            key="ID-10", project_code="PRI-10", title="T", description="d",
            status=None, subtask_ids=[], timestamp=1, terminal=True)

    def validate_connection(self, project=None):
        return {}

    def iter_raw(self, board, limit, *, sync_filter=None, now_ms=None):
        return TaskListing(rows=())

    def normalize_meta(self, raw):
        return self.normalize(raw)

    def list_targets(self, project):
        return {"targets": [], "options": [], "warnings": []}

    def create(self, doc_md, *, title, target, project):
        return {}

    def finish(self, key, pr_url, *, note=None, mark_done=True, target=None):
        self.finished = (key, pr_url, note, mark_done, target)
        return {"key": key, "board_id": "u1", "done_set": True,
                "pr_link_added": True, "already_closed": False, "warnings": []}

    def fetch_one(self, key):
        self.fetched = key
        return self._raw

    def normalize(self, raw):
        return {"key": raw.key, "title": raw.title, "status": "done",
                "project": "PRI", "description": raw.description,
                "url": "https://board.example/#PRI-10"}

    def close(self):
        self.closed = True


class _FakeVCS:
    def __init__(self, body="## Что сделано\n\nтекст", fail=False):
        self.body = body
        self.fail = fail
        self.updated = []
        self.closed = False

    def get_pull_request(self, number):
        if self.fail:
            raise RuntimeError("403 нет прав")
        return type("PR", (), {"number": number, "body": self.body})()

    def update_pull_request_body(self, number, body):
        self.updated.append((number, body))

    def close(self):
        self.closed = True


class _Svc(MCPReviewService):
    def __init__(self, configured, provider=None, task_service=None, contexts=None,
                 vcs=None):
        provider = provider or _Provider()
        contexts = contexts if contexts is not None else []
        self.settings = Settings(_env_file=None)
        self.components = type("C", (), {
            "task_service": task_service or _FakeTaskService()})()
        self._vcs_factory = (lambda owner, name: vcs) if vcs is not None else None
        self._review_service = None   # реальный ReviewService не нужен: VCS даёт фабрика
        specs = []
        values = {}
        for board_type in configured:
            def factory(context: ProviderBuildContext, type_=board_type):
                contexts.append(context)
                provider.board_type = type_
                return provider

            env = f"{board_type.upper()}_TOKEN"
            specs.append(BoardProviderSpec(
                board_type=board_type,
                factory=factory,
                credential_fields=(CredentialFieldSpec(env, "Token", secret=True),),
                option_fields=(ProviderOptionSpec("status_field", "Status field"),),
                setup=ProviderSetupSpec(
                    board_type, "https://fake/help", "Configure.", FAKE_PROVIDER_ACCESS
                ),
            ))
            values[env] = "secret"
        self._board_registry = BoardProviderRegistry(specs)
        self._board_credentials = ProviderCredentialSource(values=values)


def test_finish_task_resolves_single_board():
    prov = _Provider()
    out = _Svc(["fake"], prov).finish_task("PRI-10", "https://github.com/o/r/pull/7")
    assert out["status"] == "ok"
    assert out["board_type"] == "fake"
    assert out["done_set"] is True
    assert prov.finished == ("PRI-10", "https://github.com/o/r/pull/7",
                             None, True, None)
    assert prov.closed is True


def test_finish_task_no_board_configured():
    out = _Svc([]).finish_task("PRI-10", "url")
    assert out["status"] == "error"
    assert "board_type" in out["reason"]


def test_finish_task_ambiguous_requires_board_type():
    out = _Svc(["first", "second"]).finish_task("PRI-10", "url")
    assert out["status"] == "error"


def test_finish_task_explicit_board_type():
    prov = _Provider()
    out = _Svc(["first", "second"], prov).finish_task(
        "TES-1", "url", board_type="second", target="Done")
    assert out["status"] == "ok"
    assert out["board_type"] == "second"
    assert prov.finished[4] == "Done"


def test_finish_task_migrates_legacy_status_field_and_done_column():
    prov = _Provider()
    contexts = []
    out = _Svc(["fake"], prov, contexts=contexts).finish_task(
        "TES-1",
        "url",
        board_type="fake",
        status_field="Stage",
        done_column="Готово",
    )
    assert contexts[0].options == {"status_field": "Stage"}
    assert prov.finished[4] == "Готово"
    assert len(out["warnings"]) == 3   # 2 migration + пропуск бэклинка (pr_url не распознан)


def test_finish_task_failsoft():
    class Boom(_Provider):
        def finish(self, *a, **k):
            raise RuntimeError("kaboom")

    provider = Boom()
    out = _Svc(["fake"], provider).finish_task("PRI-10", "url")
    assert out["status"] == "error"
    assert "kaboom" in out["reason"]
    assert provider.closed is True


def test_finish_task_writes_through_to_store():
    # После записи в доску закрытая задача сразу переиндексируется в стор reviewer
    # (без гонки с watermark инкрементального sync_board).
    prov = _Provider()
    ts = _FakeTaskService()
    out = _Svc(["fake"], prov, task_service=ts).finish_task(
        "PRI-10", "https://github.com/o/r/pull/7")
    assert out["status"] == "ok"
    assert out["reindexed"] is True
    assert prov.fetched == "PRI-10"
    assert len(ts.indexed) == 1
    assert ts.indexed[0]["key"] == "ID-10"
    assert ts.indexed[0]["status"] == "done"


def test_finish_task_writethrough_failsoft_when_fetch_none():
    # fetch_one вернул None (сбой доски) → finish всё равно ok, reindexed=False,
    # стор не трогается (fallback — обычный sync_board).
    class _NoRaw(_Provider):
        def fetch_one(self, key):
            self.fetched = key
            return None

    prov = _NoRaw()
    ts = _FakeTaskService()
    out = _Svc(["fake"], prov, task_service=ts).finish_task("PRI-10", "url")
    assert out["status"] == "ok"
    assert out["reindexed"] is False
    assert ts.indexed == []


PR_URL = "https://github.com/o/r/pull/7"


def test_finish_task_backlinks_task_into_pr_body():
    # Связь двусторонняя: PR-ссылка ушла в задачу, ссылка на задачу — в тело PR.
    vcs = _FakeVCS()
    out = _Svc(["fake"], vcs=vcs).finish_task("PRI-10", PR_URL)
    assert out["status"] == "ok"
    assert out["task_link_added"] is True
    assert len(vcs.updated) == 1
    number, body = vcs.updated[0]
    assert number == 7
    assert body.startswith("Задача: [PRI-10](https://board.example/#PRI-10)")
    assert "<!-- reviewer:task-link -->" in body
    assert body.endswith("## Что сделано\n\nтекст")


def test_finish_task_backlink_idempotent_on_second_run():
    vcs = _FakeVCS(body="Задача: [PRI-10](https://board.example/#PRI-10)\n"
                        "<!-- reviewer:task-link -->\n\nтекст")
    out = _Svc(["fake"], vcs=vcs).finish_task("PRI-10", PR_URL)
    assert out["status"] == "ok"
    assert out["task_link_added"] is False
    assert vcs.updated == []
    assert out["warnings"] == []


def test_finish_task_backlink_failsoft_on_vcs_error():
    # Доска уже закрыта — сбой правки PR не откатывает успех finish_task.
    vcs = _FakeVCS(fail=True)
    out = _Svc(["fake"], vcs=vcs).finish_task("PRI-10", PR_URL)
    assert out["status"] == "ok"
    assert out["done_set"] is True
    assert out["task_link_added"] is False
    assert any("403" in w for w in out["warnings"])


def test_finish_task_backlink_skipped_without_task_url():
    class _NoUrl(_Provider):
        def normalize(self, raw):
            return {"key": raw.key, "title": raw.title, "status": "done",
                    "project": "PRI", "description": raw.description, "url": None}

    vcs = _FakeVCS()
    out = _Svc(["fake"], _NoUrl(), vcs=vcs).finish_task("PRI-10", PR_URL)
    assert out["status"] == "ok"
    assert out["task_link_added"] is False
    assert vcs.updated == []
    assert any("url_template" in w for w in out["warnings"])


def test_finish_task_backlink_skipped_on_unparsable_pr_url():
    vcs = _FakeVCS()
    out = _Svc(["fake"], vcs=vcs).finish_task("PRI-10", "не ссылка")
    assert out["status"] == "ok"
    assert out["task_link_added"] is False
    assert vcs.updated == []


def test_finish_task_backlink_skipped_when_writethrough_failed():
    # Без брифа неоткуда взять url задачи — но finish всё равно ok.
    class _NoRaw(_Provider):
        def fetch_one(self, key):
            return None

    vcs = _FakeVCS()
    out = _Svc(["fake"], _NoRaw(), vcs=vcs).finish_task("PRI-10", PR_URL)
    assert out["status"] == "ok"
    assert out["reindexed"] is False
    assert out["task_link_added"] is False
    assert vcs.updated == []
