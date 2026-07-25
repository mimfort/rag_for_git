"""Discovery Asana: секции проекта, резолв target, validate_connection, registry spec."""
from __future__ import annotations

import httpx
import pytest

from reviewer.tasks.boards.asana import AsanaBoard, provider_spec
from reviewer.tasks.boards.errors import BoardProviderError
from reviewer.tasks.boards.registry import BoardProviderRegistry

BASE = "https://app.asana.test/api/1.0"
PROJECT_GID = "1201234567890"
SECRET = "asana-secret-token"


def _board(handler, **kwargs: object) -> AsanaBoard:
    options: dict = {
        "access_token": SECRET,
        "api_base": BASE,
        "project_gid": PROJECT_GID,
        "key_prefix": "ASN",
        "key_pattern": r"ASN-\d+",
        "url_template": "",
        "attachment_max_bytes": 1000,
        "attachment_timeout": 1.0,
        "attachment_store_chars": 1000,
        "transport": httpx.MockTransport(handler),
        "sleeper": lambda _: None,
    }
    options.update(kwargs)
    return AsanaBoard(**options)  # type: ignore[arg-type]


def _sections(*rows: dict, cursor: str | None = None) -> dict:
    payload: dict = {"data": list(rows), "next_page": None}
    if cursor:
        payload["next_page"] = {"offset": cursor, "path": "/sections", "uri": BASE}
    return payload


def test_list_targets_maps_sections_to_the_normalized_shape() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("offset") == "next-1":
            return _response(_sections({"gid": "5003", "name": "Done"}))
        return _response(
            _sections(
                {"gid": "5001", "name": "Todo"},
                {"gid": "5002", "name": "In progress"},
                cursor="next-1",
            )
        )

    board = _board(handler)
    result = board.list_targets("ASN")

    assert set(result) == {"targets", "options", "warnings"}
    assert result["targets"] == [
        {"id": "5001", "label": "Todo", "purposes": ["create", "done"]},
        {"id": "5002", "label": "In progress", "purposes": ["create", "done"]},
        {"id": "5003", "label": "Done", "purposes": ["create", "done"]},
    ]
    assert result["options"] == []
    assert result["warnings"] == []
    assert requests[0].url.path == f"/api/1.0/projects/{PROJECT_GID}/sections"
    assert dict(requests[0].url.params) == {"limit": "100", "opt_fields": "gid,name"}
    board.close()


def _response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


def test_list_targets_without_project_gid_warns_and_makes_no_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(_sections())

    board = _board(handler, project_gid="")
    result = board.list_targets(None)

    assert result["targets"] == []
    assert result["warnings"]
    assert SECRET not in repr(result)
    assert requests == []
    board.close()


def test_list_targets_is_fail_soft_when_sections_are_forbidden() -> None:
    board = _board(lambda _request: httpx.Response(403, json={"token": SECRET}))
    result = board.list_targets("ASN")

    assert result["targets"] == []
    assert result["warnings"]
    assert SECRET not in repr(result)
    board.close()


def test_section_is_resolved_by_gid_and_by_exact_name() -> None:
    board = _board(
        lambda _request: _response(
            _sections({"gid": "5001", "name": "Todo"}, {"gid": "5003", "name": "Done"})
        )
    )
    sections = board._sections(PROJECT_GID)

    by_gid, warning = board._resolve_section(sections, "5003")
    assert by_gid == {"gid": "5003", "name": "Done"}
    assert warning is None

    by_name, warning = board._resolve_section(sections, "Done")
    assert by_name == {"gid": "5003", "name": "Done"}
    assert warning is None
    board.close()


def test_missing_and_ambiguous_sections_report_a_warning() -> None:
    board = _board(
        lambda _request: _response(
            _sections(
                {"gid": "5003", "name": "Done"},
                {"gid": "5004", "name": "Done"},
                {"gid": "5001", "name": "Todo"},
            )
        )
    )
    sections = board._sections(PROJECT_GID)

    missing, warning = board._resolve_section(sections, "Missing")
    assert missing is None
    assert "Missing" in str(warning)

    ambiguous, warning = board._resolve_section(sections, "Done")
    assert ambiguous is None
    assert "Done" in str(warning)
    board.close()


def test_validate_connection_reports_identity_project_and_capabilities() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/1.0/users/me":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "gid": "9876",
                        "name": "Robot",
                        "workspaces": [{"gid": "111", "name": "Acme"}],
                    }
                },
            )
        return httpx.Response(
            200,
            json={"data": {"gid": PROJECT_GID, "name": "Reviewer"}},
        )

    board = _board(handler)
    result = board.validate_connection("ASN")

    assert set(result) == {"status", "identity", "project", "capabilities", "warnings"}
    assert result["status"] == "ok"
    assert result["identity"] == {"gid": "9876", "name": "Robot"}
    assert result["project"] == {"gid": PROJECT_GID, "name": "Reviewer"}
    assert result["capabilities"] == {
        "sync": True,
        "create": True,
        "finish": True,
        "attachments": True,
    }
    assert result["warnings"] == []
    assert SECRET not in repr(result)
    assert [call.url.path for call in requests] == [
        "/api/1.0/users/me",
        f"/api/1.0/projects/{PROJECT_GID}",
    ]
    board.close()


def test_validate_connection_warns_about_missing_project_gid_and_key_prefix() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"gid": "9876", "name": "Robot"}})

    board = _board(handler, project_gid="", key_prefix="")
    result = board.validate_connection(None)

    assert result["status"] == "ok"
    assert result["project"] is None
    assert result["capabilities"] == {
        "sync": False,
        "create": False,
        "finish": False,
        "attachments": True,
    }
    assert len(result["warnings"]) == 2
    assert [call.url.path for call in requests] == ["/api/1.0/users/me"]
    board.close()


@pytest.mark.parametrize(
    ("status", "category"),
    [(403, "permission"), (404, "not_found"), (401, "authentication")],
)
def test_validate_connection_maps_transport_status_to_a_category(
    status: int,
    category: str,
) -> None:
    board = _board(lambda _request: httpx.Response(status, json={"token": SECRET}))

    with pytest.raises(BoardProviderError) as exc_info:
        board.validate_connection("ASN")

    assert exc_info.value.category == category
    assert SECRET not in f"{exc_info.value!s}{exc_info.value!r}"
    board.close()


def test_provider_spec_declares_credentials_options_and_labels() -> None:
    spec = provider_spec()

    assert spec.board_type == "asana"
    assert [(item.env, item.secret, item.required, item.default) for item in
            spec.credential_fields] == [
        ("ASANA_ACCESS_TOKEN", True, True, ""),
        ("ASANA_API_BASE", False, False, "https://app.asana.com/api/1.0"),
    ]
    assert [(item.key, item.required_for) for item in spec.option_fields] == [
        ("project_gid", ("sync", "create", "finish")),
        ("key_prefix", ("sync",)),
    ]
    assert spec.default_api_base == "https://app.asana.com/api/1.0"
    assert spec.setup.label == "Asana"
    assert spec.setup.help_url == "https://app.asana.com/0/my-apps"
    assert spec.setup.help_text
    assert spec.create_target_label
    assert spec.done_target_label


def test_registry_builds_a_full_provider_from_the_spec() -> None:
    registry = BoardProviderRegistry([provider_spec()])
    provider = registry.create(
        "asana",
        credentials={"ASANA_ACCESS_TOKEN": SECRET, "ASANA_API_BASE": BASE},
        options={"project_gid": PROJECT_GID, "key_prefix": "ASN"},
        build_defaults={
            "key_pattern": r"ASN-\d+",
            "url_template": "https://app.asana.test/task/{code}",
            "attachment_max_bytes": 1000,
            "attachment_timeout": 1.0,
            "attachment_store_chars": 1000,
        },
    )

    assert isinstance(provider, AsanaBoard)
    assert provider.board_type == "asana"
    assert provider._key_for("42") == "ASN-42"
    assert provider._resolve_project_gid(None) == PROJECT_GID
    provider.close()

