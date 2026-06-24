# Design — `reviewer update`: cache-control fix

## Problem

`reviewer update` при выходе новой версии на PyPI требует 2+ запусков подряд, чтобы
обнаружить обновление. Первый запуск: «Версия актуальна». Второй: «Доступна новая версия».

Причина: запрос к `https://pypi.org/pypi/rag-reviewer/json` через `urllib.request.urlopen`
идёт без `Cache-Control`-заголовков. PyPI за Fastly CDN, который кеширует JSON-ответ на
edge-нодах. Разные вызовы могут попадать на разные edge-ноды со stale/fresh кешем.

## Fix

Добавить `Cache-Control` и `Pragma` заголовки к HTTP-запросу в `reviewer/entrypoints/cli.py:484-486`.

### Изменение (1 файл, ~4 строки)

**Было:**
```python
with urllib.request.urlopen("https://pypi.org/pypi/rag-reviewer/json", timeout=10) as resp:
    latest_ver = json.loads(resp.read())["info"]["version"]
```

**Стало:**
```python
req = urllib.request.Request(
    "https://pypi.org/pypi/rag-reviewer/json",
    headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
)
with urllib.request.urlopen(req, timeout=10) as resp:
    latest_ver = json.loads(resp.read())["info"]["version"]
```

### Семантика заголовков

| Заголовок | Назначение |
|---|---|
| `Cache-Control: no-cache` | Заставляет CDN/Fastly перевалидировать ответ с origin (PyPI) |
| `Cache-Control: no-store` | Запрещает CDN сохранять ответ в кеш |
| `Pragma: no-cache` | Обратная совместимость с HTTP/1.0 прокси (если есть) |

### Что не меняется

- Логика сравнения версий (`_ver_tuple`)
- Вывод (`click.echo`)
- Вызов `uv tool upgrade`
- Все остальные команды CLI

## Edge cases

- **PyPI недоступен** — поведение не меняется: `except Exception: pass`, выводится «Не удалось получить информацию с PyPI»
- **Заголовки игнорируются CDN** — маловероятно для Fastly, но даже в этом случае поведение не хуже текущего (запрос без заголовков)

## Testing

Юнит-тест на `update` отсутствует. В рамках этого фикса тест не добавляется — изменение
тривиально и покрывается ручной проверкой: `reviewer update` дважды подряд должен давать
одинаковый результат.
