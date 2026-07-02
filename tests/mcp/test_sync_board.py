from reviewer.mcp.service import MCPReviewService


class _Svc(MCPReviewService):
    """Обходим тяжёлый __init__: ставим только нужное для sync_board."""
    def __init__(self, sync_service):
        self.components = type("C", (), {"sync_service": sync_service})()


def test_sync_board_no_provider_returns_error():
    out = _Svc(None).sync_board()
    assert out["status"] == "error"
    reason = out["reason"].lower()
    assert "board" in reason
    # подсказка: какой ключ задать и как достать его у yougile/youtrack
    assert "yougile_api_key" in reason or "youtrack_token" in reason
    assert "auth/keys" in reason


def test_sync_board_delegates_to_sync_service():
    class FakeSync:
        def __init__(self):
            self.called_with = None

        def run(self, board=None, limit=None, purge_orphaned=False,
                keep_with_prs=True, board_type=None, status_field=None):
            self.called_with = (board, limit, purge_orphaned, keep_with_prs,
                                board_type, status_field)
            return {"enumerated": 3, "changed": 1, "warnings": []}

    fake = FakeSync()
    out = _Svc(fake).sync_board(board="B", board_type="yougile", limit=5)
    assert out["enumerated"] == 3 and out["changed"] == 1
    assert fake.called_with == ("B", 5, False, True, "yougile", None)


def test_sync_board_threads_board_type():
    class FakeSync:
        def run(self, board=None, limit=None, purge_orphaned=False,
                keep_with_prs=True, board_type=None, status_field=None):
            self.called_with = (board, board_type)
            return {"enumerated": 1, "warnings": []}
    fake = FakeSync()
    _Svc(fake).sync_board(board="PRI", board_type="yougile")
    assert fake.called_with == ("PRI", "yougile")


def test_sync_board_threads_status_field():
    class FakeSync:
        def run(self, board=None, limit=None, purge_orphaned=False,
                keep_with_prs=True, board_type=None, status_field=None):
            self.called_with = status_field
            return {"enumerated": 1, "warnings": []}
    fake = FakeSync()
    _Svc(fake).sync_board(board="TES", board_type="youtrack", status_field="Stage")
    assert fake.called_with == "Stage"


def test_sync_board_failsoft_on_exception():
    class Boom:
        def run(self, **kw):
            raise RuntimeError("kaboom")

    out = _Svc(Boom()).sync_board()
    assert out["status"] == "error" and "kaboom" in out["reason"]
