from __future__ import annotations

from dataclasses import dataclass
import socket
import sys
from types import FunctionType
from typing import Any

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

@dataclass(frozen=True)
class _SocketPolicyState:
    socket_type: Any
    socketpair: Any
    socket_connect_defined: bool
    socket_connect: Any
    getaddrinfo: Any
    gethostbyname: Any


@dataclass(frozen=True)
class _SessionPolicyState:
    session: pytest.Session
    monkeypatch: pytest.MonkeyPatch
    sockets: _SocketPolicyState


_SESSION_POLICIES: list[_SessionPolicyState] = []
_TRUE_SOCKET_TYPE = socket.socket
_TRUE_SOCKETPAIR = socket.socketpair


def _build_windows_unit_socketpair() -> Any:
    """Изолировать штатную Windows socketpair от подмены socket.socket."""
    if not isinstance(_TRUE_SOCKETPAIR, FunctionType):
        return _TRUE_SOCKETPAIR

    isolated_globals = dict(_TRUE_SOCKETPAIR.__globals__)
    isolated_globals["socket"] = _TRUE_SOCKET_TYPE
    isolated = FunctionType(
        _TRUE_SOCKETPAIR.__code__,
        isolated_globals,
        _TRUE_SOCKETPAIR.__name__,
        _TRUE_SOCKETPAIR.__defaults__,
        _TRUE_SOCKETPAIR.__closure__,
    )
    isolated.__kwdefaults__ = _TRUE_SOCKETPAIR.__kwdefaults__
    return isolated


_WINDOWS_UNIT_SOCKETPAIR = (
    _build_windows_unit_socketpair() if sys.platform == "win32" else _TRUE_SOCKETPAIR
)


def _disable_unit_sockets() -> None:
    """Запретить сеть, оставив Windows asyncio внутреннюю wake-up socketpair."""
    disable_socket(allow_unix_socket=True)
    if sys.platform == "win32":
        socket.socketpair = _WINDOWS_UNIT_SOCKETPAIR


def _capture_socket_policy() -> _SocketPolicyState:
    return _SocketPolicyState(
        socket_type=socket.socket,
        socketpair=socket.socketpair,
        socket_connect_defined="connect" in socket.socket.__dict__,
        socket_connect=socket.socket.__dict__.get("connect"),
        getaddrinfo=socket.getaddrinfo,
        gethostbyname=socket.gethostbyname,
    )


def _restore_socket_policy(state: _SocketPolicyState) -> None:
    socket.socket = state.socket_type
    socket.socketpair = state.socketpair
    if state.socket_connect_defined:
        socket.socket.connect = state.socket_connect
    elif "connect" in socket.socket.__dict__:
        delattr(socket.socket, "connect")
    socket.getaddrinfo = state.getaddrinfo
    socket.gethostbyname = state.gethostbyname


def pytest_sessionstart(session: pytest.Session) -> None:
    monkeypatch = pytest.MonkeyPatch()
    _SESSION_POLICIES.append(
        _SessionPolicyState(
            session=session,
            monkeypatch=monkeypatch,
            sockets=_capture_socket_policy(),
        )
    )
    _disable_unit_sockets()
    install_unit_db_guards(monkeypatch)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _SESSION_POLICIES or _SESSION_POLICIES[-1].session is not session:
        raise RuntimeError("Нарушен LIFO-порядок pytest session policy")
    state = _SESSION_POLICIES.pop()
    state.monkeypatch.undo()
    _restore_socket_policy(state.sockets)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    yield
    enable_socket()
    _disable_unit_sockets()


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
        _disable_unit_sockets()
        return None

    production = Settings()
    test = InfrastructureTestSettings()
    endpoints = validate_test_endpoints(test, production)
    apply_test_environment(monkeypatch, test)
    install_integration_guards(monkeypatch, endpoints)
    return test
