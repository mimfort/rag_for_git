from reviewer.tasks.boards.youtrack import YouTrackBoard

PR = "https://github.com/o/r/pull/7"


class _Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _Client:
    def __init__(self, get_routes, post_status=None):
        self._get = get_routes
        self._post_status = post_status or {}  # path -> status
        self.calls = []  # (method, path, json)

    def get(self, path, params=None):
        self.calls.append(("GET", path, None))
        return self._get[path]

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        return _Resp(self._post_status.get(path, 200), {})

    def close(self):
        pass


def _board(get_routes, post_status=None):
    b = YouTrackBoard.__new__(YouTrackBoard)  # обойти httpx.Client в __init__
    b._client = _Client(get_routes, post_status)
    return b


def test_youtrack_finish_edits_desc_and_runs_state_command():
    b = _board({"/issues/TES-1": _Resp(200, {"description": "тело"})})
    res = b.finish("TES-1", PR, done_state="Fixed")
    assert res["pr_link_added"] is True
    assert res["done_set"] is True
    posts = [c for c in b._client.calls if c[0] == "POST"]
    edit = next(c for c in posts if c[1] == "/issues/TES-1")
    assert PR in edit[2]["description"]
    cmd = next(c for c in posts if c[1] == "/commands")
    assert cmd[2]["query"] == "State Fixed"
    assert cmd[2]["issues"] == [{"idReadable": "TES-1"}]


def test_youtrack_finish_default_state_fixed():
    b = _board({"/issues/TES-1": _Resp(200, {"description": ""})})
    b.finish("TES-1", PR)  # done_state не задан
    cmd = next(c for c in b._client.calls if c[1] == "/commands")
    assert cmd[2]["query"] == "State Fixed"


def test_youtrack_finish_command_failsoft():
    b = _board({"/issues/TES-1": _Resp(200, {"description": ""})},
               post_status={"/commands": 400})
    res = b.finish("TES-1", PR, done_state="NoSuchState")
    assert res["done_set"] is False
    assert res["warnings"]  # предупреждение о неуспешной команде, без краха


def test_youtrack_finish_idempotent_pr_link():
    b = _board({"/issues/TES-1": _Resp(200, {"description": f"тело\n\nPR: {PR}"})})
    res = b.finish("TES-1", PR, mark_done=False)
    assert res["pr_link_added"] is False
    assert not [c for c in b._client.calls if c[1] == "/issues/TES-1" and c[0] == "POST"]
