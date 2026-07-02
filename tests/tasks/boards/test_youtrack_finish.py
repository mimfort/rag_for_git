from reviewer.tasks.boards.youtrack import YouTrackBoard, _state_of

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
    b._status_field = "State"
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
    assert cmd[2]["query"] == "State {Fixed}"  # значение в {…}: анти-DSL-инъекция
    assert cmd[2]["issues"] == [{"idReadable": "TES-1"}]


def test_youtrack_finish_default_state_fixed():
    b = _board({"/issues/TES-1": _Resp(200, {"description": ""})})
    b.finish("TES-1", PR)  # done_state не задан
    cmd = next(c for c in b._client.calls if c[1] == "/commands")
    assert cmd[2]["query"] == "State {Fixed}"


def test_youtrack_finish_state_command_injection_neutralized():
    # done_state с попыткой дописать команду в DSL: фигурные скобки убраны,
    # значение обёрнуто в единый {…} — доп. команды не выполнятся.
    b = _board({"/issues/TES-1": _Resp(200, {"description": ""})})
    b.finish("TES-1", PR, done_state="Fixed} tag urgent {")
    cmd = next(c for c in b._client.calls if c[1] == "/commands")
    assert cmd[2]["query"] == "State {Fixed tag urgent }"
    assert "}" not in cmd[2]["query"][:-1]  # нет закрывающей скобки внутри значения


def test_youtrack_finish_encodes_key_in_path():
    # ключ с путевыми сегментами не должен выходить за /issues/ (path traversal).
    b = _board({"/issues/..%2Fadmin": _Resp(200, {"description": ""})})
    b.finish("../admin", PR, mark_done=False)
    assert any(c[1] == "/issues/..%2Fadmin" for c in b._client.calls)


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


def test_state_of_reads_custom_field():
    issue = {"customFields": [{"name": "Stage", "value": {"name": "Готово"}}]}
    assert _state_of(issue, "Stage") == "Готово"
    assert _state_of(issue) is None  # дефолт State — поля нет


def test_youtrack_finish_uses_configured_status_field():
    b = _board({"/issues/TES-1": _Resp(200, {"description": ""})})
    b._status_field = "Stage"  # как выставит make_board_provider/set_status_field
    b.finish("TES-1", PR, done_state="Готово")
    cmd = next(c for c in b._client.calls if c[1] == "/commands")
    assert cmd[2]["query"] == "Stage {Готово}"


def test_youtrack_finish_rejects_unsafe_status_field():
    # Имя поля идёт в DSL голым токеном; небезопасное (скобки + пробелы = попытка вклинить
    # команду) отклоняется fail-soft: /commands НЕ вызывается, warning есть, done_set=False.
    b = _board({"/issues/TES-1": _Resp(200, {"description": ""})})
    b._status_field = "Stage} tag x {"  # попытка DSL-инъекции через имя поля
    res = b.finish("TES-1", PR, done_state="Готово")
    assert not any(c[1] == "/commands" for c in b._client.calls)  # команда не отправлена
    assert res["done_set"] is False
    assert res["warnings"]  # предупреждение о небезопасном имени поля


def test_youtrack_finish_allows_cyrillic_status_field():
    # Кириллическое одиночное имя поля валидно (Unicode \w); многословное значение
    # в {…} сохраняется целиком.
    b = _board({"/issues/TES-1": _Resp(200, {"description": ""})})
    b._status_field = "Готовность"
    res = b.finish("TES-1", PR, done_state="Готово к сдаче")
    cmd = next(c for c in b._client.calls if c[1] == "/commands")
    assert cmd[2]["query"] == "Готовность {Готово к сдаче}"
    assert res["done_set"] is True


def test_youtrack_set_status_field_defaults_to_state():
    b = _board({"/issues/TES-1": _Resp(200, {"description": ""})})
    b.set_status_field(None)
    b.finish("TES-1", PR, done_state="Fixed")
    cmd = next(c for c in b._client.calls if c[1] == "/commands")
    assert cmd[2]["query"] == "State {Fixed}"
