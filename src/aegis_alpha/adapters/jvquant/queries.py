from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any

from aegis_alpha.cache import TTLCache
from aegis_alpha.alerts.provider_health import record_provider_failure
from aegis_alpha.rate_limit import TokenBucket


class JvQuantQueryClient:
    """Thin query wrapper with TTL cache, timeout, and token-bucket throttling."""

    def __init__(
        self,
        *,
        cache_ttl_seconds: float = 30.0,
        query_rate_per_second: float = 3.0,
        query_burst: float = 6.0,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.cache: TTLCache[str, dict[str, Any]] = TTLCache(cache_ttl_seconds)
        self.limiter = TokenBucket(rate=query_rate_per_second, capacity=query_burst)

    def query(self, client: Any, query: str, sort_key: str = "") -> dict[str, Any]:
        cache_key = f"{query}|{sort_key}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        wait_seconds = self.limiter.wait_time()
        if wait_seconds == float("inf"):
            raise TimeoutError("jvQuant token bucket cannot satisfy requested query cost")
        if wait_seconds > 0:
            time.sleep(min(wait_seconds, self.timeout_seconds))
        if not self.limiter.consume():
            raise TimeoutError("Timed out waiting for jvQuant query rate-limit token")

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(client.query, query, 1, 1, sort_key)
            try:
                payload = future.result(timeout=self.timeout_seconds)
            except Exception as exc:
                record_provider_failure(
                    provider="jvQuant",
                    component="semantic_query",
                    error=str(exc) or type(exc).__name__,
                    metadata={"query": query[:160], "sort_key": sort_key},
                )
                raise
        self.cache.set(cache_key, payload)
        return payload
