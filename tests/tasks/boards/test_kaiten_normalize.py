"""Нормализация Kaiten: нативный markdown, чеклисты → criteria, дети → subtask, вложения."""
from __future__ import annotations

import httpx

from tests.tasks.boards.test_kaiten_read import BASE_URL, TOKEN, board, card, columns

DESCRIPTION = "## Задача\n\nСмотри также KTN-909 и KTN-777.\n\n- пункт"


def detailed_card(**overrides) -> dict:
    """Точечный ответ ``GET /cards/{id}``: чеклисты, файлы и дети приходят инлайн."""
    payload = card(
        1,
        description=DESCRIPTION,
        children=[{"id": 909, "title": "Дочерняя карточка"}],
        checklists=[
            {
                "id": 11,
                "name": "Критерии",
                "items": [
                    {"id": 2, "text": "Тесты зелёные", "checked": False, "sort_order": 2},
                    {"id": 1, "text": "Код смержен", "checked": True, "sort_order": 1},
                    {"id": 3, "text": "Удалённый", "checked": False, "sort_order": 3,
                     "deleted": True},
                ],
            }
        ],
        files=[
            {"id": 5, "name": "spec.txt", "url": f"{BASE_URL}/files/spec.txt", "size": 24,
             "type": 1},
        ],
    )
    payload.update(overrides)
    return payload


def handler_for(payload: dict, *, listing: dict | None = None):
    """Транспорт: listing отдаёт ``listing`` (по умолчанию — саму карточку), точка — ``payload``."""
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/columns"):
            return httpx.Response(200, json=columns())
        if path == "/api/latest/cards":
            return httpx.Response(200, json=[listing if listing is not None else payload])
        if path == "/api/latest/cards/101":
            return httpx.Response(200, json=payload)
        if path == "/files/spec.txt":
            return httpx.Response(200, text="Критерий из вложения")
        return httpx.Response(404, json={})

    return handle


def test_description_stays_native_markdown() -> None:
    provider = board(handler_for(detailed_card()))
    raw = next(iter(provider.iter_raw("KTN", 1)))

    result = provider.normalize(raw)

    assert result["description"] == DESCRIPTION
    assert "<p>" not in result["description"]


def test_checklist_items_become_criteria_and_children_become_subtask_links() -> None:
    provider = board(handler_for(detailed_card()))
    raw = next(iter(provider.iter_raw("KTN", 1)))

    result = provider.normalize(raw)

    assert result["criteria"] == ["[x] Код смержен", "[ ] Тесты зелёные"]
    assert {"type": "subtask", "key": "KTN-909", "title": "Дочерняя карточка"} in result["links"]
    assert {"type": "related", "key": "KTN-777", "title": ""} in result["links"]
    assert all(link["key"] != "KTN-909" or link["type"] == "subtask" for link in result["links"])


def test_normalized_shape_carries_key_aliases_status_url_and_project() -> None:
    provider = board(handler_for(detailed_card()))
    raw = next(iter(provider.iter_raw("KTN", 1)))

    result = provider.normalize(raw)

    assert result["key"] == "KTN-101"
    assert result["aliases"] == ["101"]
    assert result["status"] == "В работе"
    assert result["url"] == f"{BASE_URL}/101"
    assert result["project"] == "KTN"


def test_url_template_wins_over_the_derived_card_url() -> None:
    provider = board(
        handler_for(detailed_card()),
        url_template="https://acme.kaiten.ru/space/7/card/{code}",
    )
    raw = next(iter(provider.iter_raw("KTN", 1)))

    assert provider.normalize(raw)["url"] == "https://acme.kaiten.ru/space/7/card/101"


def test_attachment_text_is_downloaded_from_the_board_host() -> None:
    provider = board(handler_for(detailed_card()))
    raw = next(iter(provider.iter_raw("KTN", 1)))

    attachment = provider.normalize(raw)["attachments"][0]

    assert attachment["name"] == "spec.txt"
    assert attachment["size"] == 24
    assert attachment["content_text"] == "Критерий из вложения"


def test_offhost_attachment_keeps_metadata_and_reports_a_warning() -> None:
    payload = detailed_card(
        files=[{"id": 6, "name": "spec.txt", "url": "https://cdn.evil.test/spec.txt", "size": 9,
                "type": 1}],
    )
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return handler_for(payload)(request)

    provider = board(handle)
    raw = next(iter(provider.iter_raw("KTN", 1)))

    result = provider.normalize(raw)

    assert result["attachments"] == [
        {"name": "spec.txt", "mime_type": None, "size": 9, "content_text": None}
    ]
    assert any("вложение" in warning for warning in result["warnings"])
    assert not any("cdn.evil.test" in call for call in calls)


def test_unsupported_attachment_format_keeps_metadata_only() -> None:
    payload = detailed_card(
        files=[{"id": 7, "name": "diagram.dwg", "url": f"{BASE_URL}/files/diagram.dwg",
                "size": 12, "type": 1}],
    )
    provider = board(handler_for(payload))
    raw = next(iter(provider.iter_raw("KTN", 1)))

    result = provider.normalize(raw)

    assert result["attachments"][0]["content_text"] is None
    assert any("формат" in warning for warning in result["warnings"])


def test_normalize_fetches_card_details_when_the_listing_lacks_them() -> None:
    listing = card(1, description=DESCRIPTION, children=[{"id": 909, "title": "Дочерняя карточка"}])
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return handler_for(detailed_card(), listing=listing)(request)

    provider = board(handle)
    raw = next(iter(provider.iter_raw("KTN", 1)))
    assert raw.provider_data["detailed"] is False
    calls.clear()

    result = provider.normalize(raw)

    assert "/api/latest/cards/101" in calls
    assert result["criteria"] == ["[x] Код смержен", "[ ] Тесты зелёные"]
    assert result["attachments"][0]["name"] == "spec.txt"


def test_unavailable_card_details_degrade_to_a_warning() -> None:
    listing = card(1, description=DESCRIPTION)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/columns"):
            return httpx.Response(200, json=columns())
        if request.url.path == "/api/latest/cards":
            return httpx.Response(200, json=[listing])
        return httpx.Response(403, json={"token": TOKEN})

    provider = board(handle)
    raw = next(iter(provider.iter_raw("KTN", 1)))

    result = provider.normalize(raw)

    assert result["criteria"] == []
    assert result["attachments"] == []
    assert any("permission" in warning for warning in result["warnings"])
    assert TOKEN not in repr(result)


def test_normalize_meta_makes_no_requests_and_skips_details() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return handler_for(detailed_card())(request)

    provider = board(handle)
    raw = next(iter(provider.iter_raw("KTN", 1)))
    calls.clear()

    result = provider.normalize_meta(raw)

    assert calls == []
    assert result["key"] == "KTN-101"
    assert result["criteria"] == []
    assert result["attachments"] == []
    assert result["status"] == "В работе"
    assert {"type": "subtask", "key": "KTN-909", "title": "Дочерняя карточка"} in result["links"]
