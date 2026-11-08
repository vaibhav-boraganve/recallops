from __future__ import annotations

import logging
import re

from openai import AsyncOpenAI

from .config import Settings

log = logging.getLogger(__name__)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(RuntimeError):
    pass


class ChatLLM:
    """Thin async wrapper over any OpenAI-compatible endpoint (Groq by default)."""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=60.0, max_retries=2)

    async def complete(self, system: str, user: str, *, temperature: float = 0.2) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                temperature=temperature,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
        except Exception as exc:
            raise LLMError(f"LLM call failed: {exc}") from exc
        content = (response.choices[0].message.content or "").strip()
        content = _THINK_BLOCK.sub("", content).strip()
        if not content:
            raise LLMError("LLM returned an empty response")
        return content


def build_llm(settings: Settings) -> ChatLLM | None:
    if not settings.llm_enabled:
        log.warning("No GROQ_API_KEY set: triage briefs will be assembled without an LLM.")
        return None
    return ChatLLM(settings.llm_api_key, settings.llm_base_url, settings.llm_model)
