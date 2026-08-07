"""fetch_one(key) — единичный RawTask по idReadable для write-through после finish."""
from reviewer.config.task_board import TaskSyncFilter
from reviewer.tasks.boards.base import TaskListing
from reviewer.tasks.boards.youtrack import YouTrackBoard


class _Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data if json_data is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _Client:
    def __init__(self, get_routes):
        self._get = get_routes
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        return self._get[path]

    def close(self):
        pass


def _board(get_routes, status_field="State"):
    b = YouTrackBoard.__new__(YouTrackBoard)
    b._client = _Client(get_routes)
    b._status_field = status_field
    return b


def test_fetch_one_builds_rawtask():
    issue = {"idReadable": "TES-5", "summary": "Саммари", "description": "тело",
             "updated": 999,
             "customFields": [{"name": "State", "value": {"name": "Fixed"}}]}
    b = _board({"/issues/TES-5": _Resp(200, issue)})
    raw = b.fetch_one("TES-5")
    assert raw is not None
    assert raw.key == "TES-5"
    assert raw.title == "Саммари"
    assert raw.status == "Fixed"
    assert raw.timestamp == 999
    assert raw.archived is None
    assert raw.terminal is None


def test_fetch_one_honours_custom_status_field():
    issue = {"idReadable": "TES-6", "summary": "S", "description": "",
             "updated": 1,
             "customFields": [{"name": "Stage", "value": {"name": "Готово"}}]}
    b = _board({"/issues/TES-6": _Resp(200, issue)}, status_field="Stage")
    raw = b.fetch_one("TES-6")
    assert raw.status == "Готово"


def test_fetch_one_none_on_error():
    b = _board({"/issues/TES-404": _Resp(500, {})})
    assert b.fetch_one("TES-404") is None


def test_iter_raw_returns_listing_with_zero_stats_and_no_lifecycle_guessing():
    issues = [
        {
            "idReadable": "TES-1",
            "updated": 999,
            "customFields": [{"name": "State", "value": {"name": "Fixed"}}],
        },
        {"idReadable": "TES-2"},
        {"idReadable": "TES-3", "updated": "invalid"},
    ]
    board = _board({"/issues": _Resp(200, issues)})

    listing = board.iter_raw(
        "TES",
        None,
        sync_filter=TaskSyncFilter(max_age_days=30, include_archived=False),
        now_ms=123,
    )

    assert isinstance(listing, TaskListing)
    rows = list(listing)
    assert [row.timestamp for row in rows] == [999, None, None]
    assert all(row.archived is None and row.terminal is None for row in rows)
    assert listing.stats.filtered_by_age == 0
    assert listing.stats.filtered_archived == 0
    assert listing.stats.warnings == []
