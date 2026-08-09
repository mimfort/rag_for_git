"""Чтение доски Kaiten: пагинация ``/cards`` (limit/offset), маппинг RawTask, limit, время."""
from __future__ import annotations

import dataclasses

import httpx
import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.kaiten import KaitenBoard, provider_spec
from reviewer.tasks.boards.registry import BoardProviderRegistry

BASE_URL = "https://acme.kaiten.ru"
TOKEN = "kaiten-secret-token"


def card(number: int, **overrides) -> dict:
    """Карточка Kaiten в форме listing-ответа (описание — через additional_card_fields)."""
    payload = {
        "id": 100 + number,
        "title": f"Карточка {number}",
        "description": f"Описание {number}",
        "updated": "2026-07-23T09:05:00.000Z",
        "created": "2026-07-01T10:00:00.000Z",
        "board_id": 1,
        "column_id": 2,
        "lane_id": 5,
        "state": 2,
        "condition": 1,
        "children": [],
    }
    payload.update(overrides)
    return payload


def columns() -> list[dict]:
    """Колонки доски: type 1 — очередь, 2 — в работе, 3 — готово; вложенные подколонки."""
    return [
        {"id": 1, "title": "Очередь", "type": 1, "sort_order": 1, "board_id": 1,
         "subcolumns": []},
        {"id": 2, "title": "В работе", "type": 2, "sort_order": 2, "board_id": 1,
         "subcolumns": [
             {"id": 4, "title": "Ревью", "type": 2, "sort_order": 1, "board_id": 1},
         ]},
        {"id": 3, "title": "Готово", "type": 3, "sort_order": 3, "board_id": 1,
         "subcolumns": []},
    ]


def board(handler, **kwargs) -> KaitenBoard:
    return KaitenBoard(
        token=TOKEN,
        base_url=BASE_URL,
        board_id=kwargs.pop("board_id", "1"),
        key_prefix=kwargs.pop("key_prefix", "KTN"),
        key_pattern=r"KTN-\d+",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
        **kwargs,
    )


def test_iter_raw_walks_offset_pages_with_exact_params() -> None:
    cards = [card(number) for number in range(1, 151)]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/columns"):
            return httpx.Response(200, json=columns())
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params.get("limit", 100))
        return httpx.Response(200, json=cards[offset : offset + limit])

    rows = list(board(handler).iter_raw("KTN", None))

    assert len(rows) == 150
    pages = [request for request in requests if request.url.path.endswith("/cards")]
    assert [request.url.path for request in pages] == ["/api/latest/cards"] * 2
    assert dict(pages[0].url.params) == {
        "board_id": "1",
        "additional_card_fields": "description",
        "order_by": "updated",
        "order_direction": "desc",
        "limit": "100",
        "offset": "0",
    }
    assert pages[1].url.params.get("offset") == "100"
    assert pages[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_raw_task_maps_key_project_board_id_status_and_epoch_ms() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/columns"):
            return httpx.Response(200, json=columns())
        return httpx.Response(200, json=[card(1, children=[{"id": 909, "title": "Дочерняя"}])])

    row = next(iter(board(handler).iter_raw("KTN", None)))

    assert row.key == "KTN-101"
    assert row.project_code == "KTN-101"
    assert row.board_id == "101"
    assert row.status == "В работе"
    assert row.timestamp == 1784797500000
    assert row.archived is False
    assert row.terminal is False
    assert row.subtask_ids == ["KTN-909"]
    assert row.links == [{"type": "subtask", "key": "KTN-909", "title": "Дочерняя"}]
    assert row.provider_data["card_id"] == 101
    assert row.provider_data["column_id"] == 2


def test_status_falls_back_to_card_state_when_columns_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/columns"):
            return httpx.Response(500, json={"token": TOKEN})
        return httpx.Response(200, json=[card(1, state=3, column_id=3)])

    row = next(iter(board(handler).iter_raw("KTN", None)))

    assert row.status == "done"
    assert row.terminal is True


def test_lifecycle_preserves_documented_values_and_unknowns() -> None:
    missing = card(3, column_id=None, state=None, condition=None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/columns"):
            return httpx.Response(200, json=columns())
        return httpx.Response(
            200,
            json=[
                card(1, column_id=3, state=2, condition=2),
                card(2, column_id=2, state=2, condition=1),
                missing,
            ],
        )

    rows = list(board(handler).iter_raw("KTN", None))

    assert [row.archived for row in rows] == [True, False, None]
    assert [row.terminal for row in rows] == [True, False, None]


@pytest.mark.parametrize("updated", [None, "не дата", True])
def test_invalid_or_missing_update_timestamp_is_unknown(updated: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/columns"):
            return httpx.Response(200, json=columns())
        return httpx.Response(200, json=[card(1, updated=updated)])

    row = next(iter(board(handler).iter_raw("KTN", None)))

    assert row.timestamp is None


def test_limit_stops_before_the_next_page() -> None:
    pages = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal pages
        if request.url.path.endswith("/columns"):
            return httpx.Response(200, json=columns())
        pages += 1
        return httpx.Response(200, json=[card(number) for number in range(1, 101)])

    rows = list(board(handler).iter_raw("KTN", 2))

    assert [row.key for row in rows] == ["KTN-101", "KTN-102"]
    assert pages == 1


def test_numeric_board_argument_overrides_the_configured_option() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/columns"):
            return httpx.Response(200, json=columns())
        seen.append(request.url.params.get("board_id", ""))
        return httpx.Response(200, json=[])

    list(board(handler).iter_raw("42", None))

    assert seen == ["42"]


def test_space_id_option_scopes_the_listing() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/columns"):
            return httpx.Response(200, json=columns())
        seen.append(request.url.params.get("space_id", ""))
        return httpx.Response(200, json=[])

    list(board(handler, space_id="7").iter_raw("KTN", None))

    assert seen == ["7"]


def test_fetch_one_shares_the_mapper_and_maps_missing_card_to_none() -> None:
    detailed = card(1, children=[{"id": 909, "title": "Дочерняя"}])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/columns"):
            return httpx.Response(200, json=columns())
        if request.url.path == "/api/latest/cards/404":
            return httpx.Response(404, json={"token": TOKEN})
        if request.url.path == "/api/latest/cards/101":
            return httpx.Response(200, json=detailed)
        return httpx.Response(200, json=[detailed])

    provider = board(handler)
    row = next(iter(provider.iter_raw("KTN", 1)))
    one = provider.fetch_one("KTN-101")

    assert one is not None
    assert dataclasses.asdict(one) == dataclasses.asdict(row)
    assert provider.fetch_one("KTN-404") is None
    provider.close()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://acme.kaiten.ru",
        "https://user@acme.kaiten.ru",
        "https://acme.kaiten.ru?token=secret",
        "https://acme.kaiten.ru#fragment",
        "",
    ],
)
def test_constructor_rejects_unsafe_base_url_before_any_request(base_url: str) -> None:
    requests: list[httpx.Request] = []

    with pytest.raises(BoardProviderError) as error:
        KaitenBoard(
            token=TOKEN,
            base_url=base_url,
            transport=httpx.MockTransport(
                lambda request: requests.append(request) or httpx.Response(200, json=[])
            ),
        )

    assert error.value.category == "configuration"
    assert TOKEN not in repr(error.value)
    assert requests == []


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://acme.kaiten.ru", "/api/latest/cards"),
        ("https://acme.kaiten.ru/", "/api/latest/cards"),
        ("https://acme.kaiten.ru/api/latest", "/api/latest/cards"),
        ("https://acme.kaiten.ru/api/v1", "/api/v1/cards"),
        ("https://kaiten.acme.test/kaiten", "/kaiten/api/latest/cards"),
    ],
)
def test_api_suffix_is_appended_only_when_missing(base_url: str, expected: str) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=[])

    provider = KaitenBoard(
        token=TOKEN,
        base_url=base_url,
        board_id="1",
        key_prefix="KTN",
        transport=httpx.MockTransport(handler),
    )
    list(provider.iter_raw("KTN", None))
    provider.close()

    assert expected in seen


def test_provider_spec_registers_and_builds_a_full_provider() -> None:
    spec = provider_spec()
    registry = BoardProviderRegistry([spec])

    provider = registry.create(
        "kaiten",
        credentials={"KAITEN_BASE_URL": BASE_URL, "KAITEN_API_TOKEN": TOKEN},
        options={"board_id": 5, "key_prefix": "KTN", "space_id": 7},
        build_defaults={
            "key_pattern": r"KTN-\d+",
            "url_template": "",
            "attachment_max_bytes": 10,
            "attachment_timeout": 1.0,
            "attachment_store_chars": 10,
        },
    )

    assert provider.board_type == "kaiten"
    assert [(field.env, field.secret, field.required) for field in spec.credential_fields] == [
        ("KAITEN_BASE_URL", False, True),
        ("KAITEN_API_TOKEN", True, True),
    ]
    assert [(option.key, option.required_for) for option in spec.option_fields] == [
        ("board_id", ("sync", "create", "finish")),
        ("key_prefix", ("sync",)),
        ("space_id", ()),
    ]
    assert spec.default_api_base == ""
    provider.close()


def test_help_url_builder_points_at_the_company_api_key_page() -> None:
    build_url = provider_spec().setup.help_url_builder
    assert build_url is not None

    assert build_url({"KAITEN_BASE_URL": BASE_URL}) == f"{BASE_URL}/profile/api-key"
    assert build_url({"KAITEN_BASE_URL": f"{BASE_URL}/api/latest"}) == (
        f"{BASE_URL}/profile/api-key"
    )
    assert build_url({"KAITEN_BASE_URL": "not-a-url"}) == "https://developers.kaiten.ru/"


def test_secret_never_leaks_from_a_failing_page_request() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"token": TOKEN})

    with pytest.raises(BoardProviderError) as error:
        list(board(handler).iter_raw("KTN", None))

    assert error.value.category == "permission"
    assert TOKEN not in f"{error.value!s}{error.value!r}"
