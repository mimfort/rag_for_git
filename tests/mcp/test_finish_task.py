import pytest

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
from reviewer.vcs.base import ChangedFile
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
    assert out["task_link_status"] == "added"
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
    # Ссылка уже на месте — это норма, а не сбой: отдельный статус отличает её от неудачи.
    assert out["task_link_status"] == "already_present"
    assert vcs.updated == []
    assert out["warnings"] == []


def test_finish_task_backlink_already_present_when_author_linked_manually():
    # Ручная ссылка автора PR (без нашего маркера) — тоже идемпотентный no-op.
    vcs = _FakeVCS(body="см. [PRI-10](https://board.example/#PRI-10)\n\nтекст")
    out = _Svc(["fake"], vcs=vcs).finish_task("PRI-10", PR_URL)
    assert out["task_link_added"] is False
    assert out["task_link_status"] == "already_present"
    assert vcs.updated == []
    assert out["warnings"] == []


def test_finish_task_backlink_failsoft_on_vcs_error():
    # Доска уже закрыта — сбой правки PR не откатывает успех finish_task.
    vcs = _FakeVCS(fail=True)
    out = _Svc(["fake"], vcs=vcs).finish_task("PRI-10", PR_URL)
    assert out["status"] == "ok"
    assert out["done_set"] is True
    assert out["task_link_added"] is False
    assert out["task_link_status"] == "failed"
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
    assert out["task_link_status"] == "failed"
    assert vcs.updated == []
    assert any("url_template" in w for w in out["warnings"])


def test_finish_task_backlink_skipped_on_unparsable_pr_url():
    vcs = _FakeVCS()
    out = _Svc(["fake"], vcs=vcs).finish_task("PRI-10", "не ссылка")
    assert out["status"] == "ok"
    assert out["task_link_added"] is False
    assert out["task_link_status"] == "failed"
    assert any("не распознан" in w for w in out["warnings"])
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
    assert out["task_link_status"] == "failed"
    assert vcs.updated == []


def test_finish_task_link_status_matches_warnings_contract():
    # Контракт: 'failed' ⇔ есть warning про ссылку; норма ('added'/'already_present') — без него.
    vcs = _FakeVCS()
    out = _Svc(["fake"], vcs=vcs).finish_task("PRI-10", PR_URL)
    assert out["task_link_status"] in {"added", "already_present", "failed"}
    assert out["task_link_added"] is (out["task_link_status"] == "added")
    assert not [w for w in out["warnings"] if "ссылка на задачу" in w]


# --- Съём качества брифа на finish_task (PRI-270) -----------------------------


def _write_brief(tmp_path, name, relevant):
    directory = tmp_path / "docs" / "superpowers" / "briefs"
    directory.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(f"- `{path}` — зачем" for path in relevant)
    (directory / name).write_text(f"# Brief\n\n## Relevant code\n{lines}\n", encoding="utf-8")


class _FakeHistory:
    """Подложка ReviewHistory: помнит аргументы record_brief_quality в recorded."""

    def __init__(self, recorded):
        self._recorded = recorded

    def record_brief_quality(self, run_id, repo, pr_number, head_sha, measurement):
        self._recorded["run_id"] = run_id
        self._recorded["repo"] = repo
        self._recorded["pr_number"] = pr_number
        self._recorded["head_sha"] = head_sha
        self._recorded["status"] = measurement.status
        return 1


class _MetricVCS(_FakeVCS):
    """`_FakeVCS` + дифф/head_sha/base_ref, нужные съёму метрики
    (get_changed_files, PR.head_sha, PR.base_ref)."""

    def __init__(self, *, vcs_raises=False, base_ref="main",
                 changed_path="reviewer/app.py", **kwargs):
        super().__init__(**kwargs)
        self._vcs_raises = vcs_raises
        self._base_ref = base_ref
        self._changed_path = changed_path

    def get_changed_files(self, number):
        if self._vcs_raises:
            raise RuntimeError("VCS недоступен")
        return [ChangedFile(path=self._changed_path, status="modified", patch=None)]

    def get_pull_request(self, number):
        pr = super().get_pull_request(number)
        return type("PR", (), {
            "number": pr.number, "body": pr.body, "head_sha": "deadbeef",
            "base_ref": self._base_ref,
        })()


def _service_with_board(monkeypatch, tmp_path, recorded, *, vcs_raises=False, on_vcs_open=None):
    """Собрать finish_task-сервис с подложками съёма: клон, политика, история.

    `_resolve_policy`/`_repo_clone_path` подменены напрямую — их резолв не
    предмет этой задачи (Task 3/4 уже покрыты своими тестами), а VCS-фабрика
    даёт `_MetricVCS`, умеющую и бэклинк, и `get_changed_files`/`head_sha` для
    съёма метрики — один провайдер на обе операции, как того требует `_pr_session`.
    """
    from reviewer.metrics.brief_quality.config import DEFAULT

    vcs = _MetricVCS(vcs_raises=vcs_raises)
    svc = _Svc(["fake"], vcs=vcs)
    if on_vcs_open is not None:
        factory = svc._vcs_factory

        def _tracked(owner, name):
            on_vcs_open((owner, name))
            return factory(owner, name)

        svc._vcs_factory = _tracked
    svc._review_service = type(
        "RS", (), {"_ensure_history": lambda self: _FakeHistory(recorded)}
    )()
    monkeypatch.setattr(svc, "_repo_clone_path", lambda repo: str(tmp_path))
    monkeypatch.setattr(
        svc, "_resolve_policy",
        lambda repo, branch: (type("P", (), {"brief_quality": DEFAULT})(), None),
    )
    return svc


def test_finish_task_records_brief_quality(monkeypatch, tmp_path):
    """Съём метрики на закрытии задачи: строка пишется, run_id остаётся пустым."""
    _write_brief(tmp_path, "2026-09-01-PRI-1-x.md", ["reviewer/app.py"])
    recorded = {}
    svc = _service_with_board(monkeypatch, tmp_path, recorded)   # хелпер модуля
    out = svc.finish_task("PRI-1", "https://github.com/o/r/pull/7")
    assert out["status"] == "ok"
    assert out["brief_quality_status"] == "measured"
    assert recorded["run_id"] is None and recorded["pr_number"] == 7


def test_finish_task_survives_metric_failure(monkeypatch, tmp_path):
    """Полный отказ съёма не меняет прежний результат finish_task (крит. 4 PRI-270)."""
    svc = _service_with_board(monkeypatch, tmp_path, {}, vcs_raises=True)
    out = svc.finish_task("PRI-1", "https://github.com/o/r/pull/7")
    assert out["status"] == "ok"
    assert out["task_link_status"] in {"added", "already_present", "failed"}
    assert out["brief_quality_status"] is None


def test_finish_task_metric_resolves_policy_from_pr_base_ref_not_primary_branch(
    monkeypatch, tmp_path
):
    """Съём метрики на finish_task обязан взять конфиг ЦЕЛЕВОЙ ветки PR, а не
    первичной ветки репозитория — как и на публикации ревью (`_record_brief_quality`
    берёт `p.prq.base_ref`). Репозиторий отслеживает две ветки с РАЗНЫМ core_paths;
    PR закрывается в неглавную ветку. Если резолв ошибочно возьмёт первичную ветку,
    ядро посчитается по чужому конфигу и статус будет другим."""
    from reviewer.metrics.brief_quality.config import BriefQualityConfig

    _write_brief(tmp_path, "2026-09-01-PRI-1-x.md", ["release_core/app.py"])
    recorded = {}

    primary_config = BriefQualityConfig(core_paths=("primary_core/**",), configured=True)
    target_config = BriefQualityConfig(core_paths=("release_core/**",), configured=True)

    # PR закрывается в "release" — неглавную ветку; "dev" — первичная.
    vcs = _MetricVCS(base_ref="release", changed_path="release_core/app.py")
    svc = _Svc(["fake"], vcs=vcs)
    svc._review_service = type(
        "RS", (), {"_ensure_history": lambda self: _FakeHistory(recorded)}
    )()
    monkeypatch.setattr(svc, "_repo_clone_path", lambda repo: str(tmp_path))
    monkeypatch.setattr(svc, "_bug_branches", lambda repo: ("dev", ("dev", "release")))

    def _resolve_policy(repo, branch):
        if branch == "release":
            return type("P", (), {"brief_quality": target_config})(), None
        if branch == "dev":
            return type("P", (), {"brief_quality": primary_config})(), None
        raise AssertionError(f"неожиданная ветка: {branch}")

    monkeypatch.setattr(svc, "_resolve_policy", _resolve_policy)

    out = svc.finish_task("PRI-1", "https://github.com/o/r/pull/7")

    assert out["status"] == "ok"
    # Ядро "release_core/**" совпадает с изменённым файлом только в конфиге
    # ЦЕЛЕВОЙ ветки — если бы резолв взял первичную "dev", ядро было бы пустым
    # (configured=True → status="empty_core_denominator", а не "measured").
    assert out["brief_quality_status"] == "measured"
    assert recorded["status"] == "measured"


def test_finish_task_opens_vcs_once(monkeypatch, tmp_path):
    """Бэклинк и съём делят одно соединение: PR-ссылка резолвится один раз."""
    opened = []
    svc = _service_with_board(monkeypatch, tmp_path, {}, on_vcs_open=opened.append)
    svc.finish_task("PRI-1", "https://github.com/o/r/pull/7")
    assert len(opened) == 1


# --- Регрессия ревью Task 7: yield внутри except открытия VCS -----------------
#
# `_pr_session` — генератор-контекстменеджер. Протокол генераторов доставляет
# исключение из тела `with`-блока обратно В ГЕНЕРАТОР через .throw() ровно в
# точку yield. Если этот yield лежит внутри try/except, задуманного только для
# ошибок ОТКРЫТИЯ VCS, исключение тела блока ловится той же веткой, логируется
# под чужой причиной и пытается yield'нуть второй раз — что contextlib на выходе
# подменяет на RuntimeError("generator didn't stop after throw()"), теряя
# исходную ошибку. Тесты ниже бьют по `_pr_session` напрямую (без finish_task),
# потому что это тот самый юнит, где сидел дефект.

def test_pr_session_reraises_body_exception():
    """Исключение из тела with-блока доходит наружу как есть, а не как
    RuntimeError('generator didn't stop after throw()')."""
    svc = _Svc(["fake"], vcs=_FakeVCS())
    with pytest.raises(ValueError, match="boom-echo"):
        with svc._pr_session(PR_URL) as (target, vcs, error):
            assert vcs is not None
            raise ValueError("boom-echo")


def test_pr_session_open_failure_unchanged():
    """Сбой ОТКРЫТИЯ VCS (а не тела блока) — поведение прежнее: (target, None,
    текст ошибки), без исключения наружу."""
    svc = _Svc(["fake"])   # _vcs_factory=None и _review_service=None → создание VCS падает
    with svc._pr_session(PR_URL) as (target, vcs, error):
        assert vcs is None
        assert target is not None
        assert error


def test_pr_session_closes_vcs_once_when_service_owns_lifecycle():
    """Закрытие — ровно один раз и только когда `_vcs_factory is None`
    (владелец жизненного цикла — сам finish_task, не тестовая фабрика)."""
    vcs = _FakeVCS()

    class _RS:
        def _create_vcs_provider(self, owner, name, **kwargs):
            return vcs

    svc = _Svc(["fake"])
    svc._review_service = _RS()
    with svc._pr_session(PR_URL) as (target, got_vcs, error):
        assert got_vcs is vcs
        assert vcs.closed is False   # ещё не закрыт внутри блока
    assert vcs.closed is True        # закрыт по выходу


def test_pr_session_does_not_close_test_owned_vcs():
    """Тестовая `_vcs_factory` владеет жизненным циклом сама: `_pr_session` не закрывает."""
    vcs = _FakeVCS()
    svc = _Svc(["fake"], vcs=vcs)
    with svc._pr_session(PR_URL) as (target, got_vcs, error):
        assert got_vcs is vcs
    assert vcs.closed is False


def test_finish_task_backlink_failure_surfaces_original_reason(monkeypatch, tmp_path):
    """End-to-end воспроизведение находки ревью: сбой `_apply_backlink` не
    маскируется под RuntimeError generator-протокола — доска к этому моменту
    уже записана, но причина ошибки в ответе — подлинная."""
    recorded = {}
    svc = _service_with_board(monkeypatch, tmp_path, recorded)

    def _boom(*a, **k):
        raise ValueError("boom-echo")

    monkeypatch.setattr(svc, "_apply_backlink", _boom)
    out = svc.finish_task("PRI-1", "https://github.com/o/r/pull/7")
    assert out["status"] == "error"
    assert "boom-echo" in out["reason"]
    assert "didn't stop after throw" not in out["reason"]
