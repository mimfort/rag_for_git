"""Повторно используемый contract-test набор для адаптеров досок.

Фейки провайдеров живут по файлам в ``tests/tasks/boards/fakes/<type>.py`` и экспортируют
``ADAPTER``; регистрация нового провайдера — одна строка в ``_adapters()``. Пороги (число
строк, пути страниц) задаёт сам фейк через поля ``ProviderAdapter``, а не тело теста.

``ProviderAdapter`` и ``FakeState`` определены в ``fakes/base.py`` (модуль, который сам
ничего из ``fakes`` не импортирует) — так фейки берут тип оттуда и цикла импортов нет;
здесь они реэкспортируются для обратной совместимости импорта из ``contract``.
"""
from __future__ import annotations

import dataclasses
import logging

import pytest

from reviewer.tasks.boards.errors import BoardProviderError
from tests.tasks.boards.fakes.base import FakeState, ProviderAdapter

__all__ = ["ADAPTERS", "FakeState", "ProviderAdapter", "ProviderContract"]


def _adapters() -> tuple[ProviderAdapter, ...]:
    """Явный список зарегистрированных фейков: одна строка на провайдера."""
    from tests.tasks.boards.fakes import (
        asana,
        clickup,
        github,
        jira,
        kaiten,
        linear,
        trello,
        yandex_tracker,
        yougile,
        youtrack,
    )

    return (
        yougile.ADAPTER,
        youtrack.ADAPTER,
        jira.ADAPTER,
        github.ADAPTER,
        trello.ADAPTER,
        linear.ADAPTER,
        clickup.ADAPTER,
        asana.ADAPTER,
        yandex_tracker.ADAPTER,
        kaiten.ADAPTER,
    )


ADAPTERS = _adapters()


class ProviderContract:
    """Общие поведенческие проверки полного provider contract."""

    @pytest.fixture
    def adapter(self, request: pytest.FixtureRequest) -> ProviderAdapter:
        return request.param

    def test_validate_connection_is_safe(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory()
        result = provider.validate_connection(adapter.project)
        assert set(result) == {"status", "identity", "project", "capabilities", "warnings"}
        assert result["status"] == "ok"
        assert adapter.secret not in repr(result)

    def test_iter_raw_reads_all_pages_and_maps_stable_timestamp(
        self,
        adapter: ProviderAdapter,
    ) -> None:
        provider, state = adapter.provider_factory()
        rows = list(provider.iter_raw(adapter.project, None))
        assert len(rows) > adapter.min_rows
        assert rows[0].timestamp > 0
        page_calls = [call for call in state.calls if call[1].endswith(adapter.page_paths)]
        assert len(page_calls) >= 2

    def test_normalize_meta_has_zero_http_budget(self, adapter: ProviderAdapter) -> None:
        provider, state = adapter.provider_factory()
        raw = next(iter(provider.iter_raw(adapter.project, 1)))
        state.calls.clear()
        result = provider.normalize_meta(raw)
        assert state.calls == []
        assert result["key"] == raw.key
        assert result["attachments"] == []

    def test_normalize_preserves_markdown_links_subtasks_and_attachments(
        self,
        adapter: ProviderAdapter,
    ) -> None:
        provider, _ = adapter.provider_factory()
        raw = next(iter(provider.iter_raw(adapter.project, 1)))
        result = provider.normalize(raw)
        assert "<p>" not in result["description"]
        assert any(link["type"] == "subtask" for link in result["links"])
        assert result["attachments"][0]["name"] == "spec.txt"

    def test_fetch_one_matches_iter_mapper(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory()
        raw = next(iter(provider.iter_raw(adapter.project, 1)))
        one = provider.fetch_one(raw.key)
        assert one is not None
        assert dataclasses.asdict(one) == dataclasses.asdict(raw)

    def test_create_reports_exact_target(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory()
        result = provider.create(
            "# Новая задача",
            title="Новая задача",
            target=adapter.target_id,
            project=adapter.project,
        )
        assert result["key"]
        assert result["target_resolved"] in {adapter.target_id, adapter.target_label}
        assert result["warnings"] == []

    def test_create_falls_back_with_warning(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory()
        result = provider.create(
            "# Новая задача",
            title="Новая задача",
            target=adapter.missing_target,
            project=adapter.project,
        )
        assert result["key"]
        assert result["target_resolved"] != adapter.missing_target
        assert result["warnings"]

    def test_list_targets_uses_normalized_shape(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory()
        result = provider.list_targets(adapter.project)
        assert set(result) == {"targets", "options", "warnings"}
        assert {"id", "label", "purposes"} <= set(result["targets"][0])
        assert all("create" in target["purposes"] for target in result["targets"])

    def test_finish_is_idempotent_and_uses_target(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory()
        first = provider.finish(
            adapter.finish_key,
            "https://github.test/pull/7",
            note="Проверено",
            target=adapter.target_id,
        )
        second = provider.finish(
            adapter.finish_key,
            "https://github.test/pull/7",
            note="Проверено",
            target=adapter.target_id,
        )
        assert first["pr_link_added"] is True
        assert first["done_set"] is True
        assert second["pr_link_added"] is False
        assert second["done_set"] is False
        assert second["already_closed"] is True

    def test_close_closes_transport_after_success(self, adapter: ProviderAdapter) -> None:
        provider, state = adapter.provider_factory()
        provider.validate_connection(adapter.project)
        provider.close()
        assert state.closed is True

    def test_close_closes_transport_after_error(self, adapter: ProviderAdapter) -> None:
        provider, state = adapter.provider_factory(forbidden=True)
        with pytest.raises(BoardProviderError) as exc_info:
            provider.validate_connection(adapter.project)
        assert exc_info.value.category == "permission"
        provider.close()
        assert state.closed is True

    def test_not_found_transport_error_is_mapped(self, adapter: ProviderAdapter) -> None:
        provider, _ = adapter.provider_factory(error_status=404)
        with pytest.raises(BoardProviderError) as exc_info:
            provider.validate_connection(adapter.project)
        assert exc_info.value.category == "not_found"

    def test_secrets_absent_from_error_result_and_logs(
        self,
        adapter: ProviderAdapter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        provider, _ = adapter.provider_factory(forbidden=True)
        with caplog.at_level(logging.WARNING), pytest.raises(BoardProviderError) as exc_info:
            provider.validate_connection(adapter.project)
        text = f"{exc_info.value!s}\n{exc_info.value!r}\n{caplog.text}"
        assert adapter.secret not in text
