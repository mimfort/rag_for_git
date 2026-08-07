"""Чтение задач Yandex Tracker: пагинация ``_search``, маппинг RawTask, limit, время.

Факты API (v3) сверены с официальной докой:
https://yandex.ru/support/tracker/en/api-ref/issues/search-issues (POST /v3/issues/_search,
``perPage``/``page``, ``expand=attachments``), https://yandex.ru/support/tracker/ru/api-ref/access
(заголовки ``Authorization`` + ``X-Org-ID``/``X-Cloud-Org-ID``).
"""
from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.yandex_tracker import YandexTrackerBoard, provider_spec

TOKEN = "yandex-tracker-secret-token"


def _board(handler, **kwargs) -> YandexTrackerBoard:
    """Провайдер на MockTransport: без сети и без ожиданий retry."""
    options: dict = {
        "token": TOKEN,
        "org_id": "org-42",
        "key_pattern": r"[A-Z]+-\d+",
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _: None,
    }
    options.update(kwargs)
    return YandexTrackerBoard(**options)


def _issue(number: int, **over) -> dict:
    issue = {
        "self": f"https://api.tracker.yandex.net/v3/issues/TREK-{number}",
        "id": f"6000{number}",
        "key": f"TREK-{number}",
        "summary": f"Задача {number}",
        "description": f"Описание TREK-{number}",
        "status": {"id": "1", "key": "open", "display": "Открыт"},
        "queue": {"id": "3", "key": "TREK", "display": "Trek"},
        "updatedAt": "2026-07-23T09:11:12.347+0000",
        "attachments": [],
    }
    issue.update(over)
    return issue


def test_search_pagination_uses_page_per_page_and_queue_filter() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.params["page"])
        per_page = int(request.url.params["perPage"])
        issues = [_issue(number) for number in range(1, 102)]
        start = (page - 1) * per_page
        return httpx.Response(200, json=issues[start : start + per_page])

    rows = list(_board(handler).iter_raw("TREK", None))

    assert [row.key for row in rows[:2]] == ["TREK-1", "TREK-2"]
    assert rows[-1].key == "TREK-101"
    assert [request.method for request in seen] == ["POST", "POST"]
    assert seen[0].url.path == "/v3/issues/_search"
    assert dict(seen[0].url.params) == {
        "expand": "attachments",
        "page": "1",
        "perPage": "100",
    }
    assert dict(seen[1].url.params)["page"] == "2"
    assert json.loads(seen[0].content) == {
        "filter": {"queue": "TREK"},
        "order": "+updatedAt",
    }
    assert seen[0].headers["Authorization"] == f"OAuth {TOKEN}"
    assert seen[0].headers["X-Org-ID"] == "org-42"
    assert "X-Cloud-Org-ID" not in seen[0].headers


def test_iter_raw_maps_key_project_code_board_id_and_status() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_issue(7)])

    row = next(iter(_board(handler).iter_raw("TREK", None)))

    assert (row.key, row.project_code, row.board_id) == ("TREK-7", "TREK-7", "60007")
    assert row.title == "Задача 7"
    assert row.status == "Открыт"
    assert row.subtask_ids == []
    assert row.links == []
    assert row.archived is None
    assert row.terminal is None
    assert row.provider_data["queue"]["key"] == "TREK"


@pytest.mark.parametrize(
    ("updated_at", "expected"),
    [
        ("2026-07-23T09:11:12.347+0000", 1784797872347),  # формат Трекера: смещение без «:»
        ("2026-07-23T09:11:12.347Z", 1784797872347),
        ("2026-07-23T12:11:12.347+0300", 1784797872347),
        ("2026-07-23T09:11:12", 1784797872000),  # naive трактуется как UTC
        ("", None),
        ("не дата", None),
        (None, None),
    ],
)
def test_updated_at_is_parsed_into_utc_epoch_ms(
    updated_at: object,
    expected: int | None,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_issue(1, updatedAt=updated_at)])

    row = next(iter(_board(handler).iter_raw("TREK", None)))

    assert row.timestamp == expected


def test_limit_stops_without_loading_the_next_page() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[_issue(number) for number in range(1, 101)])

    rows = list(_board(handler).iter_raw("TREK", 1))

    assert [row.key for row in rows] == ["TREK-1"]
    assert calls == 1


def test_iter_raw_requires_a_queue() -> None:
    board = _board(lambda _: httpx.Response(200, json=[]))

    with pytest.raises(BoardProviderError) as exc_info:
        list(board.iter_raw(None, None))

    assert exc_info.value.category == "configuration"
    board.close()


def test_queue_option_is_used_when_project_is_absent() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=[_issue(1)])

    list(_board(handler, queue="TREK").iter_raw(None, None))

    assert bodies[0]["filter"] == {"queue": "TREK"}


def test_project_argument_overrides_the_queue_option() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=[])

    list(_board(handler, queue="OTHER").iter_raw("TREK", None))

    assert bodies[0]["filter"] == {"queue": "TREK"}


def test_fetch_one_shares_the_mapper_and_404_is_none() -> None:
    issue = _issue(1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/TREK-404"):
            return httpx.Response(404, json={"token": TOKEN})
        if request.method == "GET":
            return httpx.Response(200, json=issue)
        return httpx.Response(200, json=[issue])

    board = _board(handler)
    raw = next(iter(board.iter_raw("TREK", 1)))
    one = board.fetch_one("TREK-1")

    assert one is not None
    assert dataclasses.asdict(one) == dataclasses.asdict(raw)
    assert board.fetch_one("TREK-404") is None
    board.close()


def test_fetch_one_asks_for_attachments_expand() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_issue(1))

    _board(handler).fetch_one("TREK-1")

    assert seen[0].url.path == "/v3/issues/TREK-1"
    assert dict(seen[0].url.params) == {"expand": "attachments"}


def test_bearer_scheme_and_cloud_org_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    board = YandexTrackerBoard(
        token=TOKEN,
        auth_scheme="iam",
        cloud_org_id="cloud-org-7",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
    )
    list(board.iter_raw("TREK", None))

    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert seen[0].headers["X-Cloud-Org-ID"] == "cloud-org-7"
    assert "X-Org-ID" not in seen[0].headers
    board.close()


@pytest.mark.parametrize(
    "org",
    [{}, {"org_id": "org-42", "cloud_org_id": "cloud-org-7"}],
)
def test_exactly_one_org_header_is_required(org: dict) -> None:
    requests: list[httpx.Request] = []

    with pytest.raises(BoardProviderError) as exc_info:
        YandexTrackerBoard(
            token=TOKEN,
            transport=httpx.MockTransport(
                lambda request: requests.append(request) or httpx.Response(200, json=[])
            ),
            **org,
        )

    assert exc_info.value.category == "configuration"
    assert TOKEN not in repr(exc_info.value)
    assert requests == []


@pytest.mark.parametrize(
    "api_base",
    [
        "http://api.tracker.yandex.net/v3",
        "https://user@api.tracker.yandex.net/v3",
        "https://api.tracker.yandex.net/v3?token=leak",
        "",
    ],
)
def test_api_base_must_be_a_plain_https_url(api_base: str) -> None:
    with pytest.raises(BoardProviderError) as exc_info:
        YandexTrackerBoard(
            token=TOKEN,
            org_id="org-42",
            api_base=api_base,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[])),
        )

    assert exc_info.value.category == "configuration"
    assert TOKEN not in repr(exc_info.value)


@pytest.mark.parametrize(
    "scheme",
    ["Basic", "token", ""],
)
def test_unknown_auth_scheme_is_configuration_error(scheme: str) -> None:
    with pytest.raises(BoardProviderError) as exc_info:
        YandexTrackerBoard(
            token=TOKEN,
            org_id="org-42",
            auth_scheme=scheme,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[])),
        )

    assert exc_info.value.category == "configuration"


def _identity_handler(seen: list[httpx.Request] | None = None, *, status: int = 200):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"token": TOKEN})
        if request.url.path == "/v3/myself":
            return httpx.Response(
                200,
                json={"self": "https://api.tracker.yandex.net/v3/users/11", "uid": 11,
                      "login": "robot", "display": "Робот"},
            )
        if request.url.path == "/v3/queues/TREK":
            return httpx.Response(200, json={"id": 3, "key": "TREK", "name": "Trek"})
        return httpx.Response(404, json={})

    return handle


def test_validate_connection_reports_identity_and_queue() -> None:
    seen: list[httpx.Request] = []
    board = _board(_identity_handler(seen))

    result = board.validate_connection("TREK")

    assert set(result) == {"status", "identity", "project", "capabilities", "warnings"}
    assert result["status"] == "ok"
    assert result["identity"] == {"id": "11", "login": "robot", "display": "Робот"}
    assert result["project"] == "TREK"
    assert result["capabilities"] == {"read": True, "create": True, "transition": True}
    assert result["warnings"] == []
    assert [request.url.path for request in seen] == ["/v3/myself", "/v3/queues/TREK"]
    assert TOKEN not in repr(result)
    board.close()


def test_validate_connection_without_queue_warns() -> None:
    board = _board(_identity_handler())

    result = board.validate_connection(None)

    assert result["status"] == "ok"
    assert result["project"] is None
    assert result["capabilities"]["create"] is False
    assert result["warnings"]
    board.close()


@pytest.mark.parametrize(("status", "category"), [(403, "permission"), (404, "not_found")])
def test_validate_connection_maps_error_status(status: int, category: str) -> None:
    board = _board(_identity_handler(status=status))

    with pytest.raises(BoardProviderError) as exc_info:
        board.validate_connection("TREK")

    assert exc_info.value.category == category
    assert TOKEN not in f"{exc_info.value!s}{exc_info.value!r}"
    board.close()


def test_provider_spec_declares_credentials_options_and_labels() -> None:
    spec = provider_spec()

    assert spec.board_type == "yandex_tracker"
    assert [(field.env, field.secret, field.required, field.default)
            for field in spec.credential_fields] == [
        ("YANDEX_TRACKER_TOKEN", True, True, ""),
        ("YANDEX_TRACKER_API_BASE", False, False, "https://api.tracker.yandex.net/v3"),
        ("YANDEX_TRACKER_ORG_ID", False, False, ""),
        ("YANDEX_TRACKER_CLOUD_ORG_ID", False, False, ""),
        ("YANDEX_TRACKER_AUTH_SCHEME", False, False, "OAuth"),
    ]
    assert [(option.key, option.required_for) for option in spec.option_fields] == [
        ("queue", ("sync", "create", "finish")),
    ]
    assert spec.default_api_base == "https://api.tracker.yandex.net/v3"
    assert spec.setup.help_url.startswith("https://yandex.ru/support/tracker/")
    assert spec.create_target_label and spec.done_target_label
