"""Приёмка на настоящих семействах этого репозитория.

Синтетика не ловит регрессию, ради которой делалась задача: провал
воспроизводился именно на 11 адаптерах досок, где SCIP молчит, а три
адаптера вдобавок не имеют общей базы.

Тесты читают исходники с диска — внешних сервисов не требуется.
"""
from __future__ import annotations

import pathlib

import pytest

from reviewer.graph.builder import build_graph_from_files
from reviewer.graph.family import effective_methods, structural_matches
from reviewer.index.chunker import chunk_python

REPO = pathlib.Path(__file__).resolve().parents[2]

REST_BASE = "reviewer/tasks/boards/restbase.py#RestBoardBase"
REST_ADAPTERS = {
    "reviewer/tasks/boards/asana.py#AsanaBoard",
    "reviewer/tasks/boards/clickup.py#ClickUpBoard",
    "reviewer/tasks/boards/github.py#GitHubIssuesBoard",
    "reviewer/tasks/boards/kaiten.py#KaitenBoard",
    "reviewer/tasks/boards/linear.py#LinearBoard",
    "reviewer/tasks/boards/trello.py#TrelloBoard",
    "reviewer/tasks/boards/weeek.py#WeeekBoard",
    "reviewer/tasks/boards/yandex_tracker.py#YandexTrackerBoard",
}
LEGACY_ADAPTERS = {
    "reviewer/tasks/boards/jira.py#JiraCloudBoard",
    "reviewer/tasks/boards/yougile.py#YougileBoard",
    "reviewer/tasks/boards/youtrack.py#YouTrackBoard",
}
BOARD_CONTRACT = "reviewer/tasks/boards/base.py#TaskBoardProvider"
VCS_CONTRACT = "reviewer/vcs/base.py#VCSProvider"


@pytest.fixture(scope="module")
def sources() -> dict[str, str]:
    """Исходники пакета reviewer/, ключ — путь относительно корня репозитория."""
    return {
        str(p.relative_to(REPO)): p.read_text(encoding="utf-8")
        for p in (REPO / "reviewer").rglob("*.py")
    }


@pytest.fixture(scope="module")
def graph(sources):
    """Граф из tree-sitter по реальным исходникам."""
    return build_graph_from_files(sources)


@pytest.fixture(scope="module")
def own_and_bases(sources, graph):
    """Собственные методы классов и карта баз по рёбрам IMPLEMENTS."""
    own: dict[str, set[str]] = {}
    for path, src in sources.items():
        for c in chunk_python(path, src.encode("utf-8")):
            if "." not in c.symbol_fqn:
                continue
            cls, _, method = c.symbol_fqn.rpartition(".")
            if "." in cls:
                continue
            own.setdefault(f"{path}#{cls}", set()).add(method)
    bases: dict[str, list[str]] = {}
    for src_id, rel, dst_id in graph[1]:
        if rel == "IMPLEMENTS":
            bases.setdefault(src_id, []).append(dst_id)
    return own, bases


def test_rest_board_base_yields_all_eight_subclasses(graph):
    """Критерий 1: восемь наследников RestBoardBase видны как IMPLEMENTS."""
    subclasses = {
        src for src, rel, dst in graph[1]
        if rel == "IMPLEMENTS" and dst == REST_BASE
    }
    assert REST_ADAPTERS <= subclasses


def test_board_contract_family_covers_all_eleven_adapters(own_and_bases):
    """Критерий 2: семейство перечисляется целиком, включая три легаси."""
    own, bases = own_and_bases
    contract_methods = effective_methods(BOARD_CONTRACT, own, bases)
    candidates = {cls: effective_methods(cls, own, bases) for cls in own}
    found = set(structural_matches(BOARD_CONTRACT, contract_methods, candidates))
    assert REST_ADAPTERS <= found
    assert LEGACY_ADAPTERS <= found


def test_board_contract_family_has_no_false_positives(own_and_bases):
    """Структурный фильтр не притягивает посторонние классы."""
    own, bases = own_and_bases
    contract_methods = effective_methods(BOARD_CONTRACT, own, bases)
    candidates = {cls: effective_methods(cls, own, bases) for cls in own}
    found = set(structural_matches(BOARD_CONTRACT, contract_methods, candidates))
    assert found <= (REST_ADAPTERS | LEGACY_ADAPTERS)


def test_vcs_protocol_family_covers_both_providers(own_and_bases):
    """Оба VCS-провайдера находятся, хотя Protocol наследования не даёт."""
    own, bases = own_and_bases
    contract_methods = effective_methods(VCS_CONTRACT, own, bases)
    candidates = {cls: effective_methods(cls, own, bases) for cls in own}
    found = set(structural_matches(VCS_CONTRACT, contract_methods, candidates))
    assert any(f.endswith("#GitHubProvider") for f in found)
    assert any(f.endswith("#GitLabProvider") for f in found)
