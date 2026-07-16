from __future__ import annotations

import pytest
from pytest_socket import disable_socket, enable_socket

from reviewer.config.settings import Settings
from tests.infrastructure_policy import (
    InfrastructureTestSettings,
    apply_test_environment,
    install_integration_guards,
    install_unit_db_guards,
    validate_test_endpoints,
)

_SESSION_POLICY: pytest.MonkeyPatch | None = None


def pytest_sessionstart(session: pytest.Session) -> None:
    global _SESSION_POLICY
    _SESSION_POLICY = pytest.MonkeyPatch()
    enable_socket()
    disable_socket(allow_unix_socket=True)
    install_unit_db_guards(_SESSION_POLICY)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _SESSION_POLICY
    if _SESSION_POLICY is not None:
        _SESSION_POLICY.undo()
        _SESSION_POLICY = None
    enable_socket()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    yield
    enable_socket()
    disable_socket(allow_unix_socket=True)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.get_closest_marker("integration") is not None:
            item.add_marker(pytest.mark.enable_socket)
            continue
        if item.get_closest_marker("enable_socket") is not None:
            raise pytest.UsageError(
                f"Unit-тест {item.nodeid} не может использовать enable_socket"
            )
        if "socket_enabled" in getattr(item, "fixturenames", ()):
            raise pytest.UsageError(
                f"Unit-тест {item.nodeid} не может использовать socket_enabled"
            )


@pytest.fixture(autouse=True)
def infrastructure_test_settings(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> InfrastructureTestSettings | None:
    if request.node.get_closest_marker("integration") is None:
        enable_socket()
        disable_socket(allow_unix_socket=True)
        return None

    production = Settings()
    test = InfrastructureTestSettings()
    endpoints = validate_test_endpoints(test, production)
    apply_test_environment(monkeypatch, test)
    install_integration_guards(monkeypatch, endpoints)
    return test
