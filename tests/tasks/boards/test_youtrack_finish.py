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
    """Фейк httpx-клиента. Два POST на один путь /issues/{key} (описание + customFields)
    различаются по телу: статус ответа на field-update берётся из post_status['__fields__'],
    на правку описания — из post_status[path]."""

    def __init__(self, get_routes, post_status=None):
        self._get = get_routes
        self._post_status = post_status or {}  # path | "__fields__" -> status
        self.calls = []  # (method, path, json)

    def get(self, path, params=None):
        self.calls.append(("GET", path, None))
        return self._get[path]

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        if json and "customFields" in json:
            status = self._post_status.get("__fields__", 200)
        else:
            status = self._post_status.get(path, 200)
        return _Resp(status, {})

    def close(self):
        pass


def _board(get_routes, post_status=None):
    b = YouTrackBoard.__new__(YouTrackBoard)  # обойти httpx.Client в __init__
    b._client = _Client(get_routes, post_status)
    b._status_field = "State"
    return b


def _state_field(name="State", ftype="StateIssueCustomField",
                 value_type="StateBundleElement", value_name="Open"):
    value = {"$type": value_type, "name": value_name} if value_type else {"name": value_name}
    return {"name": name, "$type": ftype, "value": value}


def _field_post(calls):
    """POST со сменой статуса (тело содержит customFields)."""
    return next(c for c in calls if c[0] == "POST" and "customFields" in (c[2] or {}))


def _has_field_post(calls):
    return any(c[0] == "POST" and "customFields" in (c[2] or {}) for c in calls)


def test_youtrack_finish_edits_desc_and_sets_state_field():
    b = _board({"/issues/TES-1": _Resp(200, {
        "description": "тело", "customFields": [_state_field()]})})
    res = b.finish("TES-1", PR, done_state="Fixed")
    assert res["pr_link_added"] is True
    assert res["done_set"] is True
    posts = [c for c in b._client.calls if c[0] == "POST"]
    edit = next(c for c in posts if "description" in (c[2] or {}))
    assert PR in edit[2]["description"]
    payload = _field_post(posts)[2]["customFields"][0]
    assert payload["name"] == "State"
    assert payload["$type"] == "StateIssueCustomField"
    assert payload["value"]["name"] == "Fixed"
    assert payload["value"]["$type"] == "StateBundleElement"
    # DSL команд больше нет
    assert not any(c[1] == "/commands" for c in b._client.calls)


def test_youtrack_finish_default_state_fixed():
    b = _board({"/issues/TES-1": _Resp(200, {
        "description": "", "customFields": [_state_field()]})})
    b.finish("TES-1", PR)  # done_state не задан
    payload = _field_post(b._client.calls)[2]["customFields"][0]
    assert payload["value"]["name"] == "Fixed"


def test_youtrack_finish_malicious_value_is_verbatim_no_dsl():
    # «Злое» значение с DSL-пунктуацией уходит в value.name ВЕРБАТИМ (структурно, без
    # command-DSL) — инъекция невозможна конструктивно; /commands не вызывается.
    b = _board({"/issues/TES-1": _Resp(200, {
        "description": "", "customFields": [_state_field()]})})
    b.finish("TES-1", PR, done_state="Fixed} tag urgent {")
    payload = _field_post(b._client.calls)[2]["customFields"][0]
    assert payload["value"]["name"] == "Fixed} tag urgent {"
    assert not any(c[1] == "/commands" for c in b._client.calls)


def test_youtrack_finish_encodes_key_in_path():
    # ключ с путевыми сегментами не должен выходить за /issues/ (path traversal).
    b = _board({"/issues/..%2Fadmin": _Resp(200, {"description": "", "customFields": []})})
    b.finish("../admin", PR, mark_done=False)
    assert any(c[1] == "/issues/..%2Fadmin" for c in b._client.calls)


def test_youtrack_finish_field_update_failsoft():
    # REST-обновление поля вернуло 400 (невалидное значение) → done_set False, warning,
    # но PR-ссылка всё равно записана.
    b = _board({"/issues/TES-1": _Resp(200, {
        "description": "", "customFields": [_state_field()]})},
               post_status={"__fields__": 400})
    res = b.finish("TES-1", PR, done_state="NoSuchState")
    assert res["done_set"] is False
    assert res["warnings"]
    assert res["pr_link_added"] is True


def test_youtrack_finish_idempotent_pr_link():
    # mark_done=False + PR уже в описании: ни одного POST.
    b = _board({"/issues/TES-1": _Resp(200, {
        "description": f"тело\n\nPR: {PR}", "customFields": [_state_field()]})})
    res = b.finish("TES-1", PR, mark_done=False)
    assert res["pr_link_added"] is False
    assert not [c for c in b._client.calls if c[0] == "POST"]


def test_state_of_reads_custom_field():
    issue = {"customFields": [{"name": "Stage", "value": {"name": "Готово"}}]}
    assert _state_of(issue, "Stage") == "Готово"
    assert _state_of(issue) is None  # дефолт State — поля нет


def test_youtrack_finish_uses_configured_status_field():
    b = _board({"/issues/TES-1": _Resp(200, {
        "description": "", "customFields": [_state_field(name="Stage")]})})
    b._status_field = "Stage"  # как выставит make_board_provider/set_status_field
    res = b.finish("TES-1", PR, done_state="Готово")
    payload = _field_post(b._client.calls)[2]["customFields"][0]
    assert payload["name"] == "Stage"
    assert payload["value"]["name"] == "Готово"
    assert res["done_set"] is True


def test_youtrack_finish_field_not_found():
    # Поле status_field отсутствует в customFields задачи → warning, done_set False,
    # field-POST не шлётся (поле не трогаем).
    b = _board({"/issues/TES-1": _Resp(200, {
        "description": "", "customFields": [_state_field(name="State")]})})
    b._status_field = "Stage"  # такого поля на задаче нет
    res = b.finish("TES-1", PR, done_state="Готово")
    assert res["done_set"] is False
    assert res["warnings"]
    assert not _has_field_post(b._client.calls)


def test_youtrack_finish_multiword_field_name_works():
    # un-break: многословное имя поля (аллоулист удалён) теперь работает, если поле есть.
    b = _board({"/issues/TES-1": _Resp(200, {
        "description": "", "customFields": [_state_field(name="Kanban State")]})})
    b._status_field = "Kanban State"
    res = b.finish("TES-1", PR, done_state="Done")
    payload = _field_post(b._client.calls)[2]["customFields"][0]
    assert payload["name"] == "Kanban State"
    assert res["done_set"] is True


def test_youtrack_finish_cyrillic_field_and_multiword_value():
    # Кириллическое имя поля + многословное значение — payload корректен.
    b = _board({"/issues/TES-1": _Resp(200, {
        "description": "",
        "customFields": [_state_field(name="Готовность", value_name="Открыто")]})})
    b._status_field = "Готовность"
    res = b.finish("TES-1", PR, done_state="Готово к сдаче")
    payload = _field_post(b._client.calls)[2]["customFields"][0]
    assert payload["name"] == "Готовность"
    assert payload["value"]["name"] == "Готово к сдаче"
    assert res["done_set"] is True


def test_youtrack_set_status_field_defaults_to_state():
    b = _board({"/issues/TES-1": _Resp(200, {
        "description": "", "customFields": [_state_field()]})})
    b.set_status_field(None)
    b.finish("TES-1", PR, done_state="Fixed")
    payload = _field_post(b._client.calls)[2]["customFields"][0]
    assert payload["name"] == "State"


def test_youtrack_finish_null_value_type_from_mapping():
    # value=None: тип элемента выводится из _FIELD_TO_ELEMENT по $type поля.
    b = _board({"/issues/TES-1": _Resp(200, {
        "description": "",
        "customFields": [{"name": "State", "$type": "SingleEnumIssueCustomField",
                          "value": None}]})})
    res = b.finish("TES-1", PR, done_state="Done")
    payload = _field_post(b._client.calls)[2]["customFields"][0]
    assert payload["$type"] == "SingleEnumIssueCustomField"
    assert payload["value"]["$type"] == "EnumBundleElement"
    assert payload["value"]["name"] == "Done"
    assert res["done_set"] is True


def test_youtrack_finish_unknown_field_type_value_without_type():
    # value=None и $type поля вне маппинга → value без $type (YouTrack резолвит по имени).
    b = _board({"/issues/TES-1": _Resp(200, {
        "description": "",
        "customFields": [{"name": "State", "$type": "SomeWeirdCustomField",
                          "value": None}]})})
    res = b.finish("TES-1", PR, done_state="Done")
    payload = _field_post(b._client.calls)[2]["customFields"][0]
    assert "$type" not in payload["value"]
    assert payload["value"]["name"] == "Done"
    assert res["done_set"] is True
