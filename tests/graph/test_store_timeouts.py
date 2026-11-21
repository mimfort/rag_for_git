"""Таймауты драйвера Neo4j: цена одного захода в мёртвый граф (PRI-276)."""
from reviewer.config.settings import Settings
from reviewer.graph import store as store_mod


class _FakeDriver:
    def close(self):
        pass


def _capture(monkeypatch) -> dict:
    """Подменить GraphDatabase фейком и вернуть словарь с kwargs вызова."""
    captured: dict = {}

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri, **kwargs):
            captured["uri"] = uri
            captured.update(kwargs)
            return _FakeDriver()

    monkeypatch.setattr(store_mod, "GraphDatabase", _FakeGraphDatabase)
    return captured


def test_driver_gets_explicit_timeouts(monkeypatch):
    """Дефолты драйвера (30/60/30 с) заменены единицами секунд."""
    captured = _capture(monkeypatch)
    store_mod.GraphStore("neo4j://localhost:7687", "neo4j", "pass")
    assert captured["connection_timeout"] == 5.0
    assert captured["connection_acquisition_timeout"] == 10.0
    assert captured["max_transaction_retry_time"] == 5.0


def test_driver_timeouts_are_overridable(monkeypatch):
    """Деплою с медленным удалённым Neo4j нужен выход."""
    captured = _capture(monkeypatch)
    store_mod.GraphStore("neo4j://remote:7687", "neo4j", "pass",
                         connection_timeout=20.0, acquisition_timeout=40.0,
                         max_retry_time=30.0)
    assert captured["connection_timeout"] == 20.0
    assert captured["connection_acquisition_timeout"] == 40.0
    assert captured["max_transaction_retry_time"] == 30.0


def test_settings_expose_timeout_keys():
    """Значения приходят из Settings, а не из констант в конструкторе."""
    s = Settings()
    assert s.neo4j_connection_timeout == 5.0
    assert s.neo4j_acquisition_timeout == 10.0
    assert s.neo4j_max_retry_time == 5.0


def test_notifications_setting_survives(monkeypatch):
    """Прежний аргумент не потерян: notification-спам драйвера по-прежнему заглушен."""
    captured = _capture(monkeypatch)
    store_mod.GraphStore("neo4j://localhost:7687", "neo4j", "pass")
    assert captured["notifications_min_severity"] == "OFF"
