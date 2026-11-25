"""Async LLM client for ghst.

Supports OpenAI, Anthropic and ChatGPT-plan (Sign in with ChatGPT) APIs with
circuit breaker, caching, connection pooling, and per-request-type timeouts.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from ghst import chatgpt
from ghst.config import GhstConfig

logger = logging.getLogger("ghst.llm")


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class ConnectionHealth:
    """Tracks connection quality for adaptive behavior."""

    last_success_time: float = 0.0
    last_failure_time: float = 0.0
    consecutive_failures: int = 0
    latency_samples: list[float] = field(default_factory=list)
    circuit_state: CircuitState = CircuitState.CLOSED
    circuit_opened_at: float = 0.0

    FAILURE_THRESHOLD: int = 3
    COOLDOWN_SECONDS: float = 30.0
    MAX_LATENCY_SAMPLES: int = 10
    HIGH_LATENCY_MS: float = 2000.0

    @property
    def avg_latency_ms(self) -> float:
        if not self.latency_samples:
            return 0.0
        return sum(self.latency_samples) / len(self.latency_samples)

    def record_success(self, latency_ms: float) -> None:
        self.last_success_time = time.monotonic()
        self.consecutive_failures = 0
        self.latency_samples.append(latency_ms)
        if len(self.latency_samples) > self.MAX_LATENCY_SAMPLES:
            self.latency_samples.pop(0)
        if self.circuit_state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
            self.circuit_state = CircuitState.CLOSED
            logger.info("Circuit breaker closed — connection recovered")

    def record_failure(self) -> None:
        self.last_failure_time = time.monotonic()
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.FAILURE_THRESHOLD:
            if self.circuit_state != CircuitState.OPEN:
                self.circuit_state = CircuitState.OPEN
                self.circuit_opened_at = time.monotonic()
                logger.warning("Circuit breaker opened — %d consecutive failures",
                               self.consecutive_failures)
            elif self.circuit_state == CircuitState.HALF_OPEN:
                # Probe failed
                self.circuit_state = CircuitState.OPEN
                self.circuit_opened_at = time.monotonic()

    def should_allow_request(self) -> bool:
        if self.circuit_state == CircuitState.CLOSED:
            return True
        if self.circuit_state == CircuitState.OPEN:
            elapsed = time.monotonic() - self.circuit_opened_at
            if elapsed >= self.COOLDOWN_SECONDS:
                self.circuit_state = CircuitState.HALF_OPEN
                logger.info("Circuit breaker half-open — allowing probe request")
                return True
            return False
        # HALF_OPEN: allow one probe
        return True

    @property
    def is_high_latency(self) -> bool:
        return self.avg_latency_ms > self.HIGH_LATENCY_MS

    def status_display(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "state": self.circuit_state.value,
            "consecutive_failures": self.consecutive_failures,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
        }
        if self.circuit_state == CircuitState.OPEN:
            remaining = self.COOLDOWN_SECONDS - (time.monotonic() - self.circuit_opened_at)
            result["probe_in_seconds"] = max(0, round(remaining, 1))
        if self.last_success_time:
            result["last_success_ago_seconds"] = round(
                time.monotonic() - self.last_success_time, 1
            )
        return result


# Request-type timeout profiles
TIMEOUT_AUTOCOMPLETE = httpx.Timeout(connect=1.0, read=3.0, write=1.0, pool=1.0)
TIMEOUT_NL = httpx.Timeout(connect=2.0, read=12.0, write=1.0, pool=1.0)
TIMEOUT_HISTORY = httpx.Timeout(connect=2.0, read=8.0, write=1.0, pool=1.0)


@dataclass
class CacheEntry:
    response: str
    timestamp: float


class ResponseCache:
    """In-memory response cache with TTL."""

    def __init__(self, ttl: float = 300.0) -> None:
        self.ttl = ttl
        self._cache: dict[str, CacheEntry] = {}

    def _make_key(self, *parts: str) -> str:
        raw = "|".join(parts)
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, *key_parts: str) -> str | None:
        key = self._make_key(*key_parts)
        entry = self._cache.get(key)
        if entry is None:
            return None
        if (time.monotonic() - entry.timestamp) > self.ttl:
            del self._cache[key]
            return None
        return entry.response

    def set(self, value: str, *key_parts: str) -> None:
        key = self._make_key(*key_parts)
        self._cache[key] = CacheEntry(response=value, timestamp=time.monotonic())
        # Evict old entries periodically
        if len(self._cache) > 200:
            self._evict()

    def _evict(self) -> None:
        now = time.monotonic()
        expired = [k for k, v in self._cache.items() if (now - v.timestamp) > self.ttl]
        for k in expired:
            del self._cache[k]


class LLMClient:
    """Async LLM client supporting OpenAI and Anthropic."""

    def __init__(self, config: GhstConfig) -> None:
        self.config = config
        self.health = ConnectionHealth()
        self.cache = ResponseCache()
        self.chatgpt = chatgpt.ChatGPTSession()
        self.last_error = ""
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
                http2=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _is_anthropic(self) -> bool:
        return self.config.provider.name == "anthropic"

    def _is_chatgpt(self) -> bool:
        return self.config.provider.name == "chatgpt"

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        timeout: httpx.Timeout = TIMEOUT_AUTOCOMPLETE,
        use_cache_key: tuple[str, ...] | None = None,
        single_line: bool = False,
    ) -> str:
        """Send a completion request to the LLM.

        Args:
            messages: Chat messages (system/user/assistant).
            model: Model override; defaults to config.
            timeout: Request timeout profile.
            use_cache_key: If provided, check/store in cache.

        Returns:
            The LLM's response text, or empty string on failure.
        """
        # Check cache
        if use_cache_key:
            cached = self.cache.get(*use_cache_key)
            if cached is not None:
                return cached

        # Check circuit breaker
        if not self.health.should_allow_request():
            logger.debug("Circuit breaker open — request rejected")
            return ""

        model = model or self.config.provider.model
        start = time.monotonic()

        try:
            if self._is_anthropic():
                result = await self._complete_anthropic(messages, model, timeout, single_line)
            elif self._is_chatgpt():
                result = await self._complete_chatgpt(messages, model, timeout, single_line)
            else:
                result = await self._complete_openai(messages, model, timeout, single_line)

            latency_ms = (time.monotonic() - start) * 1000
            self.health.record_success(latency_ms)
            self.last_error = ""

            if use_cache_key and result:
                self.cache.set(result, *use_cache_key)

            return result

        except chatgpt.ChatGPTError as e:
            self.health.record_failure()
            self.last_error = str(e)
            logger.info("ChatGPT request failed: %s", e)
            return ""
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as e:
            self.health.record_failure()
            self.last_error = str(e) or type(e).__name__
            logger.debug("LLM request failed: %s", e)
            return ""
        except Exception:
            self.health.record_failure()
            logger.exception("Unexpected LLM error")
            return ""

    async def _complete_openai(
        self,
        messages: list[dict[str, Any]],
        model: str,
        timeout: httpx.Timeout,
        single_line: bool = False,
    ) -> str:
        client = await self._get_client()
        base_url = self.config.provider.effective_api_base_url.rstrip("/")
        body: dict[str, Any] = {
            "model": model, "messages": messages, "temperature": 0.3, "max_tokens": 200,
        }
        if single_line:
            # Ghost text is one line; stop there instead of generating a list
            body.update(max_tokens=self.config.ui.autocomplete_max_tokens, stop=["\n"])
        response = await client.post(
            f"{base_url}/chat/completions",
            json=body,
            headers={
                "Authorization": f"Bearer {self.config.provider.api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()

    async def _complete_anthropic(
        self,
        messages: list[dict[str, Any]],
        model: str,
        timeout: httpx.Timeout,
        single_line: bool = False,
    ) -> str:
        client = await self._get_client()
        base_url = self.config.provider.effective_api_base_url.rstrip("/")

        # Convert from OpenAI message format to Anthropic
        system_text = ""
        anthropic_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            else:
                anthropic_messages.append({
                    "role": msg["role"],
                    "content": msg["content"],
                })

        body: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": self.config.ui.autocomplete_max_tokens if single_line else 200,
            "temperature": 0.3,
        }
        if system_text:
            body["system"] = [
                {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
            ]

        response = await client.post(
            f"{base_url}/v1/messages",
            json=body,
            headers={
                "x-api-key": self.config.provider.api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "prompt-caching-2024-07-31",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        return data["content"][0]["text"].strip()

    async def _complete_chatgpt(
        self,
        messages: list[dict[str, Any]],
        model: str,
        timeout: httpx.Timeout,
        single_line: bool = False,
    ) -> str:
        # The plan flow rejects max_output_tokens and temperature, so ghost
        # text is capped by closing the stream instead
        client = await self._get_client()
        body = chatgpt.responses_body(messages, model)
        max_chars = self.config.ui.autocomplete_max_tokens * 4 if single_line else 0
        token = await self.chatgpt.access_token(client)
        try:
            text = await chatgpt.stream_response(
                client, token, body, timeout, single_line=single_line, max_chars=max_chars,
            )
        except chatgpt.ChatGPTError as e:
            if e.status != 401:
                raise
            token = await self.chatgpt.access_token(client, force_refresh=True)
            text = await chatgpt.stream_response(
                client, token, body, timeout, single_line=single_line, max_chars=max_chars,
            )
        return text.strip()

    async def probe(self) -> dict[str, Any]:
        """Check that the LLM server answers and lists the configured models."""
        provider = self.config.provider
        base_url = provider.effective_api_base_url.rstrip("/")
        if self._is_chatgpt():
            return await self._probe_chatgpt()
        if self._is_anthropic():
            url = f"{base_url}/v1/models"
            headers = {"x-api-key": provider.api_key, "anthropic-version": "2023-06-01"}
        else:
            url = f"{base_url}/models"
            headers = {"Authorization": f"Bearer {provider.api_key}"}

        result: dict[str, Any] = {"ok": False, "url": base_url}
        client = await self._get_client()
        start = time.monotonic()
        try:
            response = await client.get(url, headers=headers, timeout=httpx.Timeout(3.0))
        except httpx.ConnectError:
            result["error"] = "cannot connect"
            return result
        except httpx.TimeoutException:
            result["error"] = "timed out after 3s"
            return result
        except httpx.HTTPError as e:
            result["error"] = str(e) or type(e).__name__
            return result
        result["latency_ms"] = round((time.monotonic() - start) * 1000)

        if response.status_code in (401, 403):
            result["error"] = f"API key rejected (HTTP {response.status_code})"
            return result
        if response.status_code >= 400:
            result["error"] = f"HTTP {response.status_code}"
            return result

        result["ok"] = True
        try:
            models = [m.get("id") for m in response.json().get("data", [])]
        except (ValueError, AttributeError, TypeError):
            models = []
        return self._with_models(result, models)

    def _with_models(self, result: dict[str, Any], models: list[str]) -> dict[str, Any]:
        result["models"] = models
        if models:
            provider = self.config.provider
            wanted = dict.fromkeys([provider.effective_autocomplete_model, provider.model])
            result["unlisted_models"] = [m for m in wanted if m not in models]
        return result

    async def _probe_chatgpt(self) -> dict[str, Any]:
        record = self.chatgpt.record() or {}
        result: dict[str, Any] = {
            "ok": False, "url": chatgpt.API_BASE, "account": record.get("email", ""),
        }
        client = await self._get_client()
        start = time.monotonic()
        try:
            token = await self.chatgpt.access_token(client)
            response = await client.get(
                f"{chatgpt.API_BASE}/models", timeout=httpx.Timeout(5.0),
                headers={"Authorization": f"Bearer {token}"},
            )
        except chatgpt.ChatGPTError as e:
            result["error"] = str(e)
            return result
        except httpx.HTTPError as e:
            result["error"] = str(e) or type(e).__name__
            return result
        result["latency_ms"] = round((time.monotonic() - start) * 1000)
        if response.status_code >= 400:
            result["error"] = str(chatgpt.plan_error(response.status_code, response.content))
            return result
        result["ok"] = True
        result["plan"] = True
        try:
            models = chatgpt.list_models_from(response.json())
        except ValueError:
            models = []
        return self._with_models(result, models)

    async def complete_with_retry(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        timeout: httpx.Timeout = TIMEOUT_NL,
        retries: int = 1,
        retry_delay: float = 0.5,
    ) -> str:
        """Complete with retry for NL commands and history search."""
        import asyncio

        for attempt in range(retries + 1):
            result = await self.complete(messages, model=model, timeout=timeout)
            if result:
                return result
            if attempt < retries:
                await asyncio.sleep(retry_delay)
        return ""
