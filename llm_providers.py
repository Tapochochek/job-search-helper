"""Provider-agnostic LLM layer.

Every provider exposes one coroutine:  complete(system, user, max_tokens, temperature) -> str
so the rest of the app never cares whether it talks to Ollama, OpenAI, Anthropic, etc.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

import config

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    async def complete(
        self, system: str, user: str, *, max_tokens: int = 700, temperature: float = 0.4,
        json_mode: bool = False,
    ) -> str: ...


class OpenAICompatProvider(LLMProvider):
    """Ollama, OpenAI, OpenRouter, DeepSeek, LM Studio, vLLM... anything OpenAI-compatible."""

    def __init__(self, name: str, cfg: dict):
        from openai import AsyncOpenAI

        if not cfg.get("api_key"):
            raise LLMError(f"API key for provider '{name}' is not set")
        self.name = name
        self.model = cfg["model"]
        self._token_param = cfg.get("token_param", "max_tokens")
        self._client = AsyncOpenAI(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            timeout=cfg.get("timeout", 60.0),
            max_retries=1,
        )

    async def complete(self, system, user, *, max_tokens=700, temperature=0.4, json_mode=False) -> str:
        extra = {"response_format": {"type": "json_object"}} if json_mode else {}
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **{self._token_param: max_tokens},
                **extra,
            )
        except Exception as exc:  # network, auth, rate limit...
            raise LLMError(f"{self.name}: {exc}") from exc
        return (resp.choices[0].message.content or "").strip()


class OllamaProvider(LLMProvider):
    """Ollama through its NATIVE /api/chat endpoint.

    Why not the OpenAI-compatible /v1 endpoint: it cannot set the context window. Ollama then
    silently uses its default (2048-4096 tokens) and CUTS OFF THE START of the prompt - i.e. the
    instructions - once resume + vacancy don't fit. The model then improvises. Here we always send
    options.num_ctx and warn if the prompt still doesn't fit.
    """

    def __init__(self, name: str, cfg: dict, transport: httpx.AsyncBaseTransport | None = None):
        self.name = name
        self.model = cfg["model"]
        self._url = cfg["base_url"].rstrip("/") + "/api/chat"
        self._num_ctx = int(cfg.get("num_ctx", 8192))
        self._keep_alive = cfg.get("keep_alive", "30m")
        self._timeout = cfg.get("timeout", 300.0)
        self._transport = transport

    async def complete(self, system, user, *, max_tokens=700, temperature=0.4, json_mode=False) -> str:
        body = {
            "model": self.model,
            "stream": False,
            "think": False,  # Qwen3 & co: no reasoning text in the answer (ignored by other models)
            "keep_alive": self._keep_alive,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {
                "num_ctx": self._num_ctx,
                "num_predict": max_tokens,
                "temperature": temperature,
                "repeat_penalty": 1.1,
            },
        }
        if json_mode:
            body["format"] = "json"
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as c:
                resp = await c.post(self._url, json=body)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"{self.name}: HTTP {exc.response.status_code}: {exc.response.text[:300]}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.name}: {exc} (is `ollama serve` running and the model pulled?)") from exc
        used = int(data.get("prompt_eval_count") or 0)
        if used and used >= self._num_ctx - max_tokens:
            logger.warning(
                "Prompt used %d tokens of num_ctx=%d - it may have been truncated! "
                "Raise OLLAMA_NUM_CTX or lower VACANCY_MAX_CHARS / RESUME_MAX_CHARS.",
                used, self._num_ctx,
            )
        logger.debug("%s: prompt=%s tokens, output=%s tokens", self.name, used, data.get("eval_count"))
        return (data.get("message", {}).get("content") or "").strip()


class AnthropicProvider(LLMProvider):
    """Claude via the Messages API (plain httpx, no extra SDK needed)."""

    def __init__(self, name: str, cfg: dict, transport: httpx.AsyncBaseTransport | None = None):
        if not cfg.get("api_key"):
            raise LLMError(f"API key for provider '{name}' is not set")
        self.name = name
        self.model = cfg["model"]
        self._url = cfg["base_url"].rstrip("/") + "/messages"
        self._headers = {
            "x-api-key": cfg["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        self._timeout = cfg.get("timeout", 60.0)
        self._transport = transport

    async def complete(self, system, user, *, max_tokens=700, temperature=0.4, json_mode=False) -> str:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as c:
                resp = await c.post(self._url, headers=self._headers, json=body)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"{self.name}: HTTP {exc.response.status_code}: {exc.response.text[:300]}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.name}: {exc}") from exc
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


_cache: dict[str, LLMProvider] = {}


def get_provider(name: str | None = None) -> LLMProvider:
    name = name or config.LLM_PROVIDER
    if name in _cache:
        return _cache[name]
    cfg = config.PROVIDERS.get(name)
    if cfg is None:
        raise LLMError(f"Unknown provider '{name}'. Available: {', '.join(config.PROVIDERS)}")
    provider: LLMProvider
    if cfg["kind"] == "anthropic":
        provider = AnthropicProvider(name, cfg)
    elif cfg["kind"] == "ollama":
        provider = OllamaProvider(name, cfg)
    else:
        provider = OpenAICompatProvider(name, cfg)
    logger.info("LLM provider: %s (model=%s)", provider.name, provider.model)
    _cache[name] = provider
    return provider
