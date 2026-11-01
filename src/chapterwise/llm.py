"""LLM access through any OpenAI-compatible chat API (DeepSeek by default)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class LLMError(Exception):
    """The model returned nothing usable."""


@dataclass(frozen=True)
class Completion:
    text: str
    tokens_in: int
    tokens_out: int


class LLM(Protocol):
    @property
    def model(self) -> str: ...

    def complete(self, system: str, user: str, json_mode: bool = False) -> Completion: ...


class OpenAICompatibleLLM:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 300.0):
        from openai import OpenAI

        # The SDK retries rate limits, timeouts and 5xx errors with backoff.
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=3)
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def complete(self, system: str, user: str, json_mode: bool = False) -> Completion:
        extra = {"response_format": {"type": "json_object"}} if json_mode else {}
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **extra,
        )
        if not response.choices:
            raise LLMError("The model returned no choices")
        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        if not text:
            raise LLMError(f"The model returned an empty answer (finish_reason={choice.finish_reason})")
        usage = response.usage
        return Completion(
            text=text,
            tokens_in=(usage.prompt_tokens or 0) if usage else 0,
            tokens_out=(usage.completion_tokens or 0) if usage else 0,
        )
