from __future__ import annotations

import logging
import math
import threading
from collections import OrderedDict

from reviewer.index._retry import with_voyage_retry

log = logging.getLogger(__name__)

# Максимальный размер кэша query-эмбеддингов.
# Агент в tool-loop повторяет одинаковые запросы; кэш экономит вызовы Voyage (free tier: 3 RPM).
_DEFAULT_CACHE_SIZE = 512

# Voyage отвергает батч дороже 120000 токенов валидационной ошибкой, которую
# _retry.py не ретраит (это не транспортный сбой). Бюджет по умолчанию взят с
# запасом: оценка токенов приблизительная, а недооценка на границе стоила бы
# лишнего раунда деления батча.
_DEFAULT_TOKEN_BUDGET = 100_000

# Консервативная эвристика на случай, когда у клиента нет локального счётчика
# токенов: код и кириллица токенизируются плотнее английской прозы, поэтому
# делитель занижен намеренно — лучше пере-, чем недооценить.
_CHARS_PER_TOKEN = 3.0

# Предел рекурсивного деления батча при недооценке токенов: батч из одного
# чанка дальше делить нельзя, его текст усекается вдвое, и без предела цикл
# ушёл бы в бесконечное усечение.
_MAX_SPLIT_DEPTH = 12


def _is_token_limit(exc: Exception) -> bool:
    """Отличить отказ «батч дороже лимита токенов» от прочих ошибок Voyage."""
    msg = str(exc).lower()
    return "token" in msg and ("max allowed" in msg or "limit" in msg or "too large" in msg)


class VoyageEmbedder:
    def __init__(self, client=None, model: str = "voyage-code-3",
                 dim: int = 1024, batch_size: int = 128,
                 cache_size: int = _DEFAULT_CACHE_SIZE,
                 token_budget: int = _DEFAULT_TOKEN_BUDGET):
        if client is None:
            import voyageai
            client = voyageai.Client()      # VOYAGE_API_KEY из env
        self._client = client
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self.token_budget = max(1, token_budget)
        # Потокобезопасный LRU-кэш для query-эмбеддингов.
        # Документы не кэшируем — там дедуп по content_hash уже есть на уровне store.
        self._query_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def _count_tokens(self, text: str) -> int:
        """Оценить стоимость текста в токенах — счётчиком Voyage либо эвристикой.

        Счётчик клиента локальный (без сети), но у него нет гарантий контракта
        для всех версий/фейков, поэтому любая его ошибка молча откатывается на
        эвристику: оценка нужна лишь для набора батча, точность не критична.
        """
        counter = getattr(self._client, "count_tokens", None)
        if counter is not None:
            try:
                return int(counter([text], model=self.model))
            except Exception:  # noqa: BLE001 — оценка не обязана быть точной
                pass
        return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))

    def _fit_to_budget(self, text: str) -> str:
        """Усечь одиночный чанк, который сам по себе дороже бюджета батча.

        Пропуск здесь недопустим: вызывающий сопоставляет векторы с входом по
        позиции, поэтому длина результата обязана совпадать с длиной входа.
        """
        tokens = self._count_tokens(text)
        if tokens <= self.token_budget:
            return text
        # Первое приближение — по эвристике; дальше ужимаем, пока в бюджет не
        # уложится ИЗМЕРЕННАЯ стоимость: при точном счётчике эвристический срез
        # сам по себе гарантии не даёт (плотность токенов у кода выше).
        keep = min(len(text) - 1, max(1, int(self.token_budget * _CHARS_PER_TOKEN)))
        while keep > 1 and self._count_tokens(text[:keep]) > self.token_budget:
            keep //= 2
        log.warning(
            "Чанк дороже бюджета батча (%d токенов оценочно, бюджет %d): усечён "
            "до %d символов из %d",
            tokens, self.token_budget, keep, len(text),
        )
        return text[:keep]

    def _batches(self, texts: list[str]) -> list[list[str]]:
        """Набрать батчи по бюджету токенов, не превышая batch_size по элементам."""
        batches: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0
        for text in texts:
            tokens = self._count_tokens(text)
            over_budget = current and current_tokens + tokens > self.token_budget
            over_count = len(current) >= self.batch_size
            if over_budget or over_count:
                batches.append(current)
                current, current_tokens = [], 0
            current.append(text)
            current_tokens += tokens
        if current:
            batches.append(current)
        return batches

    def _call(self, batch: list[str], input_type: str) -> list[list[float]]:
        resp = with_voyage_retry(lambda: self._client.embed(
            batch, model=self.model,
            input_type=input_type, output_dimension=self.dim,
        ))
        return list(resp.embeddings)

    def _embed_batch(self, batch: list[str], input_type: str,
                     depth: int = 0) -> list[list[float]]:
        """Отправить батч, при недооценке токенов разделив его и повторив.

        Оценка приблизительная, поэтому отказ по лимиту токенов возможен даже
        после набора по бюджету. Батч из нескольких чанков делится пополам;
        батч из одного — усекается вдвое, потому что делить его уже нечем.
        """
        try:
            return self._call(batch, input_type)
        except Exception as exc:  # noqa: BLE001 — ниже фильтруем по типу отказа
            if not _is_token_limit(exc) or depth >= _MAX_SPLIT_DEPTH:
                raise
            if len(batch) > 1:
                middle = len(batch) // 2
                log.warning(
                    "Батч из %d чанков отвергнут по лимиту токенов: делим пополам",
                    len(batch),
                )
                return (
                    self._embed_batch(batch[:middle], input_type, depth + 1)
                    + self._embed_batch(batch[middle:], input_type, depth + 1)
                )
            text = batch[0]
            if len(text) <= 1:
                raise
            log.warning(
                "Одиночный чанк отвергнут по лимиту токенов: усекаем с %d до %d символов",
                len(text), len(text) // 2,
            )
            return self._embed_batch([text[: len(text) // 2]], input_type, depth + 1)

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        out: list[list[float]] = []
        prepared = [self._fit_to_budget(t) for t in texts]
        for batch in self._batches(prepared):
            out.extend(self._embed_batch(batch, input_type))
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        """Вернуть эмбеддинг запроса, используя потокобезопасный LRU-кэш."""
        with self._lock:
            if text in self._query_cache:
                self._query_cache.move_to_end(text)
                return self._query_cache[text]
            vec = self._embed([text], "query")[0]
            if len(self._query_cache) >= self._cache_size:
                self._query_cache.popitem(last=False)
            self._query_cache[text] = vec
            return vec

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Эмбеддинги нескольких запросов одним батчем через тот же LRU-кэш.

        Мультизапросный ретрив (PRI-255) считает N подзапросов, и N отдельных
        вызовов упёрлись бы в 3 RPM free tier. Блокировка на время сетевого
        вызова не держится: под ней только чтение и запись кэша.
        """
        if not texts:
            return []
        unique = list(dict.fromkeys(texts))
        with self._lock:
            cached = {}
            for text in unique:
                if text in self._query_cache:
                    self._query_cache.move_to_end(text)
                    cached[text] = self._query_cache[text]
        missing = [text for text in unique if text not in cached]
        fresh: dict[str, list[float]] = {}
        if missing:
            fresh = dict(zip(missing, self._embed(missing, "query"), strict=True))
            with self._lock:
                for text, vec in fresh.items():
                    if len(self._query_cache) >= self._cache_size:
                        self._query_cache.popitem(last=False)
                    self._query_cache[text] = vec
        merged = {**cached, **fresh}
        return [merged[text] for text in texts]
