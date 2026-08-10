# Brief — PRI-228 Батчинг эмбеддингов Voyage по токенам, а не по числу чанков
https://ru.yougile.com/team/686c049c8af8/#PRI-228

## Task
`VoyageEmbedder._embed` режет вход на батчи фиксированного размера в чанках (`embedding_batch_size`,
дефолт 256). Voyage лимитирует батч по токенам (120000/запрос); ~630 токенов на чанк × 256 = ~161k →
`InvalidRequestError`, который не ретраится (валидационный отказ) и обрывает `reviewer index`.
Нужно: набирать батч по бюджету токенов; дешёвая оценка токенов; `embedding_batch_size` остаётся
верхней границей по числу элементов; одиночный сверхлимитный чанк — усечение/пропуск с warning,
без обрыва индексации.

Критерии: (1) `reviewer index` на этом репо проходит с дефолтами; (2) unit — несколько вызовов
клиента, ни один не превышает бюджет; (3) unit — сверхлимитный одиночный чанк не роняет;
(4) `embedding_batch_size` продолжает ограничивать число элементов; (5) тесты без сети.

## Relevant code
- `reviewer/index/embeddings.py:30-39` — `VoyageEmbedder._embed`, цикл по фиксированному
  `batch_size`; единственная точка правки.
- `reviewer/index/embeddings.py:14-28` — конструктор: `client`, `model`, `dim`, `batch_size`;
  сюда добавляется бюджет токенов.
- `reviewer/index/_retry.py` — `with_voyage_retry`: ретраит транспортные сбои, валидационный
  `InvalidRequestError` мимо него.
- `reviewer/config/settings.py:65` — `embedding_batch_size: int = 256` (верхняя граница по числу).
- `reviewer/app.py:93` — прокидывание `batch_size` в `VoyageEmbedder`.
(dropped 0)

## Constraints / open questions
- Оценка токенов: у клиента Voyage есть `count_tokens`; при отсутствии — эвристика по длине
  (консервативный делитель символов на токен). Недооценка не должна ронять: разбивать и повторять.
- Тот же путь используется в `sync_board` и записи сводок подсистем.
- Индекс dev отставал на 12 коммитов на момент сбора брифа; переиндексация после мержа.

Собран на: премиум-тир (сессионная модель), режим: inline

## Токены (этап solve-task)
Модель: claude-opus-5
fresh-in 46 · out 9.2K · cache-write 173.6K · cache-read 1.6M
Всего: 1.8M токенов
