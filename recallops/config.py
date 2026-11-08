from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    memory_backend: str
    hindsight_url: str
    hindsight_api_key: str | None
    bank_id: str
    local_memory_path: Path
    llm_api_key: str | None
    llm_base_url: str
    llm_model: str

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)


def load_settings() -> Settings:
    load_dotenv(ROOT_DIR / ".env")
    backend = os.getenv("MEMORY_BACKEND", "hindsight").strip().lower()
    if backend not in {"hindsight", "local"}:
        raise ValueError(f"MEMORY_BACKEND must be 'hindsight' or 'local', got {backend!r}")
    return Settings(
        memory_backend=backend,
        hindsight_url=os.getenv("HINDSIGHT_URL", "https://api.hindsight.vectorize.io"),
        hindsight_api_key=os.getenv("HINDSIGHT_API_KEY") or None,
        bank_id=os.getenv("HINDSIGHT_BANK_ID", "recallops-acme-sre"),
        local_memory_path=Path(os.getenv("LOCAL_MEMORY_PATH", ROOT_DIR / ".recallops_memory.json")),
        llm_api_key=os.getenv("GROQ_API_KEY") or os.getenv("LLM_API_KEY") or None,
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
        llm_model=os.getenv("LLM_MODEL", "openai/gpt-oss-120b"),
    )
